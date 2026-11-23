"""Manual test fit: the site laid out by hand in Drafted, then sent down the pipeline exactly like an automatic layout.

base()   -> everything the editor needs, in the same local frame the automatic layout uses (feet; x along the main frontage,
            y into the site): parcel, building and pavement envelopes, constraints, roads, existing buildings and pavement,
            the program and the automatic layout's buildings as a starting point.
build()  -> the editor's design, treated as a sketch that locks intent (building footprints + positions, each parking field's
            position, size, orientation and modules), refined into the same result generate_layout returns ({metrics, sp, local, labels,
            frame_angle_deg}): driveways, curb returns, cross aisles,
            islands, walks, landscaping and pond are drawn the way the automatic layout draws them, so the concept sheet, DXF, 3D model, render, package and full design all work unchanged.

Design (local feet), as the editor sends it:
  buildings: [{id, name, use, label, cx, cy, w, d, rot (deg), stories, drive_thru}]
  parking:   [{id, stalls: [[[x, y] x4], ...], aisles: [[[x, y], ...], ...]}]   (stalls and aisles laid out by the editor)
  drives:    [{id, pts: [[x, y], ...], width}]
  pavement:  [{pts: [[x, y], ...]}]           extra paved areas (loading, plazas)
  ponds:     [{pts: [[x, y], ...]}]           none drawn + auto_pond -> one is placed like the automatic layout does
  zones:     {bz: [[[x, y], ...]], pz: [...]}   Redline only: the building / pavement setback lines as drawn (per-edge setbacks)
  grading:   [{pts: [[x, y], ...], min_pct, max_pct}]   areas graded as one plane within a slope range (flat pads etc.)
  auto_pond: bool, parking_ratio: stalls per 1,000 SF of GFA (for the required count)
"""
import math

import numpy as np
from shapely import affinity, make_valid
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polylabel, unary_union

from .ec_drawing import _polys, classify_edges, envelope, road_key
from .existing import KEEP_CLEAR_FT
from .layout import Frame, ada_required
from .layout2 import WALK, _close, _fillet, _largest, _pond, _short

R_CURB = 10.0       # curb return where a drive meets the road


# ---------------------------------------------------------------- frame + site
def _frame(ctx, setbacks, drive_road=None, frame=None):
    rds = ctx["rds"] if isinstance(ctx["rds"], list) else []
    named = [r for r in rds if r["class"] != "service"]
    keys = [road_key(r) for r in named]
    edges = classify_edges(ctx["parcel_sp"], [r["centerline"] for r in named], ctx["k"], keys)
    main = None
    if drive_road:
        on = [e for e in edges if e["kind"] == "front" and e.get("road") == drive_road]
        main = max(on, key=lambda e: e["len_ft"]) if on else None
    fr = Frame(ctx["parcel_sp"], edges, ctx["k"], main=main)
    if frame:                       # the frame the editor drew in: geometry comes back in exactly the same coordinates
        fr.o, fr.a = tuple(frame["o"]), float(frame["a"])
    return fr, edges, rds, named, keys


def _site(ctx, fr, edges, setbacks, psb, steep, keep_geoms, reuse_geoms):
    k = ctx["k"]
    P = fr.fwd(ctx["parcel_sp"])
    wet = ctx["wet"] if isinstance(ctx["wet"], list) else []
    fl = ctx["fl"] if isinstance(ctx["fl"], list) else []
    KEEP = unary_union([g.buffer(KEEP_CLEAR_FT / k) for g in keep_geoms]) if keep_geoms else None
    RP = unary_union(list(reuse_geoms)).intersection(ctx["parcel_sp"]) if reuse_geoms else None
    if RP is not None and RP.is_empty:
        RP = None
    C = unary_union([w["geom"] for w in wet] + [f["geom"] for f in fl if f.get("sfha") == "T"] + ([KEEP] if KEEP is not None else []) + list(steep or []))
    C = fr.fwd(C.intersection(ctx["parcel_sp"].buffer(50 / k))) if not C.is_empty else Polygon()
    bz = fr.fwd(envelope(ctx["parcel_sp"], edges, setbacks, k)).difference(C).buffer(0)
    pz = fr.fwd(envelope(ctx["parcel_sp"], edges, psb, k)).difference(C).buffer(0)
    return P, C, bz, pz, (fr.fwd(RP) if RP is not None else None)


# ---------------------------------------------------------------- base for the editor
def _rings(g, tol=0.5, nd=1):
    out = []
    for p in _polys(g) if g is not None and not g.is_empty else []:
        p = p.simplify(tol)
        if p.is_empty or p.area < 1:
            continue
        out.append([[[round(x, nd), round(y, nd)] for x, y in r.coords] for r in [p.exterior, *p.interiors]])
    return out


def _line(g, tol=0.5):
    out = []
    for ln in (getattr(g, "geoms", None) or [g]) if g is not None and not g.is_empty else []:
        if ln.geom_type == "LineString":
            out.append([[round(x, 1), round(y, 1)] for x, y in ln.simplify(tol).coords])
    return out


def rect_of(poly):
    """Centre, width (along the long side), depth and rotation (deg) of a building footprint."""
    mrr = poly.minimum_rotated_rectangle
    cs = list(mrr.exterior.coords)[:4]
    e1 = (cs[1][0] - cs[0][0], cs[1][1] - cs[0][1])
    e2 = (cs[2][0] - cs[1][0], cs[2][1] - cs[1][1])
    l1, l2 = math.hypot(*e1), math.hypot(*e2)
    v, w, d = (e1, l1, l2) if l1 >= l2 else (e2, l2, l1)
    rot = math.degrees(math.atan2(v[1], v[0]))
    rot = (rot + 90) % 180 - 90
    c = mrr.centroid
    return {"cx": round(c.x, 1), "cy": round(c.y, 1), "w": round(w, 1), "d": round(d, 1), "rot": round(rot, 2)}


