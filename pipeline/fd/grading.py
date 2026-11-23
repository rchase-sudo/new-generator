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


def spine(g, pave_mask, hard, entry_pt, target=None):
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
    if target is not None:
        tj, ti = g.ij(*target)
        if tj is not None and 0 <= tj < fin.shape[0] and 0 <= ti < fin.shape[1] and fin[tj, ti] >= 0:
            far = (tj, ti)
    path = [tuple(far)]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    path = path[::-1]
    if len(path) < 2:
        return None
    ln = LineString([(g.xs[i], g.ys[j]) for j, i in path]).simplify(CELL * 0.8)
    return ln


def clean_alignment(sp, tol=8.0, min_seg=20.0, min_defl=5.0):
    """The drive centreline as straight tangents for the CAD alignment: the grid path the spine is traced on steps and
    zigzags; this keeps only real bends (Douglas-Peucker at `tol` ft, then drops bends under `min_defl` degrees and
    tangents shorter than `min_seg`). Returns (line, remap) where remap(s) moves a station on the spine onto it."""
    ln = sp.simplify(tol)
    cs = list(ln.coords)
    changed = True
    while changed and len(cs) > 2:
        changed = False
        for k in range(1, len(cs) - 1):
            a, b, c = cs[k - 1], cs[k], cs[k + 1]
            h1 = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])); h2 = math.degrees(math.atan2(c[1] - b[1], c[0] - b[0]))
            d = abs((h2 - h1 + 180) % 360 - 180)
            if d < min_defl or math.dist(a, b) < min_seg or math.dist(b, c) < min_seg:
                del cs[k]
                changed = True
                break
    new = LineString(cs)
    s_max = new.length

    def remap(s):
        return min(max(new.project(sp.interpolate(s)), 0.0), s_max)
    return new, remap


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
    jj, ii = np.nonzero(pave_mask)
    # branches: pavement far from the drive's centreline (a parking field off to one side) gets its own graded centreline
    # from the nearest point of the spine, so its cross slope never runs out into a flat
    fp, zf, branches = _branches(g, pave_mask, hard, sp, fp, zf, ex_z, near_park)
    tree = cKDTree(fp)
    dd, k = tree.query(np.c_[g.xs[ii], g.ys[jj]])
    z_new = np.full(g.X.shape, np.nan)
    # centreline elevation carried smoothly across the pavement (harmonic between the spine cells, so where the spine wraps
    # round a parking field the two sides blend instead of meeting in a cliff), then the 2% cross slope off the spine
    zc = np.full(g.X.shape, np.nan)
    zc[jj, ii] = zf[k]
    fixed = np.zeros(g.X.shape, bool)
    fixed[jj, ii] = dd <= g.cell * 0.75
    zc = _harmonic(zc, pave_mask, fixed)
    z_new[jj, ii] = zc[jj, ii] - CROSS * np.minimum(dd, 120.0)
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

    # grading areas (Sitecraft): the rest of each area (not pavement, walk or building) is one plane at a slope between its
    # min and max, through the area's mean existing grade so cut and fill balance; the slopes beyond tie out like the rest
    for a in L.get("grade_areas") or []:
        m = g.mask(a["poly"]) & ~np.isfinite(z_new) & np.isfinite(ex_z)
        pl = grade_plane(g, ex_z, a["poly"], a["min"], a["max"])
        a["plane"] = pl
        if pl is not None and m.any():
            z_new[m] = np.round(plane_z(pl, g.X[m], g.Y[m]), 3)
    # pond (elevations are set by the hydrology step; a placeholder floor lets the daylighting see the basin)
    design = np.isfinite(z_new)
    return {"spine": sp, "stations": st, "profile": zprof, "ex_profile": zx, "pvi": pv, "curves": cv, "z_design": z_new, "design_mask": design,
            "ffe": ffe, "pave_mask": pave_mask, "bmask": allb, "walk_mask": walk_mask, "warnings": warns,
            "z_pave": z_pave, "curb_mask": curb_mask, "ada": ada_info, "branches": branches}


