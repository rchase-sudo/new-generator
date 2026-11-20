"""Test fits sketched in Apollo Redline over a Drafted existing-conditions plan.

Redline's test fit is Drafted's own editor, so the design comes in the same shape the Drafted editor sends; only its
coordinates differ (Redline's frame is the PDF sheet in feet). The property line traced on the sheet is matched to the
job's real parcel (rotation + translation + a scale within a few percent, for sheet-scale error), and every piece of the
sketch is carried through that transform into Drafted's layout frame. No AI is involved.
"""
import copy
import math

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import Polygon


def _ring(pts):
    p = Polygon([tuple(map(float, q)) for q in pts])
    return p if p.is_valid else p.buffer(0)


def _sample(poly, step=2.0):
    ring = poly.exterior
    n = max(40, int(ring.length / step))
    return np.array([ring.interpolate(i / n, normalized=True).coords[0] for i in range(n)])


def _umeyama(src, dst):
    """Similarity (s, R, t) with dst ~ s R src + t, least squares (no reflection)."""
    ms, md = src.mean(0), dst.mean(0)
    a, b = src - ms, dst - md
    U, S, Vt = np.linalg.svd(b.T @ a / len(src))
    D = np.eye(2)
    if np.linalg.det(U @ Vt) < 0:
        D[1, 1] = -1
    R = U @ D @ Vt
    var = (a ** 2).sum() / len(src)
    s = float(np.trace(np.diag(S) @ D) / var) if var > 0 else 1.0
    return s, R, md - s * R @ ms


def register(src_poly, dst_poly, scale_tol=0.06):
    """Best similarity transform taking the Redline property line onto the Drafted parcel.
    Returns dict(s, theta, t, rms_ft, fit) where fit = overlap / union of the two outlines after the transform."""
    S, Dp = _sample(src_poly), _sample(dst_poly)
    tree = cKDTree(Dp)
    cs, cd = np.array(src_poly.centroid.coords[0]), np.array(dst_poly.centroid.coords[0])
    s0 = math.sqrt(dst_poly.area / src_poly.area) if src_poly.area > 0 else 1.0
    cands = []
    for k in range(72):                                     # every 5 degrees, then ICP refines
        th = math.radians(5 * k)
        R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
        s, t = s0, cd - s0 * R @ cs
        for _ in range(120):
            Y = (s * (R @ S.T)).T + t
            d, idx = tree.query(Y)
            s, R, t = _umeyama(S, Dp[idx])
            s = min(max(s, 1 - scale_tol), 1 + scale_tol)
        Y = (s * (R @ S.T)).T + t
        d, _ = tree.query(Y)
        cands.append((float(np.sqrt((d ** 2).mean())), s, R, t))
    # a symmetric lot (a rectangle) fits as well turned 180 degrees: among equally good fits take the one whose frontage
    # agrees with Drafted's (both frames run x along the main street), i.e. the smallest rotation
    lo = min(c[0] for c in cands)
    ok = [c for c in cands if c[0] <= max(lo * 1.25, lo + 0.75)]
    ang = lambda c: math.atan2(c[2][1, 0], c[2][0, 0])
    a0 = ang(min(ok, key=lambda c: abs(ang(c))))
    near = [c for c in ok if abs((ang(c) - a0 + math.pi) % (2 * math.pi) - math.pi) < math.radians(25)]
    best = min(near, key=lambda c: c[0])
    rms, s, R, t = best
    th = math.atan2(R[1, 0], R[0, 0])
    # polish: point-to-boundary distances (ICP's point pairs leave a fraction of a degree on long straight edges)
    import shapely
    from scipy.optimize import minimize
    bnd = dst_poly.exterior

    def cost(v):
        th_, tx, ty, sc = v
        c_, s_ = math.cos(th_), math.sin(th_)
        X = sc * (c_ * S[:, 0] - s_ * S[:, 1]) + tx
        Y = sc * (s_ * S[:, 0] + c_ * S[:, 1]) + ty
        d = shapely.distance(shapely.points(X, Y), bnd)
        pen = max(0.0, abs(sc - 1) - scale_tol) * 1e4
        return float((d ** 2).mean()) + pen
    r = minimize(cost, [th, t[0], t[1], s], method="Nelder-Mead", options={"xatol": 1e-6, "fatol": 1e-8, "maxiter": 4000})
    if r.fun <= rms ** 2 + 1e-9:
        th, t, s = float(r.x[0]), np.array(r.x[1:3]), float(r.x[3])
        rms = math.sqrt(r.fun)
    R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    moved = Polygon([tuple(s * (R @ np.array(p)) + t) for p in src_poly.exterior.coords])
    fit = moved.intersection(dst_poly).area / max(moved.union(dst_poly).area, 1e-9)
    return {"s": float(s), "theta": float(th), "t": [float(t[0]), float(t[1])], "rms_ft": rms, "fit": float(fit)}