def base(ctx, setbacks, psb, steep, keep, demo, pave_keep, pave_rm, prog, rules, drive_road=None, seed_layout=None):
    """seed_layout = {sp, metrics} of the current layout (state plane): its buildings and pond open in the editor."""
    fr, edges, rds, named, keys = _frame(ctx, setbacks, drive_road)
    P, C, bz, pz, RP = _site(ctx, fr, edges, setbacks, psb, steep, [b["geom"] for b in keep], [p["geom"] for p in pave_keep])
    view = P.buffer(500)
    wet = ctx["wet"] if isinstance(ctx["wet"], list) else []
    fl = ctx["fl"] if isinstance(ctx["fl"], list) else []
    roads = []
    for r in rds:
        pv = fr.fwd(r["pavement"]).intersection(view) if r.get("pavement") is not None else None
        cl = fr.fwd(r["centerline"]).intersection(view)
        if (pv is None or pv.is_empty) and cl.is_empty:
            continue
        roads.append({"key": road_key(r) if r["class"] != "service" else None, "name": r.get("name"), "class": r["class"],
                      "pave": _rings(pv, 1.0) if pv is not None else [], "cl": _line(cl, 1.0)})
    fronts = [{"road": e.get("road"), "len_ft": round(e["len_ft"]), "line": _line(fr.fwd(e["line"]))} for e in edges if e["kind"] == "front"]
    a = ctx.get("aerial") or {}
    b = {
        "frame": {"o": list(fr.o), "a": fr.a, "k": fr.k, "drive_road": drive_road},
        "parcel": _rings(P, 0.2), "parcel_sf": round(P.area), "bz": _rings(bz), "pz": _rings(pz),
        "wet": _rings(fr.fwd(unary_union([w["geom"] for w in wet])).intersection(view)) if wet else [],
        "flood": _rings(fr.fwd(unary_union([f["geom"] for f in fl if f.get("sfha") == "T"])).intersection(view)) if any(f.get("sfha") == "T" for f in fl) else [],
        "steep": _rings(fr.fwd(unary_union(list(steep))).intersection(P)) if steep else [],
        "keep_bldgs": [{"id": x["id"], "rings": _rings(fr.fwd(x["geom"]), 0.2)} for x in keep],
        "demo_bldgs": [{"id": x["id"], "rings": _rings(fr.fwd(x["geom"]), 0.2)} for x in demo],
        "reuse_pave": [{"id": x["id"], "rings": _rings(fr.fwd(x["geom"]))} for x in pave_keep],
        "roads": roads, "fronts": fronts,
        "aerial": {"extent": list(a.get("extent") or []), "k": ctx["k"]},
        "setbacks": setbacks, "pave_setbacks": psb,
        "program": {"use": prog.get("use"), "buildings": prog.get("buildings") or [], "parking": prog.get("parking") or {}},
        "limits": {k_: rules.get(k_) for k_ in ("max_lot_coverage_pct", "max_impervious_pct", "max_far", "max_height_ft", "max_stories") if rules.get(k_) is not None},
    }
    gfa0 = sum(float(x.get("footprint_sf") or 0) * float(x.get("stories") or 1) for x in b["program"]["buildings"])
    req0 = (prog.get("parking") or {}).get("required")
    b["parking_ratio"] = round(float(req0) / gfa0 * 1000, 2) if isinstance(req0, (int, float)) and req0 > 0 and gfa0 > 0 else None
    if seed_layout and seed_layout.get("sp"):
        sp_ = seed_layout["sp"]
        loc = {"bldgs": [fr.fwd(g) for g in sp_.get("bldgs") or []], "pond": fr.fwd(sp_["pond"]) if sp_.get("pond") is not None else None}
        b["seed"] = seed_from({"local": loc, "metrics": seed_layout.get("metrics") or {}}, prog)
    return b


def seed_from(res, prog):
    """The automatic layout's buildings (and pond) as a starting point for the editor."""
    loc = res.get("local") or {}
    bl = (res.get("metrics") or {}).get("buildings") or []
    pb = prog.get("buildings") or []
    out = {"buildings": [], "ponds": []}
    for i, g in enumerate(loc.get("bldgs") or []):
        m = bl[i] if i < len(bl) else {}
        pr = next((x for x in pb if x.get("name") == m.get("name")), {})
        out["buildings"].append({"id": f"b{i + 1}", "name": m.get("name") or f"Building {chr(65 + i)}", "use": m.get("use") or pr.get("use"),
                                 "label": pr.get("label"), "stories": m.get("stories") or pr.get("stories") or 1, "drive_thru": bool(m.get("drive_thru")), **rect_of(g)})
    if loc.get("pond") is not None and not loc["pond"].is_empty:
        out["ponds"].append({"id": "p1", "pts": _rings(loc["pond"], 1.0)[0][0]})
    return out


# ---------------------------------------------------------------- design -> layout result
def _poly(pts):
    try:
        p = Polygon([tuple(map(float, q)) for q in pts]).buffer(0)
        return p if not p.is_empty else None
    except Exception:  # noqa: BLE001
        return None


def _bldg_parts(b):
    """Main block + wings (rectangles in the block's own axes, centred at u, v), in the block's local frame."""
    w, d = float(b["w"]), float(b["d"])
    out = [box(-w / 2, -d / 2, w / 2, d / 2)]
    for wg in b.get("wings") or []:
        u, v, ww, wd = (float(wg[k]) for k in ("u", "v", "w", "d"))
        if ww > 0 and wd > 0:
            out.append(box(u - ww / 2, v - wd / 2, u + ww / 2, v + wd / 2))
    return out


def _bldg_poly(b):
    g = unary_union(_bldg_parts(b)).buffer(0.5, join_style=2).buffer(-0.5, join_style=2)   # wings flush with the block (to rounding) merge cleanly
    g = max(_polys(g), key=lambda q: q.area) if g.geom_type != "Polygon" else g
    g = Polygon(g.exterior)                       # a building footprint has no holes
    g = affinity.rotate(g, float(b.get("rot") or 0), origin=(0, 0))
    return affinity.translate(g, float(b["cx"]), float(b["cy"]))


def _bldg_box(b):
    xs = [c for p in _bldg_parts(b) for c in (p.bounds[0], p.bounds[2])]
    ys = [c for p in _bldg_parts(b) for c in (p.bounds[1], p.bounds[3])]
    return max(xs) - min(xs), max(ys) - min(ys)


def arrow_poly(at, d, L=14.0):
    """Pavement-marking traffic arrow (stem + head) centred on `at`, pointing along `d` (feet)."""
    m = math.hypot(d[0], d[1]) or 1.0
    ux, uy = d[0] / m, d[1] / m
    nx, ny = -uy, ux
    pts = [(-L / 2, -0.8), (L / 2 - 4.5, -0.8), (L / 2 - 4.5, -2.6), (L / 2, 0), (L / 2 - 4.5, 2.6), (L / 2 - 4.5, 0.8), (-L / 2, 0.8)]
    return Polygon([(at[0] + ux * x + nx * y, at[1] + uy * x + ny * y) for x, y in pts])


