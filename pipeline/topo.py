"""Rough topography: USGS 3DEP elevation (served best-available, ~10 m nationwide, lidar-derived where it exists) turned
into slope, steep-area constraints, contours and a cut/fill estimate. Not survey grade -- good for siting, not grading."""
import io
import math

import numpy as np
from PIL import Image
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from .geo import to_crs
from .http import get

M2FT = 3.28084
DEM_SERVICES = ["https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"]
DEFAULT_MAX_SLOPE = 15.0          # % -- steeper than this is kept out of the building / parking area
CLASSES = [(0, 5, "0-5%"), (5, 10, "5-10%"), (10, 15, "10-15%"), (15, 25, "15-25%"), (25, 1e9, "over 25%")]


def _export(svc, bbox, epsg, W, H):
    r = get(f"{svc}/exportImage", params={
        "bbox": ",".join(f"{v:.3f}" for v in bbox), "bboxSR": epsg, "imageSR": epsg, "size": f"{W},{H}", "format": "tiff",
        "pixelType": "F32", "interpolation": "RSP_BilinearInterpolation", "f": "image"}, timeout=90)
    im = Image.open(io.BytesIO(r.content))
    a = np.asarray(im).astype("float32")
    if a.ndim != 2 or a.shape != (H, W):
        raise RuntimeError(f"unexpected DEM shape {a.shape}")
    return a


def _samples(svc, bbox, epsg, W, H):
    """Fallback: point samples on a grid (slower, coarser)."""
    import json
    x0, y0, x1, y1 = bbox
    xs, ys = np.linspace(x0, x1, W), np.linspace(y1, y0, H)
    pts = [[float(x), float(y)] for y in ys for x in xs]
    out = np.full(W * H, np.nan, dtype="float32")
    for i in range(0, len(pts), 800):
        chunk = pts[i:i + 800]
        r = get(f"{svc}/getSamples", params={
            "geometry": json.dumps({"points": chunk, "spatialReference": {"wkid": int(epsg)}}),
            "geometryType": "esriGeometryMultipoint", "returnFirstValueOnly": "true", "interpolation": "RSP_BilinearInterpolation", "f": "json"}, timeout=60)
        for s in r.json().get("samples", []):
            try:
                out[i + int(s["locationId"])] = float(s["value"])
            except (KeyError, ValueError, TypeError):
                pass
    return out.reshape(H, W)


def fetch(extent_sp, epsg, k, cell_ft=10.0):
    """Elevation grid (feet) over the extent plus a 100 ft margin. Returns {z, x0, y1, cell, k} (row 0 = north) or {"error"}."""
    minx, miny, maxx, maxy = extent_sp
    pad = 100.0 / k
    bbox = (minx - pad, miny - pad, maxx + pad, maxy + pad)
    W = int(min(1400, max(40, (bbox[2] - bbox[0]) * k / cell_ft)))
    H = int(min(1400, max(40, (bbox[3] - bbox[1]) * k / cell_ft)))
    last = None
    for svc in DEM_SERVICES:
        for fn in (_export, _samples if W * H <= 40000 else None):
            if fn is None:
                continue
            try:
                a = fn(svc, bbox, epsg, W, H)
                a = np.where((a < -500) | (a > 30000) | ~np.isfinite(a), np.nan, a)
                if np.isnan(a).mean() > 0.3:
                    raise RuntimeError("DEM has gaps")
                if np.isnan(a).any():
                    a = np.where(np.isnan(a), np.nanmedian(a), a)
                z = a * M2FT
                cell = (bbox[2] - bbox[0]) / W
                return {"z": z.astype("float32"), "x0": bbox[0], "y1": bbox[3], "cell": cell, "cell_ft": cell * k, "k": k, "epsg": epsg}
            except Exception as e:  # noqa: BLE001
                last = e
    return {"error": f"{type(last).__name__}: {last}"[:200]}


def _smooth(z, n=1):
    a = z
    for _ in range(n):
        p = np.pad(a, 1, mode="edge")
        a = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] + 4 * p[1:-1, 1:-1]) / 8.0
    return a


def analyze(dem):
    """Adds slope (%, 3x3 smoothed) to the DEM dict."""
    z = _smooth(dem["z"], 2)
    c = dem["cell_ft"]
    gy, gx = np.gradient(z, c)
    dem["zs"] = z.astype("float32")
    dem["slope"] = (np.hypot(gx, gy) * 100.0).astype("float32")
    return dem


def _xy(dem):
    h, w = (dem["zs"] if "zs" in dem else dem["z"]).shape
    xs = dem["x0"] + (np.arange(w) + 0.5) * dem["cell"]
    ys = dem["y1"] - (np.arange(h) + 0.5) * dem["cell"]
    return xs, ys


