"""Concept layout generator (front-parking template).

Works in a local frame in feet: x runs along the main frontage, y runs into the site (y = 0 on the front line).
Lays out double-loaded parking modules (9x18 stalls, 24 ft aisles) behind the front parking setback, a building
row behind the parking, a full-access driveway to the road, drive-thru lanes where the program asks for them,
and a stormwater pond in the leftover land. Searches building scale / module count / position and keeps the
layout that fits the most building area while meeting the parking requirement.
"""
import math

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polylabel, unary_union

from .ec_drawing import _polys, classify_edges, envelope

STALL_W, STALL_D, AISLE = 9.0, 18.0, 24.0
MODULE = STALL_D * 2 + AISLE  # 60 ft double-loaded bay
WALK = 10.0
DT_LANE = 14.0  # drive-thru / bypass lane width
DEFAULT_PARK_SB = {"front": 10.0, "side": 5.0, "rear": 5.0}


# ---------------------------------------------------------------- frame
class Frame:
    def __init__(self, parcel, edges, k, main=None):
        fronts = [e for e in edges if e["kind"] == "front"] or [max(edges, key=lambda e: e["len_ft"])]
        e = main or max(fronts, key=lambda e: e["len_ft"])
        (x0, y0), (x1, y1) = e["line"].coords[0], e["line"].coords[-1]
        self.k, self.o, self.a = k, (x0, y0), math.atan2(y1 - y0, x1 - x0)
        if self.fwd(parcel).centroid.y < 0:
            self.a += math.pi
        self.front_line = self.fwd(e["line"])

    def fwd(self, g):
        g = affinity.translate(g, -self.o[0], -self.o[1])
        g = affinity.rotate(g, -self.a, origin=(0, 0), use_radians=True)
        return affinity.scale(g, self.k, self.k, origin=(0, 0))

    def inv(self, g):
        g = affinity.scale(g, 1 / self.k, 1 / self.k, origin=(0, 0))
        g = affinity.rotate(g, self.a, origin=(0, 0), use_radians=True)
        return affinity.translate(g, self.o[0], self.o[1])


def ada_required(n):
    for lim, req in ((25, 1), (50, 2), (75, 3), (100, 4), (150, 5), (200, 6), (300, 7), (400, 8), (500, 9)):
        if n <= lim:
            return req
    return math.ceil(n * 0.02) if n <= 1000 else 20 + math.ceil((n - 1000) / 100)


# ---------------------------------------------------------------- pieces
def _row(buildings, scale, x0, yb):
    """Building rects (plus drive-thru lane areas) for a row starting at x0, fronts on y = yb."""
    out, x = [], x0
    for i, b in enumerate(buildings):
        fp = b["footprint_sf"] * scale
        d = min(b["depth_ft"], max(30.0, math.sqrt(fp) * 1.6))
        w = max(fp / d, 20.0)
        rect = box(x, yb, x + w, yb + d)
        lane = None
        if b.get("drive_thru"):
            # lane wraps the rear and right side of the building
            lane = box(x, yb + d, x + w + DT_LANE, yb + d + DT_LANE).union(box(x + w, yb, x + w + DT_LANE, yb + d + DT_LANE))
            w += DT_LANE
        out.append({"rect": rect, "lane": lane, "w": w, "d": d, "b": b, "fp": rect.area})
        x += w + (30.0 if i + 1 < len(buildings) else 0)
    return out, x - x0


def _stalls(pz_prep, fa, fb, y0, n, drive):
    """Stall boxes for n modules from y0 between x = fa..fb. Returns list of (box, module, top_row)."""
    xs = np.arange(fa, fb - STALL_W + 0.01, STALL_W)
    if len(xs) == 0:
        return []
    out = []
    for m in range(n):
        for row, ys in ((0, y0 + m * MODULE), (1, y0 + m * MODULE + STALL_D + AISLE)):
            boxes = shapely.box(xs, ys, xs + STALL_W, ys + STALL_D)
            ok = shapely.contains(pz_prep, boxes)
            if drive is not None:
                ok &= ~shapely.intersects(drive, boxes)
            out += [(bx, m, row == 1 and m == n - 1) for bx in boxes[ok]]
    return out