def _drive_poly(dv, road_pave, P):
    pts = [tuple(map(float, q)) for q in dv.get("pts") or []]
    if len(pts) < 2:
        return None
    w = float(dv.get("width") or 24.0)
    # an end that stops short of the road is carried straight on until it reaches the road pavement (up to 150 ft)
    for end in (0, -1):
        a, b = (pts[0], pts[1]) if end == 0 else (pts[-1], pts[-2])
        if road_pave is None or Point(a).distance(road_pave) < 1.0 or Point(a).within(P.buffer(-w)):
            continue
        dx, dy = a[0] - b[0], a[1] - b[1]
        L = math.hypot(dx, dy) or 1.0
        far = (a[0] + dx / L * 150, a[1] + dy / L * 150)
        hit = LineString([a, far]).intersection(road_pave)
        if not hit.is_empty:
            q = min((Point(c) for g in getattr(hit, "geoms", [hit]) for c in g.coords), key=lambda c: c.distance(Point(a)))
            q = (q.x + dx / L * 2.0, q.y + dy / L * 2.0)
            if end == 0:
                pts.insert(0, q)
            else:
                pts.append(q)
    ln = LineString(pts)
    d = ln.buffer(w / 2, cap_style=2, join_style=1)
    if road_pave is not None and d.intersects(road_pave):      # curb returns into the road
        u = unary_union([d, road_pave.intersection(d.buffer(R_CURB * 2.5))])
        fil = u.buffer(R_CURB, join_style=1).buffer(-R_CURB, join_style=1)
        d = unary_union([d, make_valid(fil).buffer(0).intersection(d.buffer(R_CURB + 1.0))]).difference(road_pave).buffer(0)
    return make_valid(d).buffer(0)


# ---------------------------------------------------------------- refinement: the sketch locks intent, the concept is drawn properly
SW, SD, AIS, AIS1, ISL = 9.0, 18.0, 24.0, 20.0, 10
R_C = 36.0          # centerline radius where a drive bends
R_J = 6.0           # fillet where drives and aisles meet
R_EDGE = 9.0        # rounding on the outside corners of drives and aisles
MIN_CONV = 6        # a run this long gives up its end stall for an island when there is no room outside it


def _field_xf(prm):
    """Field-local (u along the rows, v across them) -> local feet, as the editor's F(u, v); returns (fwd, inv) matrices."""
    a = math.radians(float(prm.get("rot") or 0))
    c, s = math.cos(a), math.sin(a)
    cx, cy = float(prm["cx"]), float(prm["cy"])
    m = np.array([[-s, c], [c, s]]) if prm.get("flip") else np.array([[c, -s], [s, c]])
    mi = np.linalg.inv(m)
    t = mi @ np.array([cx, cy])
    fwd = [m[0, 0], m[0, 1], m[1, 0], m[1, 1], cx, cy]
    inv = [mi[0, 0], mi[0, 1], mi[1, 0], mi[1, 1], -t[0], -t[1]]
    return fwd, inv


