"""Small Claude API helper (Messages API, optional server-side web search)."""
import json
import os
import re

from .http import ClientError, post

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# USD per million tokens (input, output) -- platform.claude.com/docs/en/about-claude/pricing, Oct 2026
PRICES = {"claude-sonnet-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0),
          "claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-4": (3.0, 15.0), "claude-haiku-4": (1.0, 5.0)}
SEARCH_USD = 0.01
from .usage import USAGE  # noqa: E402  -- calls made during the current worker step; jobs.run flushes it onto the job row


def record(js, label, model=None):
    """Log one Messages API response's usage and cost (called for every Claude call, including failures that return usage)."""
    u = (js or {}).get("usage") or {}
    if not u:
        return
    model = model or (js or {}).get("model") or MODEL
    pin, pout = next((v for k, v in sorted(PRICES.items(), key=lambda kv: -len(kv[0])) if model.startswith(k)), (2.0, 10.0))
    inp, out = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
    cw, cr = int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0)
    searches = int(((u.get("server_tool_use") or {}).get("web_search_requests")) or 0)
    cost = (inp * pin + cw * pin * 1.25 + cr * pin * 0.1 + out * pout) / 1e6 + searches * SEARCH_USD
    USAGE.append({"call": label, "provider": "Anthropic", "model": model, "input_tokens": inp + cw + cr, "output_tokens": out,
                  "searches": searches, "cost_usd": round(cost, 4)})


def ask(system, user, max_tokens=4000, web_search=0, timeout=300, label="claude", model=None):
    msgs = [{"role": "user", "content": user}]
    body = {"model": model or MODEL, "max_tokens": max_tokens, "system": system, "messages": msgs}
    if web_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": web_search}]
    blocks = []
    for _ in range(6):   # long server-tool turns come back as pause_turn and must be continued
        r = post("https://api.anthropic.com/v1/messages",
                 headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
                 json=body, timeout=timeout, tries=2)
        js = r.json()
        record(js, label)
        content = js.get("content", []) or []
        blocks += content
        no_text = not any(b.get("type") == "text" and b.get("text", "").strip() for b in content)
        # continue long server-tool turns, and turns that ran out of room while still searching
        if js.get("stop_reason") != "pause_turn" and not (js.get("stop_reason") == "max_tokens" and no_text):
            break
        if js.get("stop_reason") == "max_tokens":
            body["max_tokens"] = min(32000, int(body["max_tokens"] * 1.5))
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


def _loads(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(re.sub(r",\s*([}\]])", r"\1", raw))  # trailing commas


def ask_json(system, user, **kw):
    text, sources = ask(system, user, **kw)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"Claude returned no JSON: {text[:300]}")
    raw = m.group(0)
    try:
        data = _loads(raw)
    except json.JSONDecodeError:
        # one repair pass (unescaped quotes such as 2" taps, stray comments) -- no web search, same content
        fixed, _ = ask("Return the user's data as strictly valid JSON: same keys, same values, escape inner quotes, "
                       "no comments, no prose. Output only the JSON object.", raw, max_tokens=kw.get("max_tokens", 8000),
                       timeout=kw.get("timeout", 300), label=kw.get("label", "claude") + " (JSON repair)")
        m2 = re.search(r"\{.*\}", fixed, re.S)
        data = _loads(m2.group(0) if m2 else fixed)
    return data, sources


def ask_tool(system, user, name, description, max_tokens=8000, timeout=300, properties=None, label=None, model=None):
    """Structured output via a forced tool call: the API returns the tool input as parsed JSON (never malformed)."""
    schema = {"type": "object", "properties": properties or {}, "additionalProperties": not properties}
    body = {"model": model or MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}],
            "tools": [{"name": name, "description": description, "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": name}}
    hdr = {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        r = post("https://api.anthropic.com/v1/messages", headers=hdr, json=body, timeout=timeout, tries=2)
    except ClientError as e:
        if "tool_choice" not in str(e):
            raise
        body["tool_choice"] = {"type": "auto"}   # models that think by default refuse a forced tool; ask for it instead
        body["messages"][0]["content"] += f"\n\nRespond only by calling the {name} tool."
        r = post("https://api.anthropic.com/v1/messages", headers=hdr, json=body, timeout=timeout, tries=2)
    js = r.json()
    record(js, label or name)
    for b in js.get("content", []) or []:
        if b.get("type") == "tool_use" and b.get("name") == name:
            if js.get("stop_reason") == "max_tokens":
                tail = json.dumps(b.get("input"))[-500:]
                kinds = [x.get("type") for x in js.get("content", [])]
                raise RuntimeError(f"{name}: output hit max_tokens usage={js.get('usage')} blocks={kinds} tail={tail}")
            return b.get("input") or {}
    raise RuntimeError(f"{name}: no tool call (stop_reason={js.get('stop_reason')}, error={js.get('error')})")
