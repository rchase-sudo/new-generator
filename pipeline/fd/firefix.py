"""Fire apparatus access is designed in, not just checked: the fire route along the drive centreline gets a 24 ft clear lane with
25 ft inside radii at its turns, and a dead-end longer than 150 ft gets a 120 ft hammerhead turnaround (IFC Appendix D)."""
import math

import numpy as np
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from .base import polys

LANE = 24.0          # clear width provided
R_IN = 25.0          # inside turning radius
R_CL = R_IN + LANE / 2


def fillet(line, R=R_CL, tol=4.0):
    """Round the corners of a polyline to radius R (less where the legs are too short). Returns (line, smallest radius used at a real turn)."""
    pts = [np.array(c[:2], float) for c in line.simplify(tol).coords]
    # the raster centreline ends in short hooks where it reaches the pavement edge: straighten them
    while len(pts) > 3 and np.linalg.norm(pts[-1] - pts[-2]) < 30.0:
        pts.pop(-1)
    while len(pts) > 3 and np.linalg.norm(pts[1] - pts[0]) < 30.0:
        pts.pop(0)
    # a corner the raster path cut diagonally (short leg between two long ones) becomes one corner where the long legs meet
    i = 1
    while i < len(pts) - 2:
        a, b1, b2, c = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        if np.linalg.norm(b2 - b1) < 40.0:
            d1, d2 = b1 - a, c - b2
            den = d1[0] * d2[1] - d1[1] * d2[0]
            if abs(den) > 1e-6 * np.linalg.norm(d1) * np.linalg.norm(d2) and abs(den) / (np.linalg.norm(d1) * np.linalg.norm(d2)) > 0.25:
                tt = ((b2[0] - a[0]) * d2[1] - (b2[1] - a[1]) * d2[0]) / den
                x = a + d1 * tt
                if np.linalg.norm(x - b1) < 60.0 and np.linalg.norm(x - b2) < 60.0:
                    pts[i:i + 2] = [x]
                    continue
        i += 1
    if len(pts) < 3:
        return LineString([tuple(p) for p in pts]), None
    out, rmin = [pts[0]], None
    n = len(pts)
    for i, (a, b, c) in enumerate(zip(pts, pts[1:], pts[2:])):
        la, lc = np.linalg.norm(a - b), np.linalg.norm(c - b)
        fa, fc = (0.95 if i == 0 else 0.48), (0.95 if i == n - 3 else 0.48)       # an end leg is not shared with another curve
        if la < 1e-6 or lc < 1e-6:
            continue
        u, v = (a - b) / la, (c - b) / lc
        th = math.acos(max(-1.0, min(1.0, float(u @ v))))            # interior angle
        defl = math.pi - th
        if defl < math.radians(8) or th < 1e-3:
            out.append(b)
            continue
        t = min(R / math.tan(th / 2), la * fa, lc * fc)
        r = t * math.tan(th / 2)
        if defl > math.radians(25):
            rmin = r if rmin is None else min(rmin, r)
        bis = (u + v) / np.linalg.norm(u + v)
        cen = b + bis * (r / math.sin(th / 2))
        p0, p1 = b + u * t, b + v * t
        a0, a1 = math.atan2(*(p0 - cen)[::-1]), math.atan2(*(p1 - cen)[::-1])
        da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
        for k in range(0, 9):
            ang = a0 + da * k / 8
            out.append(cen + r * np.array([math.cos(ang), math.sin(ang)]))
    out.append(pts[-1])
    return LineString([tuple(p) for p in out]), rmin


def is_loop(pave, bldgs):
    """True when the pavement circulates around something (a through route), so the drive is not a dead end."""
    for p in polys(pave):
        for ring in p.interiors:
            hole = box(*ring.bounds)
            if hole.area > 1500 and min(ring.bounds[2] - ring.bounds[0], ring.bounds[3] - ring.bounds[1]) > 30:
                return True
    return False