def _field(prm, pz, block):
    """One parking field rebuilt from its locked parameters (position, size, orientation, flip, one-way, islands):
    the same row/aisle modules as the editor, stalls kept inside the pavement envelope and clear of buildings + walks,
    planted islands at the end of every row and every N stalls, aisles joined by 24 ft cross aisles at the field ends.
    Returns stalls, aisles, cross aisles, islands and traffic arrows in local feet."""
    fwd, inv = _field_xf(prm)
    W, D = (float(prm["d"]), float(prm["w"])) if prm.get("flip") else (float(prm["w"]), float(prm["d"]))
    A = float(prm["aisle"]) if prm.get("aisle") and float(prm["aisle"]) >= 18 else (AIS1 if prm.get("oneway") else AIS)
    isl = prm.get("islands")
    every = 10 ** 9 if isl == 0 else int(isl or ISL)
    pzl = affinity.affine_transform(pz, inv).buffer(0.5)
    bl = affinity.affine_transform(block, inv) if block is not None and not block.is_empty else Polygon()
    rows, v = [], -D / 2
    while v + 2 * SD + A <= D / 2 + 0.01:
        rows += [("s", v, v + SD), ("a", v + SD, v + SD + A), ("s", v + SD + A, v + 2 * SD + A)]
        v += 2 * SD + A
    if v + SD + A <= D / 2 + 0.01:
        rows += [("s", v, v + SD), ("a", v + SD, v + SD + A)]
    n_sl = int((W + 0.01) // SW)
    u0 = -W / 2

    def ok(g):
        return pzl.contains(g) and not g.intersects(bl)

    def layout(res_lo, res_hi):
        """res_lo / res_hi: u ranges kept clear for a cross aisle carved inside the field (None = none)."""
        def clear(g):
            b = g.bounds
            return not ((res_lo and b[0] < res_lo[1] - 0.01 and b[2] > res_lo[0] + 0.01) or (res_hi and b[0] < res_hi[1] - 0.01 and b[2] > res_hi[0] + 0.01))
        stalls, islands, span = [], [], {}
        for ri, (k, v0, v1) in enumerate(rows):
            if k != "s":
                continue
            st = []
            for i in range(n_sl):
                g = box(u0 + i * SW, v0, u0 + (i + 1) * SW, v1)
                good = ok(g) and clear(g)
                st.append(("I" if (i > 0 and i % (every + 1) == every) else "S") if good else "X")
            runs, cur = [], []
            for i, t in enumerate(st + ["X"]):
                if t == "X":
                    if cur:
                        runs.append(cur)
                    cur = []
                else:
                    cur.append(i)
            lo_, hi_ = math.inf, -math.inf
            for run in runs:
                while run and st[run[0]] == "I":
                    run = run[1:]
                while run and st[run[-1]] == "I":
                    run = run[:-1]
                ns = sum(1 for i in run if st[i] == "S")
                if not ns:
                    continue
                ends = []
                for side in (0, -1):
                    i = run[side]
                    j = i - 1 if side == 0 else i + 1
                    g = box(u0 + j * SW, v0, u0 + (j + 1) * SW, v1)
                    if ok(g) and clear(g):
                        ends.append(g)             # room past the end of the row: island outside it
                    elif ns >= MIN_CONV:
                        st[i] = "I"                # no room: the end stall becomes the island
                        ends.append(box(u0 + i * SW, v0, u0 + (i + 1) * SW, v1))
                        ns -= 1
                for i in run:
                    g = box(u0 + i * SW, v0, u0 + (i + 1) * SW, v1)
                    (stalls if st[i] == "S" else islands).append(g)
                islands += [e for e in ends if not any(e.equals(q) for q in islands)]
                xs = [g.bounds for g in ends] + [box(u0 + run[0] * SW, v0, u0 + (run[-1] + 1) * SW, v1).bounds]
                lo_, hi_ = min(lo_, min(b[0] for b in xs)), max(hi_, max(b[2] for b in xs))
            span[ri] = (lo_, hi_)
        aisles = []
        for ri, (k, v0, v1) in enumerate(rows):
            if k != "a":
                continue
            ss = [span[j] for j in (ri - 1, ri + 1) if j in span and span[j][1] > span[j][0]]
            if ss:
                aisles.append([min(s[0] for s in ss), max(s[1] for s in ss), v0, v1])
        return stalls, islands, aisles

    stalls, islands, aisles = layout(None, None)
    cross = []
    if len(aisles) >= 2 or (aisles and prm.get("oneway")):
        res = [None, None]
        for side in (0, 1):
            e = min(a[0] for a in aisles) if side == 0 else max(a[1] for a in aisles)
            vlo, vhi = min(a[2] for a in aisles), max(a[3] for a in aisles)
            out = box(e - AIS, vlo, e, vhi) if side == 0 else box(e, vlo, e + AIS, vhi)
            if ok(out):
                cross.append((side, out))
            else:
                res[side] = (e, e + AIS) if side == 0 else (e - AIS, e)
        if res[0] or res[1]:
            stalls, islands, aisles = layout(*res)
            for side in (0, 1):
                if res[side] and aisles:
                    e = min(a[0] for a in aisles) if side == 0 else max(a[1] for a in aisles)
                    vlo, vhi = min(a[2] for a in aisles), max(a[3] for a in aisles)
                    g = box(e - AIS, vlo, e, vhi) if side == 0 else box(e, vlo, e + AIS, vhi)
                    if pzl.buffer(2).contains(g) and not g.intersects(bl):
                        cross.append((side, g))
        for side, g in cross:              # every aisle runs on to the cross aisle
            for a in aisles:
                if side == 0:
                    a[0] = min(a[0], g.bounds[2])
                else:
                    a[1] = max(a[1], g.bounds[0])
    arrows = []
    sg0 = -1 if prm.get("rev") else 1
    for ai, (a0, a1, v0, v1) in enumerate(aisles):
        vm, L = (v0 + v1) / 2, a1 - a0
        n = max(1, round(L / 70))
        sg = (-1 if ai % 2 else 1) * sg0
        for i in range(n):
            um = a0 + L * (i + 0.5) / n
            if prm.get("oneway"):
                arrows.append(((um, vm), (sg, 0)))
            else:
                arrows += [((um - 9, vm - A / 4), (1, 0)), ((um + 9, vm + A / 4), (-1, 0))]
    for side, g in cross:
        b = g.bounds
        um, L = (b[0] + b[2]) / 2, b[3] - b[1]
        n = max(1, round(L / 90))
        for i in range(n):
            vm = b[1] + L * (i + 0.5) / n
            arrows += [((um + AIS / 4, vm - 9), (0, 1)), ((um - AIS / 4, vm + 9), (0, -1))]

    def X(g):
        return affinity.affine_transform(g, fwd)
    M = np.array([[fwd[0], fwd[1]], [fwd[2], fwd[3]]])
    arr = [(tuple(M @ np.array(p) + np.array(fwd[4:])), tuple(M @ np.array(d))) for p, d in arrows]
    return {"stalls": [X(g) for g in stalls], "islands": [X(g) for g in islands], "aisles": [X(box(a0, v0, a1, v1)) for a0, a1, v0, v1 in aisles],
            "cross": [X(g) for _, g in cross], "arrows": arr, "oneway": bool(prm.get("oneway"))}


def _smooth(pts, rc=R_C):
    """Polyline with each bend replaced by a curve of (up to) radius rc on the centerline."""
    if len(pts) < 3:
        return list(pts)
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = (np.array(p, float) for p in (pts[i - 1], pts[i], pts[i + 1]))
        l1, l2 = np.linalg.norm(b - a), np.linalg.norm(c - b)
        if l1 < 1e-6 or l2 < 1e-6:
            continue
        u1, u2 = (b - a) / l1, (c - b) / l2
        th = math.acos(max(-1.0, min(1.0, float(u1 @ u2))))
        if th < math.radians(3):
            out.append(tuple(b))
            continue
        t = min(rc * math.tan(th / 2), l1 * 0.5 if i > 1 else l1 * 0.9, l2 * 0.5 if i < len(pts) - 2 else l2 * 0.9)
        p0, p2 = b - u1 * t, b + u2 * t
        for s in np.linspace(0, 1, 14):
            q = (1 - s) ** 2 * p0 + 2 * (1 - s) * s * b + s ** 2 * p2
            out.append(tuple(q))
    out.append(pts[-1])
    return out


def _curb_returns(at, d, w, road_pave, r=R_CURB):
    """The automatic driveway's 10 ft curb returns where a drive meets the road, for a drive leaving the road along d."""
    m = math.hypot(*d) or 1.0
    ux, uy = d[0] / m, d[1] / m
    nx, ny = -uy, ux
    h = w / 2 + r
    fl = box(-h, -8, h, r).difference(Point(-h, r).buffer(r, 48)).difference(Point(h, r).buffer(r, 48))
    fl = affinity.affine_transform(fl, [nx, ux, ny, uy, at[0], at[1]])
    return make_valid(fl.difference(road_pave)).buffer(0) if road_pave is not None else fl


def _drive_refined(dv, road_pave, P, circ_hint, B=None):
    """A drawn drive as the automatic layout would build it: carried to the road, bends rounded, 10 ft curb returns
    at the road, its inside end carried on to the parking it serves."""
    pts = [tuple(map(float, q)) for q in dv.get("pts") or []]
    pts = [p for i, p in enumerate(pts) if i == 0 or math.dist(p, pts[i - 1]) > 1.0]
    if len(pts) < 2:
        return None
    w = max(20.0, float(dv.get("width") or AIS))
    if road_pave is not None and Point(pts[-1]).distance(road_pave) < Point(pts[0]).distance(road_pave):
        pts = pts[::-1]                                     # road end first
    # road end: carried straight on to the road pavement (up to 150 ft), entering it 2 ft
    if road_pave is not None:
        a, b = pts[0], pts[1]
        dx, dy = a[0] - b[0], a[1] - b[1]
        L = math.hypot(dx, dy) or 1.0
        if not Point(a).within(road_pave):
            hit = LineString([a, (a[0] + dx / L * 150, a[1] + dy / L * 150)]).intersection(road_pave)
            if not hit.is_empty:
                q = min((Point(c) for g in getattr(hit, "geoms", [hit]) for c in g.coords), key=lambda c: c.distance(Point(a)))
                pts[0] = (q.x + dx / L * 2.0, q.y + dy / L * 2.0)
        else:      # starts inside the road: pull back to its edge
            ln = LineString([a, b])
            ex = ln.difference(road_pave)
            if not ex.is_empty:
                cs = [c for g in getattr(ex, "geoms", [ex]) for c in g.coords]
                q = min(cs, key=lambda c: math.dist(c, a))
                pts[0] = (q[0] + dx / L * 2.0, q[1] + dy / L * 2.0)
    # site end: carried on (up to 60 ft) until it meets the parking it serves
    if circ_hint is not None and not circ_hint.is_empty and Point(pts[-1]).distance(circ_hint) > 0.5:
        a, b = pts[-1], pts[-2]
        dx, dy = a[0] - b[0], a[1] - b[1]
        L = math.hypot(dx, dy) or 1.0
        ext = LineString([a, (a[0] + dx / L * 60, a[1] + dy / L * 60)])
        hit = ext.intersection(circ_hint)
        if not hit.is_empty and (B is None or not ext.intersects(B)):
            q = min((Point(c) for g in getattr(hit, "geoms", [hit]) for c in g.coords), key=lambda c: c.distance(Point(a)))
            pts[-1] = (q.x + dx / L * (w / 2), q.y + dy / L * (w / 2))
        else:      # the parking is off to one side: turn into it at the nearest point
            from shapely.ops import nearest_points
            q = nearest_points(circ_hint, Point(a))[0]
            link = LineString([a, (q.x, q.y)])
            if link.length < 150 and (B is None or not link.intersects(B)):
                ex, ey = q.x - a[0], q.y - a[1]
                m = math.hypot(ex, ey) or 1.0
                pts.append((q.x + ex / m * (w / 2), q.y + ey / m * (w / 2)))
    cl = LineString(_smooth(pts))
    body = cl.buffer(w / 2, cap_style=2, join_style=1)
    ret = Polygon()
    if road_pave is not None and body.intersects(road_pave):
        seg = LineString(pts[:2])
        e = seg.intersection(road_pave.boundary)
        cs = [c for g in getattr(e, "geoms", [e]) for c in getattr(g, "coords", [])]
        if cs:
            at = max(cs, key=lambda c: math.dist(c, pts[0]))   # where the drive leaves the road
            ret = _curb_returns(at, (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]), w, road_pave)
        body = body.difference(road_pave)
    arrows = []
    L = cl.length
    n = max(1, round(L / 90)) if L >= 45 else 0
    for k in range(n):
        t = L * (k + 0.5) / n
        p, p2 = cl.interpolate(max(0, t - 1)), cl.interpolate(min(L, t + 1))
        ux, uy = p2.x - p.x, p2.y - p.y
        m = math.hypot(ux, uy) or 1.0
        ux, uy = ux / m, uy / m
        nx, ny = -uy, ux
        c = cl.interpolate(t)
        arrows += [((c.x - nx * w / 4 - ux * 9, c.y - ny * w / 4 - uy * 9), (ux, uy)), ((c.x + nx * w / 4 + ux * 9, c.y + ny * w / 4 + uy * 9), (-ux, -uy))]
    return {"body": make_valid(body).buffer(0), "returns": ret, "arrows": arrows, "cl": cl, "w": w}


