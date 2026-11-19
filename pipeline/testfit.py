"""Manual test fit: the site laid out by hand in Drafted, then sent down the pipeline exactly like an automatic layout.

base()   -> everything the editor needs, in the same local frame the automatic layout uses (feet; x along the main frontage,
            y into the site): parcel, building and pavement envelopes, constraints, roads, existing buildings and pavement,
            the program and the automatic layout's buildings as a starting point.
build()  -> the editor's design turned into the same result generate_layout returns ({metrics, sp, local, labels,
            frame_angle_deg}), so the concept sheet, DXF, 3D model, render, package and full design all work unchanged.

Design (local feet), as the editor sends it:
  buildings: [{id, name, use, label, cx, cy, w, d, rot (deg), stories, drive_thru}]
  parking:   [{id, stalls: [[[x, y] x4], ...], aisles: [[[x, y], ...], ...]}]   (stalls and aisles laid out by the editor)
  drives:    [{id, pts: [[x, y], ...], width}]
  pavement:  [{pts: [[x, y], ...]}]           extra paved areas (loading, plazas)
  ponds:     [{pts: [[x, y], ...]}]           none drawn + auto_pond -> one is placed like the automatic layout does
  auto_pond: bool, parking_ratio: stalls per 1,000 SF of GFA (for the required count)
"""
import math

import numpy as np
from shapely import affinity, make_valid
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polylabel, unary_union

from .ec_drawing import _polys, classify_edges, envelope, road_key
from .existing import KEEP_CLEAR_FT
from .layout import DEFAULT_PARK_SB, Frame, ada_required
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


