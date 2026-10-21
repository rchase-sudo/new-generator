"""Find candidate parcels for a listing.

Order: Regrid (when the token covers the county) -> statewide / county ArcGIS parcel layers.
Every candidate carries its own acreage computed from geometry in state-plane feet so the listing
acreage can be checked independently of whatever the source reports.
"""
import json
import os
import re

from shapely.geometry import Point, mapping, shape
from shapely.ops import unary_union

from .geo import geojson_to, state_plane_epsg, to_crs, units_to_ft
from .http import get, post

REGRID = "https://app.regrid.com/api/v2"

# Statewide parcel layers (ArcGIS REST, queryable; each is validated at query time and skipped on failure). Add more as states come up; county layers are
# discovered on demand and cached in Supabase (county_sources).
STATEWIDE = {
    "VA": ["https://vginmaps.vdem.virginia.gov/arcgis/rest/services/VA_Base_Layers/VA_Parcels/FeatureServer/0"],
}


def _acres(geom_sp, epsg):
    k = units_to_ft(epsg)
    return geom_sp.area * k * k / 43560.0


def _norm_apn(s):
    return re.sub(r"[^0-9A-Za-z]", "", str(s or "")).upper()


# ---------------------------------------------------------------- Regrid
def regrid_search(listing, lat, lon, epsg, radius_m=600, limit=60):
    tok = os.environ.get("REGRID_TOKEN")
    if not tok:
        return []
    feats = []
    try:
        if listing.get("apn"):
            r = get(f"{REGRID}/parcels/apn", params={"parcelnumber": listing["apn"], "token": tok, "limit": 10})
            feats = _features(r.json())
        street = (listing.get("street_address") or "").strip()
        if not feats and street[:1].isdigit():
            q = ", ".join(x for x in [street, listing.get("city"), listing.get("state"), listing.get("zip")] if x)
            r = get(f"{REGRID}/parcels/address", params={"query": q, "token": tok, "limit": 5})
            feats = _features(r.json())
            if feats:  # also pull neighbours so the approval screen can offer alternatives (assemblages, wrong match)
                try:
                    r2 = get(f"{REGRID}/parcels/point", params={"lat": lat, "lon": lon, "radius": 150, "limit": 15, "token": tok})
                    feats += _features(r2.json())
                except Exception:  # noqa: BLE001
                    pass
        if not feats:
            r = get(f"{REGRID}/parcels/point", params={"lat": lat, "lon": lon, "radius": radius_m, "limit": limit, "token": tok})
            feats = _features(r.json())
    except Exception:  # noqa: BLE001  (no coverage / sandbox limits / quota) -> fall back
        return []
    out = []
    for f in feats:
        if not f.get("geometry"):
            continue
        p = f.get("properties", {})
        fields = p.get("fields", p)
        out.append(_candidate("regrid", f["geometry"], epsg, apn=fields.get("parcelnumb"), owner=fields.get("owner"),
                              address=fields.get("address"), zoning=fields.get("zoning"), attrs=fields))
    return out


def _features(js):
    if "features" in js:
        return js["features"]
    p = js.get("parcels")
    if isinstance(p, dict):
        return p.get("features", [])
    return []


# ---------------------------------------------------------------- ArcGIS
def arcgis_query_envelope(layer_url, lat, lon, half_m=700):
    d_lat = half_m / 111320.0
    import math
    d_lon = half_m / (111320.0 * math.cos(math.radians(lat)))
    env = f"{lon - d_lon},{lat - d_lat},{lon + d_lon},{lat + d_lat}"
    params = {"geometry": env, "geometryType": "esriGeometryEnvelope", "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
              "outFields": "*", "returnGeometry": "true", "outSR": 4326, "f": "geojson", "resultRecordCount": 2000}
    r = get(f"{layer_url}/query", params=params, timeout=90)
    js = r.json()
    if "error" in js:
        raise RuntimeError(js["error"])
    return js.get("features", [])


