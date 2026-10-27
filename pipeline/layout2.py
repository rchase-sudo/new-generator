"""Concept layout v2: a development pad shaped by the buildable area.

Local frame in feet (x along the main frontage, y into the site). The development region D is cut from the
parking-allowed zone (parcel minus parking setbacks minus wetlands/flood), so its edges follow the real setback
lines; its convex corners are filleted. A 24 ft two-way ring drive runs inside D's edge; the island inside the
ring holds the buildings, interior parking bays and landscaping. Perimeter stalls are laid along the island
edge in whatever direction that edge runs, with landscape islands at row ends and every ~10 stalls. The search
keeps the smallest pavement that holds the full program and meets the parking requirement, then turns any
surplus stalls into landscaping so the plan shows just what the ordinance requires.
"""
import math

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polylabel, substring, unary_union

from .ec_drawing import _polys, classify_edges, envelope, road_key
from .existing import KEEP_CLEAR_FT
from .layout import DEFAULT_PARK_SB, Frame, ada_required

SW, SD, AIS, RING = 9.0, 18.0, 24.0, 24.0
WALK = 6.0
R_OUT = 32.0       # outer pavement corner radius
R_JOIN = 18.0      # fillet where drives meet
GAP = 40.0         # between building envelopes (walks + grass)
DT = 12.0          # drive-thru lane
SERVICE = 10.0     # grass behind a building
ISLAND_EVERY = 10  # landscape island after this many stalls in a run
END_GAP = 5.0      # keep stalls off the ends of each edge (corner islands)
MIN_RUN = 3        # shortest row of stalls worth drawing


def _short(b):
    """Short plan label for a building: the program's label, else its use without parentheticals."""
    import re
    t = b.get("label") or b.get("use") or b.get("name") or "BUILDING"
    t = re.sub(r"\(.*?\)", "", str(t)).split(" with ")[0].split(",")[0].strip()
    return (t[:24].rsplit(" ", 1)[0] if len(t) > 24 else t).upper()


def _polys_lines(g):
    """LineStrings out of whatever a union of shared edges returns."""
    from shapely.ops import linemerge
    if g.is_empty:
        return []
    if g.geom_type in ("LineString", "LinearRing"):
        return [g]
    lines = [x for x in getattr(g, "geoms", []) if x.geom_type == "LineString"]
    m = linemerge(lines) if lines else None
    return list(getattr(m, "geoms", [m])) if m is not None and not m.is_empty else []


def _largest(g):
    ps = _polys(g) if g is not None and not g.is_empty else []
    return max(ps, key=lambda p: p.area) if ps else None


def _fillet(g, r):
    out = g.buffer(-r, join_style=1).buffer(r, join_style=1)
    return out if not out.is_empty else g


def _close(g, r):
    return g.buffer(r, join_style=1).buffer(-r, join_style=1)


# ---------------------------------------------------------------- buildings
def _bldg_dims(b, scale):
    fp = b["footprint_sf"] * scale
    d = float(b.get("depth_ft") or 60)
    d = min(max(d, 25.0), max(30.0, math.sqrt(fp) * 1.8))
    w = max(fp / d, 20.0)
    if w < d:  # keep the long side along the row
        w, d = d, w
    return w, d


def _row(buildings, scale, xc, y_front, facing):
    """facing = -1: building fronts face -y (toward the road); +1: fronts face +y.
    Returns list of dicts with rect, lane (drive-thru U + legs), walk, env (everything that must sit in the island)."""
    dims = [_bldg_dims(b, scale) for b in buildings]
    env_w = [w + (2 * DT if b.get("drive_thru") else 0) for (w, d), b in zip(dims, buildings)]
    total = sum(env_w) + GAP * (len(buildings) - 1)
    x = xc - total / 2
    out = []
    for (w, d), ew, b in zip(dims, env_w, buildings):
        bx0 = x + (DT if b.get("drive_thru") else 0)
        bx1 = bx0 + w
        if facing < 0:
            by0, by1 = y_front, y_front + d
            fy, back = by0, by1
        else:
            by0, by1 = y_front - d, y_front
            fy, back = by1, by0
        rect = box(bx0, by0, bx1, by1)
        lane = legs = None
        if b.get("drive_thru"):
            ext = 150.0   # trimmed to the island edge in _build so the lane always reaches the drive aisle
            if facing < 0:
                U = box(bx0 - DT, by0, bx1 + DT, by1 + DT)
                legs = unary_union([box(bx0 - DT, by0 - ext, bx0, by0), box(bx1, by0 - ext, bx1 + DT, by0)])
            else:
                U = box(bx0 - DT, by0 - DT, bx1 + DT, by1)
                legs = unary_union([box(bx0 - DT, by1, bx0, by1 + ext), box(bx1, by1, bx1 + DT, by1 + ext)])
            lane = U.difference(rect)
            walk = box(bx0, fy - WALK, bx1, fy) if facing < 0 else box(bx0, fy, bx1, fy + WALK)
            service = None
        else:
            walk = rect.buffer(WALK, join_style=1).difference(rect)
            service = box(bx0 - WALK, back, bx1 + WALK, back + SERVICE) if facing < 0 else box(bx0 - WALK, back - SERVICE, bx1 + WALK, back)
        env = unary_union([g for g in (rect, walk, lane) if g is not None])
        out.append({"b": b, "rect": rect, "lane": lane, "legs": legs, "walk": walk, "service": service, "env": env,
                    "w": w, "d": d, "fp": w * d, "facing": facing})
        x += ew + GAP
    return out, total


def _band_plan(m, mode, back_row, dA, dB=0.0, med=5.0):
    """Offsets (from the island front) of stall rows, aisles and building fronts, plus island depth.
    Rows that sit head-to-head form a pair separated by a median `med` (5 ft landscape strip, or 0 = painted line).
    Returns rows [(y, pair_id or None)], aisles [y], fronts [(y, facing)], depth.
    mode back : parking toward the road, one building row behind it facing the road
    mode front: one building row near the road, facing back into the site, parking behind
    mode split: two building rows back to back, the first facing the road, the second facing the rear"""
    rows, aisles, fronts = [], [], []
    pid = 0
    if mode in ("back", "split"):
        y = 0.0
        prev = (y, None)                         # front perimeter row (faces the ring)
        y += SD
        for _ in range(m):
            rows.append((prev[0], pid)); y += med
            rows.append((y, pid)); y += SD; pid += 1
            aisles.append(y); y += AIS
            prev = (y, None); y += SD
        rows.append(prev)                        # row facing the building walk (or the lone perimeter row)
        y += WALK
        fronts.append((y, -1))
        if mode == "back":
            y += dA + SERVICE
            if back_row:
                rows.append((y, None)); y += SD
            return rows, aisles, fronts, y
        y += dA + 2 * SERVICE + dB              # backs apart: grass strip / service between the two rows
        fronts.append((y, 1))
        y += WALK
        rows.append((y, None)); y += SD          # rear perimeter row
        return rows, aisles, fronts, y
    y = SERVICE + dA
    fronts.append((y, 1))
    y += WALK
    cur = (y, None); y += SD                     # row facing the building walk
    for _ in range(m):
        if cur is not None:
            rows.append(cur)
        aisles.append(y); y += AIS
        a_ = y; y += SD + med
        rows.append((a_, pid)); rows.append((y, pid)); pid += 1
        cur = None
        y += SD
    if m == 0:
        rows.append(cur)
    return rows, aisles, fronts, y