def _driveway(fr, P, R, xd, y_to):
    """24 ft two-way driveway from the road edge to y_to at x = xd, with 15 ft curb-return flares."""
    probe = LineString([(xd, y_to), (xd, P.bounds[1] - 400)])
    hit = probe.intersection(R) if R is not None and not R.is_empty else None
    if hit is not None and not hit.is_empty:
        y_r = max(c[1] for g in getattr(hit, "geoms", [hit]) for c in g.coords if c[1] < y_to)
    else:
        b = probe.intersection(P.exterior)
        ys = [c[1] for g in getattr(b, "geoms", [b]) for c in g.coords if c[1] < y_to] if not b.is_empty else []
        y_r = min(ys) if ys else 0.0
    d = box(xd - AISLE / 2, y_r, xd + AISLE / 2, y_to)
    r = 15.0
    flare = box(xd - AISLE / 2 - r, y_r, xd + AISLE / 2 + r, y_r + r)
    flare = flare.difference(Point(xd - AISLE / 2 - r, y_r + r).buffer(r, 32)).difference(Point(xd + AISLE / 2 + r, y_r + r).buffer(r, 32))
    return d.union(flare), y_r


def _try(P, pz, pz_prep, bz_prep, R, front_x, buildings, scale, n, required, y0):
    """Best layout for one (scale, modules) combination, or None."""
    row_proto, row_w = _row(buildings, scale, 0, 0)
    yb = y0 + n * MODULE + WALK
    bminx, _, bmaxx, _ = pz.bounds
    best = None
    for x0 in np.arange(math.floor(bminx), bmaxx - row_w + 0.01, 10.0):
        row, _ = _row(buildings, scale, x0, yb)
        shapes = [r["rect"] if r["lane"] is None else r["rect"].union(r["lane"]) for r in row]
        if not all(bz_prep.contains(s) for s in shapes):
            continue
        # parking field: start under the row, grow sideways until parking is met or the site runs out
        band = pz.intersection(box(bminx - 1, y0, bmaxx + 1, y0 + n * MODULE))
        if band.is_empty:
            continue
        cx = x0 + row_w / 2
        comp = min(_polys(band), key=lambda g: g.distance(Point(cx, y0 + MODULE / 2)))
        cminx, _, cmaxx, _ = comp.bounds
        fa, fb = max(cminx, x0), min(cmaxx, x0 + row_w)
        # driveway: on the frontage, nearest the field centre, 40 ft clear of property corners
        lo, hi = front_x[0] + 40 + AISLE / 2, front_x[1] - 40 - AISLE / 2
        if hi < lo:
            lo = hi = (front_x[0] + front_x[1]) / 2
        while True:
            xd = min(max((fa + fb) / 2, lo), hi)
            fa_, fb_ = min(fa, xd - AISLE / 2 - STALL_W), max(fb, xd + AISLE / 2 + STALL_W)
            drive, y_r = _driveway(None, P, R, xd, y0 + STALL_D + AISLE)
            st = _stalls(pz_prep, fa_, fb_, y0, n, drive)
            if len(st) >= required * 1.03 or (fa <= cminx and fb >= cmaxx):
                break
            fa, fb = max(cminx, fa - STALL_W * 2), min(cmaxx, fb + STALL_W * 2)
        fa, fb = fa_, fb_
        # aisles (and end cross-aisles tying modules together)
        aisles = [box(fa, y0 + m * MODULE + STALL_D, fb, y0 + m * MODULE + STALL_D + AISLE) for m in range(n)]
        if n > 1:
            aisles += [box(fa - AISLE, y0 + STALL_D, fa, y0 + (n - 1) * MODULE + STALL_D + AISLE),
                       box(fb, y0 + STALL_D, fb + AISLE, y0 + (n - 1) * MODULE + STALL_D + AISLE)]
        aisles = [a.intersection(pz) for a in aisles]
        # drop stalls that lost their aisle
        aisle_u = unary_union(aisles + [drive])
        st = [s for s in st if s[0].buffer(1.0).intersects(aisle_u)]
        stalls = len(st)
        bsf = sum(r["fp"] * r["b"].get("stories", 1) for r in row)
        meets = stalls >= required
        score = (1 if meets else 0, bsf if meets else bsf * stalls / max(required, 1), -abs(stalls - required))
        if best is None or score > best["score"]:
            walk = box(min(r["rect"].bounds[0] for r in row), yb - WALK, max(r["rect"].bounds[2] for r in row), yb)
            best = {"score": score, "row": row, "stalls": st, "aisles": aisles, "drive": drive, "walk": walk,
                    "n": n, "scale": scale, "meets": meets, "template": "front_parking"}
    return best


