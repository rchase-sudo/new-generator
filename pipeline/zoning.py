"""Zoning research (cached per jurisdiction + district) and highest-and-best-use program selection."""
import datetime as dt
import json
import re

import requests

from . import supa
from .claude import ask_json

ZONING_SYSTEM = """You are a land-use planner researching a zoning ordinance for a preliminary site plan.
Use web search to find the CURRENT official ordinance text (municode, amlegal, ecode360, county/city site).
Report numbers exactly as the ordinance states them; if a value depends on conditions, give the value that applies
to a typical commercial pad site and explain in notes. Never invent values: use null when you cannot find one.
Return ONLY one JSON object, no prose before or after."""

ZONING_SCHEMA = """{
  "jurisdiction": "short name of who administers zoning here, max 40 characters (e.g. 'Durham City-County', 'New Kent County')",
  "district": "code", "district_name": "full name",
  "ordinance": "code citation for the district (e.g. 'UDO Sec. 6.2')",
  "setbacks_ft": {"front": n, "side": n, "rear": n, "corner_side": n},
  "parking_setbacks_ft": {"front": n, "side": n, "rear": n},
  "landscape_buffer_front_ft": n,
  "buffer_notes": "required buffers/yards adjoining residential, etc.",
  "max_height_ft": n, "max_stories": n,
  "max_lot_coverage_pct": n, "max_impervious_pct": n, "max_far": n,
  "min_lot_area_sf": n, "min_lot_width_ft": n,
  "parking_ratios": [{"use": "retail", "spaces": n, "per": "1000 sf" | "seat" | "unit" | "bay" | "washer" ...}],
  "uses": [{"use": "name", "status": "permitted" | "conditional" | "special" | "prohibited" | "unknown", "note": "..."}],
  "confidence": "high" | "medium" | "low",
  "notes": "anything a site planner must know (overlays, design standards, access rules)"
}"""

PROGRAM_SYSTEM = """You are a commercial real-estate development planner choosing the highest-and-best-use concept
for a vacant land listing, to be drawn as a preliminary site plan. Be realistic for the market and the road: favour uses
that are permitted by right, that fit the buildable area with surface parking, and that a buyer of this listing would
actually build. Size buildings so that building + parking + drive aisles + stormwater fit in the buildable acreage
(surface parking needs ~350 SF per stall including aisles; stormwater ~8-10% of impervious area).
Parking: compute the MINIMUM number of stalls the ordinance requires for exactly this program, use by use, from the
ordinance's ratios (round up per the ordinance) and report that number; do not add a cushion. Mark drive_thru true for
any use that needs a vehicle queue lane (drive-thru restaurant/coffee/bank/pharmacy, tunnel car wash).
Return ONLY one JSON object."""

PROGRAM_SCHEMA = """{
  "use": "short name of the concept (e.g. 'Retail / QSR outparcels', 'Flex industrial')",
  "rationale": "2-3 sentences: why this use for this site",
  "buildings": [{"name": "Building A", "use": "retail", "footprint_sf": n, "stories": n, "depth_ft": n,
                 "drive_thru": true|false, "notes": "..."}],
  "parking": {"calc": "per-use calculation from the ordinance ratios, e.g. 'Laundromat 4,225 SF @ 1/200 = 22; Car wash: 2 + stacking = 2'",
              "required": n},
  "layout_hint": "front_parking" | "side_parking" | "rear_parking",
  "alternatives": [{"use": "...", "why_not": "..."}]
}"""


def _cache_key(state, jurisdiction_hint, district):
    return re.sub(r"[^a-z0-9|]", "", f"{state}|{jurisdiction_hint}|{district}".lower())


def _cache_get(key):
    r = requests.get(f"{supa.URL}/rest/v1/zoning_cache", params={"key": f"eq.{key}", "select": "rules,updated_at"},
                     headers=supa._h(), timeout=30)
    rows = r.json() if r.ok else []
    if rows:
        age = dt.datetime.utcnow() - dt.datetime.fromisoformat(rows[0]["updated_at"].replace("Z", "").split("+")[0])
        if age.days < 180:
            return rows[0]["rules"]
    return None