def grade_plane(g, ex_z, poly, s_min, s_max):
    """Best-fit plane of the existing ground over `poly`, its slope held between s_min and s_max (ft/ft) in the same
    downhill direction (dead level when both are 0), passing through the area's mean existing grade."""
    m = g.mask(poly) & np.isfinite(ex_z)
    if m.sum() < 3:
        c = poly.centroid
        zc = float(np.nanmean(g.at(ex_z, [c.x], [c.y])))
        return None if not np.isfinite(zc) else (c.x, c.y, zc, 0.0, 0.0)
    x, y, z = g.X[m], g.Y[m], ex_z[m]
    xm, ym, zm = float(x.mean()), float(y.mean()), float(z.mean())
    A = np.c_[x - xm, y - ym]
    (a, b), *_ = np.linalg.lstsq(A, z - zm, rcond=None)
    s = math.hypot(a, b)
    lo, hi = sorted((max(0.0, s_min), max(0.0, s_max)))
    t = min(max(s, lo), hi)
    if s > 1e-9:
        a, b = a * t / s, b * t / s
    else:
        a, b = t, 0.0
    return (xm, ym, zm, float(a), float(b))


def plane_z(pl, X, Y):
    xm, ym, zm, a, b = pl
    return zm + a * (X - xm) + b * (Y - ym)


def _branches(g, pave_mask, hard, sp, fp, zf, ex_z, near_park, reach=110.0, max_n=6):
    """Extra graded centrelines for pavement more than `reach` ft from every centreline so far: each starts on the nearest
    centreline (at its elevation) and runs through the far pavement, profiled like the drive (1% min, 4.5% in parking)."""
    from scipy.spatial import cKDTree
    jj, ii = np.nonzero(pave_mask)
    cells = np.c_[g.xs[ii], g.ys[jj]]
    lines_ = []
    for _ in range(max_n):
        d, kk = cKDTree(fp).query(cells)
        far = d > reach
        if far.sum() * g.cell ** 2 < 1500:
            break
        # the far pavement cell nearest the existing centrelines, and the centreline point it hangs off
        c1 = int(np.argmax(d))                                   # the pavement farthest from every centreline so far
        j0 = int(kk[c1])                                         # ... and the centreline point nearest it
        start = (float(fp[j0][0]), float(fp[j0][1]))
        br = spine(g, pave_mask, hard, start, target=(float(cells[c1][0]), float(cells[c1][1])))
        if br is None or br.length < reach * 0.5:
            break
        L_ = br.length
        st = np.arange(0, L_ + 10.0, 10.0)
        st = st[st <= L_] if len(st) and st[-1] > L_ else st
        if st[-1] < L_:
            st = np.append(st, L_)
        pts = [br.interpolate(s_) for s_ in st]
        zx = g.at(ex_z, [p_.x for p_ in pts], [p_.y for p_ in pts])
        zx = np.asarray(zx, float)
        zx = zx - zx[0] + float(zf[j0])                          # tied to the centreline it leaves
        gm = [MAX_PARK_GRADE if near_park is not None and near_park.contains(p_) else MAX_DRIVE_GRADE for p_ in pts]
        zd = design_profile(st, zx, gm) if len(st) > 3 else zx
        zd[0] = float(zf[j0])
        pv = _min_tangents(pvis(st, zd, tol=0.15), st, gm)
        fine = np.arange(0, L_, 2.5)
        bp = np.array([[br.interpolate(s_).x, br.interpolate(s_).y] for s_ in fine])
        bz = np.interp(fine, [q[0] for q in pv], [q[1] for q in pv])
        fp = np.vstack([fp, bp])
        zf = np.concatenate([zf, bz])
        lines_.append(br)
    return fp, zf, lines_


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


def _cap_parking(g, z, pave_mask, fixed, L, cap=PARK_MAX - 0.004, iters=250):
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


# Slope rules (all jobs): proposed slopes 3:1 or flatter. Only where 3:1 cannot daylight before a limit (property line,
# wetland): 2:1 (slope shown, erosion control matting), then 1:1 (riprap), and steeper than that a retaining wall.
SLOPE_MAT, SLOPE_RIP = 1.0 / 2.0, 1.0


