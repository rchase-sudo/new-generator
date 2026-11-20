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
MAX_PARK_GRADE = 0.045  # spine grade through parking: with the 2% cross slope a stall never exceeds 5%
PARK_MAX = 0.05        # parking areas: no steeper than 5% in any direction
MIN_GRADE = 0.01       # every proposed surface drains at 1% or more (pads, pond floors and ADA spaces excepted)
CROSS = 0.02           # pavement cross-slope off the spine (2% preferred)
CURB = 0.5             # 6" granite curb along every pavement edge (reveal)
WALK_X = 0.02          # walks: 2% cross slope down to the curb
ADA_MAX, ADA_MIN = 0.015, 0.01   # ADA spaces + aisles: a plane at 1-1.5% (ADA limit 1:48 = 2.08% any direction)
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


def design_profile(stations, z_ex, gmax_at=None):
    """Vertical profile: smoothed existing ground, grade-limited (gmax_at: per-station limit, lower through parking), with a
    minimum grade; returns elevations at stations."""
    ds = stations[1] - stations[0]
    n = len(z_ex)
    w = max(3, int(60 / ds) | 1)
    t = ndi.uniform_filter1d(z_ex, w, mode="nearest")
    z = t.copy()
    z[0] = z_ex[0]                               # tie to existing at the right-of-way
    gm = np.full(n, MAX_DRIVE_GRADE) if gmax_at is None else np.asarray(gmax_at, float)
    for i in range(1, n):                        # forward: limit to max grade, follow the target
        z[i] = min(max(t[i], z[i - 1] - gm[i] * ds), z[i - 1] + gm[i] * ds)
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
    stalls = unary_union(L.get("stalls") or []) if L.get("stalls") else None
    near_park = stalls.buffer(75.0) if stalls is not None and not stalls.is_empty else None
    gmax_at = [MAX_PARK_GRADE if near_park is not None and near_park.contains(p) else MAX_DRIVE_GRADE for p in pts]
    zd = design_profile(st, zx, gmax_at) if len(st) > 3 else zx.copy()
    pv = pvis(st, zd, tol=0.15)
    if len(pv) < 2:
        pv = [(0.0, float(zd[0])), (float(st[-1]), float(zd[-1]))]
    pv = _min_tangents(pv, st, gmax_at)
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
    # centreline elevation carried smoothly across the pavement (harmonic between the spine cells, so where the spine wraps
    # round a parking field the two sides blend instead of meeting in a cliff), then the 2% cross slope off the spine
    zc = np.full(g.X.shape, np.nan)
    zc[jj, ii] = zf[k]
    fixed = np.zeros(g.X.shape, bool)
    fixed[jj, ii] = dd <= g.cell * 0.75
    zc = _harmonic(zc, pave_mask, fixed)
    z_new[jj, ii] = zc[jj, ii] - CROSS * np.minimum(dd, 220.0)
    z_new = _cap_parking(g, z_new, pave_mask, fixed, L)
    z_new, ada_info = _ada_planes(g, z_new, pave_mask, L)
    z_pave = z_new.copy()                      # gutter (bottom of curb) elevations, before curbs / walks / pads
    # 6" granite curb on every pavement edge (not where the drive meets the road or existing pavement that stays)
    no_curb = [r[2] for r in (L.get("roads") or []) if r[2] is not None and not r[2].is_empty] + ([L["keep_pave"]] if L.get("keep_pave") is not None else [])
    nc_mask = g.mask(unary_union(no_curb).buffer(6.0)) if no_curb else np.zeros(g.X.shape, bool)
    dist_p, idx_p = ndi.distance_transform_edt(~pave_mask, return_indices=True)
    dist_ft = dist_p * g.cell
    z_edge = z_pave[idx_p[0], idx_p[1]]                     # nearest gutter elevation everywhere
    curb_mask = (~pave_mask) & (dist_ft <= g.cell + 0.01) & ~allb & ~nc_mask
    z_new[curb_mask] = z_edge[curb_mask] + CURB
    # walks: top of curb at the curb, rising 2% toward the building (they drain to the curb line)
    walk_mask = g.mask(L["walk"]) & ~pave_mask & ~allb if L.get("walk") is not None and not L["walk"].is_empty else np.zeros(g.X.shape, bool)
    if walk_mask.any():
        z_new[walk_mask] = np.round(z_edge[walk_mask] + CURB + WALK_X * np.maximum(dist_ft[walk_mask] - 0.5, 0.0), 2)
    # buildings: slab above the highest adjacent walk / top of curb (+1"), rounded up to 0.1
    ffe = []
    for b, m in zip(bl, bmask):
        ring = g.mask(b.buffer(14)) & ~m & (walk_mask | curb_mask)
        ringp = g.mask(b.buffer(14)) & pave_mask & ~m
        if ring.any():
            base = float(np.nanmax(z_new[ring])) + 0.08
        elif ringp.any():
            base = float(np.nanmax(z_new[ringp])) + CURB + 0.12
        else:
            base = float(np.nanmean(g.at(ex_z, [b.centroid.x], [b.centroid.y]))) + 0.5
        e = math.ceil(base * 10 - 1e-6) / 10.0
        ffe.append(e)
        z_new[m] = e

    # pond (elevations are set by the hydrology step; a placeholder floor lets the daylighting see the basin)
    design = np.isfinite(z_new)
    return {"spine": sp, "stations": st, "profile": zprof, "ex_profile": zx, "pvi": pv, "curves": cv, "z_design": z_new, "design_mask": design,
            "ffe": ffe, "pave_mask": pave_mask, "bmask": allb, "walk_mask": walk_mask, "warnings": warns,
            "z_pave": z_pave, "curb_mask": curb_mask, "ada": ada_info}