def build(ctx, design, setbacks, psb, steep, keep, pave_keep, prog, rules, shared_stalls=0):
    fr, edges, rds, named, keys = _frame(ctx, setbacks, frame=design.get("frame"))
    P, C, bz, pz, RPf = _site(ctx, fr, edges, setbacks, psb, steep, [b["geom"] for b in keep], [p["geom"] for p in pave_keep])
    zones = design.get("zones") or {}
    for key in ("bz", "pz"):            # Redline Sitecraft: setback lines set edge by edge (and along curved property lines)
        zs = [p for p in (_poly(r) for r in zones.get(key) or [] if r and len(r) >= 3) if p is not None and not p.is_empty]
        if zs:
            z_ = unary_union(zs).intersection(P).difference(C).buffer(0)
            if key == "bz":
                bz = z_
            else:
                pz = z_
    road_pave = unary_union([make_valid(fr.fwd(r["pavement"])).buffer(0) for r in rds if r.get("pavement") is not None]) if rds else None
    if road_pave is not None:
        road_pave = road_pave.intersection(P.buffer(400)).buffer(0)
        if road_pave.is_empty:
            road_pave = None
    warn = []
    # buildings
    rows = []
    for i, b in enumerate(design.get("buildings") or []):
        g = _bldg_poly(b)
        if g.area < 100:
            continue
        rows.append({"b": {"name": b.get("name") or f"Building {chr(65 + i)}", "use": b.get("use"), "label": b.get("label") or b.get("use"),
                           "stories": int(b.get("stories") or 1), "drive_thru": bool(b.get("drive_thru"))},
                     "rect": g, "w": _bldg_box(b)[0], "d": _bldg_box(b)[1], "bw": float(b["w"]), "bd": float(b["d"]),
                     "rot": float(b.get("rot") or 0), "fp": g.area, "cx": float(b["cx"]), "cy": float(b["cy"]), "irregular": bool(b.get("wings"))})
        if not bz.buffer(0.5).contains(g):
            warn.append(f"{rows[-1]['b']['name']} is outside the building setbacks")
        if not C.is_empty and g.intersects(C):
            warn.append(f"{rows[-1]['b']['name']} sits on a constraint (wetland, flood zone, steep ground or an existing building)")
    camp = _camp(design)
    if not rows and not camp["sites"]:
        raise RuntimeError("The test fit has no buildings or campsites")
    bldgs = [r["rect"] for r in rows]
    B = unary_union(bldgs + camp["units"]) if bldgs or camp["units"] else Polygon()
    # The test fit is a sketch that locks intent: building footprints and positions, and each parking field's position,
    # size, orientation and modules. Everything else is drawn the way the automatic layout draws it.
    # parking: each field rebuilt from its parameters, stalls clear of buildings + their walks
    block = unary_union([B.buffer(WALK, join_style=1)] + ([C] if not C.is_empty else []))
    fields, n_sketch = [], 0
    for f in design.get("parking") or []:
        n_sketch += len(f.get("stalls") or [])
        prm = f.get("params")
        if prm and all(prm.get(k_) is not None for k_ in ("cx", "cy", "w", "d")):
            fields.append(_field(prm, pz, block))
        else:      # an old design without field parameters: its stalls and aisles as drawn
            st_ = [p for p in (_poly(s_) for s_ in f.get("stalls") or []) if p is not None and p.area > 20 and not p.intersects(block)]
            ai_ = [p.difference(B) for p in (_poly(a_) for a_ in f.get("aisles") or []) if p is not None]
            fields.append({"stalls": st_, "islands": [], "aisles": ai_, "cross": [], "oneway": False,
                           "arrows": [(tuple(map(float, a_["at"])), tuple(map(float, a_["dir"]))) for a_ in f.get("arrows") or []]})
    stalls = [g for f in fields for g in f["stalls"]]
    islands = [g for f in fields for g in f["islands"]]
    aisles = [g for f in fields for g in f["aisles"] + f["cross"]]
    arrows = [a for f in fields for a in f["arrows"]]
    lot = unary_union(aisles + stalls) if aisles or stalls else Polygon()
    # drives: rebuilt like the automatic driveway (rounded bends, curb returns at the road, joined to the parking)
    drs = []
    for dv in design.get("drives") or []:
        d_ = _drive_refined(dv, road_pave, P, unary_union(aisles) if aisles else lot, B)
        if d_ is None:
            continue
        d_["surface"] = dv.get("surface") or "paved"
        if dv.get("oneway"):        # one-way loops: single arrows the way the sketch drew them
            d_["arrows"] = [(tuple(map(float, a_["at"])), tuple(map(float, a_["dir"]))) for a_ in dv.get("arrows") or []]
        drs.append(d_)
    if not drs and not lot.is_empty and road_pave is not None:
        # none drawn: straight in from the nearest point of the road, like the automatic layout
        from shapely.ops import nearest_points
        tgt = unary_union(aisles) if aisles else lot
        a_, b_ = nearest_points(tgt, road_pave)
        dv = {"pts": [(b_.x, b_.y), (a_.x, a_.y)], "width": AIS}
        d0 = _drive_refined(dv, road_pave, P, tgt, B)
        if d0 is not None and not d0["body"].intersects(B):
            drs.append(d0)
            warn.append("No driveway was drawn: one was added from the nearest road")
    drive_body = unary_union([d["body"] for d in drs]) if drs else Polygon()
    returns = unary_union([d["returns"] for d in drs if not d["returns"].is_empty]) if drs else Polygon()
    arrows += [a for d in drs for a in d["arrows"]]
    if not drive_body.is_empty:       # the drive wins over any stall or island it crosses
        dcut = drive_body.buffer(1.0)
        stalls = [g for g in stalls if g.intersection(dcut).area < 0.05 * g.area]
        islands = [g for g in islands if g.intersection(dcut).area < 0.05 * g.area]
        if drive_body.intersects(B):
            warn.append("A driveway runs into a building")
    extra = [p for p in (_poly(x.get("pts") or []) for x in design.get("pavement") or []) if p is not None]
    # pavement: circulation with rounded outside corners and filleted junctions, stalls crisp, islands left open
    circ = unary_union(aisles + extra + ([drive_body] if not drive_body.is_empty else []))
    circ = make_valid(circ).buffer(0)
    circ = unary_union([_fillet(g, R_EDGE) for g in _polys(circ)]) if not circ.is_empty else circ
    circ = make_valid(_close(circ, R_J)).buffer(0)
    pave = make_valid(unary_union([circ, *stalls])).buffer(0)
    pave = make_valid(_close(pave, 3.0)).buffer(0)           # rounded island noses
    isl_u = unary_union(islands).buffer(0) if islands else Polygon()
    if not isl_u.is_empty:
        pave = pave.difference(_fillet(isl_u, 3.0)).buffer(0)
    pave = unary_union([pave, returns]).buffer(0) if not returns.is_empty else pave
    pave = pave.difference(B).buffer(0)
    drive = unary_union([drive_body, returns]).buffer(0) if drs else Polygon()
    # campground: gravel drives and the site pads / car spots are graded and drained with the pavement but drawn as gravel
    gdr = [d["body"] for d in drs if d.get("surface") == "gravel"]
    gravel = unary_union(gdr + camp["gravel"]).buffer(0) if gdr or camp["gravel"] else Polygon()
    if camp["paved"]:
        pave = unary_union([pave] + camp["paved"]).buffer(0)
    if not gravel.is_empty:
        pave = unary_union([pave, gravel]).buffer(0).difference(B).buffer(0)
    keep_in = pz.union(drive.buffer(R_J + 2)) if not drive.is_empty else pz
    pave = pave.intersection(keep_in.union(unary_union(extra)) if extra else keep_in).buffer(0)
    if road_pave is not None:
        pave = pave.difference(road_pave).buffer(0)
    if RPf is not None:
        pave = pave.difference(RPf).buffer(0)
    out_pz = unary_union([g for f in fields for g in f["stalls"] + f["aisles"]]).difference(pz.buffer(1.0)) if stalls else Polygon()
    if out_pz.area > 200:
        warn.append(f"About {out_pz.area:,.0f} SF of the sketched parking was inside the pavement setbacks and was left out")
    if n_sketch and len(stalls) < 0.9 * n_sketch:
        warn.append(f"Refining the parking left {len(stalls)} stalls of the {n_sketch} sketched (islands, setbacks and walks)")
    if not drive.is_empty:
        off = drive.difference(P.buffer(1.0))
        if road_pave is not None:
            off = off.difference(road_pave.buffer(100.0))   # the stretch across the right-of-way to the road is expected
        if off.area > 300:
            warn.append("The driveway runs outside the property (over a neighbor or into the right-of-way away from the road)")
    if drive.is_empty:
        warn.append("No driveway: the site is not connected to a road")
    elif road_pave is not None and not drive.buffer(3.0).intersects(road_pave):
        warn.append("The driveway does not reach the road")
    # walks: all round each building, rounded corners, as the automatic layout draws them
    walks = unary_union([r["rect"].buffer(WALK, join_style=1).difference(r["rect"]) for r in rows]).difference(pave).intersection(P).buffer(0) if rows else Polygon()
    bldgs = bldgs + camp["units"]                 # cabins are buildings for grading (floor pads), roofs and services
    dev = unary_union([pave, walks, *bldgs])
    grass = _fillet(dev.buffer(14, join_style=1), 6).intersection(P).difference(C).difference(dev)
    holes = unary_union([Polygon(h) for p in _polys(pave) for h in p.interiors]) if not pave.is_empty else Polygon()
    grass = unary_union([grass, holes.difference(dev), isl_u.difference(dev)])
    imperv_sf = pave.area + walks.area + sum(b.area for b in bldgs) + (RPf.area if RPf is not None else 0.0)
    ponds = [p for p in (_poly(x.get("pts") or []) for x in design.get("ponds") or []) if p is not None]
    pond = None
    if ponds:      # where it was sketched, shaped like the automatic pond: off the paving, organic edge
        g = unary_union(ponds).intersection(P.buffer(-10)).difference(dev.buffer(12)).difference(C.buffer(10) if not C.is_empty else Polygon())
        g = g.buffer(8, join_style=1).buffer(-14, join_style=1).buffer(6, join_style=1)
        pond = _largest(g)
        if pond is None or pond.area < 0.5 * unary_union(ponds).area:
            pond = _largest(unary_union(ponds).intersection(P).buffer(6, join_style=1).buffer(-6, join_style=1)) or unary_union(ponds).intersection(P)
    elif design.get("auto_pond", True) and not prog.get("no_pond"):
        land = [g for g in _polys(P.difference(C)) if g.intersects(dev)]
        pond = _pond(unary_union(land) if land else P, dev.union(grass), C, max(3000.0, imperv_sf * 0.08))
    if pond is not None and not pond.is_empty:
        grass = grass.difference(pond)
        if pond.intersects(dev):
            warn.append("The pond overlaps paving or a building")
    pad = dev.buffer(14, join_style=2, mitre_limit=1.6).simplify(4).intersection(P).difference(C)
    pad = _largest(pad) or grass
    # striping: the two long sides of every stall
    stripes = []
    for st in stalls:
        cs = list(st.exterior.coords)
        es = sorted(((math.hypot(b[0] - a[0], b[1] - a[1]), a, b) for a, b in zip(cs, cs[1:])), key=lambda t: -t[0])[:2]
        stripes += [LineString([a, b]) for _, a, b in es]
    n_st = len(stalls)
    n_ada = ada_required(n_st) if n_st else 0
    fronts = walks if not walks.is_empty else B
    ada = sorted(stalls, key=lambda s_: s_.centroid.distance(fronts))[:n_ada]
    # trees: one in each planted island (and any other hole in the paving), then around the edge of the development
    trees = []
    for g in _polys(unary_union([holes, isl_u]).difference(dev.buffer(1.0)).buffer(0)):
        if g.area >= 60:
            mrr = g.minimum_rotated_rectangle
            cs = list(mrr.exterior.coords)
            short_ = min(math.dist(cs[0], cs[1]), math.dist(cs[1], cs[2]))
            long_ = max(math.dist(cs[0], cs[1]), math.dist(cs[1], cs[2]))
            if short_ < 4.5:
                continue
            if long_ > 60 and short_ < 14:      # long medians: one every ~40 ft
                axis = LineString(cs[0:2] if math.dist(cs[0], cs[1]) >= math.dist(cs[1], cs[2]) else cs[1:3])
                c0 = mrr.centroid
                m0 = axis.interpolate(0.5, normalized=True)
                for i in range(max(1, int(long_ // 40))):
                    q = axis.interpolate((i + 0.5) / max(1, int(long_ // 40)), normalized=True)
                    q = Point(q.x + c0.x - m0.x, q.y + c0.y - m0.y)
                    if g.buffer(0.5).contains(q):
                        trees.append((q, 6.5))
            else:
                trees.append((polylabel(g, 0.5), min(7.5, max(5.0, short_ * 0.9))))
    edge_line = unary_union([g.exterior for g in _polys(dev.buffer(8, join_style=1))])
    pad_ok = pad.buffer(-4) if pad is not None and not pad.is_empty else None
    keep_out = drive.buffer(30) if not drive.is_empty else Polygon()
    arrows = [arrow_poly(p, d) for p, d in arrows]
    nt = int(edge_line.length // 45) if not edge_line.is_empty else 0
    for i in range(nt):
        q = edge_line.interpolate(i / max(nt, 1), normalized=True)
        if pad_ok is not None and pad_ok.contains(q) and not keep_out.contains(q) and q.distance(dev) > 5 and (pond is None or q.distance(pond) > 6):
            trees.append((q, 8.5))
    trees = [(q, r) for q, r in trees if all(q.distance(q2) > (r + r2) * 0.8 for q2, r2 in trees if q2 is not q)]
    # roof accent blocks, as the automatic layout draws them, in each building's own orientation
    roof_blocks = []
    for r in rows:
        w_, d_ = r["bw"], r["bd"]
        if r["fp"] < 2000:
            roof_blocks.append(None)
            continue
        if max(w_, d_) / max(min(w_, d_), 1) >= 2.2:
            e = 0.2 * max(w_, d_)
            blk = box(-w_ / 2, -d_ / 2, -w_ / 2 + e, d_ / 2) if w_ >= d_ else box(-w_ / 2, -d_ / 2, w_ / 2, -d_ / 2 + e)
        else:
            c_ = min(28.0, 0.38 * min(w_, d_))
            blk = box(-w_ / 2, -d_ / 2, -w_ / 2 + c_, -d_ / 2 + c_)
        roof_blocks.append(affinity.translate(affinity.rotate(blk, r["rot"], origin=(0, 0)), r["cx"], r["cy"]))
    roof_blocks += [None] * len(camp["units"])
    # numbers
    parcel_sf = P.area
    bsf = sum(r["fp"] for r in rows)
    gfa_sf = sum(r["fp"] * r["b"]["stories"] for r in rows)
    if camp["sites"]:
        for st in camp["sites"]:
            if st["lot"] is not None and not P.buffer(1.0).contains(st["lot"]):
                warn.append(f"Campsite {st['label']} runs past the property line")
        if not C.is_empty and any(st["pad"] is not None and st["pad"].intersects(C) for st in camp["sites"]):
            warn.append("A campsite pad sits on a constraint (wetland, flood zone, steep ground or an existing building)")
    ratio = design.get("parking_ratio")
    req0 = (prog.get("parking") or {}).get("required")
    if isinstance(ratio, (int, float)) and ratio >= 0:
        req = math.ceil(gfa_sf / 1000 * ratio)
    elif isinstance(req0, (int, float)):
        gfa0 = sum(float(x.get("footprint_sf") or 0) * float(x.get("stories") or 1) for x in prog.get("buildings") or []) or gfa_sf
        req = math.ceil(float(req0) * gfa_sf / gfa0)
    else:
        req = math.ceil(gfa_sf / 1000 * 4)
    req = max(0, req - int(shared_stalls or 0))
    bz_left = bz.difference(dev.buffer(25)).difference(pond.buffer(15) if pond is not None else Polygon())
    metrics = {
        "concept": prog.get("use"),
        "buildings": [{"name": r["b"]["name"], "use": r["b"]["use"], "w_ft": round(r["w"]), "d_ft": round(r["d"]), "footprint_sf": round(r["fp"], -1),
                       "stories": r["b"]["stories"], "drive_thru": r["b"]["drive_thru"]} for r in rows],
        "drive_road": design.get("drive_road") or (design.get("frame") or {}).get("drive_road"),
        "building_sf": round(bsf, -1), "gfa_sf": round(gfa_sf, -1), "program_scale": 1.0,
        "stalls": n_st, "stalls_sketch": n_sketch, "stalls_required": req, "ada_stalls": n_ada, "template": "manual/testfit",
        "one_way_aisles": any((f.get("params") or {}).get("oneway") for f in design.get("parking") or []),
        "impervious_sf": round(imperv_sf, -1), "impervious_pct": round(100 * imperv_sf / parcel_sf, 1),
        "lot_coverage_pct": round(100 * bsf / parcel_sf, 1), "far": round(gfa_sf / parcel_sf, 3),
        "pond_sf": round(pond.area, -1) if pond is not None else 0,
        "development_acres": round(dev.area / 43560, 2),
        "remaining_developable_acres": round(sum(g.area for g in _polys(bz_left) if g.area > 10000) / 43560, 2),
        "parking_setbacks_ft": psb, "setbacks_ft": setbacks, "manual": True,
        **({"campground": {"sites": len(camp["sites"]), "by_kind": camp["by_kind"], "gpd": camp["gpd"], "sewage": camp["sewage"],
                           "gravel_sf": round(gravel.area, -1)}} if camp["sites"] else {}),
        **({"reused_pavement_sf": round(RPf.area, -1), "shared_stalls": int(shared_stalls or 0)} if RPf is not None else {}),
    }
    if n_st < req and rows:
        warn.append(f"Parking short: {n_st} provided vs {req} required")
    for key, val, lab in (("max_impervious_pct", metrics["impervious_pct"], "impervious"), ("max_lot_coverage_pct", metrics["lot_coverage_pct"], "lot coverage")):
        lim = rules.get(key)
        if isinstance(lim, (int, float)) and val > lim:
            warn.append(f"{lab} {val}% exceeds the {lim}% maximum")
    if isinstance(rules.get("max_far"), (int, float)) and metrics["far"] > rules["max_far"]:
        warn.append(f"FAR {metrics['far']} exceeds {rules['max_far']}")
    metrics["warnings"] = warn
    def areal(g):            # polygons only (make_valid / differences can leave stray lines and points)
        if g is None:
            return None
        ps = [q for q in _polys(g) if q.area > 0.5]
        return unary_union(ps) if ps else Polygon()
    pave, walks, grass, pad, drive, dev = (areal(x) for x in (pave, walks, grass, pad, drive, dev))
    pond = areal(pond) if pond is not None else None
    local = {"P": P, "bz": bz, "pz": pz, "C": C, "pave": pave, "walk": walks, "grass": grass, "pad": pad, "roof_blocks": roof_blocks, "bldgs": bldgs,
             "reuse": RPf, "stalls": stalls, "ada": ada, "stripes": stripes, "pond": pond, "drive": drive, "dev": dev, "arrows": [a for a in arrows if a.within(pave.buffer(1.0))],
             "trees": [q.buffer(r, 24) for q, r in trees], "gravel": gravel if not gravel.is_empty else None,
             "camp_lots": [st["lot"] for st in camp["sites"] if st["lot"] is not None], "camp_pads": [g for st in camp["sites"] for g in (st["pad"], st["park"]) if g is not None],
             "camp_units": camp["units"], "camp_tables": [st["table"] for st in camp["sites"] if st["table"] is not None],
             "camp_rings": [st["ring"] for st in camp["sites"] if st["ring"] is not None], "camp_peds": [st["ped"] for st in camp["sites"] if st["ped"] is not None],
             "camp_svc": [st["svc"] for st in camp["sites"]], "camp_at": [st["at"] for st in camp["sites"]],
             "drive_cl": [d["cl"] for d in drs] if camp["sites"] else []}
    sp = {k_: ([fr.inv(g) if g is not None else None for g in v] if isinstance(v, list) else (fr.inv(v) if v is not None else None)) for k_, v in local.items()}
    labels = []
    for r in rows:
        c = fr.inv(polylabel(r["rect"], 1.0) if r["irregular"] else r["rect"].centroid)
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a) + r["rot"], "w_ft": r["w"], "d_ft": r["d"],
                       "lines": [f"PROPOSED {_short(r['b'])}", f"{r['w']:.0f}' X {r['d']:.0f}'", f"{r['fp']:,.0f} SF"]})
    for st in camp["sites"]:
        c = fr.inv(st["at"])
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a), "lines": [st["label"]], "small": True, "camp": True})
    if pond is not None and not pond.is_empty:
        c = fr.inv(polylabel(_largest(pond) or pond, 2))
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a), "lines": ["STORMWATER", "MANAGEMENT"], "small": True})
    grades = []
    for x in design.get("grading") or []:
        gp = _poly(x.get("pts") or [])
        gp = areal(gp.intersection(P)) if gp is not None else None
        if gp is not None and not gp.is_empty and gp.area > 25:
            lo, hi = sorted((max(0.0, float(x.get("min_pct") or 0)), max(0.0, float(x.get("max_pct") or 0))))
            grades.append({"poly": gp, "min": lo / 100.0, "max": hi / 100.0})
    local["grade_polys"] = [q["poly"] for q in grades]
    sp["grade_polys"] = [fr.inv(q["poly"]) for q in grades]
    sp["grade_meta"] = [[q["min"], q["max"]] for q in grades]
    sp["camp_meta"] = [{"label": st["label"], "kind": st["kind"], "hook": st["hook"], "gpd": st["gpd"]} for st in camp["sites"]]
    sp["camp_sewage"], sp["camp_gpd"] = camp["sewage"], camp["gpd"]
    return {"metrics": metrics, "sp": sp, "local": local, "labels": labels, "frame_angle_deg": math.degrees(fr.a)}