def _try_spine(P, pz, bz, R, front_x, buildings, scale, n, required, y0):
    """Deep / narrow lots: a drive aisle runs from the road into the site with 90-degree parking both sides;
    buildings face the spine (one side, or alternating sides) with drive-thru lanes behind."""
    lo, hi = front_x[0] + 40 + AISLE / 2, front_x[1] - 40 - AISLE / 2
    if hi < lo:
        lo = hi = (front_x[0] + front_x[1]) / 2
    _, _, _, pmaxy = pz.bounds
    best = None
    for xs in np.arange(lo, hi + 0.01, 10.0):
        for side in (1, -1, 0):
            sides = [side or (1 if i % 2 == 0 else -1) for i in range(len(buildings))]
            row, cursor, ok = [], {1: y0 + 20.0, -1: y0 + 20.0}, True
            for b, sd in zip(buildings, sides):
                fp = b["footprint_sf"] * scale
                d = min(b["depth_ft"], max(30.0, math.sqrt(fp) * 1.6))
                w = max(fp / d, 20.0)
                xf = xs + sd * (AISLE / 2 + STALL_D + (n - 1) * MODULE * 0 + WALK)
                xa, xb = sorted((xf, xf + sd * d))
                placed = None
                for yb0 in np.arange(cursor[sd], pmaxy, 10.0):
                    rect = box(xa, yb0, xb, yb0 + w)
                    lane = None
                    if b.get("drive_thru"):
                        lx = (xb, xb + DT_LANE) if sd > 0 else (xa - DT_LANE, xa)
                        lane = box(lx[0], yb0, lx[1], yb0 + w + DT_LANE).union(box(min(xa, lx[0]), yb0 + w, max(xb, lx[1]), yb0 + w + DT_LANE))
                    if bz.contains(rect if lane is None else rect.union(lane)):
                        placed = {"rect": rect, "lane": lane, "w": w, "d": d, "b": b, "fp": rect.area, "side": sd}
                        break
                if placed is None:
                    ok = False
                    break
                row.append(placed)
                cursor[sd] = placed["rect"].bounds[3] + (DT_LANE if placed["lane"] is not None else 0) + 30.0
            if not ok:
                continue
            y_end = max(r["rect"].bounds[3] for r in row)
            drive, _ = _driveway(None, P, R, xs, y0 + 1)
            while True:
                spine = box(xs - AISLE / 2, y0, xs + AISLE / 2, y_end)
                ys = np.arange(y0, y_end - STALL_W + 0.01, STALL_W)
                cols = [(xs - AISLE / 2 - STALL_D, xs - AISLE / 2), (xs + AISLE / 2, xs + AISLE / 2 + STALL_D)]
                st = []
                for ca, cb in cols:
                    boxes = shapely.box(ca, ys, cb, ys + STALL_W)
                    good = shapely.contains(pz, boxes) & ~shapely.intersects(drive, boxes)
                    st += [(bx, 0, True) for bx in boxes[good]]
                if len(st) >= required or y_end + 18 > pmaxy or not pz.contains(box(xs - AISLE / 2, y0, xs + AISLE / 2, y_end + 18)):
                    break
                y_end += 18
            if not pz.buffer(1).contains(spine):
                continue
            # walks along the building fronts
            walks = []
            for r in row:
                x0_, y0_, x1_, y1_ = r["rect"].bounds
                walks.append(box(x0_ - WALK, y0_, x0_, y1_) if r["side"] > 0 else box(x1_, y0_, x1_ + WALK, y1_))
            bsf = sum(r["fp"] * r["b"].get("stories", 1) for r in row)
            meets = len(st) >= required
            score = (1 if meets else 0, bsf if meets else bsf * len(st) / max(required, 1), -abs(len(st) - required) - 0.5)
            if best is None or score > best["score"]:
                best = {"score": score, "row": row, "stalls": st, "aisles": [spine], "drive": drive, "walk": unary_union(walks),
                        "n": 1, "scale": scale, "meets": meets, "template": "spine"}
    return best


