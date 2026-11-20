"""A site from Apollo Redline alone: a test fit sketched over an existing-conditions plan made outside Drafted.

Redline sends everything in the sheet's own CAD coordinates (feet, the same as its DXF export of that sheet, so Drafted's
CAD files land on top of the surveyor's drawing):
  parcels, frontage edge, the test-fit design (with the frame that maps sheet coordinates into it), setbacks, program,
  the EC plan's contours (read off the PDF with their labels) and spot grades, wetlands / flood / existing buildings
  tagged on the sheet, the sewer and water tie-in points the user picked, the site's town (rainfall and soils only),
  and the sheet itself as an image (the "aerial" behind the concept and the rendering).
This module turns that into the same context Drafted builds from public data, so the rest of the pipeline is unchanged.
No AI is used.
"""
import base64
import io
import math

import numpy as np
from scipy.interpolate import griddata
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

CELL_FT = 5.0


def _poly(r):
    p = Polygon([tuple(map(float, q)) for q in r])
    return p if p.is_valid else p.buffer(0)


def parcel(payload):
    rings = [r for r in payload.get("parcels") or [] if r and len(r) >= 3]
    if not rings:
        raise ValueError("The sketch has no property line")
    P = unary_union([_poly(r) for r in rings])
    return P if P.geom_type == "Polygon" else max(P.geoms, key=lambda g: g.area)


def dem_from_contours(payload, P, pad=150.0):
    """Existing ground from the EC plan's contours (and spot grades): a TIN of the contour vertices rasterised at 5 ft,
    nearest value past the last contour. Same shape as the 3DEP grid Drafted normally uses."""
    pts, zs = [], []
    for c in payload.get("contours") or []:
        z = c.get("z")
        if z is None or not c.get("pts") or len(c["pts"]) < 2:
            continue
        ln = LineString([tuple(map(float, q)) for q in c["pts"]])
        n = max(2, int(ln.length / 4.0))
        for i in range(n + 1):
            q = ln.interpolate(i / n, normalized=True)
            pts.append((q.x, q.y))
            zs.append(float(z))
    for s in payload.get("spots") or []:
        if s.get("z") is not None:
            pts.append((float(s["x"]), float(s["y"])))
            zs.append(float(s["z"]))
    if len(pts) < 20 or len(set(zs)) < 2:
        raise ValueError("No contours with elevations were read from the EC plan (the site needs labelled contours for the existing ground)")
    pts, zs = np.array(pts), np.array(zs)
    x0, y0, x1, y1 = P.buffer(pad).bounds
    nx, ny = int(math.ceil((x1 - x0) / CELL_FT)), int(math.ceil((y1 - y0) / CELL_FT))
    xs = x0 + (np.arange(nx) + 0.5) * CELL_FT
    ys = y1 - (np.arange(ny) + 0.5) * CELL_FT
    X, Y = np.meshgrid(xs, ys)
    Z = griddata(pts, zs, (X, Y), method="linear")
    hole = ~np.isfinite(Z)
    if hole.any():
        _, k = cKDTree(pts).query(np.c_[X[hole], Y[hole]])
        Z[hole] = zs[k]
    hull = Polygon(LineString(pts).convex_hull.exterior.coords) if len(pts) > 3 else None
    cover = hull.intersection(P).area / P.area if hull is not None and P.area > 0 else 0.0
    from . import topo
    dem = {"z": Z.astype("float32"), "x0": x0, "y1": y1, "cell": CELL_FT, "cell_ft": CELL_FT, "k": 1.0, "epsg": None, "source": "EC plan contours"}
    topo.analyze(dem)
    return dem, cover


