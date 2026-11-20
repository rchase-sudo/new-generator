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
    lines_ = []
    for c in payload.get("contours") or []:
        z = c.get("z")
        if z is None or not c.get("pts") or len(c["pts"]) < 2:
            continue
        ln = LineString([tuple(map(float, q)) for q in c["pts"]])
        if ln.length >= 25.0:                       # symbols, hatching and stray marks are not contours
            lines_.append((float(z), ln))
    if lines_:      # elevations that disagree wildly with the rest are misread labels
        zz = np.array([z for z, _ in lines_])
        med = float(np.median(zz))
        mad = float(np.median(np.abs(zz - med))) or 1.0
        lines_ = [(z, ln) for z, ln in lines_ if abs(z - med) <= max(25.0, 6 * mad)]
    gr = payload.get("ground") or {}
    has_ground = bool(gr.get("z") and gr.get("nx") and gr.get("ny"))
    near = [(z, ln) for z, ln in lines_ if ln.distance(P) < 150.0]
    if not has_ground and (len(near) < 5 or len({z for z, _ in near}) < 3):
        raise ValueError(f"Couldn't read the existing contours on this sheet: only {len(near)} usable labelled contour line(s) near the property "
                         f"(Drafted needs the EC plan's contours with their elevation labels). Nothing was designed. Check that the contours on this PDF "
                         f"are vector linework with readable labels, or send a sheet that has them.")
    for z, ln in lines_:
        n = max(2, int(ln.length / 4.0))
        for i in range(n + 1):
            q = ln.interpolate(i / n, normalized=True)
            pts.append((q.x, q.y))
            zs.append(float(z))
    zlo, zhi = (min(z for z, _ in lines_) - 15.0, max(z for z, _ in lines_) + 15.0) if lines_ else (-1e9, 1e9)
    for s in payload.get("spots") or []:
        if s.get("z") is not None and zlo <= float(s["z"]) <= zhi:
            pts.append((float(s["x"]), float(s["y"])))
            zs.append(float(s["z"]))
    # a LandXML existing surface (sampled by Redline on a grid in the sheet's coordinates) takes over wherever it reaches;
    # the sheet's contours only fill in past its edge
    if has_ground:
        gz = np.array([np.nan if v is None else float(v) for v in gr["z"]], float).reshape(int(gr["ny"]), int(gr["nx"]))
        gx = float(gr["x0"]) + np.arange(int(gr["nx"])) * float(gr["cell"])
        gy = float(gr["y0"]) + np.arange(int(gr["ny"])) * float(gr["cell"])
        GX, GY = np.meshgrid(gx, gy)
        ok = np.isfinite(gz)
        if ok.sum() >= 20:
            from shapely.geometry import MultiPoint
            cov = MultiPoint(np.c_[GX[ok], GY[ok]][:: max(1, int(ok.sum() // 4000))]).convex_hull.buffer(float(gr["cell"]))
            keep = [i for i, q in enumerate(pts) if not cov.contains(Point(q))]
            pts, zs = [pts[i] for i in keep] + list(map(tuple, np.c_[GX[ok], GY[ok]])), [zs[i] for i in keep] + list(gz[ok])
    if len(pts) < 20 or len(set(zs)) < 2:
        raise ValueError("No contours with elevations were read from the EC plan (the site needs labelled contours or a LandXML surface for the existing ground)")
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
    src = f"LandXML surface ({gr.get('source') or 'existing'})" if has_ground else "EC plan contours"
    dem = {"z": Z.astype("float32"), "x0": x0, "y1": y1, "cell": CELL_FT, "cell_ft": CELL_FT, "k": 1.0, "epsg": None, "source": src}
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
    nob = [_poly(r) for r in tagged.get("nobuild") or [] if len(r) >= 3]     # tagged no-build / no-disturb / easement areas
    sb_ = payload.get("setbacks") or {}
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
    if cover < 0.5:
        raise ValueError(f"The contours read from the EC plan cover only {cover * 100:.0f}% of the property, too little to model the existing ground. Nothing was designed.")
    if cover < 0.9:
        warnings.append(f"The EC plan's contours cover about {cover * 100:.0f}% of the property; the rest of the existing ground is extended from the nearest contour")
    if ll is None:
        warnings.append("The site location could not be found: rainfall and soils use regional defaults")
    ctx = {"parcel_sp": P, "k": 1.0, "epsg": None, "latlon": ll, "rds": frontage_road(payload, P), "wet": wet, "fl": fl, "bld": bld, "pave": [],
           "topo": dem, "aerial": {"extent": tuple(ext), "source": "EC plan (sheet)", "jpg": sheet_jpg, "image": img}, "warnings": warnings,
           "source": "redline", "nobuild": nob, "wet_setback": float(sb_.get("wet") or 0), "wet_nodisturb": float(sb_.get("wet_nd") or 0)}
    return ctx
