"""Site context from free public sources: NAIP aerial, NWI wetlands, FEMA flood zones, OSM roads."""
import time
import io
import math

import numpy as np
from PIL import Image
from shapely.geometry import LineString, Polygon, box, shape
from shapely.ops import unary_union

from .geo import to_crs
from .http import get, post

NAIP_SERVICES = [
    "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer",
    "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer",
    "https://gis.apfo.usda.gov/arcgis/rest/services/NAIP/USDA_CONUS_PRIME/ImageServer",   # USDA's own NAIP service
]
NWI_LAYER = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0"
FEMA_LAYER = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
TIGERWEB = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer"
# Census MAF/TIGER feature class -> OSM-like class (S1730 alleys, S1740 private, S1780 parking lots are skipped)
MTFCC = {"S1100": "trunk", "S1200": "primary", "S1400": "residential", "S1630": "service", "S1640": "service"}

# Typical lane / pavement widths (ft) by OSM class when `width`/`lanes` are missing.
LANE_FT = 12.0
DEFAULT_LANES = {"motorway": 2, "trunk": 2, "primary": 2, "secondary": 2, "tertiary": 2, "unclassified": 2, "residential": 2, "service": 1}
SHOULDER_FT = {"motorway": 10, "trunk": 8, "primary": 6, "secondary": 4, "tertiary": 2}


def aerial(extent_sp, epsg, ft_per_unit, ft_per_px=1.0, max_px=4000):
    """Georeferenced NAIP image covering extent (minx, miny, maxx, maxy) in state-plane units."""
    minx, miny, maxx, maxy = extent_sp
    w_ft, h_ft = (maxx - minx) * ft_per_unit, (maxy - miny) * ft_per_unit
    scale = max(w_ft, h_ft) / max_px
    fpp = max(ft_per_px, scale)
    W, H = int(round(w_ft / fpp)), int(round(h_ft / fpp))
    last = None
    import time
    for attempt in range(3):                 # the USGS server throws intermittent 502s; retry the set a few times
        for svc in NAIP_SERVICES:
            try:
                r = get(f"{svc}/exportImage", params={
                    "bbox": f"{minx},{miny},{maxx},{maxy}", "bboxSR": epsg, "imageSR": epsg, "size": f"{W},{H}",
                    "format": "jpgpng", "interpolation": "RSP_BilinearInterpolation", "f": "image"}, timeout=180)
                if r.headers.get("content-type", "").startswith("image"):
                    img = Image.open(io.BytesIO(r.content)).convert("RGB")
                    if np.asarray(img).std() > 3:   # not a blank tile
                        return {"image": img, "extent": extent_sp, "source": svc.split("/")[-2]}
                    last = RuntimeError("blank image")
                else:
                    last = RuntimeError(r.text[:300])
            except Exception as e:  # noqa: BLE001
                last = e
        time.sleep(8 * (attempt + 1))
    raise RuntimeError(f"NAIP imagery unavailable: {last}")


def _arcgis_polys(layer, extent_sp, epsg, fields):
    minx, miny, maxx, maxy = extent_sp
    r = get(f"{layer}/query", params={
        "geometry": f"{minx},{miny},{maxx},{maxy}", "geometryType": "esriGeometryEnvelope", "inSR": epsg,
        "spatialRel": "esriSpatialRelIntersects", "outFields": fields, "returnGeometry": "true", "outSR": epsg, "f": "geojson"}, timeout=120)
    js = r.json()
    if "error" in js:
        raise RuntimeError(js["error"])
    out = []
    for f in js.get("features", []):
        if f.get("geometry"):
            g = shape(f["geometry"])
            out.append((g if g.is_valid else g.buffer(0), f.get("properties", {})))
    return out


