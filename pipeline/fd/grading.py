"""Proposed grading from the concept layout: drive centerline + vertical profile, pavement / pad / pond surfaces, 3:1
daylighting into the existing DEM, proposed contours, spot grades, earthwork. Everything in the local feet frame."""
import heapq
import math

import matplotlib
import numpy as np
from scipy import ndimage as ndi
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import substring, unary_union

from .base import CELL, Grid, lines, polys

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

MAX_DRIVE_GRADE = 0.06
MIN_GRADE = 0.006
CROSS = 0.015          # pavement cross-slope off the spine
DAYLIGHT = 1.0 / 3.0   # 3:1 slopes
K_CREST, K_SAG = 12.0, 12.0


def spine(g, pave_mask, hard, entry_pt):
    """Centerline of the paved circulation from the entrance to its far end: least-cost path through the pavement that
    prefers the middle of the pavement."""
    dist_edge = ndi.distance_transform_edt(pave_mask) * g.cell
    cost = 1.0 / (0.6 + np.minimum(dist_edge, 14.0) / 6.0)
    cost = np.where(pave_mask, cost, np.inf)
    ij = g.ij(*entry_pt)
    # snap the start to the nearest paved cell
    if not pave_mask[ij]:
        jj, ii = np.nonzero(pave_mask)
        k = np.argmin((g.xs[ii] - entry_pt[0]) ** 2 + (g.ys[jj] - entry_pt[1]) ** 2)
        ij = (jj[k], ii[k])
    dist = np.full(cost.shape, np.inf)
    prev = {}
    dist[ij] = 0.0
    pq = [(0.0, ij)]
    while pq:
        d, (j, i) = heapq.heappop(pq)
        if d > dist[j, i]:
            continue
        for dj, di in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            jj, ii = j + dj, i + di
            if 0 <= jj < cost.shape[0] and 0 <= ii < cost.shape[1] and np.isfinite(cost[jj, ii]):
                nd = d + cost[jj, ii] * (1.414 if dj and di else 1.0)
                if nd < dist[jj, ii]:
                    dist[jj, ii] = nd
                    prev[(jj, ii)] = (j, i)
                    heapq.heappush(pq, (nd, (jj, ii)))
    fin = np.where(np.isfinite(dist), dist, -1)
    far = np.unravel_index(np.argmax(fin), fin.shape)
    path = [tuple(far)]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    path = path[::-1]
    if len(path) < 2:
        return None
    ln = LineString([(g.xs[i], g.ys[j]) for j, i in path]).simplify(CELL * 0.8)
    return ln


def design_profile(stations, z_ex):
    """Vertical profile: smoothed existing ground, grade-limited, with a minimum grade; returns elevations at stations."""
    ds = stations[1] - stations[0]
    n = len(z_ex)
    w = max(3, int(60 / ds) | 1)
    t = ndi.uniform_filter1d(z_ex, w, mode="nearest")
    z = t.copy()
    z[0] = z_ex[0]                               # tie to existing at the right-of-way
    gmax = MAX_DRIVE_GRADE
    for i in range(1, n):                        # forward: limit to max grade, follow the target
        z[i] = min(max(t[i], z[i - 1] - gmax * ds), z[i - 1] + gmax * ds)
    # minimum grade so the pavement drains
    trend = np.sign(z[-1] - z[0]) or 1.0
    for i in range(1, n):
        g = (z[i] - z[i - 1]) / ds
        if abs(g) < MIN_GRADE:
            z[i] = z[i - 1] + MIN_GRADE * (np.sign(g) if g != 0 else trend) * ds
    return z


def pvis(stations, z, tol=0.3):
    """Douglas-Peucker on the profile -> PVIs [(sta, elev)]."""
    from shapely.geometry import LineString as LS
    pl = LS(list(zip(stations, z))).simplify(tol)
    return [(float(a), float(b)) for a, b in pl.coords]


def curves(pv):
    """Vertical curves at interior PVIs. Returns (functions list, table rows)."""
    rows = []
    for k in range(1, len(pv) - 1):
        (s0, z0), (s1, z1), (s2, z2) = pv[k - 1], pv[k], pv[k + 1]
        g1, g2 = (z1 - z0) / (s1 - s0), (z2 - z1) / (s2 - s1)
        A = (g2 - g1) * 100.0
        if abs(A) < 0.4:
            continue
        K = K_CREST if A < 0 else K_SAG
        L = max(30.0, K * abs(A))
        L = min(L, 0.9 * min(s1 - s0, s2 - s1) * 2)
        rows.append({"pvi_sta": s1, "pvi_elev": z1, "g1": g1 * 100, "g2": g2 * 100, "A": A, "K": L / abs(A) if A else 0.0, "L": L,
                     "bvc": s1 - L / 2, "evc": s1 + L / 2, "bvce": z1 - g1 * L / 2, "evce": z1 + g2 * L / 2,
                     "low_sta": (s1 - L / 2) + (-g1 * 100 / A) * L if A > 0 else None})
    return rows