def _camp(design):
    """Campsite rows from Sitecraft: each site's lot, pad (+ car spot), cabin, table, fire ring, utility pedestal, and where its
    services connect (the pedestal; a cabin's centre)."""
    out = {"sites": [], "units": [], "gravel": [], "paved": [], "gpd": 0.0, "by_kind": {}, "sewage": (design.get("camp") or {}).get("sewage") or "septic"}
    for row in design.get("campsites") or []:
        hook = row.get("hookups", "wes") or ""
        gpd = float(row.get("gpd_per_site") or 0)
        kind = row.get("kind") or "backin"
        for st in row.get("sites") or []:
            pad, park, unit = _poly(st.get("pad") or []), _poly(st.get("park") or []) if st.get("park") else None, _poly(st.get("unit") or []) if st.get("unit") else None
            ped = Point(*map(float, st["pedestal"])) if st.get("pedestal") else None
            svc = ped if ped is not None and ("s" in hook or "w" in hook) else (unit.centroid if unit is not None else None)
            o = {"label": st.get("label") or f"P{st.get('n')}", "kind": kind, "hook": hook, "gpd": gpd, "lot": _poly(st.get("lot") or []), "pad": pad, "park": park, "unit": unit,
                 "table": _poly(st.get("table") or []) if st.get("table") else None, "ring": Point(*map(float, st["fire_ring"])).buffer(1.75, 12) if st.get("fire_ring") else None,
                 "ped": ped.buffer(1.0, cap_style=3) if ped is not None else None, "svc": svc, "at": Point(*map(float, st.get("at") or (0, 0)))}
            out["sites"].append(o)
            if unit is not None:
                out["units"].append(unit)
            for g in (pad, park):
                if g is not None:
                    out["paved" if row.get("surface") == "paved" else "gravel"].append(g)
            out["gpd"] += gpd
            out["by_kind"][kind] = out["by_kind"].get(kind, 0) + 1
    return out