def transform_design(design, T, frame):
    """The editor's design carried through T = {s, theta, t} into Drafted's frame (given as `frame`)."""
    s, th, (tx, ty) = T["s"], T["theta"], T["t"]
    c, sn = math.cos(th), math.sin(th)
    deg = math.degrees(th)

    def P(p):
        x, y = float(p[0]), float(p[1])
        return [round(s * (c * x - sn * y) + tx, 2), round(s * (sn * x + c * y) + ty, 2)]

    def V(d):
        x, y = float(d[0]), float(d[1])
        return [round(c * x - sn * y, 4), round(sn * x + c * y, 4)]

    def pts(a):
        return [P(q) for q in a or []]

    D = copy.deepcopy(design)
    D["frame"] = frame
    D.pop("parcels", None)
    for b in D.get("buildings") or []:
        b["cx"], b["cy"] = P((b["cx"], b["cy"]))
        b["rot"] = round(float(b.get("rot") or 0) + deg, 3)
        b["w"], b["d"] = round(float(b["w"]) * s, 2), round(float(b["d"]) * s, 2)
        for w in b.get("wings") or []:
            for k in ("u", "v", "w", "d"):
                w[k] = round(float(w[k]) * s, 2)
        if b.get("outline"):
            b["outline"] = pts(b["outline"])
    for f in D.get("parking") or []:
        p = f["params"]
        p["cx"], p["cy"] = P((p["cx"], p["cy"]))
        p["rot"] = round(float(p.get("rot") or 0) + deg, 3)
        p["w"], p["d"] = round(float(p["w"]) * s, 2), round(float(p["d"]) * s, 2)
        for key in ("stalls", "aisles", "islands", "ends"):
            f[key] = [pts(r) for r in f.get(key) or []]
        f["arrows"] = [{**a, "at": P(a["at"]), "dir": V(a["dir"])} for a in f.get("arrows") or []]
    for d in D.get("drives") or []:
        d["pts"] = pts(d.get("pts"))
        if d.get("full"):
            d["full"] = pts(d["full"])
        d["arrows"] = [{**a, "at": P(a["at"]), "dir": V(a["dir"])} for a in d.get("arrows") or []]
    for key in ("ponds", "pavement"):
        for o in D.get(key) or []:
            o["pts"] = pts(o.get("pts"))
    return D


def place(design, parcels, parcel_local, frame):
    """Redline design + its property line(s) -> the design in Drafted's frame, with the registration report."""
    rings = [r for r in parcels or [] if r and len(r) >= 3]
    if not rings:
        raise ValueError("The Redline test fit has no property line")
    src = max((_ring(r) for r in rings), key=lambda p: p.area)
    T = register(src, parcel_local)
    if T["fit"] < 0.85:
        raise ValueError(f"The property line traced in Redline does not match this job's parcel (only {T['fit'] * 100:.0f}% overlap after "
                         f"lining them up). Check the sheet scale and that the sketch was drawn on this project's existing-conditions plan.")
    return transform_design(design, T, frame), T