def arcgis_query_polygon(layer_url, poly_ll):
    """Parcels intersecting a (simplified) lon/lat polygon, queried in ~2 km tiles to stay under record caps."""
    from shapely.geometry import box as _box
    poly_ll = poly_ll.simplify(0.0002)
    minx, miny, maxx, maxy = poly_ll.bounds
    step = 0.02
    feats, seen = [], set()
    y = miny
    while y < maxy:
        x = minx
        while x < maxx:
            cell = poly_ll.intersection(_box(x, y, x + step, y + step))
            x += step
            for part in getattr(cell, "geoms", [cell]):
                if part.is_empty or part.geom_type != "Polygon":
                    continue
                rings = [list(map(list, part.exterior.coords))]
                geom = json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}})
                r = post(f"{layer_url}/query", data={"geometry": geom, "geometryType": "esriGeometryPolygon", "inSR": 4326,
                                                      "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "returnGeometry": "true",
                                                      "outSR": 4326, "f": "geojson"}, timeout=120)
                js = r.json()
                if "error" in js:
                    raise RuntimeError(js["error"])
                for f in js.get("features", []):
                    key = json.dumps(f.get("geometry", {}).get("coordinates", [])[:1])[:200]
                    if key not in seen:
                        seen.add(key)
                        feats.append(f)
        y += step
    return feats


def find_layer(service_url):
    """Given a MapServer/FeatureServer root, return the parcel polygon layer URL."""
    js = get(service_url, params={"f": "json"}).json()
    best = None
    for lyr in js.get("layers", []):
        name = lyr.get("name", "").lower()
        if "parcel" in name and "label" not in name and "anno" not in name and lyr.get("geometryType", "esriGeometryPolygon") == "esriGeometryPolygon":
            if best is None or name in ("parcels", "parcel", "tax parcels"):
                best = lyr
    return f"{service_url.rstrip('/')}/{best['id']}" if best else None


APN_KEYS = ("PARCELID", "PARCEL_ID", "PIN", "GPIN", "APN", "TAXMAP", "TAX_MAP", "MAP_NUM", "MAPNUM", "PARCELNUMB", "PARCELNUM", "PARCEL_NUM", "LOCALID", "PARCEL")
OWNER_KEYS = ("OWNER", "OWNER1", "OWNERNAME", "OWNER_NAME", "OWNNAME")


def _pick(attrs, keys):
    up = {k.upper(): v for k, v in attrs.items()}
    for k in keys:
        if up.get(k) not in (None, ""):
            return up[k]
    return None


def arcgis_search(layer_urls, listing, lat, lon, epsg, half_m=700, corridor_ll=None):
    out = []
    for url in layer_urls:
        try:
            feats = arcgis_query_polygon(url, corridor_ll) if corridor_ll is not None else arcgis_query_envelope(url, lat, lon, half_m)
        except Exception:  # noqa: BLE001
            continue
        for f in feats:
            if not f.get("geometry"):
                continue
            a = f.get("properties", {})
            out.append(_candidate("arcgis:" + url, f["geometry"], epsg, apn=_pick(a, APN_KEYS), owner=_pick(a, OWNER_KEYS), attrs=a))
        if out:
            break
    return out


