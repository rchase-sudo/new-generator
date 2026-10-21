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
from shapely.ops import polylabel, unary_union

from .ec_drawing import _polys, classify_edges, envelope
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
            walk = rect.buffer(WALK, join_style=2).difference(rect)
            service = box(bx0 - WALK, back, bx1 + WALK, back + SERVICE) if facing < 0 else box(bx0 - WALK, back - SERVICE, bx1 + WALK, back)
        env = unary_union([g for g in (rect, walk, lane) if g is not None])
        out.append({"b": b, "rect": rect, "lane": lane, "legs": legs, "walk": walk, "service": service, "env": env,
                    "w": w, "d": d, "fp": w * d, "facing": facing})
        x += ew + GAP
    return out, total


def _band_plan(m, mode, back_row, dA, dB=0.0):
    """Offsets (from the island front) of interior stall rows, interior aisles and building fronts, plus island depth.
    mode back : parking toward the road, one building row behind it facing the road
    mode front: one building row near the road, facing back into the site, parking behind
    mode split: two building rows back to back, the first facing the road, the second facing the rear"""
    rows, aisles, fronts = [], [], []
    if mode in ("back", "split"):
        y = SD                                   # front perimeter row (edge-following)
        for _ in range(m):
            rows.append(y); y += SD               # back-to-back row
            aisles.append(y); y += AIS
            rows.append(y); y += SD
        y += WALK
        fronts.append((y, -1))
        if mode == "back":
            y += dA + SERVICE + (SD if back_row else 0)
            return rows, aisles, fronts, y
        y += dA + 2 * SERVICE + dB              # backs apart: grass strip / service between the two rows
        fronts.append((y, 1))
        y += WALK + SD                            # rear perimeter row (edge-following)
        return rows, aisles, fronts, y
    y = SERVICE + dA
    fronts.append((y, 1))
    y += WALK
    rows.append(y); y += SD
    for i in range(m):
        aisles.append(y); y += AIS
        rows.append(y); y += SD
        if i < m - 1:
            rows.append(y); y += SD
    y += SD if m else 0                           # back perimeter row (edge-following)
    return rows, aisles, fronts, y


def _gdims(group, scale):
    dims = [_bldg_dims(b, scale) for b in group]
    w = sum(w + (2 * DT if b.get("drive_thru") else 0) for (w, d), b in zip(dims, group)) + GAP * (len(group) - 1)
    d = max(d + (DT if b.get("drive_thru") else 0) for (w, d), b in zip(dims, group))
    return w, d


# ---------------------------------------------------------------- stalls
def _grid_row(I, y, x0, x1, blockers):
    xs = np.arange(x0, x1 - SW + 0.01, SW)
    if not len(xs):
        return []
    boxes = shapely.box(xs, y, xs + SW, y + SD)
    ok = shapely.contains(I, boxes)
    if blockers is not None:
        ok &= ~shapely.intersects(blockers, shapely.buffer(boxes, -0.3))
    return list(boxes[ok])


