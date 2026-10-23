"""Utilities, fire access, landscape, lighting and erosion control for the full design. Local feet frame."""
import math

import numpy as np
from scipy import ndimage as ndi
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import nearest_points, substring, unary_union
from shapely import affinity

from .base import lines, polys

GPD_PER_SF = {"residential": 0.12, "apartment": 0.12, "hotel": 0.15, "medical": 0.10, "office": 0.075, "retail": 0.05, "restaurant": 0.8,
              "industrial": 0.04, "warehouse": 0.02, "storage": 0.01, "school": 0.1, "church": 0.05, "gas": 0.2, "car wash": 1.5}


def _off(line, d):
    try:
        o = line.offset_curve(d)
        if o.geom_type == "MultiLineString":
            o = max(o.geoms, key=lambda q: q.length)
        return o
    except Exception:  # noqa: BLE001
        return line


def _along(line, s):
    return line.interpolate(min(max(s, 0.0), line.length))


def design_flow(prog):
    tot = 0.0
    for b in (prog.get("buildings") or []):
        use = (b.get("use") or b.get("label") or "").lower()
        f = next((v for kk, v in GPD_PER_SF.items() if kk in use), 0.08)
        tot += f * b.get("footprint_sf", 0) * b.get("stories", 1)
    return tot