def _gdims(group, scale):
    dims = [_bldg_dims(b, scale) for b in group]
    w = sum(w + (2 * DT if b.get("drive_thru") else 0) for (w, d), b in zip(dims, group)) + GAP * (len(group) - 1)
    d = max(d + (DT if b.get("drive_thru") else 0) for (w, d), b in zip(dims, group))
    return w, d


# ---------------------------------------------------------------- stalls
class Stalls:
    """Stall polygons with run / unit bookkeeping. A unit is what gets removed together (a column of a matched
    pair = 2 stalls, or a single stall); a run is an ordered list of units between islands."""
    def __init__(self):
        self.polys, self.unit_of, self.units, self.runs, self.clines = [], [], {}, {}, []

    def add_run(self, columns):
        """columns: list of lists of polygons (one list per unit, in order along the run)."""
        rid = len(self.runs)
        self.runs[rid] = []
        for col in columns:
            uid = len(self.units)
            self.units[uid] = []
            for p in col:
                self.units[uid].append(len(self.polys))
                self.polys.append(p)
                self.unit_of.append(uid)
            self.runs[rid].append(uid)
        return rid

    @property
    def n(self):
        return sum(len(self.units[u]) for r in self.runs.values() for u in r)

    def live(self):
        return [self.polys[i] for r in self.runs.values() for u in r for i in self.units[u]]

    def transform(self, fn):
        self.polys = [fn(p) for p in self.polys]
        self.clines = [(fn(l), rid) for l, rid in self.clines]


def _runs(valid):
    """Contiguous index runs of a boolean array."""
    out, cur = [], []
    for i, v in enumerate(valid):
        if v:
            cur.append(i)
        elif cur:
            out.append(cur); cur = []
    if cur:
        out.append(cur)
    return out


def _chunk(run):
    """End islands + an island every ISLAND_EVERY stalls: drop the first/last slot of longer runs and split."""
    if len(run) >= 5:
        run = run[1:-1]
    if len(run) < MIN_RUN:
        return []
    k = ISLAND_EVERY
    n_ch = math.ceil((len(run) + 1) / (k + 1))
    size = (len(run) - (n_ch - 1)) / n_ch      # even chunks with one island slot between
    out, i = [], 0
    for c in range(n_ch):
        L = int(round(size * (c + 1))) - int(round(size * c))
        out.append(run[i:i + L])
        i += L + 1
    return [ch for ch in out if len(ch) >= MIN_RUN]


def _grid_rows(S, I, rows, iy0, xg0, xg1, blk, med):
    """Grid rows on one shared x grid. Paired rows use the same columns (matched start/end, islands aligned)."""
    xs = np.arange(xg0, xg1 - SW + 0.01, SW)
    if not len(xs):
        return
    def ok_row(y):
        bx = shapely.box(xs, y, xs + SW, y + SD)
        ok = shapely.contains(I, bx)
        if blk is not None:
            ok &= ~shapely.intersects(blk, shapely.buffer(bx, -0.3))
        return bx, ok
    pairs = {}
    singles = []
    for y, pid in rows:
        if pid is None:
            singles.append(y)
        else:
            pairs.setdefault(pid, []).append(y)
    for pid, ys in pairs.items():
        ya, yb = sorted(ys)
        bxa, oka = ok_row(iy0 + ya)
        bxb, okb = ok_row(iy0 + yb)
        for run in _runs(oka & okb):
            for ch in _chunk(run):
                rid = S.add_run([[bxa[i], bxb[i]] for i in ch])
                if med <= 0.01:   # head-to-head without a median: painted divider through the matched stalls
                    yl = iy0 + ya + SD
                    S.clines.append((LineString([(xs[ch[0]], yl), (xs[ch[-1]] + SW, yl)]), rid))
    for y in singles:
        bx, ok = ok_row(iy0 + y)
        for run in _runs(ok):
            for ch in _chunk(run):
                S.add_run([[bx[i]] for i in ch])