def _min_tangents(pv, st, gmax_at):
    """Every profile tangent at 1% or more (the vertical curves still round the grade breaks), within the grade limit there."""
    pv = [list(p) for p in pv]
    trend = np.sign(pv[-1][1] - pv[0][1]) or 1.0
    for k in range(1, len(pv)):
        ds = pv[k][0] - pv[k - 1][0]
        if ds <= 0:
            continue
        gr = (pv[k][1] - pv[k - 1][1]) / ds
        gm = MAX_DRIVE_GRADE if gmax_at is None else float(np.interp(pv[k][0], st, gmax_at))
        if abs(gr) < MIN_GRADE:
            gr = MIN_GRADE * (np.sign(gr) or trend)
        gr = max(-gm, min(gm, gr))
        pv[k][1] = pv[k - 1][1] + gr * ds
    return [tuple(p) for p in pv]


def _cap_parking(g, z, pave_mask, fixed, L, cap=PARK_MAX - 0.002, iters=60):
    """Where a stall would be steeper than 5% (two drive legs at different elevations close together), relax the pavement
    there toward its neighbours until it is within 5% or cannot get flatter."""
    if not L.get("stalls"):
        return z
    st = g.mask(unary_union(L["stalls"]).buffer(5.0)) & pave_mask
    out = z.copy()
    m = pave_mask & np.isfinite(out)
    k = np.array([[0, 1, 0], [1, 0, 1], [0, 1, 0]], float)
    nb = np.where(ndi.convolve(m.astype(float), k, mode="constant") > 0, ndi.convolve(m.astype(float), k, mode="constant"), 1.0)
    for _ in range(iters):
        zz = np.where(m, out, 0.0)
        gy, gx = np.gradient(np.where(m, out, np.nan), g.cell)
        sl = np.hypot(np.nan_to_num(gx), np.nan_to_num(gy))
        bad = st & ~fixed & (sl > cap)
        if not bad.any():
            break
        grow = ndi.binary_dilation(bad, iterations=2) & m & ~fixed
        avg = ndi.convolve(zz * m, k, mode="constant") / nb
        out = np.where(grow, avg, out)
    return out


def _harmonic(z, mask, fixed, iters=600):
    """Relax z inside mask (4-neighbour average, neighbours outside the mask ignored) holding `fixed` cells."""
    z = np.where(mask, z, 0.0).astype(float)
    m = mask.astype(float)
    free = mask & ~fixed
    k = np.array([[0, 1, 0], [1, 0, 1], [0, 1, 0]], float)
    nb = ndi.convolve(m, k, mode="constant")
    nb = np.where(nb > 0, nb, 1.0)
    for _ in range(iters):
        avg = ndi.convolve(z * m, k, mode="constant") / nb
        z = np.where(free, avg, z)
    return np.where(mask, z, np.nan)