def discover_county_layer(listing):
    """Ask Claude (with web search) for the county's public ArcGIS REST parcel layer, then validate it."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    county, state = listing.get("county"), listing.get("state")
    q = (f"Find the public ArcGIS REST endpoint (MapServer or FeatureServer URL) for tax parcel polygons in {county} County, {state}. "
         "Reply with ONLY the URL, ending in /MapServer, /FeatureServer, or a numbered layer. Reply NONE if not found.")
    r = post("https://api.anthropic.com/v1/messages",
             headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
             json={"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 600,
                   "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                   "messages": [{"role": "user", "content": q}]}, timeout=180)
    txt = " ".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    m = re.findall(r"https?://\S+?/(?:MapServer|FeatureServer)(?:/\d+)?", txt)
    for u in m:
        try:
            if re.search(r"/\d+$", u):
                js = get(u, params={"f": "json"}).json()
                if js.get("geometryType") == "esriGeometryPolygon":
                    return u
            else:
                lyr = find_layer(u)
                if lyr:
                    return lyr
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------- candidates
def _candidate(source, geojson, epsg, **kw):
    g = shape(geojson)
    if not g.is_valid:
        g = g.buffer(0)
    sp = to_crs(g, 4326, epsg)
    return {"source": source, "geometry": mapping(g), "acres": round(_acres(sp, epsg), 3), **kw}


def _addr_key(a):
    """'2839 US 70 Hwy' -> ('2839', {'70'}) ; '2839 E US 70 HWY' -> ('2839', {'70'})"""
    if not a:
        return None, set()
    toks = re.findall(r"[A-Za-z0-9]+", str(a).upper())
    if not toks or not toks[0].isdigit():
        return None, set()
    skip = {"E", "W", "N", "S", "US", "HWY", "HIGHWAY", "RD", "ROAD", "ST", "STREET", "AVE", "DR", "LN", "TRL", "BLVD", "SR", "NC", "VA", "ROUTE", "RT"}
    return toks[0], {t for t in toks[1:] if t not in skip}


def rank(cands, listing, lat, lon, road_geom_ll=None):
    target = listing.get("acres")
    apn = _norm_apn(listing.get("apn"))
    pt = Point(lon, lat)
    seen, out = set(), []
    for c in cands:
        key = round(c["acres"], 3), _norm_apn(c.get("apn"))
        if key in seen:
            continue
        seen.add(key)
        g = shape(c["geometry"])
        score = 0.0
        if target:
            err = abs(c["acres"] - target) / target
            score += max(0.0, 1 - err * 4) * 60      # within 25% -> some credit; exact -> 60
        if apn and _norm_apn(c.get("apn")) and (_norm_apn(c.get("apn")) == apn or apn in _norm_apn(c.get("apn"))):
            score += 80
        hn, words = _addr_key(listing.get("street_address"))
        chn, cwords = _addr_key(c.get("address"))
        if hn and chn == hn and (words & cwords):
            score += 100                              # same house number on the same street
        if road_geom_ll is not None:
            score += max(0.0, 25 - g.distance(road_geom_ll) * 111320 / 8)  # fronting the named road
        score += max(0.0, 10 - g.distance(pt) * 111320 / 100)
        c["score"] = round(score, 1)
        out.append(c)
    out.sort(key=lambda c: -c["score"])
    return out


def find_parcels(listing, lat, lon, road_geom_ll=None, county_layer=None, max_out=6, corridor_ll=None):
    """corridor_ll: lon/lat polygon along the listing's road, used when the listing has no street number."""
    epsg = state_plane_epsg(lat, lon)
    cands = regrid_search(listing, lat, lon, epsg)
    source = "regrid" if cands else None
    if not cands:
        urls = ([county_layer] if county_layer else []) + STATEWIDE.get((listing.get("state") or "").upper(), [])
        cands = arcgis_search(urls, listing, lat, lon, epsg, half_m=1500 if road_geom_ll is not None else 900, corridor_ll=corridor_ll)
        source = "arcgis"
        if not cands:
            found = discover_county_layer(listing)
            if found:
                county_layer = found
                cands = arcgis_search([found], listing, lat, lon, epsg, half_m=1500, corridor_ll=corridor_ll)
    target = listing.get("acres")
    if corridor_ll is not None and target:
        near = [c for c in cands if abs(c["acres"] - target) / target <= 0.15]
        cands = near or cands
    ranked = rank(cands, listing, lat, lon, road_geom_ll)
    zipc = str(listing.get("zip") or "")[:5]
    if zipc and len(ranked) > 1:
        # Break ties between look-alike parcels on the same road: same ZIP as the listing wins.
        for c in ranked[:8]:
            try:
                pt = shape(c["geometry"]).representative_point()
                js = get("https://nominatim.openstreetmap.org/reverse",
                         params={"lat": pt.y, "lon": pt.x, "format": "json", "zoom": 16}).json()
                c["zip"] = (js.get("address", {}).get("postcode") or "")[:5]
                if c["zip"] == zipc:
                    c["score"] = round(c["score"] + 40, 1)
                import time as _t
                _t.sleep(1.1)  # Nominatim usage policy: 1 request / second
            except Exception:  # noqa: BLE001
                continue
        ranked.sort(key=lambda c: -c["score"])
    return {"epsg": epsg, "source": source, "county_layer": county_layer, "candidates": ranked[:max_out]}