def utilities(g, z_prop, ex_z, G, L, road_all, prog, fr):
    sp = G["spine"]
    out = {"warnings": [], "notes": []}
    bl = L["bldgs"]
    # service tap: where the spine meets the road
    tap = Point(sp.coords[0])
    road_pt = nearest_points(road_all, tap)[0] if road_all is not None and not road_all.is_empty else tap
    z_road = float(g.at(ex_z, [road_pt.x], [road_pt.y])[0])
    # --- water main along the spine (left), sewer main (right)
    wm = _off(sp, 7.0)
    sm = _off(sp, -7.0)
    wm = LineString([(road_pt.x, road_pt.y)] + list(wm.coords)) if road_pt.distance(Point(wm.coords[0])) > 1 else wm
    sm = LineString([(road_pt.x, road_pt.y)] + list(sm.coords)) if road_pt.distance(Point(sm.coords[0])) > 1 else sm
    out["water_main"], out["sewer_main"] = wm, sm
    out["tap"] = (road_pt.x, road_pt.y)
    # laterals: to the nearest face of each building
    wl, sl, svc = [], [], []
    for n, b in enumerate(bl, 1):
        a = nearest_points(wm, b)
        wl.append(LineString([a[0], a[1]]))
        c = nearest_points(sm, b)
        sl.append(LineString([c[0], c[1]]))
        svc.append({"bldg": n, "w_xy": (a[1].x, a[1].y), "s_xy": (c[1].x, c[1].y)})
    out["water_lat"], out["sewer_lat"], out["svc"] = wl, sl, svc
    # hydrants: within 250 ft along the main and 150 ft of every building face
    hyd = []
    s = 40.0
    while s < wm.length:
        hyd.append(_off(wm, 0) .interpolate(s))
        s += 250.0
    if not hyd:
        hyd.append(wm.interpolate(min(wm.length, 40.0)))
    for b in bl:
        if all(h.distance(b) > 150 for h in hyd):
            hyd.append(nearest_points(wm, b)[0])
    out["hydrants"] = [(h.x, h.y) for h in hyd]
    # gate valves: at the tap and at each lateral
    out["valves"] = [out["tap"]] + [(l.coords[0][0], l.coords[0][1]) for l in wl]
    # --- sewer inverts, from the tap upstream
    gpd = design_flow(prog)
    out["sewer_gpd"] = round(gpd, -1)
    out["water_gpd"] = round(gpd * 1.2, -1)
    inv_tap = z_road - 8.0                                       # assumed depth of the existing main
    mh = []
    n_mh = max(2, int(math.ceil(sm.length / 280.0)) + 1)
    positions = sorted(set([0.0, sm.length] + [sm.project(Point(s_["s_xy"])) if False else sm.project(Point(*[l.coords[0] for l in sl][i])) for i, s_ in enumerate(svc)]
                           + list(np.linspace(0, sm.length, n_mh))))
    pos = [positions[0]]
    for p in positions[1:]:
        if p - pos[-1] > 25.0:
            pos.append(p)
    slope = 0.005
    inv = inv_tap
    last = 0.0
    for k, p in enumerate(pos):
        pt = _along(sm, p)
        inv = inv_tap + slope * p
        rim = float(g.at(z_prop, [pt.x], [pt.y])[0])
        mh.append({"id": f"SMH-{k + 1}", "xy": (pt.x, pt.y), "sta": p, "rim": round(rim, 2), "inv": round(inv, 2), "depth": round(rim - inv, 1)})
    lift = [m for m in mh if m["depth"] < 4.0]
    if lift:
        out["warnings"].append(f"Gravity sewer cannot keep 4 ft of cover at {', '.join(m['id'] for m in lift)}; a lift station or grinder pumps are likely required")
    out["smh"] = mh
    out["sewer_slope_pct"] = slope * 100
    lat_rows = []
    for k, (b, l) in enumerate(zip(bl, sl)):
        ffe = G["ffe"][k]
        L_ft = l.length
        p = sm.project(Point(l.coords[0]))
        inv_j = inv_tap + slope * p
        inv_b_req = inv_j + 0.1 + 0.02 * L_ft
        inv_b_have = ffe - 4.5
        lat_rows.append({"bldg": k + 1, "len": round(L_ft), "inv_main": round(inv_j, 2), "inv_bldg": round(inv_b_req, 2), "ok": inv_b_have >= inv_b_req - 0.01,
                         "bldg_out_inv": round(inv_b_have, 2)})
        if inv_b_have < inv_b_req - 0.01:
            out["warnings"].append(f"Building {k + 1}: sewer lateral invert {inv_b_req:.1f} is above the building outlet {inv_b_have:.1f}; pump or deeper main required")
    out["sewer_lat_rows"] = lat_rows
    # --- dry utilities
    out["elec"] = _off(sp, 13.0)
    out["gas"] = _off(sp, -13.0)
    out["xfmr"] = []
    for b in bl[:max(1, len(bl))]:
        a = nearest_points(out["elec"], b)[0]
        out["xfmr"].append((a.x, a.y))
    out["elec_lat"] = [LineString([nearest_points(out["elec"], b)[0], nearest_points(out["elec"], b)[1]]) for b in bl]
    out["gas_lat"] = [LineString([nearest_points(out["gas"], b)[0], nearest_points(out["gas"], b)[1]]) for b in bl]
    out["pole"] = (road_pt.x, road_pt.y)
    return out


def fire(g, G, L, util):
    sp = G["spine"]
    out = {"warnings": []}
    # fire lane width along the spine
    pv = L["pave"].union(L["drive"]) if L.get("drive") is not None else L["pave"]
    ok_w = 0
    n = 0
    for s in np.arange(0, sp.length, 10.0):
        p = sp.interpolate(s)
        n += 1
        if p.distance(pv.boundary) >= 11.0:
            ok_w += 1
    out["lane_ok_pct"] = round(100.0 * ok_w / max(n, 1))
    if out["lane_ok_pct"] < 85:
        out["warnings"].append("Part of the access drive is narrower than 22 ft clear; widen for fire apparatus")
    # min turning radius from the centerline
    rmin = 1e9
    pts = [sp.interpolate(s) for s in np.arange(0, sp.length, 10.0)]
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        ab, bc, ac = a.distance(b), b.distance(c), a.distance(c)
        area = abs((b.x - a.x) * (c.y - a.y) - (c.x - a.x) * (b.y - a.y)) / 2
        if area > 1e-6:
            rmin = min(rmin, ab * bc * ac / (4 * area))
    out["min_radius_ft"] = None if rmin > 1e8 else round(rmin)
    if rmin < 30:
        out["warnings"].append(f"Centerline turning radius of about {rmin:.0f} ft is tight for a 100 ft ladder truck (inside radius of 25 ft needed)")
    out["truck_envelope"] = (_off(sp, 6.5), _off(sp, -6.5))
    out["hydrants"] = util["hydrants"]
    out["fdc"] = [nearest_points(sp, b)[1] for b in L["bldgs"]]
    out["fdc"] = [(p.x, p.y) for p in out["fdc"]]
    # dead end
    out["dead_end_ft"] = round(sp.length)
    if sp.length > 150:
        out["warnings"].append("The access drive is a dead end longer than 150 ft; confirm an approved turnaround for apparatus")
    return out


