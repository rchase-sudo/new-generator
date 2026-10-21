"""Site context from free public sources: NAIP aerial, NWI wetlands, FEMA flood zones, OSM roads."""
import io
import math

import numpy as np
from PIL import Image
from shapely.geometry import LineString, box, shape
from shapely.ops import unary_union

from .geo import to_crs
from .http import get, post

NAIP_SERVICES = [
    "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer",
    "https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer",
]
NWI_LAYER = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0"
FEMA_LAYER = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]

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
    for svc in NAIP_SERVICES:
        try:
            r = get(f"{svc}/exportImage", params={
                "bbox": f"{minx},{miny},{maxx},{maxy}", "bboxSR": epsg, "imageSR": epsg, "size": f"{W},{H}",
                "format": "jpgpng", "interpolation": "RSP_BilinearInterpolation", "f": "image"}, timeout=180)
            if r.headers.get("content-type", "").startswith("image"):
                img = Image.open(io.BytesIO(r.content)).convert("RGB")
                return {"image": img, "extent": extent_sp, "source": svc.split("/")[-2]}
            last = RuntimeError(r.text[:300])
        except Exception as e:  # noqa: BLE001
            last = e
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
    for u in OVERPASS:
        try:
            js = post(u, data={"data": q}, timeout=120).json()
            break
        except Exception:  # noqa: BLE001
            continue
    if js is None:
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
