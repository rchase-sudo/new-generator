"""Small Claude API helper (Messages API, optional server-side web search)."""
import json
import os
import re

from .http import post

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


def ask(system, user, max_tokens=4000, web_search=0, timeout=300):
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]}
    if web_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": web_search}]
    r = post("https://api.anthropic.com/v1/messages",
             headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
             json=body, timeout=timeout, tries=2)
    js = r.json()
    text = "".join(b.get("text", "") for b in js.get("content", []) if b.get("type") == "text")
    sources = []
    for b in js.get("content", []):
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