def frontage_road(payload, P):
    """The street along the frontage edge: a 24 ft road 30 ft outside the property line (its centreline), long enough to
    pass the whole frontage. Only the drive's connection and the service corridor use it; the tie-ins are the user's."""
    fr = payload.get("frontage")
    if fr and len(fr) == 2:
        a, b = np.array(fr[0], float), np.array(fr[1], float)
    else:
        cs = list(P.exterior.coords)
        a, b = np.array(cs[0], float), np.array(cs[1], float)
    u = (b - a) / (np.linalg.norm(b - a) or 1.0)
    n = np.array([-u[1], u[0]])
    mid = (a + b) / 2
    if P.contains(Point(*(mid + n * 5))):
        n = -n                                          # outward: away from the site
    off = 30.0
    c0, c1 = a - u * 200 + n * off, b + u * 200 + n * off
    cl = LineString([tuple(c0), tuple(c1)])
    return [{"centerline": cl, "name": "Frontage street", "ref": None, "class": "residential", "lanes": 2, "oneway": False,
             "width_ft": 24.0, "pavement": cl.buffer(12.0, cap_style=2), "shoulder_ft": 0, "pavement_clip": cl.buffer(12.0, cap_style=2)}]


def geocode(text):
    from .geo import get, google_geocode
    q = (text or "").strip()
    if not q:
        return None
    try:
        g = google_geocode(q)
        if g:
            return g[0], g[1]
    except Exception:  # noqa: BLE001
        pass
    try:
        js = get("https://nominatim.openstreetmap.org/search", params={"q": q, "format": "json", "limit": 1, "countrycodes": "us"}).json()
        if js:
            return float(js[0]["lat"]), float(js[0]["lon"])
    except Exception:  # noqa: BLE001
        pass
    return None


def context(payload, sheet_jpg=None):
    """The site context the rest of Drafted runs on, from the Redline payload."""
    from PIL import Image
    P = parcel(payload)
    dem, cover = dem_from_contours(payload, P)
    tagged = payload.get("tagged") or {}
    wet = [{"geom": _poly(r), "type": "Wetland (from the EC plan)", "code": "EC"} for r in tagged.get("wet") or [] if len(r) >= 3]
    fl = [{"geom": _poly(r), "sfha": "T", "zone": "EC"} for r in tagged.get("flood") or [] if len(r) >= 3]
    bld = []
    for i, r in enumerate(tagged.get("keep_bldgs") or [], 1):
        if len(r) >= 3:
            g = _poly(r)
            bld.append({"geom": g, "src": "EC plan", "kind": "Existing building", "levels": None, "osm_id": None, "area_sf": g.area,
                        "pct_on_site": 100.0, "onsite": True, "id": f"E{i}"})
    ae = (payload.get("aerial") or {})
    ext = ae.get("extent")
    if sheet_jpg is None and ae.get("jpg_b64"):
        sheet_jpg = base64.b64decode(ae["jpg_b64"])
    if sheet_jpg is None or not ext:      # no sheet image: a plain light ground the size of the site
        x0, y0, x1, y1 = P.buffer(300).bounds
        im = Image.new("RGB", (800, max(1, int(800 * (y1 - y0) / (x1 - x0)))), (236, 236, 230))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=85)
        sheet_jpg, ext = b.getvalue(), (x0, y0, x1, y1)
    img = Image.open(io.BytesIO(sheet_jpg)).convert("RGB")
    ll = geocode(payload.get("location"))
    warnings = []
    if cover < 0.9:
        warnings.append(f"The EC plan's contours cover about {cover * 100:.0f}% of the property; the rest of the existing ground is extended from the nearest contour")
    if ll is None:
        warnings.append("The site location could not be found: rainfall and soils use regional defaults")
    ctx = {"parcel_sp": P, "k": 1.0, "epsg": None, "latlon": ll, "rds": frontage_road(payload, P), "wet": wet, "fl": fl, "bld": bld, "pave": [],
           "topo": dem, "aerial": {"extent": tuple(ext), "source": "EC plan (sheet)", "jpg": sheet_jpg, "image": img}, "warnings": warnings,
           "source": "redline"}
    return ctx