def apply(L, spine):
    """Widen the fire route, open its inside corners and add a turnaround where the drive dead-ends. Changes L in place; returns notes."""
    notes = []
    pave0 = L["pave"].union(L["drive"]) if L.get("drive") is not None and not L["drive"].is_empty else L["pave"]
    solid = unary_union([b.buffer(8.0) for b in L["bldgs"]]) if L["bldgs"] else None
    inside = L["parcel"].buffer(-3.0).union(pave0)
    route, rmin = fillet(spine)
    # pavement is added only where the 20 ft clear lane along the rounded route leaves the existing pavement (inside corners, pinch points)
    lane = route.buffer(10.0, cap_style=2).intersection(inside)
    if solid is not None:
        lane = lane.difference(solid)
    bits = [p.buffer(2.0) for p in polys(lane.difference(pave0.buffer(0.5))) if p.area > 40]
    added = sum(p.area for p in bits)
    extra = list(bits)
    if bits:
        notes.append(f"Fire lane: pavement widened at {len(bits)} location{'s' if len(bits) != 1 else ''} (about {added:,.0f} SF) for a 20 ft clear lane with {R_IN:.0f} ft inside turning radii")
    # dead end: 120 ft hammerhead at the far end where it fits
    turn = None
    if spine.length > 150 and not is_loop(pave0, L["bldgs"]):
        e = np.array(route.coords[-1][:2])
        p = np.array(route.interpolate(max(route.length - 25.0, 0)).coords[0][:2])
        d = (e - p) / max(np.linalg.norm(e - p), 1e-6)
        n = np.array([-d[1], d[0]])
        free = L["parcel"].buffer(-5.0)
        if solid is not None:
            free = free.difference(solid)
        best = None
        for back in (10.0, 30.0, 50.0, 80.0):                 # centre of the T, measured back from the end of the drive
            c = e - d * back
            for (l1, l2) in ((60.0, 60.0), (70.0, 50.0), (50.0, 70.0), (120.0, 0.0), (0.0, 120.0)):
                bar = LineString([tuple(c - n * l1), tuple(c + n * l2)]).buffer(10.0, cap_style=2)
                if free.contains(bar) or bar.difference(free.union(pave0)).area < 15:
                    cut = sum(1 for s_ in L.get("stalls") or [] if s_.intersection(bar).area > 0.3 * s_.area)
                    if best is None or cut < best[0]:
                        best = (cut, bar, c)
            if best is not None and best[0] == 0:
                break
        if best is not None:
            turn = best[1]
            extra.append(turn)
            L["turnaround"] = turn
            notes.append("Dead-end access drive longer than 150 ft: a 120 ft hammerhead turnaround (20 ft wide) is provided at the end of the drive")
        else:
            L["turnaround"] = None
    if extra:
        new = unary_union([L["pave"]] + extra).buffer(0)
        if L.get("pond") is not None and not L["pond"].is_empty:
            new = new.difference(L["pond"].buffer(6.0)).union(L["pave"])
        taken = unary_union(extra).difference(pave0.buffer(0.5))
        keep, lost = [], 0
        for s_ in L.get("stalls") or []:
            if taken.intersection(s_).area > 0.25 * s_.area:
                lost += 1
            else:
                keep.append(s_)
        if lost:
            L["stalls"] = keep
            L["stripes"] = [x for x in (L.get("stripes") or []) if not taken.buffer(1.0).contains(x)]
            notes.append(f"{lost} parking stall{'s' if lost != 1 else ''} removed to make room for fire apparatus access")
        L["pave"] = new
        L["dev"] = L["dev"].union(new)
        if L.get("grass") is not None:
            L["grass"] = L["grass"].difference(new)
        L["trees"] = [t for t in (L.get("trees") or []) if not new.buffer(3.0).contains(Point(t.centroid) if hasattr(t, "centroid") else t)]
    L["fire_route"], L["fire_rmin_cl"] = route, rmin
    return notes
