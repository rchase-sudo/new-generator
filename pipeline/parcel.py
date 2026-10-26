"""Find candidate parcels for a listing.

Order: Regrid (when the token covers the county) -> statewide / county ArcGIS parcel layers.
Every candidate carries its own acreage computed from geometry in state-plane feet so the listing
acreage can be checked independently of whatever the source reports.
"""
import json

import requests
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
    "NH": ["https://nhgeodata.unh.edu/nhgeodata/rest/services/CAD/ParcelMosaic/MapServer",
           "https://granit24a.sr.unh.edu/nhgeodata/rest/services/CAD/ParcelMosaic/MapServer",
           "https://granit24a.sr.unh.edu/hosting/rest/services/Hosted/CAD_ParcelMosaic/FeatureServer"],
    "VA": ["https://vginmaps.vdem.virginia.gov/arcgis/rest/services/VA_Base_Layers/VA_Parcels/FeatureServer/0"],
}


def _acres(geom_sp, epsg):
    k = units_to_ft(epsg)
    return geom_sp.area * k * k / 43560.0


def _norm_apn(s):
    return re.sub(r"[^0-9A-Za-z]", "", str(s or "")).upper()


def _apns(listing):
    a = listing.get("apn")
    if not a:
        return []
    if isinstance(a, str):
        a = re.split(r"[;,&]|\band\b", a)
    return [x.strip() for x in a if str(x).strip()]


def _apn_hit(c, wanted):
    """True when any of the candidate's id-like attributes equals one of the listing's APNs/PINs."""
    if not wanted:
        return False
    vals = {_norm_apn(c.get("apn"))}
    for v in (c.get("attrs") or {}).values():
        if isinstance(v, (str, int)) and 5 <= len(str(v)) <= 30:
            vals.add(_norm_apn(v))
    return bool(vals & wanted)


# ---------------------------------------------------------------- Regrid
REGRID_ERRORS = []   # last search's Regrid failures (quota, auth, coverage) so the job error can say why


def regrid_search(listing, lat, lon, epsg, radius_m=300, limit=80):
    """Parcels from Regrid AT the geocoded point (plus any APN/PIN the listing gives).
    Regrid's address search is not used: it matches the wrong road too often. The geocode decides where; Regrid
    only supplies the parcel data around that point."""
    REGRID_ERRORS[:] = []
    tok = os.environ.get("REGRID_TOKEN")
    if not tok:
        REGRID_ERRORS.append("REGRID_TOKEN not set")
        return [], (lat, lon)
    feats = []

    def call(path, **params):
        try:
            r = requests.get(f"{REGRID}/parcels/{path}", params={**params, "token": tok}, timeout=60)
            if r.status_code >= 400:
                REGRID_ERRORS.append(f"{path} {r.status_code}: {r.text[:200]}")
                return []
            return _features(r.json())
        except Exception as e:  # noqa: BLE001  (no coverage / quota / bad query)
            REGRID_ERRORS.append(f"{path}: {e}"[:250])
            return []

    for apn in _apns(listing):
        feats += call("apn", parcelnumb=apn, limit=5)
    feats += call("point", lat=lat, lon=lon, radius=radius_m, limit=limit)
    out = []
    for f in feats:
        if not f.get("geometry"):
            continue
        fields = _fields(f)
        out.append(_candidate("regrid", f["geometry"], epsg, apn=fields.get("parcelnumb"), owner=fields.get("owner"),
                              address=fields.get("address"), zoning=fields.get("zoning"), house_no=_house_no(fields), attrs=fields))
    return dedupe_stacked(out), (lat, lon)


def dedupe_stacked(cands):
    """Counties often stack several records on one footprint (condo units, personal-property accounts, splits in
    progress). Keep one candidate per footprint and list all its parcel numbers."""
    groups = {}
    for c in cands:
        g = shape(c["geometry"])
        key = (round(g.area, 9), round(g.centroid.x, 6), round(g.centroid.y, 6))
        if key in groups:
            k = groups[key]
            apns = [a for a in str(k.get("apn") or "").split(", ") if a]
            if c.get("apn") and str(c["apn"]) not in apns:
                apns.append(str(c["apn"]))
            k["apn"] = ", ".join(apns)
            k["stacked"] = k.get("stacked", 1) + 1
        else:
            groups[key] = dict(c)
    return list(groups.values())


def _fields(f):
    p = f.get("properties", {})
    return p.get("fields", p)


def _house_no(fields):
    v = fields.get("saddno") or fields.get("address_number")
    if v:
        return str(v).strip().split()[0].lstrip("0") or None
    hn, _ = _addr_key(fields.get("address"))
    return hn


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