def build(ctx, design, setbacks, psb, steep, keep, pave_keep, prog, rules, shared_stalls=0):
    fr, edges, rds, named, keys = _frame(ctx, setbacks, frame=design.get("frame"))
    P, C, bz, pz, RPf = _site(ctx, fr, edges, setbacks, psb, steep, [b["geom"] for b in keep], [p["geom"] for p in pave_keep])
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
    if not rows:
        raise RuntimeError("The test fit has no buildings")
    bldgs = [r["rect"] for r in rows]
    B = unary_union(bldgs)
    # parking: stalls and aisles exactly as laid out in the editor; anything under a building is dropped
    stalls, aisles, arrows = [], [], []
    for f in (design.get("parking") or []) + (design.get("drives") or []):
        for a in f.get("arrows") or []:
            try:
                arrows.append(arrow_poly(tuple(map(float, a["at"])), tuple(map(float, a["dir"]))))
            except Exception:  # noqa: BLE001
                pass
    for f in design.get("parking") or []:
        for s in f.get("stalls") or []:
            p = _poly(s)
            if p is not None and p.area > 20 and p.intersection(B).area < 0.1 * p.area:
                stalls.append(p)
        for a in f.get("aisles") or []:
            p = _poly(a)
            if p is not None:
                aisles.append(p.difference(B))
    # drives + extra pavement
    drives = [d for d in (_drive_poly(dv, road_pave, P) for dv in design.get("drives") or []) if d is not None and not d.is_empty]
    drive = unary_union(drives) if drives else Polygon()
    extra = [p for p in (_poly(x.get("pts") or []) for x in design.get("pavement") or []) if p is not None]
    circ = unary_union(aisles + extra + ([drive] if not drive.is_empty else []))
    pave = make_valid(unary_union([circ, *stalls])).buffer(0)
    pave = make_valid(_close(pave, 2.2)).buffer(0).difference(B)
    pave = pave.intersection(P.union(drive.buffer(2.0)) if not drive.is_empty else P).buffer(0)
    if RPf is not None:
        pave = pave.difference(RPf).buffer(0)
    out_pz = pave.difference(pz.buffer(1.0)).difference(drive.buffer(1.0) if not drive.is_empty else Polygon())
    if out_pz.area > 200:
        warn.append(f"About {out_pz.area:,.0f} SF of pavement is inside the pavement setbacks or on a constraint")
    if not drive.is_empty:
        off = drive.difference(P.buffer(1.0))
        if road_pave is not None:
            off = off.difference(road_pave.buffer(100.0))   # the stretch across the right-of-way to the road is expected
        if off.area > 300:
            warn.append("The driveway runs outside the property (over a neighbor or into the right-of-way away from the road)")
    if drive.is_empty:
        warn.append("No driveway drawn: the site is not connected to a road")
    elif road_pave is not None and not drive.buffer(3.0).intersects(road_pave):
        warn.append("The driveway does not reach the road")
    # walks: 6 ft around each building where it is not paved
    walks = unary_union([r["rect"].buffer(WALK, join_style=2).difference(r["rect"]) for r in rows]).difference(pave).intersection(P).buffer(0)
    dev = unary_union([pave, walks, *bldgs])
    grass = _fillet(dev.buffer(14, join_style=1), 6).intersection(P).difference(C).difference(dev)
    holes = unary_union([Polygon(h) for p in _polys(pave) for h in p.interiors]) if not pave.is_empty else Polygon()
    grass = unary_union([grass, holes.difference(dev)])
    imperv_sf = pave.area + walks.area + sum(b.area for b in bldgs) + (RPf.area if RPf is not None else 0.0)
    ponds = [p for p in (_poly(x.get("pts") or []) for x in design.get("ponds") or []) if p is not None]
    if ponds:
        pond = unary_union(ponds).intersection(P).buffer(0)
    elif design.get("auto_pond", True) and not prog.get("no_pond"):
        land = [g for g in _polys(P.difference(C)) if g.intersects(dev)]
        pond = _pond(unary_union(land) if land else P, dev.union(grass), C, max(3000.0, imperv_sf * 0.08))
    else:
        pond = None
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
    # trees: one in each landscaped island / hole in the paving, and around the edge of the development
    trees = []
    for g in _polys(holes.difference(dev.buffer(1.0))):
        if g.area >= 90:
            q = polylabel(g, 0.5)
            mrr = g.minimum_rotated_rectangle
            cs = list(mrr.exterior.coords)
            short_ = min(math.dist(cs[0], cs[1]), math.dist(cs[1], cs[2]))
            if short_ >= 4.5:
                trees.append((q, min(7.5, max(5.0, short_ * 0.9))))
    edge_line = unary_union([g.exterior for g in _polys(dev.buffer(8, join_style=1))])
    pad_ok = pad.buffer(-4) if pad is not None and not pad.is_empty else None
    keep_out = drive.buffer(30) if not drive.is_empty else Polygon()
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
    # numbers
    parcel_sf = P.area
    bsf = sum(r["fp"] for r in rows)
    gfa_sf = sum(r["fp"] * r["b"]["stories"] for r in rows)
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
        "stalls": n_st, "stalls_required": req, "ada_stalls": n_ada, "template": "manual/testfit",
        "one_way_aisles": any((f.get("params") or {}).get("oneway") for f in design.get("parking") or []),
        "impervious_sf": round(imperv_sf, -1), "impervious_pct": round(100 * imperv_sf / parcel_sf, 1),
        "lot_coverage_pct": round(100 * bsf / parcel_sf, 1), "far": round(gfa_sf / parcel_sf, 3),
        "pond_sf": round(pond.area, -1) if pond is not None else 0,
        "development_acres": round(dev.area / 43560, 2),
        "remaining_developable_acres": round(sum(g.area for g in _polys(bz_left) if g.area > 10000) / 43560, 2),
        "parking_setbacks_ft": psb, "setbacks_ft": setbacks, "manual": True,
        **({"reused_pavement_sf": round(RPf.area, -1), "shared_stalls": int(shared_stalls or 0)} if RPf is not None else {}),
    }
    if n_st < req:
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
             "reuse": RPf, "stalls": stalls, "ada": ada, "stripes": stripes, "pond": pond, "drive": drive, "dev": dev, "arrows": [a for a in arrows if a.intersects(pave)],
             "trees": [q.buffer(r, 24) for q, r in trees]}
    sp = {k_: ([fr.inv(g) if g is not None else None for g in v] if isinstance(v, list) else (fr.inv(v) if v is not None else None)) for k_, v in local.items()}
    labels = []
    for r in rows:
        c = fr.inv(polylabel(r["rect"], 1.0) if r["irregular"] else r["rect"].centroid)
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a) + r["rot"], "w_ft": r["w"], "d_ft": r["d"],
                       "lines": [f"PROPOSED {_short(r['b'])}", f"{r['w']:.0f}' X {r['d']:.0f}'", f"{r['fp']:,.0f} SF"]})
    if pond is not None and not pond.is_empty:
        c = fr.inv(polylabel(_largest(pond) or pond, 2))
        labels.append({"at": (c.x, c.y), "rot": math.degrees(fr.a), "lines": ["STORMWATER", "MANAGEMENT"], "small": True})
    return {"metrics": metrics, "sp": sp, "local": local, "labels": labels, "frame_angle_deg": math.degrees(fr.a)}