def _cache_put(key, rules):
    requests.post(f"{supa.URL}/rest/v1/zoning_cache", json={"key": key, "rules": rules, "updated_at": dt.datetime.utcnow().isoformat() + "Z"},
                  headers=supa._h({"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}), timeout=30)


def research(listing, parcel, summary):
    """Zoning rules for the parcel's district. Uses the parcel-data zoning code when present, else the listing's."""
    attrs = (parcel or {}).get("attrs") or {}
    district = (parcel or {}).get("zoning") or attrs.get("zoning") or listing.get("zoning")
    if district and "," in str(district):  # assemblage with several codes: research the commercial-looking one first
        district = str(district).split(",")[0].strip()
    place = ", ".join(x for x in [listing.get("city"), (listing.get("county") or attrs.get("county")) and f"{listing.get('county') or attrs.get('county')} County",
                                  listing.get("state")] if x)
    juris_hint = attrs.get("zoning_description") or attrs.get("city") or listing.get("county") or listing.get("city") or ""
    key = _cache_key(listing.get("state"), place, district)
    cached = _cache_get(key)
    if cached:
        cached["_cached"] = True
        return cached
    user = (f"Property: {listing.get('street_address') or ''} {listing.get('lot') or ''}, {place}.\n"
            f"Zoning district per parcel records / listing: {district!r} (listing says {listing.get('zoning')!r}; "
            f"parcel data description: {attrs.get('zoning_description')!r}).\n"
            f"Parcel: {summary.get('parcel_acres')} acres, {summary.get('frontage_ft')} ft frontage on {', '.join(summary.get('fronting_roads') or []) or 'a public road'}.\n"
            f"Find the zoning rules for this district and return them in this JSON shape:\n{ZONING_SCHEMA}\n"
            "Include at least 15 common commercial uses in `uses` (retail, restaurant, restaurant with drive-through, "
            "convenience store with fuel, car wash, laundromat, medical office, professional office, bank with drive-through, "
            "self-storage, contractor/flex warehouse, light industrial, daycare, multifamily, hotel, auto repair) "
            "and the parking ratio for each of those uses where the ordinance gives one.")
    rules, sources = ask_json(ZONING_SYSTEM, user, max_tokens=6000, web_search=8, timeout=480)
    rules["sources"] = list(dict.fromkeys((rules.get("sources") or []) + sources))[:12]
    rules["researched_at"] = dt.date.today().isoformat()
    _cache_put(key, rules)
    return rules


def setbacks_from(rules, defaults=None):
    d = defaults or {"front": 25, "side": 10, "rear": 10}
    sb = (rules or {}).get("setbacks_ft") or {}
    return {k: float(sb[k]) if isinstance(sb.get(k), (int, float)) else float(d[k]) for k in ("front", "side", "rear")}


def program(listing, summary, rules):
    user = (f"LISTING: {json.dumps({k: listing.get(k) for k in ('street_address','city','state','acres','price','zoning','frontage_ft','utilities','summary')})}\n"
            f"SITE (measured): {json.dumps({k: summary.get(k) for k in ('parcel_acres','buildable_acres','envelope_acres','frontage_ft','fronting_roads','wetland_acres','flood_sfha_acres')})}\n"
            f"ZONING: {json.dumps({k: rules.get(k) for k in ('jurisdiction','district','district_name','setbacks_ft','max_height_ft','max_lot_coverage_pct','max_impervious_pct','max_far','parking_ratios','uses','notes')})}\n"
            f"Choose the highest-and-best-use concept and return it in this JSON shape:\n{PROGRAM_SCHEMA}")
    prog, _ = ask_json(PROGRAM_SYSTEM, user, max_tokens=2500, timeout=240)
    for b in prog.get("buildings", []):
        b["footprint_sf"] = float(b.get("footprint_sf") or 5000)
        b["stories"] = int(b.get("stories") or 1)
        b["depth_ft"] = float(b.get("depth_ft") or (60 if b["footprint_sf"] < 8000 else 100))
    return prog
