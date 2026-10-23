"""Existing pavement (parking lots, drives) on the parcel, read from the NAIP aerial: asphalt / concrete is grey and
unsaturated, unlike vegetation, bare soil and water. Buildings (footprint data) are cut out first. Rough: the user
confirms each area on a map (reuse / remove), so it favours clean, plausible shapes over completeness."""
import math

import numpy as np
from PIL import Image, ImageFilter
from shapely.geometry import Polygon
from shapely.ops import unary_union

STALL_SF = 340.0      # parking lot area per stall including aisles and islands (9x18 stall + share of aisle)


def _mask(img, scale=2):
    """Boolean asphalt/concrete-like mask at 1/scale resolution: grey, unsaturated, smooth (trees are dark and noisy)."""
    from scipy import ndimage as ndi
    sm = img.convert("RGB").resize((img.size[0] // scale, img.size[1] // scale), Image.BILINEAR)
    a = np.asarray(sm).astype(float) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx, mn = a.max(axis=2), a.min(axis=2)
    sat = (mx - mn) / (mx + 1e-6)
    lum = a.mean(axis=2)
    mu = ndi.uniform_filter(lum, 5)
    std = np.sqrt(np.maximum(ndi.uniform_filter(lum * lum, 5) - mu * mu, 0))
    veg = (g > r * 1.05) & (g > b * 1.08)
    warm = (r > b * 1.25)
    m = (sat < 0.12) & (lum > 0.26) & (lum < 0.85) & (std < 0.035) & ~veg & ~warm
    return m


def detect(aerial, parcel_sp, k, bld=None, osm=None, min_sf=2500.0):
    """-> [{id, geom (state plane), area_sf, kind, est_stalls}] for pavement inside the parcel, largest first."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy import ndimage as ndi
    img = aerial["image"]
    minx, miny, maxx, maxy = aerial["extent"]
    W, H = img.size
    scale = 2
    m = _mask(img, scale)
    h, w = m.shape
    # cut buildings and everything outside the parcel (with a little margin) out of the mask
    from PIL import ImageDraw
    keep = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(keep)
    px = lambda x, y: ((x - minx) / (maxx - minx) * w, (maxy - y) / (maxy - miny) * h)  # noqa: E731
    for p in (getattr(parcel_sp.buffer(3 / k), "geoms", [parcel_sp.buffer(3 / k)])):
        d.polygon([px(x, y) for x, y in p.exterior.coords], fill=255)
    for b in bld or []:
        for p in getattr(b["geom"].buffer(2 / k), "geoms", [b["geom"].buffer(2 / k)]):
            d.polygon([px(x, y) for x, y in p.exterior.coords], fill=0)
    m = m & (np.asarray(keep) > 127)
    # clean up: close small gaps (paint, cars), drop specks
    m = ndi.binary_closing(m, structure=np.ones((5, 5)), iterations=2)
    m = ndi.binary_opening(m, structure=np.ones((5, 5)), iterations=1)
    m = ndi.binary_fill_holes(m) | m
    if not m.any():
        m = np.zeros_like(m)
    xs = minx + (np.arange(w) + 0.5) / w * (maxx - minx)
    ys = maxy - (np.arange(h) + 0.5) / h * (maxy - miny)
    fig = plt.figure()
    cs = plt.contourf(xs, ys, ndi.gaussian_filter(m.astype(float), 1.0), levels=[0.5, 2.0]) if m.any() else None
    polys = []
    for path in (cs.get_paths() if cs is not None else []):
        for ring in path.to_polygons():
            if len(ring) >= 4:
                g = Polygon(ring).buffer(0)
                if not g.is_empty:
                    polys.append(g)
    plt.close(fig)
    if isinstance(osm, list):
        for o in osm:
            polys.append(o["geom"].intersection(parcel_sp))
    cut = unary_union([b["geom"].buffer(2 / k) for b in bld or []]) if bld else None
    if not polys:
        return []
    u = unary_union(polys).buffer(1.5 / k).buffer(-1.5 / k).buffer(0)
    if cut is not None:
        u = u.difference(cut)
    lots = unary_union([o["geom"] for o in osm if o["kind"] == "parking"]) if isinstance(osm, list) and any(o["kind"] == "parking" for o in osm) else None
    out = []
    for g in getattr(u, "geoms", [u]):
        if g.geom_type != "Polygon":
            continue
        g = Polygon(g.exterior, [hh for hh in g.interiors if Polygon(hh).area * k * k > 1500]).buffer(0)
        g = g.intersection(parcel_sp)
        for q in getattr(g, "geoms", [g]):
            if q.geom_type != "Polygon" or q.area * k * k < min_sf:
                continue
            q = q.simplify(1.0 / k)
            mrr = q.minimum_rotated_rectangle
            cs_ = list(mrr.exterior.coords)
            e1, e2 = math.dist(cs_[0], cs_[1]) * k, math.dist(cs_[1], cs_[2]) * k
            kind = "parking" if (min(e1, e2) >= 45 or (lots is not None and q.intersection(lots).area > 0.3 * q.area)) else "drive"
            a = q.area * k * k
            out.append({"geom": q, "area_sf": a, "kind": kind, "est_stalls": int(a / STALL_SF) if kind == "parking" else 0})
    out.sort(key=lambda p: -p["area_sf"])
    for i, p in enumerate(out, 1):
        p["id"] = f"P{i}"
    return out