def _edge_stalls(S, I, blockers, taken):
    """Stalls square to the island edge on edges that are NOT parallel to the bays (sides, skewed edges)."""
    taken_u = unary_union(taken).buffer(1.0) if taken else None
    placed = []
    for poly in _polys(I):
        ring = poly.exterior.simplify(1.0)
        ccw = ring.is_ccw
        cs = list(ring.coords)
        for a, b in zip(cs, cs[1:]):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            k = int((L - 2 * END_GAP) // SW)
            if k < MIN_RUN:
                continue
            tx, ty = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            if abs(ty) < 0.34:      # parallel to the bays: the grid rows handle it
                continue
            nx, ny = (-ty, tx) if ccw else (ty, -tx)   # inward normal
            s0 = (L - k * SW) / 2
            cand = []
            for i in range(k):
                px, py = a[0] + tx * (s0 + i * SW), a[1] + ty * (s0 + i * SW)
                cand.append(Polygon([(px, py), (px + tx * SW, py + ty * SW), (px + tx * SW + nx * SD, py + ty * SW + ny * SD),
                                     (px + nx * SD, py + ny * SD)]))
            arr = np.array(cand, dtype=object)
            shrunk = shapely.buffer(arr, -0.3, join_style=2)
            ok = shapely.contains(I, shrunk)
            if blockers is not None:
                ok &= ~shapely.intersects(blockers, shrunk)
            if taken_u is not None:
                ok &= ~shapely.intersects(taken_u, shrunk)
            if placed:
                ok &= ~shapely.intersects(unary_union(placed), shrunk)
            for run in _runs(ok):
                for ch in _chunk(run):
                    S.add_run([[arr[i]] for i in ch])
                    placed += [arr[i] for i in ch]


# ---------------------------------------------------------------- one candidate
def _build(ctx, groups, scale, m, mode, back_row, x0, W, req, Y0=None, med=5.0):
    pz, bz = ctx["pz"], ctx["bz"]
    gd = [_gdims(g, scale) for g in groups]
    rows, aisles_y, fronts, depth = _band_plan(m, mode, back_row, gd[0][1], gd[1][1] if len(gd) > 1 else 0.0, med)
    Y0 = ctx["y0"] if Y0 is None else Y0
    Hd = depth + 2 * RING
    D = _largest(pz.intersection(box(x0, Y0, x0 + W, Y0 + Hd)))
    if D is None or D.area < 0.6 * W * Hd:
        return None
    D = _largest(_fillet(D, R_OUT))
    if D is None:
        return None
    I = _largest(D.buffer(-RING, join_style=2))
    if I is None:
        return None
    iy0 = Y0 + RING
    row = []
    for group, (off, facing) in zip(groups, fronts):
        yf = iy0 + off
        band = I.intersection(box(x0 - 1, yf - 1 if facing > 0 else yf, x0 + W + 1, yf + 1 if facing < 0 else yf))
        if band.is_empty:
            return None
        bx0, _, bx1, _ = band.bounds
        r_, _ = _row(group, scale, (bx0 + bx1) / 2, yf, facing)
        row += r_
    Ib = I.buffer(0.5)
    for r in row:
        if r["legs"] is not None:
            near = r["rect"].buffer(DT + 1)
            parts = [g for g in _polys(r["legs"].intersection(Ib)) if g.intersects(near)]
            r["legs"] = unary_union(parts) if parts else None
            others = unary_union([q["rect"] for q in row if q is not r])
            if r["legs"] is not None and not others.is_empty and r["legs"].intersects(others.buffer(WALK)):
                return None
    for r in row:
        if not Ib.contains(r["env"]) or not bz.contains(r["rect"]):
            return None
    blk = unary_union([r["env"] for r in row] + [r["legs"] for r in row if r["legs"] is not None] +
                      [r["service"] for r in row if r["service"] is not None])
    # interior aisles (span the island, joining the ring both sides) and their stall rows
    aisles = [_largest(box(x0 - 50, iy0 + ya, x0 + W + 50, iy0 + ya + AIS).intersection(D)) for ya in aisles_y]
    aisles = [a for a in aisles if a is not None]
    S = Stalls()
    _grid_rows(S, I, rows, iy0, x0 - 400, x0 + W + 400, blk, med)
    grid = S.live()
    _edge_stalls(S, I.difference(unary_union(aisles)) if aisles else I, blk, grid + [a for a in aisles])
    return {"D": D, "I": I, "row": row, "aisles": aisles, "S": S, "n": S.n, "blk": blk, "med": med,
            "m": m, "bpos": mode, "scale": scale, "x0": x0, "W": W, "Hd": Hd}


# ---------------------------------------------------------------- search
def _rot(g, sign):
    return affinity.rotate(g, sign * 90, origin=(0, 0)) if g is not None else None


def _rot_cand(c, sign):
    out = dict(c)
    for k in ("D", "I", "blk"):
        out[k] = _rot(c[k], sign)
    out["aisles"] = [_rot(a, sign) for a in c["aisles"]]
    import copy
    out["S"] = copy.copy(c["S"])
    out["S"].polys = list(c["S"].polys)
    out["S"].clines = list(c["S"].clines)
    out["S"].transform(lambda g: _rot(g, sign))
    out["row"] = [{**r, **{k: _rot(r[k], sign) for k in ("rect", "lane", "legs", "walk", "service", "env")}} for r in c["row"]]
    return out


def _layouts(buildings, scale):
    """(mode, groups) options: one row (back / front) and, for 2+ buildings, every two-row split."""
    out = [("back", [buildings]), ("front", [buildings])]
    n = len(buildings)
    if 2 <= n <= 6:
        seen = set()
        for mask in range(1, 2 ** n - 1):
            A = [b for i, b in enumerate(buildings) if mask >> i & 1]
            B = [b for i, b in enumerate(buildings) if not mask >> i & 1]
            key = (tuple(sorted(id(x) for x in A)), tuple(sorted(id(x) for x in B)))
            if key in seen:
                continue
            seen.add(key)
            wa, wb = _gdims(A, scale)[0], _gdims(B, scale)[0]
            out.append(("split", [A, B], max(wa, wb)))
        splits = sorted([o for o in out if o[0] == "split"], key=lambda o: o[2])[:4]   # most balanced splits only
        out = out[:2] + [(m_, g_) for m_, g_, _ in splits]
    return out


def _search(ctx, buildings, scale, req):
    """Prefer 5 ft landscaped medians between head-to-head rows; fall back to a painted divider if space is tight."""
    for med in (5.0, 0.0):
        best = _search_med(ctx, buildings, scale, req, med)
        if best:
            return best
    return None


def _search_med(ctx, buildings, scale, req, med):
    """Two orientations: building rows along the frontage (bands stacked into the site), or along the depth
    (bands stacked across the site; for deep, narrow lots). The rotated case runs in a frame turned 90 deg."""
    lo, hi, y0 = ctx["drive_lo"], ctx["drive_hi"], ctx["y0"]
    best = None

    def consider(c, sign):
        nonlocal best
        if sign:
            c = _rot_cand(c, sign)
        dx0, dy0, dx1, _ = c["D"].bounds
        if dy0 > y0 + 8 or dx1 - R_OUT - 15 < lo or dx0 + R_OUT + 15 > hi:   # pad must reach the frontage at a driveway spot
            return
        pave = c["D"].area - c["I"].area + sum(a.area for a in c["aisles"]) + c["n"] * SW * SD
        cost = pave + 0.15 * c["D"].area
        if best is None or cost < best["cost"]:
            c["cost"] = cost
            best = c

    for orient in ("along", "deep"):
        if orient == "along":
            cx = ctx
        else:  # frame turned -90 deg: into-site becomes +x, the frontage runs along y
            cx = {**ctx, "pz": _rot(ctx["pz"], -1), "bz": _rot(ctx["bz"], -1), "P": _rot(ctx["P"], -1)}
            shapely.prepare(cx["pz"])
            shapely.prepare(cx["bz"])
        pminx, pminy, pmaxx, pmaxy = cx["pz"].bounds
        for mode, groups in _layouts(buildings, scale):
            gd = [_gdims(g, scale) for g in groups]
            Wmin = max(w for w, d in gd) + 2 * (RING + WALK) + 10
            for m in range(0, 5):
                for back_row in ((True, False) if mode == "back" else (True,)):
                    depth = _band_plan(m, mode, back_row, gd[0][1], gd[1][1] if len(gd) > 1 else 0.0, med)[3] + 2 * RING
                    if orient == "along":
                        if y0 + depth > pmaxy + 1 or Wmin > pmaxx - pminx + 2:
                            continue
                        starts = [(x, y0) for x in np.arange(pminx - 10, pmaxx - Wmin + 10, 15.0)]
                    else:
                        if depth > pmaxy - pminy + 2 or Wmin > pmaxx - pminx + 2:
                            continue
                        starts = [(pminx, y) for y in np.arange(pminy - 10, pmaxy - depth + 10, 15.0)]
                    for bx0, by0 in starts:
                        W = Wmin
                        while bx0 + W <= pmaxx + 40:
                            c = _build(cx, groups, scale, m, mode, back_row, bx0, W, req, Y0=by0, med=med)
                            if c is not None and c["n"] >= req:
                                consider(c, 1 if orient == "deep" else 0)
                                break
                            if c is None and W > Wmin + 200:
                                break
                            W += 18
    return best


# ---------------------------------------------------------------- driveway
def _driveway(ctx, D, flare=True):
    P, R = ctx["P"], ctx["R"]
    dminx, _, dmaxx, _ = D.bounds
    lo, hi = max(ctx["drive_lo"], dminx + R_OUT + AIS / 2), min(ctx["drive_hi"], dmaxx - R_OUT - AIS / 2)
    xd = (lo + hi) / 2 if hi >= lo else min(max((dminx + dmaxx) / 2, ctx["drive_lo"]), ctx["drive_hi"])
    # bias toward the middle of the frontage segment that faces the pad
    probe = LineString([(xd, D.bounds[3]), (xd, P.bounds[1] - 400)])
    hd = probe.intersection(D)
    y_d = min(c[1] for g in getattr(hd, "geoms", [hd]) for c in g.coords) if not hd.is_empty else D.bounds[1]
    hit = probe.intersection(R) if R is not None and not R.is_empty else None
    ys = [c[1] for g in getattr(hit, "geoms", [hit]) for c in g.coords if c[1] < y_d] if hit is not None and not hit.is_empty else []
    if ys:
        y_r = max(ys)
    else:
        b = probe.intersection(P.exterior)
        yb = [c[1] for g in getattr(b, "geoms", [b]) for c in g.coords if c[1] < y_d] if not b.is_empty else []
        y_r = (min(yb) if yb else 0.0) - 10
    drive = box(xd - AIS / 2, y_r, xd + AIS / 2, y_d + RING)
    r = 25.0
    fl_ = box(xd - AIS / 2 - r, y_r, xd + AIS / 2 + r, y_r + r)
    fl_ = fl_.difference(Point(xd - AIS / 2 - r, y_r + r).buffer(r, 48)).difference(Point(xd + AIS / 2 + r, y_r + r).buffer(r, 48))
    return (drive.union(fl_) if flare else drive), xd, y_r


def _pond(P, dev, constraints, target_sf):
    avail = P.buffer(-10).difference(dev.buffer(12)).difference(constraints.buffer(10))
    pieces = [g for g in _polys(avail.buffer(-5)) if g.area > target_sf * 0.6]
    if not pieces:
        return None
    g = min(pieces, key=lambda x: (round(x.distance(dev) / 25), -x.area))
    if g.area > target_sf * 1.4:
        near = g.exterior.interpolate(g.exterior.project(dev.centroid))
        r = math.sqrt(target_sf / math.pi)
        cand = g
        for _ in range(18):
            cand = g.intersection(near.buffer(r, 64))
            if cand.area >= target_sf:
                break
            r *= 1.08
        g = cand
    g = g.buffer(8, join_style=1).buffer(-14, join_style=1).buffer(6, join_style=1)
    return _largest(g)


# ---------------------------------------------------------------- main
def _generate_inner(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, program, rules=None, parking_setbacks_ft=None, drive_road=None, keep_bldgs=None, stem=None, steep=None, reuse_pave=None, shared_stalls=0):
    """drive_road: key of the road the driveway must connect to (ec_drawing.road_key); None = longest frontage.
    keep_bldgs: footprints of existing buildings that stay; the layout treats them (plus clearance) like wetlands."""
    rules = rules or {}
    wet = [] if isinstance(wet, dict) else wet
    fl = [] if isinstance(fl, dict) else fl
    rds = [] if isinstance(rds, dict) else rds
    named = [r for r in rds if r["class"] != "service"]
    road_lines = [r["centerline"] for r in named]
    keys = [road_key(r) for r in named]
    edges = classify_edges(parcel_sp, road_lines, k_ft, keys)
    main = None
    if drive_road:
        on = [e for e in edges if e["kind"] == "front" and e.get("road") == drive_road]
        if not on:
            raise RuntimeError(f"The parcel has no frontage on {drive_road}. Pick another road for the driveway.")
        main = max(on, key=lambda e: e["len_ft"])
    fr = Frame(parcel_sp, edges, k_ft, main=main)
    if stem:   # access-road mode: the frame sits where the access road enters the development pod, +y running into it
        fr.o, fr.a = stem["entry"], stem["angle"]
        fr.front_line = LineString([(-400.0, 0.0), (400.0, 0.0)])
    main_road = (main or max([e for e in edges if e["kind"] == "front"] or edges, key=lambda e: e["len_ft"])).get("road")
    P = fr.fwd(parcel_sp)
    if parking_setbacks_ft:
        psb = {k: float(parking_setbacks_ft.get(k, DEFAULT_PARK_SB[k])) for k in ("front", "side", "rear")}
    else:
        from .zoning import pavement_setbacks
        psb = {k: v for k, v in pavement_setbacks(rules, setbacks_ft).items() if k != "source"}
    KEEP = unary_union([g.buffer(KEEP_CLEAR_FT / k_ft) for g in (keep_bldgs or [])]) if keep_bldgs else None
    RP = unary_union([g for g in (reuse_pave or [])]).intersection(parcel_sp) if reuse_pave else None
    if RP is not None and RP.is_empty:
        RP = None
    C = unary_union([w["geom"] for w in wet] + [f["geom"] for f in fl if f.get("sfha") == "T"] + ([KEEP] if KEEP is not None else []) + list(steep or []) + ([RP.buffer(1.0 / k_ft)] if RP is not None else []))
    C = fr.fwd(C.intersection(parcel_sp.buffer(50 / k_ft))) if not C.is_empty else Polygon()
    bz = fr.fwd(envelope(parcel_sp, edges, setbacks_ft, k_ft)).difference(C).buffer(0)
    pz = fr.fwd(envelope(parcel_sp, edges, psb, k_ft)).difference(C).buffer(0)
    drive_rds = [r for r, kk in zip(named, keys) if kk == drive_road] if drive_road else rds
    R = unary_union([fr.fwd(r["pavement"]) for r in drive_rds]) if drive_rds else None
    if stem:
        R = fr.fwd(stem["poly_sp"])
    # frontage = every front edge lying along the main front line
    fl_lines = [fr.fwd(e["line"]) for e in edges if e["kind"] == "front" and (not drive_road or e.get("road") == drive_road)]
    fl_lines = [g for g in fl_lines if max(abs(c[1]) for c in g.coords) < 40] or [fr.front_line]
    if stem:
        fl_lines = [fr.front_line]
    fx = [min(c[0] for g in fl_lines for c in g.coords), max(c[0] for g in fl_lines for c in g.coords)]
    front_ln = unary_union(fl_lines)
    # wetlands can split the usable land: develop the piece that reaches the frontage
    if stem:
        pz = pz.intersection(fr.fwd(stem["pod_sp"])).intersection(box(-6000, -2.0, 6000, 6000)).buffer(0)
    pieces = [g for g in _polys(pz) if g.area > 5000]
    if not pieces:
        raise RuntimeError("No usable land left after setbacks, wetlands and flood zones.")
    pz = max(pieces, key=lambda g: g.area) if stem else min(pieces, key=lambda g: (round(g.distance(front_ln) / 50), -g.area))
    lo, hi = fx[0] + 50, fx[1] - 50
    if hi < lo:
        lo = hi = (fx[0] + fx[1]) / 2
    if stem:
        lo = hi = 0.0
    shapely.prepare(pz)
    shapely.prepare(bz)
    ctx = {"P": P, "pz": pz, "bz": bz, "R": R, "y0": math.ceil(pz.bounds[1]), "drive_lo": lo, "drive_hi": hi}

    buildings = program.get("buildings") or [{"name": "Building", "use": "retail", "footprint_sf": 8000, "depth_ft": 80, "stories": 1}]
    gfa = sum(b["footprint_sf"] * b.get("stories", 1) for b in buildings)
    req0 = (program.get("parking") or {}).get("required")
    req0 = float(req0) if isinstance(req0, (int, float)) and req0 > 0 else gfa / 1000 * 4
    req0 = max(req0 - max(0, int(shared_stalls or 0)), 0.0)   # existing stalls the new program may share

    best, scale, req, dropped = None, 1.0, math.ceil(req0), []
    # shrink the program first; if it still doesn't fit, drop buildings from the end of the list (lowest priority)
    for keep in range(len(buildings), 0, -1):
        sub = buildings[:keep]
        frac = sum(b["footprint_sf"] * b.get("stories", 1) for b in sub) / gfa
        for scale in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
            req = math.ceil(req0 * frac * scale)
            best = _search(ctx, sub, scale, req)
            if best:
                break
        if best:
            dropped = [b.get("name") or b.get("use") for b in buildings[keep:]]
            break
    if best is None:
        raise RuntimeError("Even the first building at half size does not fit the usable land. Reduce the program or adjust setbacks.")

    D, I, row = best["D"], best["I"], best["row"]
    drive, xd, y_r = _driveway(ctx, D, flare=not stem)
    if stem:
        drive = drive.union(fr.fwd(stem["poly_sp"]))
    ring = D.difference(I)
    lanes = [unary_union([g for g in (r["lane"], r["legs"]) if g is not None]) for r in row if r["lane"] is not None]
    # fillets: generous where the driveway meets the loop, tight (curb-return size) where aisles meet it, so the
    # landscape islands at row ends stay open instead of being paved over
    circ = unary_union([_close(unary_union([ring, drive]), R_JOIN), _close(unary_union([ring, *best["aisles"], *lanes]), 5.0)])
    circ = circ.difference(unary_union([r["rect"] for r in row] + [r["walk"] for r in row]))

    # just enough parking: trim whole columns from the ends of runs (farthest from the building fronts first),
    # so paired rows stay matched and every run still ends at an island; trimmed space becomes landscaping
    S = best["S"]
    fronts = unary_union([r["walk"] for r in row])
    lane_u = unary_union(lanes).buffer(-0.3) if lanes else None
    if lane_u is not None:
        for rid, units in S.runs.items():
            S.runs[rid] = [u for u in units if not any(S.polys[i].intersects(lane_u) for i in S.units[u])]
    ucen = {u: unary_union([S.polys[i] for i in idx]).centroid for u, idx in S.units.items()}
    while S.n > req:
        cands = []
        for rid, units in S.runs.items():
            if not units:
                continue
            for u in {units[0], units[-1]}:
                size = len(S.units[u])
                if S.n - size >= req:
                    cands.append((ucen[u].distance(fronts), rid, u))
        if not cands:
            break
        _, rid, u = max(cands)
        S.runs[rid].remove(u)
    for rid, units in list(S.runs.items()):   # no orphans: drop runs left shorter than MIN_RUN if parking allows
        if 0 < len(units) < MIN_RUN and S.n - sum(len(S.units[u]) for u in units) >= req:
            S.runs[rid] = []
    stalls = S.live()
    n_st = len(stalls)
    n_ada = ada_required(n_st) if n_st else 0
    ada = sorted(stalls, key=lambda s_: s_.centroid.distance(fronts))[:n_ada]
    # painted divider through matched head-to-head stalls (rows with no median)
    clines = []
    cl_runs = {rid for _, rid in S.clines}
    for rid in cl_runs:
        for u in S.runs.get(rid, []):
            a_, b_ = (S.polys[i] for i in S.units[u])
            shared = a_.boundary.intersection(b_.boundary)
            if not shared.is_empty and shared.length > 1:
                clines.append(shared)
    clines = [g for g in _polys_lines(unary_union(clines))] if clines else []

    pave = unary_union([circ, *stalls])
    pave = _close(pave, 2.2)  # rounded island noses (radius small enough to keep 5 ft medians open)
    pave = pave.difference(unary_union([r["rect"] for r in row]))
    # rounding must never push pavement into the parking setback (only the driveway may cross it)
    pave = pave.intersection(pz.union(drive.buffer(R_JOIN + 2))).buffer(0)
    RPf = fr.fwd(RP) if RP is not None else None
    if RPf is not None:
        pave = pave.difference(RPf).buffer(0)      # reused pavement is existing: only new paving is drawn / counted
    walks = unary_union([r["walk"] for r in row]).difference(pave)
    bldgs = [r["rect"] for r in row]
    dev = unary_union([pave, walks, *bldgs])
    grass = _fillet(dev.buffer(14, join_style=1), 6).intersection(P).difference(C).difference(dev)
    grass = unary_union([grass, I.difference(dev)])
    imperv_sf = pave.area + walks.area + sum(b.area for b in bldgs) + (RPf.area if RPf is not None else 0.0)
    land = [g for g in _polys(P.difference(C)) if g.intersects(dev)]
    land = unary_union(land) if land else P
    pond = _pond(land, dev.union(grass), C, max(3000.0, imperv_sf * 0.08))

    # landscape pad: straight-sided outline around the development, like the reference render
    pad = dev.buffer(14, join_style=2, mitre_limit=1.6).simplify(4).intersection(P).difference(C)
    pad = _largest(pad) or grass
    stripes = []
    for st in stalls:
        cs = list(st.exterior.coords)
        es = sorted(((math.hypot(b[0] - a[0], b[1] - a[1]), a, b) for a, b in zip(cs, cs[1:])), key=lambda t: -t[0])[:2]
        stripes += [LineString([a, b]) for _, a, b in es]
    stripes += clines

    # ornamental trees: one per parking island, along landscaped medians, in larger lawn areas, and around the pad edge
    trees = []
    lawn_in = I.difference(unary_union([pave, walks, *bldgs]).buffer(1.5))
    for g in _polys(lawn_in):
        if g.area < 90:
            continue
        mrr = g.minimum_rotated_rectangle
        cs = list(mrr.exterior.coords)
        e1, e2 = math.dist(cs[0], cs[1]), math.dist(cs[1], cs[2])
        long_, short_ = max(e1, e2), min(e1, e2)
        if g.area < 2500 or short_ < 14:
            if short_ >= 4.5:
                # islands: one tree in the middle; long medians: one every ~40 ft
                if long_ > 60:
                    axis = LineString(cs[0:2] if e1 >= e2 else cs[1:3])
                    off = (Point(cs[1 if e1 >= e2 else 2]).x - Point(cs[2 if e1 >= e2 else 3]).x) / 2, (Point(cs[1 if e1 >= e2 else 2]).y - Point(cs[2 if e1 >= e2 else 3]).y) / 2
                    nt = max(1, int(long_ // 40))
                    for i in range(nt):
                        p_ = axis.interpolate((i + 0.5) / nt, normalized=True)
                        q = Point(p_.x - off[0], p_.y - off[1])
                        if g.buffer(0.5).contains(q):
                            trees.append((q, 6.5))
                else:
                    q = polylabel(g, 0.5)
                    trees.append((q, min(7.5, max(5.0, short_ * 0.9))))
            continue
        # larger lawns inside the loop: loose grid, kept off the edges
        core = g.buffer(-9)
        x0_, y0_, x1_, y1_ = g.bounds
        for xx in np.arange(x0_ + 10, x1_, 34.0):
            for yy in np.arange(y0_ + 10, y1_, 34.0):
                q = Point(xx, yy)
                if core.contains(q):
                    trees.append((q, 8.0))
    edge_line = unary_union([g.exterior for g in _polys(dev.buffer(8, join_style=1))])
    pad_ok = pad.buffer(-4) if pad is not None and not pad.is_empty else None
    drive_keep = drive.buffer(30)
    nt = int(edge_line.length // 45)
    for i in range(nt):
        q = edge_line.interpolate(i / max(nt, 1), normalized=True)
        if pad_ok is not None and pad_ok.contains(q) and not drive_keep.contains(q) and q.distance(dev) > 5:
            trees.append((q, 8.5))
    trees = [(q, r) for q, r in trees if all(q.distance(q2) > (r + r2) * 0.8 for q2, r2 in trees if q2 is not q)] if trees else []

    parcel_sf = P.area
    bsf = sum(r["fp"] for r in row)
    gfa_sf = sum(r["fp"] * r["b"].get("stories", 1) for r in row)
    bz_left = ctx["bz"].difference(dev.buffer(25)).difference(pond.buffer(15) if pond is not None else Polygon())
    remaining_ac = sum(g.area for g in _polys(bz_left) if g.area > 10000) / 43560
    metrics = {
        "concept": program.get("use"),
        "buildings": [{"name": r["b"].get("name"), "use": r["b"].get("use"), "w_ft": round(r["w"]), "d_ft": round(r["d"]),
                       "footprint_sf": round(r["fp"], -1), "stories": r["b"].get("stories", 1), "drive_thru": bool(r["b"].get("drive_thru"))} for r in row],
        "drive_road": drive_road or main_road,
        **({"access": "road", "access_road_ft": round(stem["length_ft"])} if stem else {}),
        "building_sf": round(bsf, -1), "gfa_sf": round(gfa_sf, -1), "program_scale": scale,
        "stalls": n_st, "stalls_required": req, "ada_stalls": n_ada, "template": f"ring/{best['bpos']}/{best['m']}",
        "impervious_sf": round(imperv_sf, -1), "impervious_pct": round(100 * imperv_sf / parcel_sf, 1),
        "lot_coverage_pct": round(100 * bsf / parcel_sf, 1), "far": round(gfa_sf / parcel_sf, 3),
        "pond_sf": round(pond.area, -1) if pond is not None else 0,
        "development_acres": round(dev.area / 43560, 2),
        "remaining_developable_acres": round(remaining_ac, 2),
        "parking_setbacks_ft": psb, "setbacks_ft": setbacks_ft,
        **({"reused_pavement_sf": round(RPf.area, -1), "shared_stalls": int(shared_stalls or 0)} if RPf is not None else {}),
    }
    warn = []
    if keep_bldgs:
        kb = fr.fwd(unary_union(list(keep_bldgs)))
        if dev.intersects(kb.buffer(2.0)):
            warn.append("The layout touches an existing building that is marked to remain")
        metrics["existing_kept"] = len(keep_bldgs)
    if n_st < req:
        warn.append(f"Parking short: {n_st} provided vs {req} required")
    if scale < 1:
        warn.append(f"Program scaled to {int(scale * 100)}% to fit")
    if dropped:
        warn.append(f"Did not fit: {', '.join(map(str, dropped))}")
    for key, val, lab in (("max_impervious_pct", metrics["impervious_pct"], "impervious"), ("max_lot_coverage_pct", metrics["lot_coverage_pct"], "lot coverage")):
        lim = rules.get(key)
        if isinstance(lim, (int, float)) and val > lim:
            warn.append(f"{lab} {val}% exceeds the {lim}% maximum")
    if isinstance(rules.get("max_far"), (int, float)) and metrics["far"] > rules["max_far"]:
        warn.append(f"FAR {metrics['far']} exceeds {rules['max_far']}")
    metrics["warnings"] = warn

    # roof accent blocks (entry / office mass) like the reference: an end block on long buildings, a corner block otherwise
    roof_blocks = []
    for r in row:
        bx0, by0, bx1, by1 = r["rect"].bounds
        w_, d_ = bx1 - bx0, by1 - by0
        if r["fp"] < 2000:
            roof_blocks.append(None)
            continue
        if max(w_, d_) / min(w_, d_) >= 2.2:
            e = 0.2 * max(w_, d_)
            roof_blocks.append(box(bx0, by0, bx0 + e, by1) if w_ >= d_ else box(bx0, by0, bx1, by0 + e))
        else:
            c_ = min(28.0, 0.38 * min(w_, d_))
            fy = by0 if r["facing"] < 0 else by1 - c_
            roof_blocks.append(box(bx0, fy, bx0 + c_, fy + c_))
    local = {"P": P, "bz": bz, "pz": pz, "C": C, "pave": pave, "walk": walks, "grass": grass, "pad": pad, "roof_blocks": roof_blocks, "bldgs": bldgs,
             "reuse": RPf, "stalls": stalls, "ada": ada, "stripes": stripes, "pond": pond, "drive": drive, "dev": dev,
             "trees": [q.buffer(r, 24) for q, r in trees]}
    sp = {k: ([fr.inv(g) if g is not None else None for g in v] if isinstance(v, list) else (fr.inv(v) if v is not None else None))
          for k, v in local.items()}
    labels = []
    for r in row:
        c = fr.inv(r["rect"].centroid)
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a), "w_ft": r["w"], "d_ft": r["d"],
                       "lines": [f"PROPOSED {_short(r['b'])}",
                                 f"{r['w']:.0f}' X {r['d']:.0f}'", f"{r['fp']:,.0f} SF"]})
    if pond is not None:
        c = fr.inv(polylabel(pond, 2))
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a), "lines": ["STORMWATER", "MANAGEMENT"], "small": True})
    return {"metrics": metrics, "sp": sp, "local": local, "labels": labels, "frame_angle_deg": math.degrees(fr.a)}


