"""Small Claude API helper (Messages API, optional server-side web search)."""
import json
import os
import re

from .http import post

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


def ask(system, user, max_tokens=4000, web_search=0, timeout=300):
    msgs = [{"role": "user", "content": user}]
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": msgs}
    if web_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": web_search}]
    blocks = []
    for _ in range(6):   # long server-tool turns come back as pause_turn and must be continued
        r = post("https://api.anthropic.com/v1/messages",
                 headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                 json=body, timeout=timeout, tries=2)
        js = r.json()
        content = js.get("content", []) or []
        blocks += content
        if js.get("stop_reason") != "pause_turn":
            break
        msgs = msgs[:1] + [{"role": "assistant", "content": [b for b in blocks]}]
        body["messages"] = msgs
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text") or \
        "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text.strip():
        raise RuntimeError(f"Claude returned no text (stop_reason={js.get('stop_reason')}, error={js.get('error')})")
    sources = []
    for b in blocks:
        if b.get("type") == "web_search_tool_result" and isinstance(b.get("content"), list):
            sources += [x.get("url") for x in b["content"] if isinstance(x, dict) and x.get("url")]
        for c in b.get("citations", []) or []:
            if c.get("url"):
                sources.append(c["url"])
    return text, list(dict.fromkeys(sources))


def ask_json(system, user, **kw):
    text, sources = ask(system, user, **kw)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"Claude returned no JSON: {text[:300]}")
    raw = m.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))  # trailing commas
    return data, sources
