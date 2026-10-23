"""Existing buildings on the parcel: pick the on-site ones, give them stable ids (E1, E2, ...), and apply the user's
keep / demolish choices. Choices live in job.zoning.building_actions = {"E1": "keep" | "demo"}; the default is keep."""
from shapely.ops import unary_union

from .geo import to_crs

KEEP_CLEAR_FT = 12.0     # clear distance the layout holds around a building that stays


def prepare(bld, parcel_sp, k):
    """bld: context.buildings() list. Returns [{id, geom, onsite, area_sf, pct_on_site, src, kind, levels}] with ids
    E1.. assigned to buildings that sit on the parcel (largest first)."""
    if not isinstance(bld, list):
        return []
    out = []
    for b in bld:
        g = b["geom"]
        if g.is_empty:
            continue
        inside = g.intersection(parcel_sp).area
        pct = inside / g.area if g.area else 0.0
        out.append({**b, "area_sf": g.area * k * k, "pct_on_site": pct, "onsite": pct >= 0.3 and inside * k * k >= 100, "id": None})
    n = 0
    for b in sorted([b for b in out if b["onsite"]], key=lambda b: -b["area_sf"]):
        n += 1
        b["id"] = f"E{n}"
    return out


def summary(items, epsg):
    """On-site buildings as JSON for the UI map (lat/lon rings) and the plan facts."""
    res = []
    for b in items:
        if not b["onsite"]:
            continue
        ll = to_crs(b["geom"], epsg, 4326)
        ring = [[round(y, 6), round(x, 6)] for x, y in ll.exterior.coords]
        res.append({"id": b["id"], "area_sf": round(b["area_sf"], -1) if b["area_sf"] > 500 else round(b["area_sf"]),
                    "pct_on_site": round(b["pct_on_site"], 2), "src": b["src"], "label": b.get("kind") or "Building",
                    "levels": b.get("levels"), "ring": ring})
    return res


def split(items, actions):
    """(keep, demo) lists of on-site buildings. Anything without an explicit "demo" stays."""
    actions = actions or {}
    on = [b for b in (items or []) if b.get("onsite")]
    return [b for b in on if actions.get(b["id"]) != "demo"], [b for b in on if actions.get(b["id"]) == "demo"]


def keep_zone(keep, k):
    """Geometry the layout must stay out of: each kept building plus clearance."""
    return unary_union([b["geom"].buffer(KEEP_CLEAR_FT / k) for b in keep]) if keep else None