def _resolve_layer(url):
    """A statewide entry may be a service root: find its parcel polygon layer (name match, else the first polygon layer)."""
    if re.search(r"/\d+/?$", url):
        return url
    lyr = find_layer(url)
    if lyr:
        return lyr
    for i in range(0, 4):
        try:
            js = get(f"{url.rstrip('/')}/{i}", params={"f": "json"}, timeout=30).json()
            if js.get("geometryType") == "esriGeometryPolygon":
                return f"{url.rstrip('/')}/{i}"
        except Exception:  # noqa: BLE001
            continue
    return url


def arcgis_search(layer_urls, listing, lat, lon, epsg, half_m=700, corridor_ll=None):
    out = []
    for url in layer_urls:
        try:
            url = _resolve_layer(url)
        except Exception:  # noqa: BLE001
            continue
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
    if not county or not state:   # nothing to search for (callers fill county from the geocode first)
        return None
    county = re.sub(r"\s+(County|Parish|Borough)$", "", str(county), flags=re.I)
    q = (f"Find the public ArcGIS REST endpoint (MapServer or FeatureServer URL) for tax parcel polygons in {county} County, {state}. "
         "Reply with ONLY the URL, ending in /MapServer, /FeatureServer, or a numbered layer. Reply NONE if not found.")
    r = post("https://api.anthropic.com/v1/messages",
             headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
             json={"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), "max_tokens": 600,
                   "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                   "messages": [{"role": "user", "content": q}]}, timeout=180)
    from .claude import record
    record(r.json(), "Find county parcel layer")
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


ROAD_WORDS = {"E", "W", "N", "S", "EAST", "WEST", "NORTH", "SOUTH", "US", "HWY", "HIGHWAY", "RD", "ROAD", "ST", "STREET", "AVE",
              "AVENUE", "DR", "DRIVE", "LN", "LANE", "TRL", "TRAIL", "BLVD", "BOULEVARD", "SR", "ROUTE", "RT", "RTE", "STATE", "PIKE",
              "PKWY", "PARKWAY", "WAY", "CT", "COURT", "CIR", "TPKE", "BYP", "BYPASS", "BUS", "BUSINESS", "OLD", "THE", "I", "INTERSTATE"}


def road_tokens(s):
    return {t for t in re.findall(r"[A-Za-z0-9]+", str(s or "").upper()) if t not in ROAD_WORDS}


def _addr_key(a):
    """'2839 US 70 Hwy' -> ('2839', {'70'}) ; '2839 E US 70 HWY' -> ('2839', {'70'})"""
    if not a:
        return None, set()
    toks = re.findall(r"[A-Za-z0-9]+", str(a).upper())
    if not toks or not toks[0].isdigit():
        return None, set()
    skip = ROAD_WORDS
    return toks[0], {t for t in toks[1:] if t not in skip}


def rank(cands, listing, lat, lon, road_geom_ll=None, aliases=None, geocode_pt=None):
    target = listing.get("acres")
    geo_pt = Point(geocode_pt[1], geocode_pt[0]) if geocode_pt else Point(lon, lat)
    wanted = {_norm_apn(a) for a in _apns(listing)}
    pt = Point(lon, lat)
    seen, out = set(), []
    for c in cands:
        key = round(c["acres"], 3), round(shape(c["geometry"]).centroid.x, 6), round(shape(c["geometry"]).centroid.y, 6)
        if key in seen:
            continue
        seen.add(key)
        g = shape(c["geometry"])
        score = 0.0
        if target:
            err = abs(c["acres"] - target) / target
            score += max(0.0, 1 - err * 4) * 60      # within 25% -> some credit; exact -> 60
        c["apn_match"] = _apn_hit(c, wanted)
        if c["apn_match"]:
            score += 80
        hn, words = _addr_key(listing.get("street_address"))
        chn, cwords = _addr_key(c.get("address"))
        chn = c.get("house_no") or chn
        big_enough = not target or c["acres"] >= 0.2 * target   # a 0.07-ac lot can't be a 4-ac listing
        if hn and chn and chn.lstrip("0") == hn.lstrip("0") and g.distance(geo_pt) * 111320 < 600 and big_enough:
            c["address_match"] = True
            score += 90                               # same house number, near the geocode (street name may be an alias)
            if (words & cwords) or any((grp & words) and (grp & cwords) for grp in (aliases or [])):
                score += 20                           # and the street is the same road under any of its names
        if road_geom_ll is not None:
            score += max(0.0, 25 - g.distance(road_geom_ll) * 111320 / 8)  # fronting the named road
        # where the address actually is (Google / Census point): inside the parcel, or right next to it
        d_geo = g.distance(geo_pt) * 111320
        c["dist_m"] = round(d_geo)
        if d_geo == 0:
            score += 110
        else:
            score += max(0.0, 70 - d_geo / 2.5)          # e.g. 25 m (street centreline) -> 60, 175 m -> 0
        c["score"] = round(score, 1)
        out.append(c)
    out.sort(key=lambda c: -c["score"])
    hits = [c for c in out if c.get("apn_match")]
    if len(hits) >= 2:  # listing is an assemblage of several tax parcels -> offer the combined site first
        g = unary_union([shape(c["geometry"]) for c in hits]).buffer(0)
        out.insert(0, {"source": hits[0]["source"], "geometry": mapping(g), "acres": round(sum(c["acres"] for c in hits), 3),
                       "apn": ", ".join(str(c.get("apn")) for c in hits), "owner": "; ".join(sorted({str(c.get("owner")) for c in hits if c.get("owner")})),
                       "address": hits[0].get("address"), "zoning": ", ".join(sorted({str(c.get("zoning")) for c in hits if c.get("zoning")})),
                       "assemblage": True, "parts": len(hits), "score": round(max(c["score"] for c in hits) + 50, 1)})
    return out


def suggest_assemblage(cands, target, geocode_pt, max_parts=3, near_m=250):
    """Listings are often several tax parcels sold together, under an address the county doesn't use.
    Look for 2-3 touching parcels near the geocode whose areas add up to the listing acreage (within 3%)."""
    here = Point(geocode_pt[1], geocode_pt[0])
    pool = [c for c in cands if not c.get("assemblage") and c["acres"] < target * 1.03][:40]
    geoms = [shape(c["geometry"]).buffer(0.00003) for c in pool]  # ~3 m tolerance: parcel layers have small gaps
    best = None

    def consider(ix):
        nonlocal best
        tot = sum(pool[i]["acres"] for i in ix)
        err = abs(tot - target) / target
        if err > 0.03:
            return
        if min(geoms[i].distance(here) for i in ix) * 111320 > near_m:
            return
        if best is None or err < best[0]:
            best = (err, ix)

    n = len(pool)
    for a in range(n):
        for b in range(a + 1, n):
            if not geoms[a].intersects(geoms[b]):
                continue
            consider((a, b))
            if max_parts >= 3:
                for c in range(b + 1, n):
                    if geoms[c].intersects(geoms[a]) or geoms[c].intersects(geoms[b]):
                        consider((a, b, c))
    if not best:
        return None
    parts = [pool[i] for i in best[1]]
    g = unary_union([shape(c["geometry"]) for c in parts]).buffer(0)
    return {"source": parts[0]["source"], "geometry": mapping(g), "acres": round(sum(c["acres"] for c in parts), 3),
            "apn": ", ".join(str(c.get("apn")) for c in parts), "address": " / ".join(str(c.get("address")) for c in parts),
            "zoning": ", ".join(sorted({str(c.get("zoning")) for c in parts if c.get("zoning")})),
            "assemblage": True, "suggested": True, "parts": len(parts), "score": round(max(c["score"] for c in parts) + 45, 1)}


def find_parcels(listing, lat, lon, road_geom_ll=None, county_layer=None, max_out=6, corridor_ll=None):
    """corridor_ll: lon/lat polygon along the listing's road, used when the listing has no street number."""
    epsg = state_plane_epsg(lat, lon)
    geocode_pt = (lat, lon)
    cands, anchor = regrid_search(listing, lat, lon, epsg)
    if cands:
        lat, lon = anchor
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
    aliases = None
    if _addr_key(listing.get("street_address"))[0]:
        from .context import road_aliases
        aliases = road_aliases(lat, lon)
    ranked = rank(cands, listing, lat, lon, road_geom_ll, aliases, geocode_pt)
    zipc = str(listing.get("zip") or "")[:5]
    decided = ranked and (ranked[0].get("apn_match") or ranked[0].get("address_match") or ranked[0].get("assemblage"))
    if zipc and len(ranked) > 1 and not decided:
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
    assemblage_note = None
    if target and ranked and not decided:
        best_single = min(abs(c["acres"] - target) / target for c in ranked)
        if best_single > 0.05:
            try:
                combo = suggest_assemblage(ranked, target, geocode_pt)
            except Exception as e:  # noqa: BLE001
                combo, assemblage_note = None, f"assemblage search failed: {e}"
            else:
                assemblage_note = f"assemblage: {'found ' + combo['apn'] if combo else 'none within 3%'}"
            if combo:
                ranked.insert(0, combo)
    pool = [{"address": c.get("address"), "apn": c.get("apn"), "acres": c.get("acres"), "score": c.get("score"),
             "dist_m": round(shape(c["geometry"]).distance(Point(geocode_pt[1], geocode_pt[0])) * 111320)} for c in ranked]
    return {"epsg": epsg, "source": source, "county_layer": county_layer, "candidates": ranked[:max_out],
            "debug": {"geocode": geocode_pt, "anchor": [lat, lon], "assemblage": assemblage_note, "pool": pool[:150]}}
