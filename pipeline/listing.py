"""Turn the user's input -- a short brief ("4 ac at 2839 US 70 Hwy, Durham NC; retail + a restaurant") or a whole
pasted listing page -- into structured property fields plus the requested program, with Claude."""
import json
import os
import re

from .http import post

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

PROMPT = """You extract property facts from the user's input. It is either a short brief (property info plus what they
want to build) or a pasted real estate listing page, which may include menus, ads and other listings; use only facts about
the subject property. Return ONLY a JSON object with these keys (null when not stated, never guess):

street_address   street number + name as written, or the road name if there is no number (e.g. "Pocahontas Trl")
lot              lot / unit / parcel label if given (e.g. "Lot 6")
city, state      state as 2-letter code
zip
county
apn              parcel / tax map / PIN number exactly as written; if several parcels, a JSON list of strings
acres            number (gross site area)
price            number in dollars
zoning           code as written (e.g. "B1")
frontage_ft      number
road_name        the road the site fronts
utilities        short text on water/sewer as stated
listing_id
broker           listing broker / company
agent_name       the listing agent's name if stated, else null
agent_email      the listing agent's email if stated, else null
agent_phone      the listing agent's phone if stated, else null
summary          one sentence describing the property
request          what the user asks to be designed, in their words (uses, sizes, counts, e.g. "retail strip and a
                 sit-down restaurant", "60-unit apartments", "tunnel car wash"); null if they did not ask for anything
"""


def parse_listing(text: str) -> dict:
    key = os.environ["ANTHROPIC_API_KEY"]
    r = post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": MODEL,
            "max_tokens": 1200,
            "system": PROMPT,
            "messages": [{"role": "user", "content": text[:60000]}],
        },
        timeout=120,
    )
    from .claude import record
    record(r.json(), "Read listing", MODEL)
    out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
    m = re.search(r"\{.*\}", out, re.S)
    data = json.loads(m.group(0)) if m else {}
    for k in ("acres", "price", "frontage_ft"):
        v = data.get(k)
        if isinstance(v, str):
            try:
                data[k] = float(re.sub(r"[^\d.]", "", v))
            except ValueError:
                data[k] = None
    return data