def surface(g, ex_z, z_design, design_mask, pond=None, protect=None, limit=None, info=None, soften_ft=None):
    """Full proposed surface: design cells as set; everything else follows the existing ground clamped to the steepest
    allowed slope from the nearest design cell (cut and fill slopes daylight into the existing grade). With `limit` (cells
    that may not be regraded) each slope is the flattest standard one that still reaches existing ground before the limit:
    3:1, else 2:1, else 1:1, else a retaining wall at the edge of the design. `info` (a dict) receives the 2:1 and 1:1 areas
    and the walls."""
    z = ex_z.copy()
    dist, idx = ndi.distance_transform_edt(~design_mask, return_indices=True)
    zd = z_design[idx[0], idx[1]]
    dft = dist * g.cell
    s = np.full(z.shape, DAYLIGHT)
    wall_src = np.zeros(z.shape, bool)
    if limit is not None and limit.any():
        dl = ndi.distance_transform_edt(~limit) * g.cell
        band = (~design_mask) & (~limit) & (dft < 150.0)
        H = np.abs(zd - ex_z)
        s_req = np.where(band & (H > 0.25), H / np.maximum(dft + dl, 0.5), 0.0)       # a few inches of mismatch is not a slope
        src = np.ravel_multi_index((idx[0], idx[1]), z.shape)
        s_src = np.zeros(z.size)
        np.maximum.at(s_src, src[band], s_req[band])
        s_src = s_src.reshape(z.shape)
        # standard slopes only: 3:1, 2:1, 1:1, wall
        q = np.where(s_src <= DAYLIGHT + 1e-6, DAYLIGHT, np.where(s_src <= SLOPE_MAT + 1e-6, SLOPE_MAT, np.where(s_src <= SLOPE_RIP + 1e-6, SLOPE_RIP, np.inf)))
        wall_src = design_mask & np.isinf(q)
        s = q[idx[0], idx[1]]
    sf = np.where(np.isinf(s), 0.0, s)
    lo = np.where(np.isinf(s), -np.inf, zd - sf * dft)
    hi = np.where(np.isinf(s), np.inf, zd + sf * dft)
    out = np.clip(ex_z, lo, hi)
    out = np.where(design_mask, z_design, out)
    # soften the clamped band so contours don't kink (design cells, walls and far field stay)
    soft = ndi.gaussian_filter(out, (soften_ft / g.cell) if soften_ft else 1.0)
    band = (~design_mask) & (dft < 80) & np.isfinite(s) & (s <= DAYLIGHT + 1e-6)
    out = np.where(band, soft, out)
    out = np.where(design_mask, z_design, out)
    if limit is not None:
        out = np.where(limit & ~design_mask, ex_z, out)
    out = taper_protect(g, out, ex_z, protect, design_mask)
    if info is not None:
        info["s"] = s
        classify_slopes(g, out, ex_z, design_mask, s, wall_src, info)
    return out


def classify_slopes(g, out, ex_z, design_mask, s, wall_src, info):
    if True:
        # what was actually built, not only what was planned: any graded ground steeper than 3:1 gets its treatment
        # (to 2:1 matting, to 1:1 riprap) and anything steeper than 1:1 is a retaining wall at the edge of the design beside it
        graded = (~design_mask) & (np.abs(out - ex_z) > 0.1)
        sl = np.nan_to_num(np.hypot(*np.gradient(out, g.cell)), nan=0.0)
        tol = 0.01
        steep = graded & (sl > SLOPE_RIP + tol)
        rip = graded & (sl > SLOPE_MAT + tol) & ~steep
        mat = graded & (sl > DAYLIGHT + tol) & ~rip & ~steep
        # the planned 2:1 / 1:1 bands keep their treatment where they ease off at the toe
        mat |= graded & np.isclose(s, SLOPE_MAT) & (sl > DAYLIGHT * 0.8) & ~rip & ~steep
        rip |= graded & np.isclose(s, SLOPE_RIP) & (sl > SLOPE_MAT * 0.8) & ~steep
        mat &= ~rip
        info["matting"], info["riprap"] = mat, rip
        if steep.any():
            wall_src = wall_src | (design_mask & ndi.binary_dilation(steep, iterations=2))
        info["wall_src"] = wall_src
        info["steep_sf"] = float(steep.sum() * g.cell * g.cell)