def _pond(P, used, constraints, target_sf, keep_out_ft=10):
    """Stormwater pond in leftover land next to the development (not across a wetland), sized to target_sf."""
    avail = P.buffer(-keep_out_ft).difference(used.buffer(keep_out_ft)).difference(constraints.buffer(10))
    pieces = [g for g in _polys(avail.buffer(-5)) if g.area > target_sf * 0.6]
    if not pieces:
        return None
    g = min(pieces, key=lambda x: (x.distance(used), -x.area))
    if g.area > target_sf * 1.4:
        # grow a disc from the point of the piece nearest the pavement until it holds the target area
        near = g.exterior.interpolate(g.exterior.project(used.centroid))
        r = math.sqrt(target_sf / math.pi)
        for _ in range(16):
            cand = g.intersection(near.buffer(r, 48))
            if cand.area >= target_sf:
                break
            r *= 1.1
        g = cand
    g = g.buffer(5, join_style=1).buffer(-8, join_style=1).buffer(3, join_style=1)
    return max(_polys(g), key=lambda x: x.area) if not g.is_empty else None


# ---------------------------------------------------------------- main
def generate(parcel_sp, k_ft, rds, wet, fl, setbacks_ft, program, rules=None, parking_setbacks_ft=None):
    """Returns dict with local-frame geometry, state-plane geometry and metrics."""
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
    fx = sorted(c[0] for c in fr.front_line.coords)

    buildings = program.get("buildings") or [{"name": "Building", "use": "retail", "footprint_sf": 8000, "depth_ft": 80, "stories": 1}]
    gfa = sum(b["footprint_sf"] * b.get("stories", 1) for b in buildings)
    req0 = (program.get("parking") or {}).get("required")
    req0 = float(req0) if isinstance(req0, (int, float)) and req0 > 0 else gfa / 1000 * 4
    shapely.prepare(pz)
    shapely.prepare(bz)
    y0 = math.ceil(pz.bounds[1])

    best = None
    for scale in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
        req = math.ceil(req0 * scale)
        cands = [_try(P, pz, pz, bz, R, fx, buildings, scale, n, req, y0) for n in (1, 2, 3, 4)]
        cands.append(_try_spine(P, pz, bz, R, fx, buildings, scale, 1, req, y0))
        for cand in cands:
            if cand and (best is None or cand["score"] > best["score"]):
                cand["required"] = req
                best = cand
        if best and best["meets"]:
            break
    if best is None:
        raise RuntimeError("Program does not fit the buildable area with front parking. Reduce the program or adjust setbacks.")

    row = best["row"]
    bldgs = [r["rect"] for r in row]
    lanes = [r["lane"] for r in row if r["lane"] is not None]
    walk = best["walk"].intersection(P)
    stall_u = unary_union([s[0] for s in best["stalls"]])
    pave = unary_union([stall_u, *best["aisles"], best["drive"], *lanes])
    pave = pave.buffer(3, join_style=2).buffer(-3, join_style=2).difference(unary_union(bldgs))
    imperv_sf = pave.area + walk.area + sum(b.area for b in bldgs)
    used = unary_union([pave, walk, *bldgs])
    pond = _pond(P, used, C, max(3000.0, imperv_sf * 0.08))

    # ADA stalls: top-row stalls closest to the building fronts' centre
    n_st = len(best["stalls"])
    n_ada = ada_required(n_st) if n_st else 0
    ada = [s[0] for s in sorted(best["stalls"], key=lambda s: s[0].centroid.distance(walk))[:n_ada]]
    stripes = set()
    for s, _m, _t in best["stalls"]:
        x0, y0s, x1, y1 = s.bounds
        if (x1 - x0) < (y1 - y0s):  # stall runs along y: separators are its vertical sides
            stripes.add((round(x0, 2), round(y0s, 2), round(x0, 2), round(y1, 2)))
            stripes.add((round(x1, 2), round(y0s, 2), round(x1, 2), round(y1, 2)))
        else:
            stripes.add((round(x0, 2), round(y0s, 2), round(x1, 2), round(y0s, 2)))
            stripes.add((round(x0, 2), round(y1, 2), round(x1, 2), round(y1, 2)))
    stripes = [LineString([(a, b), (c, d)]) for a, b, c, d in stripes]

    parcel_sf = P.area
    bsf = sum(r["fp"] for r in row)
    gfa_sf = sum(r["fp"] * r["b"].get("stories", 1) for r in row)
    remaining = bz.difference(used.buffer(15)).difference(pond.buffer(15) if pond is not None else Polygon())
    remaining_ac = sum(g.area for g in _polys(remaining) if g.area > 10000) / 43560
    metrics = {
        "concept": program.get("use"), "buildings": [
            {"name": r["b"].get("name"), "use": r["b"].get("use"),
             "w_ft": round(max(r["rect"].bounds[2] - r["rect"].bounds[0], r["rect"].bounds[3] - r["rect"].bounds[1])),
             "d_ft": round(min(r["rect"].bounds[2] - r["rect"].bounds[0], r["rect"].bounds[3] - r["rect"].bounds[1])), "footprint_sf": round(r["fp"], -1), "stories": r["b"].get("stories", 1), "drive_thru": bool(r["b"].get("drive_thru"))}
            for r in row],
        "building_sf": round(bsf, -1), "gfa_sf": round(gfa_sf, -1), "program_scale": best["scale"],
        "stalls": n_st, "stalls_required": best["required"], "ada_stalls": n_ada, "parking_modules": best["n"], "template": best["template"],
        "impervious_sf": round(imperv_sf, -1), "impervious_pct": round(100 * imperv_sf / parcel_sf, 1),
        "lot_coverage_pct": round(100 * bsf / parcel_sf, 1), "far": round(gfa_sf / parcel_sf, 3),
        "pond_sf": round(pond.area, -1) if pond is not None else 0,
        "remaining_developable_acres": round(remaining_ac, 2),
        "parking_setbacks_ft": psb, "setbacks_ft": setbacks_ft,
    }
    warn = []
    if not best["meets"]:
        warn.append(f"Parking short: {n_st} provided vs {best['required']} required")
    if best["scale"] < 1:
        warn.append(f"Program scaled to {int(best['scale'] * 100)}% to fit")
    for key, val, lab in (("max_impervious_pct", metrics["impervious_pct"], "impervious"), ("max_lot_coverage_pct", metrics["lot_coverage_pct"], "lot coverage")):
        lim = rules.get(key)
        if isinstance(lim, (int, float)) and val > lim:
            warn.append(f"{lab} {val}% exceeds the {lim}% maximum")
    if isinstance(rules.get("max_far"), (int, float)) and metrics["far"] > rules["max_far"]:
        warn.append(f"FAR {metrics['far']} exceeds {rules['max_far']}")
    metrics["warnings"] = warn

    local = {"P": P, "bz": bz, "pz": pz, "C": C, "bldgs": bldgs, "lanes": lanes, "walk": walk, "pave": pave,
             "stalls": [s[0] for s in best["stalls"]], "ada": ada, "stripes": stripes, "pond": pond, "drive": best["drive"]}
    sp = {k: ([fr.inv(g) for g in v] if isinstance(v, list) else (fr.inv(v) if v is not None else None)) for k, v in local.items()}
    labels = []
    for r in row:
        c = fr.inv(r["rect"].centroid)
        bx0, by0, bx1, by1 = r["rect"].bounds
        w_, d_ = bx1 - bx0, by1 - by0
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a), "lines": [f"PROPOSED {str(r['b'].get('use') or r['b'].get('name')).upper()}",
                       f"{max(w_, d_):.0f}' x {min(w_, d_):.0f}'", f"{r['fp']:,.0f} SF"], "w_ft": w_, "d_ft": d_})
    if pond is not None:
        c = fr.inv(polylabel(pond, 2))
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a), "lines": ["STORMWATER", "MANAGEMENT"], "small": True})
    return {"metrics": metrics, "sp": sp, "local": local, "labels": labels, "frame_angle_deg": math.degrees(fr.a)}