# ---------------------------------------------------------------- access-road mode (flag / horseshoe lots)
STEM_CELL = 6.0


def _generate(*a, **kw):
    """program['gap_ft'] sets the clear space (walks + lawn) between buildings in a row; default GAP. Used for open-space layouts
    where fewer, larger buildings sit apart with lawn between them."""
    global GAP
    prog = kw.get("program") or (a[6] if len(a) > 6 else None) or {}
    g0 = GAP
    try:
        gp = float(prog.get("gap_ft") or 0)
        if gp > 0:
            GAP = gp
        return _generate_inner(*a, **kw)
    finally:
        GAP = g0


def _plan_stem(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, parking_setbacks_ft, drive_road, keep_bldgs, rules, steep=None, dem=None, reuse_pave=None, tie=False):
    """When the frontage is too narrow to build against, find the largest developable pod behind it and an access road
    (AIS wide) from the frontage to the pod. Returns {entry, angle, poly_sp, pod_sp, length_ft} or None.
    The path is the shortest route through land that is inside the side/rear setbacks and clear of wetlands, flood and
    kept buildings, so a horseshoe or a long neck is handled."""
    import heapq
    wet, fl = ([] if isinstance(wet, dict) else wet), ([] if isinstance(fl, dict) else fl)
    rds = [] if isinstance(rds, dict) else rds
    named = [r for r in rds if r["class"] != "service"]
    keys = [road_key(r) for r in named]
    edges = classify_edges(parcel_sp, [r["centerline"] for r in named], k_ft, keys)
    fronts = [e for e in edges if e["kind"] == "front" and (not drive_road or e.get("road") == drive_road)]
    if not fronts:
        print('stem: no front edges on', drive_road)
        return None
    main = max(fronts, key=lambda e: e["len_ft"])
    fr = Frame(parcel_sp, edges, k_ft, main=main)
    psb = {k: float((parking_setbacks_ft or {}).get(k, DEFAULT_PARK_SB[k])) for k in ("front", "side", "rear")}
    parts = [w["geom"] for w in wet] + [f["geom"] for f in fl if f.get("sfha") == "T"]
    C = unary_union(parts) if parts else Polygon()
    if keep_bldgs:
        C = unary_union([C, *[g.buffer(KEEP_CLEAR_FT / k_ft) for g in keep_bldgs]])
    if steep:
        C = unary_union([C, *steep])
    C = fr.fwd(C.intersection(parcel_sp.buffer(50 / k_ft))) if not C.is_empty else Polygon()
    RPl = fr.fwd(unary_union(list(reuse_pave)).intersection(parcel_sp)) if reuse_pave else None
    pz = fr.fwd(envelope(parcel_sp, edges, psb, k_ft)).difference(C).buffer(0)
    if RPl is not None and not RPl.is_empty:
        pz = pz.difference(RPl.buffer(1.0)).buffer(0)
    # where the access road may run: inside the side / rear pavement setbacks (front setback is crossable), out of constraints
    allowed = fr.fwd(envelope(parcel_sp, edges, {"front": 0, "side": psb["side"], "rear": psb["rear"]}, k_ft)).difference(C).buffer(0)
    corr = allowed.buffer(-AIS / 2)
    if corr.is_empty:
        print('stem: no corridor')
        return None
    pods = []
    for rho in (100.0, 80.0, 60.0, 45.0, 30.0):         # opened: strips narrower than 2*rho are necks, not pods
        pods = [g for g in _polys(pz.buffer(-rho).buffer(rho)) if g.area > 40000]
        if pods:
            break
    pods.sort(key=lambda g: -g.area)
    fl_lines = [fr.fwd(e["line"]) for e in fronts]
    road = unary_union([fr.fwd(r["pavement"]) for r, kk in zip(named, keys) if (not drive_road or kk == drive_road)])

    x0, y0, x1, y1 = corr.bounds
    cell = STEM_CELL if (x1 - x0) * (y1 - y0) < 6e6 else 12.0
    nx, ny = int((x1 - x0) / cell) + 1, int((y1 - y0) / cell) + 1
    xs, ys = x0 + (np.arange(nx) + 0.5) * cell, y0 + (np.arange(ny) + 0.5) * cell
    gx, gy = np.meshgrid(xs, ys)
    ok = shapely.contains_xy(corr, gx, gy)
    mult = np.ones(ok.shape)
    if dem is not None:       # slope-weighted: the road prefers flatter ground (8% costs 2x, 16% costs 5x)
        from . import topo as _topo
        wx = (gx / k_ft) * math.cos(fr.a) - (gy / k_ft) * math.sin(fr.a) + fr.o[0]
        wy = (gx / k_ft) * math.sin(fr.a) + (gy / k_ft) * math.cos(fr.a) + fr.o[1]
        mult = 1.0 + (_topo.sample(dem, wx, wy, "slope") / 8.0) ** 2
    if RPl is not None and not RPl.is_empty:      # existing drives / lots: the access road follows them where it can
        mult = mult * np.where(shapely.contains_xy(RPl, gx, gy), 0.3, 1.0)
    ctry = {}
    for pod in pods[:3]:
        tgt = shapely.contains_xy(pod.buffer(-10) if pod.buffer(-10).area > 8000 else pod, gx, gy) & ok
        if not tgt.any():
            print('stem: pod has no reachable cells', round(pod.area / 43560, 2))
            continue
        # sources: corridor cells right at a frontage line on the chosen road
        src = np.zeros_like(ok)
        if tie and RPl is not None and not RPl.is_empty:      # tie into existing pavement instead of the public road
            src = ok & shapely.contains_xy(RPl.buffer(cell), gx, gy)
        else:
            for ln in fl_lines:
                src |= ok & (shapely.distance(ln, shapely.points(gx, gy)) <= AIS / 2 + cell * 1.6)
        if not src.any():
            print('stem: no start cells on the frontage')
            continue
        INF = 1e18
        dist = np.full(ok.shape, INF)
        prev = {}
        pq = []
        for j, i in zip(*np.nonzero(src)):
            dist[j, i] = 0.0
            heapq.heappush(pq, (0.0, j, i))
        hit = None
        while pq:
            d, j, i = heapq.heappop(pq)
            if d > dist[j, i]:
                continue
            if tgt[j, i]:
                hit = (j, i)
                break
            for dj, di in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
                jj, ii = j + dj, i + di
                if 0 <= jj < ny and 0 <= ii < nx and ok[jj, ii]:
                    nd = d + cell * (1.414 if dj and di else 1.0) * mult[jj, ii]
                    if nd < dist[jj, ii]:
                        dist[jj, ii] = nd
                        prev[(jj, ii)] = (j, i)
                        heapq.heappush(pq, (nd, jj, ii))
        if hit is None:
            print('stem: no path to pod', round(pod.area / 43560, 2))
            continue
        path = [hit]
        while path[-1] in prev:
            path.append(prev[path[-1]])
        path = path[::-1]
        if len(path) < 2:
            continue      # the frontage already touches the pod: no access road needed
        line = LineString([(xs[i], ys[j]) for j, i in path]).simplify(cell * 1.2)
        if line.length < 2:
            continue
        ctry = {"pod": pod, "line": line}
        break
    if not ctry:
        print('stem: no pod/path found; pods =', [round(g.area / 43560, 2) for g in pods])
        return None
    pod, line = ctry["pod"], ctry["line"]
    # trim the road where it enters the pod; extend the start to the road edge
    inside = line.intersection(pod)
    if not inside.is_empty:
        cut = min(line.project(Point(c)) for g in getattr(inside, "geoms", [inside]) for c in g.coords)
        if cut > 4:
            line = substring(line, 0, cut)
    if line.length < (3 if tie else 12):
        print('stem: path too short', line.length)
        return None
    if tie and RPl is not None and not RPl.is_empty:
        # start where the line leaves the existing pavement; no flare, the pavement is the connection
        ds = [i * 3.0 for i in range(int(line.length // 3.0) + 1)]
        start = 0.0
        for d_ in ds:
            if RPl.buffer(1.0).contains(line.interpolate(d_)):
                start = d_
            else:
                break
        line = substring(line, max(0.0, start - 4.0), line.length)
        if line.length < 3:
            print('stem: tie path too short', line.length)
            return None
        L = line.length
        a_pt, b_pt = line.interpolate(max(0.0, L - 40.0)), line.interpolate(L)
        th = math.atan2(b_pt.y - a_pt.y, b_pt.x - a_pt.x)
        return {"entry": fr.inv(b_pt).coords[0], "angle": th - math.pi / 2 + fr.a, "poly_sp": fr.inv(line.buffer(AIS / 2, cap_style=2, join_style=1)),
                "pod_sp": fr.inv(pod.buffer(rho + 2)), "length_ft": L, "tie": True}
    sx, sy = line.coords[0]
    if not road.is_empty:
        probe = LineString([(sx, sy + 4), (sx, sy - 400)])
        hit = probe.intersection(road)
        yy = [c[1] for g in getattr(hit, "geoms", [hit]) for c in g.coords if not hit.is_empty and c[1] <= sy + 4]
        y_r = max(yy) if yy else sy - 10
    else:
        y_r = sy - 10
    line = LineString([(sx, y_r), *line.coords]) if y_r < sy - 1 else line
    # last direction before the pod -> frame for the layout
    L = line.length
    a_pt, b_pt = line.interpolate(max(0.0, L - 40.0)), line.interpolate(L)
    th = math.atan2(b_pt.y - a_pt.y, b_pt.x - a_pt.x)
    stem_poly = line.buffer(AIS / 2, cap_style=2, join_style=1)
    r = 25.0
    flare = box(sx - AIS / 2 - r, y_r, sx + AIS / 2 + r, y_r + r)
    flare = flare.difference(Point(sx - AIS / 2 - r, y_r + r).buffer(r, 48)).difference(Point(sx + AIS / 2 + r, y_r + r).buffer(r, 48))
    stem_poly = stem_poly.union(flare).intersection(parcel_poly_ext(fr.fwd(parcel_sp), y_r)).buffer(0)
    return {"entry": fr.inv(b_pt).coords[0], "angle": th - math.pi / 2 + fr.a,
            "poly_sp": fr.inv(stem_poly), "pod_sp": fr.inv(pod.buffer(rho + 2)), "length_ft": line.length}


def parcel_poly_ext(P, y_r):
    """Parcel plus everything out to the road edge (the access road leaves the parcel at the frontage)."""
    minx, miny, maxx, maxy = P.bounds
    return P.union(box(minx - 100, y_r - 5, maxx + 100, 0.0))


def generate(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, program, rules=None, parking_setbacks_ft=None, drive_road=None, keep_bldgs=None, steep=None, dem=None, reuse_pave=None, shared_stalls=0, tie_existing=True):
    """drive_road: key of the road the driveway must connect to (ec_drawing.road_key); None = longest frontage.
    keep_bldgs: footprints of existing buildings that stay; the layout treats them (plus clearance) like wetlands.
    reuse_pave: existing pavement that stays; new buildings and parking stay off it and the access road follows it.
    Tries the normal frontage layout first. If that fails, or has to shrink / drop buildings, it looks for a developable
    pod behind a narrow neck and lays the site out around an access road from the frontage to it, keeping the better."""
    if reuse_pave and tie_existing:
        # existing pavement stays: tie the new development into it (closest practical point) rather than the public road
        try:
            st = _plan_stem(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, parking_setbacks_ft, drive_road, keep_bldgs, rules or {}, steep=steep, dem=dem, reuse_pave=reuse_pave, tie=True)
            if st:
                alt = _generate(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, program, stem=st, rules=rules, parking_setbacks_ft=parking_setbacks_ft, drive_road=drive_road,
                                keep_bldgs=keep_bldgs, steep=steep, reuse_pave=reuse_pave, shared_stalls=shared_stalls)
                alt["metrics"]["warnings"].insert(0, f"Ties into the existing pavement with about {st['length_ft']:,.0f} ft of new drive")
                alt["metrics"]["access"] = "existing_pavement"
                return alt
        except Exception as e:  # noqa: BLE001
            print("tie-in layout failed:", repr(e))
    kw = dict(rules=rules, parking_setbacks_ft=parking_setbacks_ft, drive_road=drive_road, keep_bldgs=keep_bldgs, steep=steep, reuse_pave=reuse_pave, shared_stalls=shared_stalls)
    front, err = None, None
    try:
        front = _generate(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, program, **kw)
    except RuntimeError as e:
        err = e
    m = front["metrics"] if front else None
    if front and m["program_scale"] >= 1.0 and not any(w.startswith("Did not fit") for w in m["warnings"]):
        return front
    try:
        stem = _plan_stem(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, parking_setbacks_ft, drive_road, keep_bldgs, rules or {}, steep=steep, dem=dem, reuse_pave=reuse_pave)
    except Exception as e:  # noqa: BLE001
        print("stem planning failed:", repr(e))
        stem = None
    alt = None
    if stem:
        try:
            alt = _generate(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, program, stem=stem, **kw)
        except RuntimeError as e:
            print("access-road layout failed:", e)
    if alt and (front is None or alt["metrics"]["building_sf"] >= 1.15 * m["building_sf"]):
        alt["metrics"]["warnings"].insert(0, f"Access road of about {stem['length_ft']:,.0f} ft from the frontage to the developable land behind it")
        return alt
    if front:
        return front
    raise err