def _ada_planes(g, z, pave_mask, L, blend=15.0):
    """Each group of ADA spaces with its access aisles becomes one plane at 1-1.5% (never over the 2.08% ADA limit in any
    direction), keeping its drainage direction; the pavement eases back to the design grade over `blend` ft."""
    info = []
    ada = [a for a in (L.get("ada") or []) if a is not None and not a.is_empty]
    if not ada:
        return z, info
    zone = unary_union([a.buffer(6.0, join_style=2) for a in ada]).intersection(L["pave"].buffer(1.0))
    out = z.copy()
    for q in polys(zone):
        m = g.mask(q) & pave_mask & np.isfinite(z)
        if m.sum() < 3:
            m = g.mask(q.buffer(3.0)) & pave_mask & np.isfinite(z)
        if m.sum() < 3:
            continue
        X, Y, Z = g.X[m], g.Y[m], z[m]
        cx, cy = X.mean(), Y.mean()
        A = np.c_[np.ones_like(X), X - cx, Y - cy]
        c0, gx, gy = np.linalg.lstsq(A, Z, rcond=None)[0]
        s0 = math.hypot(gx, gy)
        s1 = min(max(s0, ADA_MIN), ADA_MAX)
        if s0 < 1e-6:
            gx, gy = ADA_MIN, 0.0
        else:
            gx, gy = gx * s1 / s0, gy * s1 / s0
        plane = c0 + gx * (g.X - cx) + gy * (g.Y - cy)
        inside = g.mask(q.buffer(2.0)) & pave_mask
        d = ndi.distance_transform_edt(~inside) * g.cell
        w = np.clip(d / blend, 0.0, 1.0)
        w = w * w * (3 - 2 * w)
        ring = pave_mask & (d > 0) & (d < blend)
        out[inside] = plane[inside]
        out[ring] = plane[ring] + (z[ring] - plane[ring]) * w[ring]
        info.append({"poly": q, "slope_pct": round(s1 * 100, 2), "was_pct": round(s0 * 100, 2), "c0": float(c0), "gx": float(gx), "gy": float(gy), "cx": float(cx), "cy": float(cy)})
    return out, info


def checks(g, z, G, L, inlets=None):
    """Grading rules on the finished surface: pavement >= 1%, parking <= 5%, ADA spaces <= 2.08%. Returns (rows, warnings).
    The 1% rule leaves out the crest / sag points of the drive's vertical curves and the sag at each catch basin, where the
    grade necessarily passes through zero."""
    gy, gx = np.gradient(z, g.cell)
    sl = np.hypot(gx, gy) * 100
    pave = G["pave_mask"]
    core = ndi.binary_erosion(pave, iterations=1) & ~G.get("curb_mask", np.zeros_like(pave))
    stall_m = g.mask(unary_union(L.get("stalls") or [])) & core if L.get("stalls") else np.zeros_like(pave)
    ada_m = np.zeros_like(pave)
    for a in G.get("ada") or []:
        ada_m |= g.mask(a["poly"]) & core
    rows, warns = [], []
    ex = []
    for c in G.get("curves") or []:
        if c["A"] and c["L"] > 0:
            s0 = c["bvc"] + (-c["g1"] / (c["g2"] - c["g1"])) * c["L"] if (c["g2"] - c["g1"]) else None
            if s0 is not None and c["bvc"] <= s0 <= c["evc"]:
                ex.append(G["spine"].interpolate(s0).buffer(max(20.0, c["K"] * 1.2)))
    for xy in inlets or []:
        ex.append(Point(xy).buffer(15.0))
    exm = g.mask(unary_union(ex)) if ex else np.zeros_like(pave)
    if core.any():
        lo = float(np.percentile(sl[core & ~exm], 2)) if (core & ~exm).any() else float(np.percentile(sl[core], 2))
        rows.append(("Pavement min slope", f"{lo:.1f}%"))
        flat = float((sl[core & ~exm] < MIN_GRADE * 100 - 0.05).sum() * g.cell ** 2)
        if flat > 400:
            warns.append(f"About {flat:,.0f} SF of pavement is flatter than 1%; check the spot grades there")
    if stall_m.any():
        hi = float(np.percentile(sl[stall_m], 98))
        rows.append(("Parking max slope", f"{hi:.1f}%"))
        steep = float((sl[stall_m] > PARK_MAX * 100 + 0.1).sum() * g.cell ** 2)
        if steep > 200:
            warns.append(f"About {steep:,.0f} SF of parking is steeper than 5% (max {hi:.1f}%); the site is too steep there for a single plane at this layout")
    if G.get("ada"):
        mx = max(a["slope_pct"] for a in G["ada"])
        rows.append(("ADA spaces + aisles", f"{mx:.1f}% (limit 2.08%)"))
    return rows, warns


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
        # linear upsample + light smoothing: no overshoot at the 6" curb step (a cubic rings there and draws wavy contours)
        z = ndi.gaussian_filter(ndi.zoom(np.where(np.isfinite(z), z, np.nanmean(z)), fine, order=1), fine * 0.45)
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