def landscape(L, lat):
    if lat >= 40:
        sp = {"shade": ("RM", "Acer rubrum 'Armstrong'", "Armstrong Red Maple", "2.5\" cal."), "orn": ("SB", "Amelanchier canadensis", "Serviceberry", "6' - 8'"),
              "ever": ("TS", "Thuja occidentalis 'Smaragd'", "Emerald Green Arborvitae", "5' - 6'")}
    elif lat >= 33:
        sp = {"shade": ("WO", "Quercus phellos", "Willow Oak", "3\" cal."), "orn": ("CM", "Lagerstroemia indica", "Crape Myrtle", "8' - 10'"),
              "ever": ("FH", "Ilex x attenuata 'Fosteri'", "Foster's Holly", "6' - 8'")}
    else:
        sp = {"shade": ("LO", "Quercus virginiana", "Live Oak", "3\" cal."), "orn": ("CM", "Lagerstroemia indica", "Crape Myrtle", "8' - 10'"),
              "ever": ("SP", "Sabal palmetto", "Cabbage Palm", "12' - 16' CT")}
    trees = []
    for t in L["trees"]:
        c = t.centroid
        r = math.sqrt(t.area / math.pi)
        kind = "shade" if r >= 7.0 else "orn"
        trees.append((c.x, c.y, r, kind))
    # an evergreen screen along the front / road side of the pavement edge is shown as a short row at the entrance
    cnt = {k: sum(1 for t in trees if t[3] == k) for k in ("shade", "orn")}
    lawn = L["grass"].intersection(L["parcel"]) if L.get("grass") is not None else None
    area = lawn.area if lawn is not None else 0.0
    return {"species": sp, "trees": trees, "counts": cnt, "landscape_sf": round(area, -1)}


def lighting(g, G, L, mount=20.0, target_max=6.5):
    pave = L["pave"].union(L["walk"]) if L.get("walk") is not None else L["pave"]
    if L.get("drive") is not None:
        pave = pave.union(L["drive"])
    cand = []
    for p in polys(pave):
        ring = p.exterior
        for s in np.arange(0, ring.length, 10.0):
            q = ring.interpolate(s)
            cand.append((q.x, q.y))
    cells = np.argwhere(g.mask(pave))
    cell_xy = np.c_[g.xs[cells[:, 1]], g.ys[cells[:, 0]]]
    cover_r = 48.0
    left = np.ones(len(cell_xy), bool)
    poles = []
    cand = np.array(cand) if cand else np.zeros((0, 2))
    ctree = cKDTree(cell_xy) if len(cell_xy) else None
    while left.any() and len(poles) < 40 and len(cand):
        best, bi = -1, None
        for i in range(0, len(cand), 2):
            if any(math.hypot(cand[i][0] - a, cand[i][1] - b) < 55 for a, b in poles):
                continue
            idx = ctree.query_ball_point(cand[i], cover_r)
            c = int(left[idx].sum()) if idx else 0
            if c > best:
                best, bi = c, i
        if bi is None or best <= 0:
            break
        poles.append((float(cand[bi][0]), float(cand[bi][1])))
        left[ctree.query_ball_point(cand[bi], cover_r)] = False
    # building-mounted / entrance: nothing extra
    I0 = target_max * mount ** 2
    n = 3.0

    def fc(x, y):
        e = np.zeros_like(x, dtype=float)
        for px, py in poles:
            r2 = (x - px) ** 2 + (y - py) ** 2
            d = np.sqrt(r2 + mount ** 2)
            e += I0 * mount ** (n + 1) / d ** (n + 3) * (1.0 if True else 0)
        return e

    E = fc(g.X, g.Y) if poles else np.zeros(g.X.shape)
    calc = g.mask(pave)
    vals = E[calc] if calc.any() else np.array([0.0])
    stats = {"avg": round(float(vals.mean()), 2), "max": round(float(vals.max()), 1), "min": round(float(vals.min()), 1),
             "max_min": round(float(vals.max() / max(vals.min(), 0.1)), 1), "avg_min": round(float(vals.mean() / max(vals.min(), 0.1)), 1)}
    return {"poles": poles, "E": E, "stats": stats, "mount_ft": mount}