def eval_profile(pv, cv, s):
    s = np.asarray(s, float)
    z = np.interp(s, [p[0] for p in pv], [p[1] for p in pv])
    for c in cv:
        m = (s >= c["bvc"]) & (s <= c["evc"])
        x = s[m] - c["bvc"]
        z[m] = c["bvce"] + c["g1"] / 100 * x + (c["g2"] - c["g1"]) / 100 / (2 * c["L"]) * x ** 2
    return z


def run(L, ex_z, grid, road_local, spec=None):
    """L: dict of local geometries: pave, walk, bldgs (list), drive, pond, parcel, keep_pave (existing pavement stays), keep_bldgs.
    Returns the grading result dict."""
    g = grid
    pave = L["pave"]
    pave_mask = g.mask(pave.buffer(2.0))
    pave_mask = ndi.binary_closing(pave_mask, iterations=1) | pave_mask
    bl = L["bldgs"]
    bmask = [g.mask(b) for b in bl]
    allb = np.zeros(g.X.shape, bool)
    for m in bmask:
        allb |= m
    warns = []

    # entrance: the paved point nearest the road
    drive = L.get("drive")
    hard = pave.union(drive) if drive is not None and not drive.is_empty else pave
    if road_local is not None and not road_local.is_empty:
        a = hard.boundary
        from shapely.ops import nearest_points
        entry = nearest_points(road_local, hard)[1]
        entry_pt = (entry.x, entry.y)
    else:
        b = hard.bounds
        entry_pt = (b[0], (b[1] + b[3]) / 2)
    sp = spine(g, g.mask(hard.buffer(1.0)), hard, entry_pt)
    if sp is None:
        sp = LineString([entry_pt, hard.centroid.coords[0]])
    sp = LineString([entry_pt] + list(sp.coords)) if Point(sp.coords[0]).distance(Point(entry_pt)) > 2 else sp
    length = sp.length
    ds = 10.0
    st = np.arange(0, length + ds, ds)
    st = st[st <= length] if st[-1] > length else st
    if st[-1] < length:
        st = np.append(st, length)
    pts = [sp.interpolate(s) for s in st]
    zx = g.at(ex_z, [p.x for p in pts], [p.y for p in pts])
    zd = design_profile(st, zx) if len(st) > 3 else zx.copy()
    pv = pvis(st, zd)
    if len(pv) < 2:
        pv = [(0.0, float(zd[0])), (float(st[-1]), float(zd[-1]))]
    cv = curves(pv)
    zprof = eval_profile(pv, cv, st)
    gr = np.diff(zprof) / np.diff(st) * 100
    if len(gr) and np.abs(gr).max() > MAX_DRIVE_GRADE * 100 + 0.5:
        warns.append(f"Drive grade reaches {np.abs(gr).max():.1f}%")

    # pavement surface: crowned off the spine (nearest spine station)
    fine = np.arange(0, length + 2.5, 2.5)
    fine = fine[fine <= length]
    fp = np.array([[sp.interpolate(s).x, sp.interpolate(s).y] for s in fine])
    zf = eval_profile(pv, cv, fine)
    from scipy.spatial import cKDTree
    tree = cKDTree(fp)
    jj, ii = np.nonzero(pave_mask)
    dd, k = tree.query(np.c_[g.xs[ii], g.ys[jj]])
    z_new = np.full(g.X.shape, np.nan)
    z_new[jj, ii] = zf[k] - CROSS * np.minimum(dd, 70.0)
    # buildings: slab 0.5 ft above the highest adjacent pavement, rounded to 0.1
    ffe = []
    for b, m in zip(bl, bmask):
        ring = g.mask(b.buffer(14)) & pave_mask & ~m
        base = float(np.nanmax(z_new[ring])) if ring.any() else float(np.nanmean(g.at(ex_z, [b.centroid.x], [b.centroid.y])))
        e = round(base + 0.5, 1)
        ffe.append(e)
        z_new[m] = e
    walk_mask = g.mask(L["walk"]) & ~pave_mask & ~allb if L.get("walk") is not None and not L["walk"].is_empty else np.zeros(g.X.shape, bool)
    if walk_mask.any():
        near = ndi.distance_transform_edt(~(np.isfinite(z_new)), return_indices=True)
        z_new[walk_mask] = np.round(z_new[near[1][0][walk_mask], near[1][1][walk_mask]] - 0.1, 2)

    # pond (elevations are set by the hydrology step; a placeholder floor lets the daylighting see the basin)
    design = np.isfinite(z_new)
    return {"spine": sp, "stations": st, "profile": zprof, "ex_profile": zx, "pvi": pv, "curves": cv, "z_design": z_new, "design_mask": design,
            "ffe": ffe, "pave_mask": pave_mask, "bmask": allb, "walk_mask": walk_mask, "warnings": warns}