def slope_features(g, z_prop, ex_z, z_design, design_mask, info, min_sf=25.0):
    """Polygons for the 2:1 (matting) and 1:1 (riprap) slopes and the retaining-wall lines with top / bottom elevations."""
    from shapely.geometry import MultiLineString
    from shapely.ops import linemerge
    res = {"matting": [], "riprap": [], "walls": []}
    for key in ("matting", "riprap"):
        m = info.get(key)
        if m is None or not m.any():
            continue
        r_ = max(1.5, g.cell * 1.2)           # cell steps rounded off: the areas read as drawn, not as pixels
        for q in polys(g.cell_poly(m).buffer(r_, join_style=1).buffer(-r_, join_style=1).simplify(max(0.75, g.cell * 0.6))):
            if q.area >= min_sf:
                res[key].append(q)
    ws = info.get("wall_src")
    if ws is not None and ws.any():
        dpoly = g.cell_poly(design_mask)
        dpoly = dpoly.buffer(g.cell, join_style=1).buffer(-g.cell, join_style=1).simplify(g.cell * 0.75)
        zone = g.cell_poly(ws).buffer(g.cell * 0.75)
        edge = unary_union([LineString(r.coords) for q in polys(dpoly) for r in [q.exterior] + list(q.interiors)])
        ln = edge.intersection(zone)
        parts = [p for p in (ln.geoms if hasattr(ln, "geoms") else [ln]) if p.geom_type == "LineString" and p.length > 4.0]
        if parts:
            mm = linemerge(MultiLineString(parts)) if len(parts) > 1 else parts[0]
            for w in (mm.geoms if hasattr(mm, "geoms") else [mm]):
                if w.length < 8.0:
                    continue
                w = w.simplify(max(0.5, g.cell * 0.75))
                n = max(2, int(w.length // 5.0) + 1)
                top, bot, hs = [], [], []
                for k in range(n):
                    pt = w.interpolate(k * w.length / (n - 1))
                    # elevations just inside the design and just outside it (the face is between)
                    a = w.interpolate(max(0.0, k * w.length / (n - 1) - 0.5)); b = w.interpolate(min(w.length, k * w.length / (n - 1) + 0.5))
                    tx, ty = b.x - a.x, b.y - a.y
                    L_ = math.hypot(tx, ty) or 1.0
                    nx_, ny_ = -ty / L_, tx / L_
                    p1, p2 = (pt.x + nx_ * 3.0, pt.y + ny_ * 3.0), (pt.x - nx_ * 3.0, pt.y - ny_ * 3.0)
                    inside = p1 if dpoly.contains(Point(p1)) else p2
                    outside = p2 if inside is p1 else p1
                    zin = float(g.at(z_prop, [inside[0]], [inside[1]])[0]); zout = float(g.at(ex_z, [outside[0]], [outside[1]])[0])
                    o_xy = (pt.x + (outside[0] - pt.x) / 30.0, pt.y + (outside[1] - pt.y) / 30.0)     # 0.1 ft out
                    if zin >= zout:          # fill wall: top at the design, bottom on the ground beyond
                        top.append(((pt.x, pt.y), zin)); bot.append((o_xy, zout))
                    else:                    # cut wall: top on the ground beyond, bottom at the design
                        top.append((o_xy, zout)); bot.append(((pt.x, pt.y), zin))
                    hs.append(abs(zin - zout))
                if max(hs) >= 1.0:
                    res["walls"].append({"line": w, "top": top, "bot": bot, "h_max": max(hs), "len": w.length})
    return res


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


def refine(g, ex_z, z_design, design_mask, bldgs=(), ffe=(), protect=None, limit=None, lod=None, pave=None, walk=None, ponds=(), grades=None, max_pts=300_000):
    """The construction surface: the design re-graded on a fine grid (1.5-3 ft) so its edges follow the real outlines
    rather than 5 ft cells, the existing ground resampled smoothly, the cut / fill slopes rebuilt with the same 3:1 / 2:1 /
    1:1 / wall rules and blended so no cell steps or ridges are left. The coarse grid stays for the analysis; exports
    (TIN, contours, Civil 3D surfaces, slope areas) use this one."""
    from .base import Grid
    W_, H_ = g.nx * g.cell, g.ny * g.cell
    cell = max(1.5, math.ceil(math.sqrt(W_ * H_ / max_pts) * 2) / 2)
    cell = min(cell, g.cell / 2)
    gf = Grid((g.x0, g.y0, g.x0 + W_, g.y0 + H_), cell)
    fi = (gf.X - g.x0) / g.cell - 0.5
    fj = (gf.Y - g.y0) / g.cell - 0.5

    def fill(a, ok):
        if ok.all():
            return a
        _, (ii, jj) = ndi.distance_transform_edt(~ok, return_indices=True)
        return a[ii, jj]

    def samp(a, order):
        return ndi.map_coordinates(a, [fj, fi], order=order, mode="nearest")

    def near(m):
        return samp(m.astype(float), 0) > 0.5
    ok = np.isfinite(ex_z)
    exf = samp(fill(ex_z, ok), 3)
    exf = ndi.gaussian_filter(exf, 2.5 / cell)                   # no spline ringing / interpolation ripple between the 5 ft samples
    dm = design_mask & np.isfinite(z_design)
    zd_f = samp(fill(np.where(dm, z_design, 0.0), dm), 1)
    # the design's extent from the real outlines (buildings, pavement + curb, walks); only what has no outline here (the
    # pond banks) comes from the 5 ft cells
    cb = np.zeros(dm.shape, bool)
    for b in bldgs or []:
        cb |= g.mask(b)
    vec = []
    if pave is not None and not pave.is_empty:
        vec.append(pave.buffer(2.0 + 0.5))
        cb |= g.mask(pave.buffer(2.0 + g.cell))
    if walk is not None and not walk.is_empty:
        vec.append(walk)
        cb |= g.mask(walk)
    for q in ponds or []:
        cb |= g.mask(q["poly"].buffer(6.0 + g.cell))
    dmf = samp((dm & ~cb).astype(float), 1) >= 0.5
    for v in vec:
        dmf |= gf.mask(v)
    # pavement / pond planes: relax the 5 ft facets inside the design (not across buildings) so they read as smooth planes
    bm = np.zeros(gf.X.shape, bool)
    for b in bldgs or []:
        bm |= gf.mask(b)
    inner = dmf & ~ndi.binary_dilation(bm, iterations=1)
    w = ndi.gaussian_filter(inner.astype(float), 1.2)
    sm = ndi.gaussian_filter(np.where(inner, zd_f, 0.0), 1.2)
    zd_f = np.where(inner & (w > 0.2), sm / np.maximum(w, 1e-6), zd_f)
    for a in grades or []:                                       # grading areas: their exact planes on the real outline
        if a.get("plane") is None:
            continue
        m = gf.mask(a["poly"]) & ~bm
        own = m
        if pave is not None and not pave.is_empty:
            own = own & ~gf.mask(pave.buffer(2.5))
        if walk is not None and not walk.is_empty:
            own = own & ~gf.mask(walk)
        zd_f[own] = plane_z(a["plane"], gf.X[own], gf.Y[own])
        dmf |= own
    for q in ponds or []:                                        # basins rewritten from their exact outlines on the fine grid
        zd_f, pm, rim, _ = pond_surface(gf, zd_f, q["poly"], q["top_el"], q["bottom_el"], q.get("side", 3.0))
        zd_f[rim & ~dmf] = q["top_el"]
        dmf |= pm | rim
    for k, b in enumerate(bldgs or []):                          # floor pads exactly at the FFE, edges on the real outline
        m = gf.mask(b)
        if k < len(ffe) and m.any():
            zd_f[m] = float(ffe[k])
            dmf |= m
    info = {}
    zf = surface(gf, exf, zd_f, dmf, protect=near(protect) if protect is not None else None,
                 limit=near(limit) if limit is not None else None, info=info, soften_ft=4.0)
    # one more pass across the grading band only (never inside the design or past the disturbed area)
    band = (~dmf) & (np.abs(zf - exf) > 0.05)
    band = ndi.binary_dilation(band, iterations=2) & ~dmf
    for _ in range(2):
        sm = ndi.gaussian_filter(zf, 2.0 / cell * 1.5)
        zf = np.where(band, sm, zf)
    if limit is not None:
        lf = near(limit)
        zf = np.where(lf & ~dmf, exf, zf)
    classify_slopes(gf, zf, exf, dmf, info["s"], info["wall_src"], info)       # treatments from the final surface
    return {"grid": gf, "z_prop": zf, "ex_z": exf, "design_mask": dmf, "z_design": zd_f, "info": info,
            "lod": near(lod) if lod is not None else None}


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