def sample(dem, x, y, field="zs"):
    """Value of a grid field at state-plane points (arrays)."""
    a = dem[field]
    h, w = a.shape
    i = np.clip(((np.asarray(x) - dem["x0"]) / dem["cell"]).astype(int), 0, w - 1)
    j = np.clip(((dem["y1"] - np.asarray(y)) / dem["cell"]).astype(int), 0, h - 1)
    return a[j, i]


def steep_polys(dem, parcel_sp, thr=DEFAULT_MAX_SLOPE, min_sf=4000.0):
    """Polygons (state plane) where slope exceeds thr %, inside the parcel's neighbourhood. Small specks are dropped and
    small gaps inside steep ground are filled, so the layout sees ground-scale features, not DEM noise."""
    if not thr or thr <= 0 or "slope" not in dem:
        return []
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    xs, ys = _xy(dem)
    s = _smooth(dem["slope"], 2)
    if s.max() <= thr:
        return []
    fig = plt.figure()
    cs = plt.contourf(xs, ys, s, levels=[thr, 1e9])
    polys = []
    for path in cs.get_paths():
        for ring in path.to_polygons():
            if len(ring) >= 4:
                g = Polygon(ring).buffer(0)
                if not g.is_empty:
                    polys.append(g)
    plt.close(fig)
    if not polys:
        return []
    u = unary_union(polys).buffer(0)
    k = dem["k"]
    out = []
    for g in getattr(u, "geoms", [u]):
        if g.geom_type != "Polygon":
            continue
        g = Polygon(g.exterior, [h for h in g.interiors if Polygon(h).area * k * k > 6000])   # keep only sizeable holes
        if g.area * k * k >= min_sf and g.intersects(parcel_sp.buffer(30 / k)):
            out.append(g.simplify(2.0 / k))
    return out


def contours(dem, interval=None):
    """[(elevation_ft, LineString)] at a relief-appropriate interval; major (index) contours flagged by the caller."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    z = dem["zs"]
    lo, hi = float(z.min()), float(z.max())
    relief = hi - lo
    iv = interval or (1.0 if relief < 6 else 2.0 if relief < 20 else 5.0 if relief < 80 else 10.0 if relief < 250 else 25.0)
    lv = np.arange(math.ceil(lo / iv) * iv, hi, iv)
    if not len(lv):
        return [], iv
    xs, ys = _xy(dem)
    fig = plt.figure()
    cs = plt.contour(xs, ys, z, levels=lv)
    out = []
    for lev, segs in zip(cs.levels, cs.allsegs):
        for seg in segs:
            if len(seg) >= 3:
                out.append((float(lev), LineString(seg).simplify(0.6 / dem["k"])))
    plt.close(fig)
    return out, iv


def summary(dem, parcel_sp, thr=DEFAULT_MAX_SLOPE):
    """Plain numbers for the UI / package, measured on the parcel."""
    import shapely
    k = dem["k"]
    xs, ys = _xy(dem)
    gx, gy = np.meshgrid(xs, ys)
    on = shapely.contains_xy(parcel_sp, gx, gy)
    if on.sum() < 4:
        on = np.ones_like(on)
    s, z = dem["slope"][on], dem["zs"][on]
    n = on.sum()
    classes = [{"label": lab, "pct": float(round(100.0 * float(((s >= a) & (s < b)).sum()) / n, 1))} for a, b, lab in CLASSES]
    return {"available": True, "relief_ft": round(float(z.max() - z.min()), 1), "min_ft": round(float(z.min()), 1), "max_ft": round(float(z.max()), 1),
            "avg_slope_pct": round(float(s.mean()), 1), "p90_slope_pct": round(float(np.percentile(s, 90)), 1),
            "steep_pct": float(round(100.0 * float((s > thr).sum()) / n, 1)) if thr else 0.0, "threshold_pct": thr, "classes": classes,
            "cell_ft": round(dem["cell_ft"], 1), "source": "USGS 3DEP"}


def earthwork(dem, dev_sp, flat_pct=2.0):
    """Rough cut / fill (cubic yards) to bring the development footprint to one pad elevation (the elevation that
    balances cut and fill). Planning number only: no pavement section, no retaining walls, no slope tie-ins."""
    import shapely
    xs, ys = _xy(dem)
    gx, gy = np.meshgrid(xs, ys)
    on = shapely.contains_xy(dev_sp, gx, gy)
    if on.sum() < 10:
        return None
    z = dem["zs"][on]
    pad = float(np.median(z))                      # median pad balances cut and fill for a roughly symmetric surface
    a = dem["cell_ft"] ** 2
    cut = float(np.clip(z - pad, 0, None).sum() * a / 27.0)
    fill = float(np.clip(pad - z, 0, None).sum() * a / 27.0)
    sl = float(dem["slope"][on].mean())
    return {"avg_slope_pct": round(sl, 1), "pad_ft": round(pad, 1), "cut_cy": round(cut, -1), "fill_cy": round(fill, -1), "net_cy": round(cut - fill, -1),
            "area_ac": round(float(on.sum() * a / 43560.0), 2)}