def wetlands(extent_sp, epsg):
    try:
        return [{"geom": g, "type": p.get("WETLAND_TYPE") or p.get("Wetlands.WETLAND_TYPE"), "code": p.get("ATTRIBUTE") or p.get("Wetlands.ATTRIBUTE")}
                for g, p in _arcgis_polys(NWI_LAYER, extent_sp, epsg, "*")]
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def flood(extent_sp, epsg):
    try:
        return [{"geom": g, "zone": p.get("FLD_ZONE"), "subtype": p.get("ZONE_SUBTY"), "sfha": p.get("SFHA_TF")}
                for g, p in _arcgis_polys(FEMA_LAYER, extent_sp, epsg, "FLD_ZONE,ZONE_SUBTY,SFHA_TF")]
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def roads(extent_sp, epsg):
    """OSM highways in the extent with estimated edge-of-pavement polygons (state-plane units)."""
    minx, miny, maxx, maxy = extent_sp
    bb_ll = to_crs(box(minx, miny, maxx, maxy), epsg, 4326).bounds  # lon/lat
    q = f"""[out:json][timeout:60];
way["highway"~"motorway|trunk|primary|secondary|tertiary|unclassified|residential|service|motorway_link|trunk_link|primary_link"]
({bb_ll[1]},{bb_ll[0]},{bb_ll[3]},{bb_ll[2]});out geom tags;"""
    js = None
    deadline = time.time() + 140                   # public Overpass mirrors go down / rate-limit often; bounded
    for attempt in range(2):
        for u in OVERPASS:
            if time.time() > deadline:
                break
            try:
                js = post(u, data={"data": q}, timeout=min(45, max(5, deadline - time.time())), tries=1).json()
                break
            except Exception:  # noqa: BLE001
                continue
        if js is not None or time.time() > deadline:
            break
        time.sleep(5)
    if js is None:
        try:
            out = _tiger_roads(bb_ll, epsg)       # Census TIGER roads: names + classes, no lane data
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
        return {"error": "Overpass unavailable"}
    from .geo import units_to_ft
    k = units_to_ft(epsg)
    out = []
    for el in js.get("elements", []):
        pts = [(p["lon"], p["lat"]) for p in el.get("geometry", [])]
        if len(pts) < 2:
            continue
        t = el.get("tags", {})
        hw = t.get("highway", "").replace("_link", "")
        if hw == "service" and t.get("service") in ("parking_aisle", "driveway"):
            continue
        cl = to_crs(LineString(pts), 4326, epsg)
        lanes = _num(t.get("lanes")) or DEFAULT_LANES.get(hw, 2)
        if t.get("oneway") == "yes" and not t.get("lanes"):
            lanes = 2 if hw in ("motorway", "trunk", "primary") else 1
        width_ft = _num(t.get("width"), feet=True) or lanes * LANE_FT
        out.append({"centerline": cl, "name": t.get("name") or t.get("ref"), "ref": t.get("ref"), "class": hw,
                    "lanes": lanes, "oneway": t.get("oneway") == "yes", "width_ft": width_ft,
                    "pavement": cl.buffer(width_ft / 2 / k, cap_style=2, join_style=2),
                    "shoulder_ft": SHOULDER_FT.get(hw, 0)})
    return out


def _road(cl, name, ref, hw, lanes, oneway, k, width_ft=None):
    width_ft = width_ft or lanes * LANE_FT
    return {"centerline": cl, "name": name, "ref": ref, "class": hw, "lanes": lanes, "oneway": oneway, "width_ft": width_ft,
            "pavement": cl.buffer(width_ft / 2 / k, cap_style=2, join_style=2), "shoulder_ft": SHOULDER_FT.get(hw, 0)}