def _edge_stalls(I, blockers, taken):
    """Stalls perpendicular to the island edge, all the way round, with islands at ends and every ISLAND_EVERY."""
    out = []
    taken_u = unary_union(taken) if taken else None
    for poly in _polys(I):
        ring = poly.exterior.simplify(1.0)
        ccw = ring.is_ccw
        cs = list(ring.coords)
        for a, b in zip(cs, cs[1:]):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            k = int((L - 2 * END_GAP) // SW)
            if k < 2:
                continue
            tx, ty = (b[0] - a[0]) / L, (b[1] - a[1]) / L
            nx, ny = (-ty, tx) if ccw else (ty, -tx)   # inward normal
            s0 = (L - k * SW) / 2
            cand, idx = [], []
            for i in range(k):
                if ISLAND_EVERY and i and i % (ISLAND_EVERY + 1) == ISLAND_EVERY:
                    continue
                px, py = a[0] + tx * (s0 + i * SW), a[1] + ty * (s0 + i * SW)
                idx.append(i)
                cand.append(Polygon([(px, py), (px + tx * SW, py + ty * SW), (px + tx * SW + nx * SD, py + ty * SW + ny * SD),
                                     (px + nx * SD, py + ny * SD)]))
            if not cand:
                continue
            arr = np.array(cand, dtype=object)
            shrunk = shapely.buffer(arr, -0.3, join_style=2)
            ok = shapely.contains(I, shrunk)
            if blockers is not None:
                ok &= ~shapely.intersects(blockers, shrunk)
            if taken_u is not None:
                ok &= ~shapely.intersects(taken_u, shrunk)
            if out:
                ok &= ~shapely.intersects(unary_union(out), shrunk)
            # keep only runs of 3+ stalls (no orphan stalls); an island break joins two runs
            run = []
            for j, i in enumerate(idx):
                if ok[j] and run and i - idx[run[-1]] <= 2:
                    run.append(j)
                    continue
                if len(run) >= MIN_RUN:
                    out += [arr[q] for q in run]
                run = [j] if ok[j] else []
            if len(run) >= MIN_RUN:
                out += [arr[q] for q in run]
    return out


# ---------------------------------------------------------------- one candidate
def _build(ctx, groups, scale, m, mode, back_row, x0, W, req, Y0=None):
    pz, bz = ctx["pz"], ctx["bz"]
    gd = [_gdims(g, scale) for g in groups]
    rows, aisles_y, fronts, depth = _band_plan(m, mode, back_row, gd[0][1], gd[1][1] if len(gd) > 1 else 0.0)
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
    grid = []
    ix0, _, ix1, _ = I.bounds
    for ry in rows:
        grid += _grid_row(I, iy0 + ry, ix0, ix1, blk)
    grid = _island_breaks(grid)
    edge = _edge_stalls(I.difference(unary_union(aisles)) if aisles else I, blk, grid)
    stalls = grid + edge
    return {"D": D, "I": I, "row": row, "aisles": aisles, "stalls": stalls, "n": len(stalls), "blk": blk,
            "m": m, "bpos": mode, "scale": scale, "x0": x0, "W": W, "Hd": Hd}


def _island_breaks(grid):
    """Every ISLAND_EVERY stalls in a straight grid row, leave one stall space as a landscape island."""
    if not grid:
        return grid
    by_row = {}
    for s in grid:
        x0, y0, _, _ = s.bounds
        by_row.setdefault(round(y0, 1), []).append((x0, s))
    out = []
    for _, lst in by_row.items():
        lst.sort(key=lambda t: t[0])
        run, prev = 0, None
        for x0, s in lst:
            if prev is not None and x0 - prev > SW + 0.5:
                run = 0
            prev = x0
            run += 1
            if run == ISLAND_EVERY + 1:
                run = 0
                continue
            out.append(s)
    return out


# ---------------------------------------------------------------- search
def _rot(g, sign):
    return affinity.rotate(g, sign * 90, origin=(0, 0)) if g is not None else None


def _rot_cand(c, sign):
    out = dict(c)
    for k in ("D", "I", "blk"):
        out[k] = _rot(c[k], sign)
    out["aisles"] = [_rot(a, sign) for a in c["aisles"]]
    out["stalls"] = [_rot(a, sign) for a in c["stalls"]]
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
                    depth = _band_plan(m, mode, back_row, gd[0][1], gd[1][1] if len(gd) > 1 else 0.0)[3] + 2 * RING
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
                            c = _build(cx, groups, scale, m, mode, back_row, bx0, W, req, Y0=by0)
                            if c is not None and c["n"] >= req:
                                consider(c, 1 if orient == "deep" else 0)
                                break
                            if c is None and W > Wmin + 200:
                                break
                            W += 18
    return best


# ---------------------------------------------------------------- driveway
def _driveway(ctx, D):
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
    flare = box(xd - AIS / 2 - r, y_r, xd + AIS / 2 + r, y_r + r)
    flare = flare.difference(Point(xd - AIS / 2 - r, y_r + r).buffer(r, 48)).difference(Point(xd + AIS / 2 + r, y_r + r).buffer(r, 48))
    return drive.union(flare), xd, y_r


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
def generate(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, program, rules=None, parking_setbacks_ft=None):
    rules = rules or {}
    wet = [] if isinstance(wet, dict) else wet
    fl = [] if isinstance(fl, dict) else fl
    rds = [] if isinstance(rds, dict) else rds
    road_lines = [r["centerline"] for r in rds if r["class"] != "service"]
    edges = classify_edges(parcel_sp, road_lines, k_ft)
    fr = Frame(parcel_sp, edges, k_ft)
    P = fr.fwd(parcel_sp)
    psb = dict(DEFAULT_PARK_SB)
    for kk, v in (parking_setbacks_ft or rules.get("parking_setbacks_ft") or {}).items():
        if isinstance(v, (int, float)) and kk in psb:
            psb[kk] = float(v)
    if isinstance(rules.get("landscape_buffer_front_ft"), (int, float)):
        psb["front"] = max(psb["front"], float(rules["landscape_buffer_front_ft"]))
    C = unary_union([w["geom"] for w in wet] + [f["geom"] for f in fl if f.get("sfha") == "T"])
    C = fr.fwd(C.intersection(parcel_sp.buffer(50 / k_ft))) if not C.is_empty else Polygon()
    bz = fr.fwd(envelope(parcel_sp, edges, setbacks_ft, k_ft)).difference(C).buffer(0)
    pz = fr.fwd(envelope(parcel_sp, edges, psb, k_ft)).difference(C).buffer(0)
    R = unary_union([fr.fwd(r["pavement"]) for r in rds]) if rds else None
    # frontage = every front edge lying along the main front line
    fl_lines = [fr.fwd(e["line"]) for e in edges if e["kind"] == "front"]
    fl_lines = [g for g in fl_lines if max(abs(c[1]) for c in g.coords) < 40] or [fr.front_line]
    fx = [min(c[0] for g in fl_lines for c in g.coords), max(c[0] for g in fl_lines for c in g.coords)]
    front_ln = unary_union(fl_lines)
    # wetlands can split the usable land: develop the piece that reaches the frontage
    pieces = [g for g in _polys(pz) if g.area > 5000]
    if not pieces:
        raise RuntimeError("No usable land left after setbacks, wetlands and flood zones.")
    pz = min(pieces, key=lambda g: (round(g.distance(front_ln) / 50), -g.area))
    lo, hi = fx[0] + 50, fx[1] - 50
    if hi < lo:
        lo = hi = (fx[0] + fx[1]) / 2
    shapely.prepare(pz)
    shapely.prepare(bz)
    ctx = {"P": P, "pz": pz, "bz": bz, "R": R, "y0": math.ceil(pz.bounds[1]), "drive_lo": lo, "drive_hi": hi}

    buildings = program.get("buildings") or [{"name": "Building", "use": "retail", "footprint_sf": 8000, "depth_ft": 80, "stories": 1}]
    gfa = sum(b["footprint_sf"] * b.get("stories", 1) for b in buildings)
    req0 = (program.get("parking") or {}).get("required")
    req0 = float(req0) if isinstance(req0, (int, float)) and req0 > 0 else gfa / 1000 * 4

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
    drive, xd, y_r = _driveway(ctx, D)
    ring = D.difference(I)
    lanes = [unary_union([g for g in (r["lane"], r["legs"]) if g is not None]) for r in row if r["lane"] is not None]
    circ = _close(unary_union([ring, drive, *best["aisles"], *lanes]), R_JOIN)
    circ = circ.difference(unary_union([r["rect"] for r in row] + [r["walk"] for r in row]))

    # just enough parking: drop surplus stalls farthest from the building fronts (they become landscaping)
    stalls = [s for s in best["stalls"] if not s.intersects(unary_union(lanes).buffer(-0.3) if lanes else Polygon())]
    fronts = unary_union([r["walk"] for r in row])
    stalls.sort(key=lambda s: s.centroid.distance(fronts))
    stalls = stalls[:req] if len(stalls) > req else stalls
    n_st = len(stalls)
    n_ada = ada_required(n_st) if n_st else 0
    ada = stalls[:n_ada]

    pave = unary_union([circ, *stalls])
    pave = _close(pave, 3.0)  # rounded island noses
    pave = pave.difference(unary_union([r["rect"] for r in row]))
    walks = unary_union([r["walk"] for r in row]).difference(pave)
    bldgs = [r["rect"] for r in row]
    dev = unary_union([pave, walks, *bldgs])
    grass = _fillet(dev.buffer(14, join_style=1), 6).intersection(P).difference(C).difference(dev)
    grass = unary_union([grass, I.difference(dev)])
    imperv_sf = pave.area + walks.area + sum(b.area for b in bldgs)
    land = [g for g in _polys(P.difference(C)) if g.intersects(dev)]
    land = unary_union(land) if land else P
    pond = _pond(land, dev.union(grass), C, max(3000.0, imperv_sf * 0.08))

    stripes = []
    for s in stalls:
        cs = list(s.exterior.coords)
        es = sorted(((math.hypot(b[0] - a[0], b[1] - a[1]), a, b) for a, b in zip(cs, cs[1:])), key=lambda t: -t[0])[:2]
        stripes += [LineString([a, b]) for _, a, b in es]

    parcel_sf = P.area
    bsf = sum(r["fp"] for r in row)
    gfa_sf = sum(r["fp"] * r["b"].get("stories", 1) for r in row)
    bz_left = ctx["bz"].difference(dev.buffer(25)).difference(pond.buffer(15) if pond is not None else Polygon())
    remaining_ac = sum(g.area for g in _polys(bz_left) if g.area > 10000) / 43560
    metrics = {
        "concept": program.get("use"),
        "buildings": [{"name": r["b"].get("name"), "use": r["b"].get("use"), "w_ft": round(r["w"]), "d_ft": round(r["d"]),
                       "footprint_sf": round(r["fp"], -1), "stories": r["b"].get("stories", 1), "drive_thru": bool(r["b"].get("drive_thru"))} for r in row],
        "building_sf": round(bsf, -1), "gfa_sf": round(gfa_sf, -1), "program_scale": scale,
        "stalls": n_st, "stalls_required": req, "ada_stalls": n_ada, "template": f"ring/{best['bpos']}/{best['m']}",
        "impervious_sf": round(imperv_sf, -1), "impervious_pct": round(100 * imperv_sf / parcel_sf, 1),
        "lot_coverage_pct": round(100 * bsf / parcel_sf, 1), "far": round(gfa_sf / parcel_sf, 3),
        "pond_sf": round(pond.area, -1) if pond is not None else 0,
        "development_acres": round(dev.area / 43560, 2),
        "remaining_developable_acres": round(remaining_ac, 2),
        "parking_setbacks_ft": psb, "setbacks_ft": setbacks_ft,
    }
    warn = []
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

    local = {"P": P, "bz": bz, "pz": pz, "C": C, "pave": pave, "walk": walks, "grass": grass, "bldgs": bldgs,
             "stalls": stalls, "ada": ada, "stripes": stripes, "pond": pond, "drive": drive, "dev": dev}
    sp = {k: ([fr.inv(g) for g in v] if isinstance(v, list) else (fr.inv(v) if v is not None else None)) for k, v in local.items()}
    labels = []
    for r in row:
        c = fr.inv(r["rect"].centroid)
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a), "w_ft": r["w"], "d_ft": r["d"],
                       "lines": [f"PROPOSED {str(r['b'].get('use') or r['b'].get('name')).upper()}",
                                 f"{r['w']:.0f}' X {r['d']:.0f}'", f"{r['fp']:,.0f} SF"]})
    if pond is not None:
        c = fr.inv(polylabel(pond, 2))
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a), "lines": ["STORMWATER", "MANAGEMENT"], "small": True})
    return {"metrics": metrics, "sp": sp, "local": local, "labels": labels, "frame_angle_deg": math.degrees(fr.a)}