def surface(g, ex_z, z_design, design_mask, pond=None, protect=None):
    """Full proposed surface: design cells as set; everything else follows the existing ground clamped to 3:1 from the
    nearest design cell (cut and fill slopes daylight into the existing grade)."""
    z = ex_z.copy()
    dist, idx = ndi.distance_transform_edt(~design_mask, return_indices=True)
    zd = z_design[idx[0], idx[1]]
    dft = dist * g.cell
    out = np.clip(ex_z, zd - DAYLIGHT * dft, zd + DAYLIGHT * dft)
    out = np.where(design_mask, z_design, out)
    # soften the clamped band so contours don't kink (design cells and far field stay)
    soft = ndi.gaussian_filter(out, 1.0)
    band = (~design_mask) & (dft < 80)
    out = np.where(band, soft, out)
    out = np.where(design_mask, z_design, out)
    return taper_protect(g, out, ex_z, protect, design_mask)


def taper_protect(g, z, ex_z, protect, design_mask, ramp=20.0):
    """Never regrade a wetland or stream: inside it the ground stays as it is, and the proposed grading eases back to the
    existing ground over `ramp` ft outside it (a smooth transition, not a cliff, so contours stay clean)."""
    if protect is None or not protect.any():
        return z
    d = ndi.distance_transform_edt(~protect) * g.cell
    w = np.clip(d / ramp, 0.0, 1.0)
    w = w * w * (3 - 2 * w)
    out = ex_z + (z - ex_z) * w
    out = np.where(design_mask & ~protect, z, out)
    return np.where(protect & ~design_mask, ex_z, out)


def pond_surface(g, zd, poly, top_el, bottom_el, side, berm=6.0):
    """Pond basin written into the design surface from the EXACT pond outline (not the 5 ft raster): the bank falls at 1:side
    from the top elevation to the floor, with a level berm outside. Returns (zd, pond_mask, rim_mask)."""
    import shapely
    zd = zd.copy()
    x0, y0, x1, y1 = poly.buffer(berm + 2).bounds
    sel = (g.X >= x0) & (g.X <= x1) & (g.Y >= y0) & (g.Y <= y1)
    pts = shapely.points(g.X[sel], g.Y[sel])
    inside = shapely.contains(poly, pts)
    dist = shapely.distance(pts, poly.boundary)
    pm = np.zeros(g.X.shape, bool)
    rim = np.zeros(g.X.shape, bool)
    pm[sel] = inside
    rim[sel] = (~inside) & (dist <= berm)
    zz = np.full(g.X.shape, np.nan)
    zz[sel] = np.where(inside, np.maximum(bottom_el, top_el - dist / max(side, 0.1)), top_el)
    zd[pm] = zz[pm]
    return zd, pm, rim, zz


def smooth_around(g, z, keep, zone, sigma=1.6, passes=2):
    """Relax the daylight slopes around the pond: smooth z inside `zone` but never inside `keep` (cells whose elevation is set)."""
    out = z.copy()
    for _ in range(passes):
        sm = ndi.gaussian_filter(out, sigma)
        out = np.where(zone & ~keep, sm, out)
    return out


def contour_lines(g, z, interval=2.0, index=10.0, clip=None, fine=1):
    lo, hi = np.nanmin(z), np.nanmax(z)
    lv = np.arange(math.ceil(lo / interval) * interval, hi, interval)
    if not len(lv):
        return []
    xs, ys = g.xs, g.ys
    if fine > 1:        # cubic upsample so a smooth surface gives smooth contours rather than 5 ft grid steps
        z = ndi.zoom(np.where(np.isfinite(z), z, np.nanmean(z)), fine, order=3)
        xs = np.linspace(g.xs[0], g.xs[-1], z.shape[1])
        ys = np.linspace(g.ys[0], g.ys[-1], z.shape[0])
    fig = plt.figure()
    cs = plt.contour(xs, ys, z, levels=lv)
    out = []
    for lev, segs in zip(cs.levels, cs.allsegs):
        for s in segs:
            if len(s) < 3:
                continue
            ln = LineString(s).simplify(0.7)
            if clip is not None:
                ln = ln.intersection(clip)
            for q in lines(ln):
                if q.length > (30 if fine > 1 else 6):
                    out.append((float(lev), q, abs(lev / index - round(lev / index)) < 1e-6))
    plt.close(fig)
    return out


def earthwork(g, ex_z, z_prop, lod_floor=0.1):
    dz = z_prop - ex_z
    a = g.cell ** 2
    cut = float(np.clip(-dz, 0, None)[np.abs(dz) > lod_floor].sum() * a / 27.0)
    fill = float(np.clip(dz, 0, None)[np.abs(dz) > lod_floor].sum() * a / 27.0)
    dist = np.abs(dz) > lod_floor
    return {"cut_cy": round(cut, -1), "fill_cy": round(fill, -1), "net_cy": round(fill - cut, -1),
            "max_cut_ft": round(float(np.clip(-dz, 0, None).max()), 1), "max_fill_ft": round(float(np.clip(dz, 0, None).max()), 1),
            "disturbed_ac": round(float(dist.sum() * a / 43560.0), 2), "dist_mask": dist}