def _tiger_roads(bb_ll, epsg):
    """Fallback road source: Census TIGERweb (finds its road layers at runtime; ids change between vintages)."""
    from .geo import units_to_ft
    k = units_to_ft(epsg)
    svc = get(TIGERWEB, params={"f": "json"}, timeout=60).json()
    layers = [l for l in svc.get("layers", []) if "road" in l.get("name", "").lower() and "label" not in l.get("name", "").lower()]
    out, seen = [], set()
    for l in layers:
        js = get(f"{TIGERWEB}/{l['id']}/query", timeout=90, params={
            "geometry": f"{bb_ll[0]},{bb_ll[1]},{bb_ll[2]},{bb_ll[3]}", "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "outSR": 4326, "f": "geojson"}).json()
        for f in js.get("features", []):
            pr = {kk.upper(): v for kk, v in (f.get("properties") or {}).items()}
            hw = MTFCC.get(str(pr.get("MTFCC") or ""))
            g = f.get("geometry") or {}
            if not hw or g.get("type") not in ("LineString", "MultiLineString"):
                continue
            parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
            for pts in parts:
                key = (pr.get("NAME"), round(pts[0][0], 6), round(pts[0][1], 6))
                if len(pts) < 2 or key in seen:
                    continue
                seen.add(key)
                cl = to_crs(LineString([(x, y) for x, y, *_ in pts]), 4326, epsg)
                out.append(_road(cl, pr.get("NAME") or pr.get("BASENAME"), None, hw, DEFAULT_LANES.get(hw, 2), False, k))
    return out


def _num(v, feet=False):
    if not v:
        return None
    try:
        s = str(v).lower().replace("'", " ft")
        n = float(s.split()[0].split(";")[0])
        if feet and ("ft" not in s):
            n = n / 0.3048  # OSM width is metres unless stated
        return n
    except ValueError:
        return None


def named_road(road_list, name):
    """Merge segments whose name/ref matches the listing's road name."""
    if not name or isinstance(road_list, dict):
        return None
    key = name.lower().replace("trl", "trail").replace("rd", "road").replace(" ", "")
    hits = [r["centerline"] for r in road_list if r["name"] and key[:8] in r["name"].lower().replace(" ", "")]
    return unary_union(hits) if hits else None


SUFFIXES = {"trl", "trail", "rd", "road", "st", "street", "ave", "avenue", "hwy", "highway", "dr", "drive", "ln", "lane", "way",
            "blvd", "boulevard", "pike", "pkwy", "parkway", "ct", "court", "cir", "circle", "tpke", "turnpike", "route", "rt", "us", "sr"}


def road_key(name):
    """'Pocahontas Trl' -> ('Pocahontas', None); 'US Route 60' -> (None, '60')."""
    import re
    if not name:
        return None, None
    words = [w for w in re.split(r"[^A-Za-z0-9]+", name) if w]
    num = next((w for w in words if w.isdigit()), None)
    core = [w for w in words if w.lower() not in SUFFIXES and not w.isdigit()]
    return (" ".join(core) or None), num


def named_road_near(name, lat, lon, radius_m=12000):
    """OSM ways matching the road name (or route number) within radius of a point. Returns lon/lat geometry."""
    core, num = road_key(name)
    if not core and not num:
        return None
    conds = []
    if core:
        conds.append(f'way(around:{radius_m},{lat},{lon})["highway"]["name"~"{core}",i];')
    if num:
        conds.append(f'way(around:{radius_m},{lat},{lon})["highway"]["ref"~"(^|[^0-9]){num}($|[^0-9])"];')
    q = f"[out:json][timeout:60];({''.join(conds)});out geom tags;"
    for u in OVERPASS:
        try:
            js = post(u, data={"data": q}, timeout=120).json()
            lines = [LineString([(p["lon"], p["lat"]) for p in el["geometry"]]) for el in js.get("elements", []) if len(el.get("geometry", [])) > 1]
            return unary_union(lines) if lines else None
        except Exception:  # noqa: BLE001
            continue
    return None


def road_aliases(lat, lon, radius_m=500):
    """Groups of names one road goes by near a point, from OSM tags (name, ref, alt/old/official names).
    e.g. {"NEW", "RALEIGH", "70"} for a way tagged name=New Raleigh Highway, ref=US 70."""
    from .parcel import road_tokens
    q = f'[out:json][timeout:25];way(around:{radius_m},{lat},{lon})["highway"];out tags;'
    for u in OVERPASS:
        try:
            js = post(u, data={"data": q}, timeout=40, tries=2).json()
        except Exception:  # noqa: BLE001
            continue
        groups = []
        for el in js.get("elements", []):
            t = el.get("tags", {})
            names = [t.get(k) for k in ("name", "ref", "alt_name", "old_name", "official_name", "old_ref", "nat_ref", "loc_name") if t.get(k)]
            toks = set()
            for n in names:
                for part in str(n).split(";"):
                    toks |= road_tokens(part)
            if toks:
                groups.append(toks)
        return groups
    return []


# ---------------------------------------------------------------- existing buildings
STRUCTURES_LAYER = "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/USA_Structures_View/FeatureServer/0"   # FEMA / ORNL USA Structures
MIN_BLDG_SF = 120.0


def _bldg_kind(t):
    b = (t.get("building") or "yes").lower()
    return {"house": "House", "detached": "House", "residential": "House", "apartments": "Apartments", "garage": "Garage", "garages": "Garage",
            "shed": "Shed", "barn": "Barn", "farm_auxiliary": "Farm building", "greenhouse": "Greenhouse", "commercial": "Commercial building",
            "retail": "Retail building", "industrial": "Industrial building", "warehouse": "Warehouse", "church": "Church", "roof": "Carport / canopy",
            "carport": "Carport", "cabin": "Cabin", "static_caravan": "Mobile home", "ruins": "Ruins", "service": "Service building"}.get(b, "Building")


def _osm_buildings(bb_ll, epsg, k, budget=100):
    q = f"""[out:json][timeout:50];way["building"]({bb_ll[1]},{bb_ll[0]},{bb_ll[3]},{bb_ll[2]});out geom tags;"""
    js, deadline = None, time.time() + budget
    for u in OVERPASS:
        if time.time() > deadline:
            break
        try:
            js = post(u, data={"data": q}, timeout=min(45, max(5, deadline - time.time())), tries=1).json()
            break
        except Exception:  # noqa: BLE001
            continue
    if js is None:
        raise RuntimeError("Overpass unavailable")
    out = []
    for el in js.get("elements", []):
        pts = [(p["lon"], p["lat"]) for p in el.get("geometry", [])]
        if len(pts) < 4:
            continue
        t = el.get("tags", {})
        g = to_crs(Polygon(pts), 4326, epsg).buffer(0)
        if g.is_empty:
            continue
        out.append({"geom": g, "src": "OpenStreetMap", "kind": t.get("name") or _bldg_kind(t),
                    "levels": _num(t.get("building:levels")), "osm_id": el.get("id")})
    return out


def _structures(extent_sp, epsg):
    out = []
    for g, p in _arcgis_polys(STRUCTURES_LAYER, extent_sp, epsg, "*"):
        for q in getattr(g, "geoms", [g]):
            if q.geom_type == "Polygon" and not q.is_empty:
                out.append({"geom": q, "src": "USA Structures", "kind": "Building", "levels": None})
    return out


def buildings(extent_sp, epsg):
    """Existing building footprints in the extent (state-plane polygons): OpenStreetMap plus the USA Structures
    footprint inventory (which covers the buildings OSM volunteers haven't mapped). Either source may fail."""
    from .geo import units_to_ft
    k = units_to_ft(epsg)
    minx, miny, maxx, maxy = extent_sp
    bb_ll = to_crs(box(minx, miny, maxx, maxy), epsg, 4326).bounds
    osm, st, errs = [], [], []
    try:
        osm = _osm_buildings(bb_ll, epsg, k)
    except Exception as e:  # noqa: BLE001
        errs.append(f"OSM: {str(e)[:80]}")
    try:
        st = _structures(extent_sp, epsg)
    except Exception as e:  # noqa: BLE001
        errs.append(f"USA Structures: {str(e)[:80]}")
    if errs and not osm and not st:
        return {"error": "; ".join(errs)}
    out = list(osm)
    for s in st:   # keep structures OSM doesn't already have
        a = s["geom"].area
        if a and not any(o["geom"].intersection(s["geom"]).area > 0.3 * a for o in osm):
            out.append(s)
    res = []
    for b in out:
        g = b["geom"].simplify(0.4 / k)
        if g.is_empty or g.area * k * k < MIN_BLDG_SF:
            continue
        res.append({**b, "geom": g})
    return res
