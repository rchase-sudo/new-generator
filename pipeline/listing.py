"""Turn pasted listing text (Ctrl+A from LoopNet, Crexi, MLS...) into structured fields with Claude."""
import json
import os
import re

from .http import post

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

PROMPT = """You extract property facts from a pasted real estate listing. The text may include menus, ads and
other listings; use only facts about the subject property. Return ONLY a JSON object with these keys
(null when not stated, never guess):

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
summary          one sentence describing the property
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