def erosion(g, G, L, util, D, net, spine):
    lod = g.cell_poly(G["lod_mask"]).buffer(4).buffer(-2)
    lod = unary_union([p for p in polys(lod) if p.area > 400]) if not lod.is_empty else lod
    lod = lod.simplify(2.0)
    out = {"lod": lod, "lod_ac": round(lod.area / 43560.0, 2)}
    # silt sock along the downhill side of the LOD, outside the pavement entrance
    ring = lod.boundary
    segs = []
    if not ring.is_empty:
        for rg in lines(ring):
            n = max(8, int(rg.length // 10))
            prev = None
            cur = []
            for k in range(n + 1):
                p = rg.interpolate(k / n, normalized=True)
                q = rg.interpolate(min(k / n + 1e-3, 1.0), normalized=True)
                # outward normal: direction away from the lod interior
                tx, ty = q.x - p.x, q.y - p.y
                nl = math.hypot(tx, ty) or 1.0
                nx_, ny_ = ty / nl, -tx / nl
                test = Point(p.x + 2 * nx_, p.y + 2 * ny_)
                if lod.contains(test):
                    nx_, ny_ = -nx_, -ny_
                gx = float(g.at(np.gradient(G["z_prop"], g.cell, axis=1), [p.x], [p.y])[0])
                gy = float(g.at(np.gradient(G["z_prop"], g.cell, axis=0), [p.x], [p.y])[0])
                down = -(gx * nx_ + gy * ny_)          # positive when ground falls outward
                if down > 0.005 and p.distance(Point(spine.coords[0])) > 20:
                    cur.append((p.x, p.y))
                else:
                    if len(cur) > 1:
                        segs.append(LineString(cur))
                    cur = []
            if len(cur) > 1:
                segs.append(LineString(cur))
    out["silt"] = segs
    out["silt_ft"] = round(sum(s.length for s in segs))
    # construction exit at the entrance
    a = Point(spine.coords[0])
    b = _along(spine, 30.0)
    ang = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
    exit_poly = affinity.rotate(box(-10, -12, 50, 12), ang, origin=(0, 0))
    exit_poly = affinity.translate(exit_poly, a.x, a.y)
    out["exit"] = exit_poly
    # stockpile + washout in the lawn area
    lawn = L["grass"].intersection(lod) if L.get("grass") is not None else lod
    if lawn is not None and not lawn.is_empty:
        big = max(polys(lawn), key=lambda p: p.area) if polys(lawn) else None
    else:
        big = None
    out["stockpile"] = None
    if big is not None and big.area > 5000:
        c = big.representative_point()
        out["stockpile"] = box(c.x - 25, c.y - 15, c.x + 25, c.y + 15)
        out["washout"] = box(c.x + 30, c.y - 6, c.x + 42, c.y + 6)
    out["inlet_prot"] = [s["xy"] for s in net["structs"] if s["kind"] == "CB"]
    out["npdes"] = out["lod_ac"] >= 1.0
    return out
