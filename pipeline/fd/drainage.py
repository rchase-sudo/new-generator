"""Stormwater: inlet placement, subcatchments, pre / post hydrology, pond sizing and the storm-sewer network with
inverts. Local feet frame throughout."""
import heapq
import math

import numpy as np
from scipy import ndimage as ndi
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, unary_union

from . import hydro
from .base import CELL, descend, lines, polys

PIPE_SIZES = [12, 15, 18, 24, 30, 36, 42, 48]
CN = {"pave": 98, "roof": 98, "lawn": {"A": 39, "B": 61, "C": 74, "D": 80}, "pre": {"A": 36, "B": 58, "C": 71, "D": 78}}


def boundary_samples(poly, step=8.0):
    out = []
    for p in polys(poly):
        ring = p.exterior
        n = max(8, int(ring.length // step))
        for k in range(n):
            pt = ring.interpolate(k / n, normalized=True)
            out.append((pt.x, pt.y, id(p)))
    return out


def place_inlets(g, z, pave_mask, pave_poly, prof_lows, spine, min_sep=90.0, max_n=7):
    """Inlets at the low points of the pavement edge (gutter) and at profile sags. Returns [(x, y)]."""
    cands = []
    for p in polys(pave_poly):
        ring = p.exterior
        n = max(12, int(ring.length // 6.0))
        pts = [ring.interpolate(k / n, normalized=True) for k in range(n)]
        zz = g.at(z, [q.x for q in pts], [q.y for q in pts])
        zz = ndi.uniform_filter1d(zz, 5, mode="wrap")
        for k in range(n):
            if zz[k] <= zz[(k - 1) % n] and zz[k] <= zz[(k + 1) % n] and zz[k] <= np.percentile(zz, 35):
                cands.append((float(zz[k]), pts[k].x, pts[k].y))
    for s in prof_lows:
        q = spine.interpolate(s)
        nr = nearest_points(q, pave_poly.boundary)[1]
        zq = float(g.at(z, [nr.x], [nr.y])[0])
        cands.append((zq - 0.3, nr.x, nr.y))       # sags win
    cands.sort()
    chosen = []
    for zq, x, y in cands:
        if all(math.hypot(x - a, y - b) >= min_sep for a, b in chosen):
            chosen.append((x, y))
        if len(chosen) >= max_n:
            break
    if not chosen and cands:
        chosen = [(cands[0][1], cands[0][2])]
    return chosen


def assign_catchments(g, z, pave_mask, pave_poly, inlets, roofs):
    """Pavement cell -> inlet by walking the gutter downhill; roofs -> nearest inlet. Returns per-inlet dict of areas / polygons."""
    n_in = len(inlets)
    inl = np.array(inlets)
    tree_in = cKDTree(inl)
    # gutter ring samples
    ring_pts, ring_idx = [], []
    for pi, p in enumerate(polys(pave_poly)):
        ring = p.exterior
        n = max(12, int(ring.length // 4.0))
        pts = [ring.interpolate(k / n, normalized=True) for k in range(n)]
        zz = ndi.uniform_filter1d(g.at(z, [q.x for q in pts], [q.y for q in pts]), 3, mode="wrap")
        start = len(ring_pts)
        for k, q in enumerate(pts):
            ring_pts.append((q.x, q.y, zz[k], start, n, k))
    rp = np.array([(a, b) for a, b, *_ in ring_pts]) if ring_pts else np.zeros((0, 2))
    term = np.zeros(len(ring_pts), int)
    inlet_ring = [int(np.argmin(np.hypot(rp[:, 0] - x, rp[:, 1] - y))) for x, y in inlets] if len(rp) else []
    for r, (x, y, zr, start, n, k) in enumerate(ring_pts):
        cur = r
        for _ in range(n + 2):                   # walk downhill (either direction) to a local minimum or an inlet
            if cur in inlet_ring:
                break
            _, _, z0, s0, n0, k0 = ring_pts[cur]
            nxt = [s0 + (k0 + 1) % n0, s0 + (k0 - 1) % n0]
            best = min(nxt, key=lambda q: ring_pts[q][2])
            if ring_pts[best][2] >= z0 - 1e-9:
                break
            cur = best
        term[r] = cur
    ring_tree = cKDTree(rp) if len(rp) else None
    jj, ii = np.nonzero(pave_mask)
    which = np.full(len(jj), -1)
    if ring_tree is not None:
        _, nn = ring_tree.query(np.c_[g.xs[ii], g.ys[jj]])
        for q, r in enumerate(nn):
            t = term[r]
            _, near = tree_in.query(rp[t])
            which[q] = near
    area = np.zeros(n_in)
    cells = [[] for _ in range(n_in)]
    for q, w in enumerate(which):
        if w >= 0:
            area[w] += g.cell ** 2
            cells[w].append((jj[q], ii[q]))
    roof = np.zeros(n_in)
    roof_of = []
    for b in roofs:
        _, near = tree_in.query([b.centroid.x, b.centroid.y])
        roof[near] += b.area
        roof_of.append(int(near))
    return area, roof, cells, roof_of


def pipe_dia(q_cfs, slope, n=0.012):
    d = ((q_cfs * n) / (0.463 * math.sqrt(max(slope, 1e-4)))) ** (3.0 / 8.0) * 12.0      # inches, full flow
    for s in PIPE_SIZES:
        if s >= d:
            return s
    return PIPE_SIZES[-1]


def full_flow(d_in, slope, n=0.012):
    d = d_in / 12.0
    return 0.463 / n * d ** (8.0 / 3.0) * math.sqrt(slope)


def corridor_route(g, a, b, blocked):
    """Least-cost grid route from a to b through cells not blocked (8-connected). Returns LineString or None."""
    ij0, ij1 = g.ij(*a), g.ij(*b)
    ok = ~blocked
    ok[ij0] = ok[ij1] = True
    dist = {ij0: 0.0}
    prev = {}
    pq = [(0.0, 0.0, ij0)]
    while pq:
        _f, d, cur = heapq.heappop(pq)
        if cur == ij1:
            break
        if d > dist.get(cur, 1e18):
            continue
        j, i = cur
        for dj, di in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            jj, ii = j + dj, i + di
            if 0 <= jj < g.ny and 0 <= ii < g.nx and ok[jj, ii]:
                nd = d + (1.414 if dj and di else 1.0) + (0.0 if abs(dj) + abs(di) == 1 or True else 0.0)
                h = math.hypot(jj - ij1[0], ii - ij1[1])
                if nd < dist.get((jj, ii), 1e18):
                    dist[(jj, ii)] = nd
                    prev[(jj, ii)] = cur
                    heapq.heappush(pq, (nd + 0.01 * h, nd, (jj, ii)))
    if ij1 not in prev and ij1 != ij0:
        return None
    path = [ij1]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    pts = [(a[0], a[1])] + [(g.xs[i], g.ys[j]) for j, i in path[::-1]][1:-1] + [(b[0], b[1])]
    if len(pts) < 2:
        return None
    return LineString(pts).simplify(CELL * 0.9)


def design(g, z_ex, z_prop, G, L, rain, soils, stype, parcel_area_sf, pond_override=None):
    """G: grading result; L: local geometries. Returns the stormwater design dict (JSON-safe parts + geometries)."""
    warns = []
    pave_poly = L["pave"]
    roofs = L["bldgs"]
    hsg = soils.get("hsg", "B")
    cn_lawn, cn_pre = CN["lawn"][hsg], CN["pre"][hsg]

    prof_lows = [c["low_sta"] for c in G["curves"] if c.get("low_sta") is not None]
    inlets = place_inlets(g, z_prop, G["pave_mask"], pave_poly, prof_lows, G["spine"])
    area_p, area_r, cells, roof_of = assign_catchments(g, z_prop, G["pave_mask"], pave_poly, inlets, roofs)

    # grass / disturbed pervious area draining toward the pond (traced on the proposed surface) vs bypass
    pond0 = pond_override if pond_override is not None else L["pond"]
    lod = G["lod_mask"]
    perv = lod & ~G["pave_mask"] & ~G["bmask"]
    grass_cells = np.argwhere(perv)
    pond_mask = g.mask(pond0.buffer(4)) if pond0 is not None else np.zeros(g.X.shape, bool)
    area_g_to_pond = 0.0
    area_g_bypass = 0.0
    for (j, i) in grass_cells[::2]:
        path = descend(z_prop, i, j, mask_stop=pond_mask | G["pave_mask"], max_steps=120)
        e = path[-1]
        if pond_mask[e] or G["pave_mask"][e]:
            area_g_to_pond += 2 * g.cell ** 2
        else:
            area_g_bypass += 2 * g.cell ** 2

    # subcatchments (post): one per inlet (pavement + roofs), plus lawn to pond, plus bypass
    sc = []
    p2 = rain["d24"][2]
    for k, (x, y) in enumerate(inlets):
        a_imp = area_p[k] + area_r[k]
        if a_imp <= 0:
            continue
        cn = hydro.composite_cn([(area_p[k], 98), (area_r[k], 98)])
        # time of concentration: 100 ft sheet over pavement + shallow flow along the gutter to the inlet
        far = 0.0
        for (j, i) in cells[k][::5]:
            far = max(far, math.hypot(g.xs[i] - x, g.ys[j] - y))
        sheet = min(100.0, far)
        shallow = max(0.0, far * 1.25 - sheet)
        tc = hydro.tc_min(sheet, 0.015, 0.011, shallow, 0.012, True, p2)
        sc.append({"id": f"SC-{len(sc) + 1}", "inlet": k, "area_sf": a_imp, "pave_sf": float(area_p[k]), "roof_sf": float(area_r[k]), "cn": cn, "tc": tc, "kind": "impervious"})
    if area_g_to_pond > 0:
        sc.append({"id": f"SC-{len(sc) + 1}", "inlet": None, "area_sf": area_g_to_pond, "pave_sf": 0.0, "roof_sf": 0.0, "cn": cn_lawn, "tc": 14.0, "kind": "lawn to pond"})
    total_to_pond = sum(s["area_sf"] for s in sc)
    area_ac = total_to_pond / 43560.0
    cn_post = hydro.composite_cn([(s["area_sf"], s["cn"]) for s in sc]) if sc else cn_lawn
    # weighted post Tc: area-weighted
    tc_post = sum(s["tc"] * s["area_sf"] for s in sc) / total_to_pond if total_to_pond else 10.0
    # pre-development: same land, undeveloped cover, longer path
    avg_slope = float(np.clip(np.nanmean(np.hypot(*np.gradient(z_ex, g.cell))[G["lod_mask"]]) if G["lod_mask"].any() else 0.03, 0.005, 0.2))
    pre_len = max(100.0, math.sqrt(total_to_pond) * 1.3)
    tc_pre = hydro.tc_min(100.0, avg_slope, 0.4, max(0.0, pre_len - 100.0), avg_slope, False, p2)
    cn_pre_area = cn_pre

    pre, post = {}, {}
    for s in hydro.STORMS:
        t, q = hydro.hydrograph(area_ac, cn_pre_area, tc_pre, rain["d24"][s], stype)
        pre[s] = q.max()
    # pond geometry
    top_polys = polys(pond0)
    pond_top = max(top_polys, key=lambda p: p.area) if top_polys else None
    if pond_top is None:
        pond_top = Point(L["parcel"].centroid).buffer(60)
        warns.append("No pond area in the layout; a detention basin footprint was assumed")
    ring = pond_top.buffer(8).difference(pond_top.buffer(1))
    rmask = g.mask(ring)
    ground_low = float(np.percentile(z_prop[rmask], 15)) if rmask.any() else float(np.nanmin(z_ex))
    ground_low = min(ground_low, float(np.percentile(z_ex[g.mask(pond_top.buffer(10))], 20))) if g.mask(pond_top.buffer(10)).any() else ground_low
    top_el = round(ground_low + 3.0, 1)
    # the footprint is the concept layout's pond and does not change; volume comes from depth
    from shapely.ops import polylabel
    try:
        inr = pond_top.exterior.distance(polylabel(pond_top, 1.0))
    except Exception:  # noqa: BLE001
        inr = 20.0
    d_total = {s: hydro.hydrograph(area_ac, cn_post, tc_post, rain["d24"][s], stype) for s in hydro.STORMS}
    res = None
    last = None
    # 3:1 earth slopes first; if the footprint is too narrow to go deep enough, steeper slopes / retaining walls
    for side in (3.0, 2.0, 1.0):
        max_depth = float(max(4.0, min(14.0, inr / side * 0.9)))
        for depth in np.arange(4.0, max_depth + 0.01, 0.5):
            bottom = round(top_el - depth, 1)
            stage, area, stor = hydro.pond_geometry(pond_top, top_el, depth, side)
            last = (depth, side)
            if stor[-1] < 500:
                continue
            for dia in (0.25, 0.33, 0.5, 0.67, 0.83, 1.0, 1.25, 1.5, 2.0):
                ok = True
                out = {}
                for s in hydro.STORMS:
                    t, q = d_total[s]
                    hp, qp, qo, hs = hydro.route_pond(t, q, stage, stor, (bottom + 0.5, dia), (top_el - 1.5, 10.0))
                    out[s] = (hp, qp, float(q.max()))
                    if s <= 50 and qp > pre[s] * 1.0:
                        ok = False
                if ok:
                    break
            if ok and out[100][0] <= top_el - 1.0:
                res = {"poly": pond_top, "stage": stage, "stor": stor, "orifice_ft": dia, "out": out, "depth": float(depth), "side": side}
                break
        if res is not None:
            break
    if res is None:
        depth, side = last or (4.0, 1.0)
        bottom = round(top_el - depth, 1)
        warns.append(f"Detention could not fully attenuate post to pre-development peaks within the layout's pond footprint at a {depth:.1f} ft depth; the footprint cannot change, so reduce impervious area or add underground storage")
        stage, area, stor = hydro.pond_geometry(pond_top, top_el, depth, side)
        dia = 0.5
        out = {}
        for s in hydro.STORMS:
            t, q = d_total[s]
            hp, qp, qo, hs = hydro.route_pond(t, q, stage, stor, (bottom + 0.5, dia), (top_el - 1.5, 10.0))
            out[s] = (hp, qp, float(q.max()))
        res = {"poly": pond_top, "stage": stage, "stor": stor, "orifice_ft": dia, "out": out, "depth": float(depth), "side": side}
    depth, side = res["depth"], res["side"]
    bottom = round(top_el - depth, 1)
    if side < 3.0:
        warns.append(f"The concept pond footprint is too narrow for 3:1 slopes at the depth needed; interior slopes are {side:g}:1 (retaining walls / structural slopes) to hold the volume")
    if depth > 8.0:
        warns.append(f"The pond is {depth:.1f} ft deep to hold the required volume inside the concept footprint; provide fencing / safety bench and confirm geotechnical and groundwater conditions")
    pond = {"poly": res["poly"], "top_el": top_el, "bottom_el": bottom, "depth_ft": depth, "footprint_fixed": True, "side": res["side"], "orifice_in": round(res["orifice_ft"] * 12, 1),
            "weir_crest": round(top_el - 1.5, 2), "weir_len_ft": 10.0, "top_area_sf": float(res["poly"].area), "storage_cf": float(res["stor"][-1]),
            "table": [{"storm": s, "pre_cfs": round(pre[s], 2), "post_cfs": round(res["out"][s][2], 2), "out_cfs": round(res["out"][s][1], 2),
                       "stage": round(res["out"][s][0], 2)} for s in hydro.STORMS]}
    if res["out"][100][0] > top_el - 1.0:
        warns.append("The 100-yr stage is within 1 ft of the pond berm; add freeboard")

    # impervious summary
    imp = float(sum(s["pave_sf"] + s["roof_sf"] for s in sc))
    return {"inlets": inlets, "subcatchments": sc, "cells": cells, "area_ac": area_ac, "cn_post": cn_post, "cn_pre": cn_pre_area, "tc_pre": tc_pre, "tc_post": tc_post,
            "pond": pond, "impervious_sf": imp, "bypass_sf": area_g_bypass, "hsg": hsg, "warnings": warns, "rain": rain, "stype": stype,
            "roof_of": roof_of, "area_p": area_p, "area_r": area_r}


def _bend_points(route, tol=8.0, min_seg=25.0):
    """Vertices of the simplified route where the pipe would have to turn; short legs are merged so structures are not stacked."""
    ls = route.simplify(tol)
    pts = list(ls.coords)
    out = [pts[0]]
    for p in pts[1:-1]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_seg and math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) >= min_seg:
            out.append(p)
    out.append(pts[-1])
    return out


def storm_network(g, z_prop, D, L, rain, blocked_extra=None):
    """Storm sewer from each inlet to the pond. Returns {structures, pipes} with rim / invert data."""
    inlets = D["inlets"]
    pond = D["pond"]
    pp = pond["poly"]
    # pipe corridor: not under buildings, not in the pond, inside the parcel margin
    blocked = g.mask(unary_union([b.buffer(6) for b in L["bldgs"]]))
    blocked |= g.mask(pp.buffer(-1))
    if L.get("wet") is not None and not L["wet"].is_empty:
        blocked |= g.mask(L["wet"])
    if blocked_extra is not None:
        blocked |= blocked_extra
    # outfall point: on the pond rim nearest the first inlet cluster
    cent = unary_union([Point(p) for p in inlets]).centroid
    fes = nearest_points(pp.exterior, cent)[0]
    structs = [{"id": "FES-1", "kind": "FES", "xy": (fes.x, fes.y), "rim": pond["top_el"], "inv_out": None, "inv_in": round(max(pond["bottom_el"] + 1.0, pond["top_el"] - 3.0), 2)}]
    pipes = []
    order = sorted(range(len(inlets)), key=lambda k: math.hypot(inlets[k][0] - fes.x, inlets[k][1] - fes.y))
    net_lines = []
    node_pts = [(fes.x, fes.y)]
    node_ids = ["FES-1"]
    cb_no = 0
    dmh_no = 0
    for k in order:
        x, y = inlets[k]
        cb_no += 1
        cid = f"CB-{cb_no}"
        # connect to the nearest existing network node or point on the network
        best, tgt = None, None
        cand = [Point(p) for p in node_pts]
        d_nodes = [Point(x, y).distance(c) for c in cand]
        ti = int(np.argmin(d_nodes))
        a = (x, y)
        b = node_pts[ti]
        route = corridor_route(g, a, b, blocked)
        if route is None:
            route = LineString([a, b])
        # snap CB onto the pavement low point, structure rim from the proposed surface
        rim = float(g.at(z_prop, [x], [y])[0])
        structs.append({"id": cid, "kind": "CB", "xy": (x, y), "rim": round(rim, 2), "inlet": k})
        # a pipe run is straight between structures: every bend gets a manhole (no joints in the pipe itself)
        cs = _bend_points(route)
        prev_id = cid
        for q, (bx, by) in enumerate(cs[1:-1], 1):
            dmh_no += 1
            did = f"DMH-{dmh_no}"
            structs.append({"id": did, "kind": "DMH", "xy": (bx, by), "rim": round(float(g.at(z_prop, [bx], [by])[0]), 2)})
            pipes.append({"from": prev_id, "to": did, "line": LineString([cs[q - 1], (bx, by)]), "inlet": k if prev_id == cid else None})
            node_pts.append((bx, by))
            node_ids.append(did)
            prev_id = did
        pipes.append({"from": prev_id, "to": node_ids[ti], "line": LineString([cs[-2], cs[-1]]), "inlet": k if prev_id == cid else None})
        node_pts.append((x, y))
        node_ids.append(cid)
    # hydraulics, from the outlet upstream
    sc = D["subcatchments"]
    byk = {s["inlet"]: s for s in sc if s["inlet"] is not None}
    q_of = {}
    tcs = {}
    P1 = rain["d1"][10]
    for p in pipes:
        if p["inlet"] is None:
            p["q_local"] = 0.0
            continue
        s = byk.get(p["inlet"])
        a_ac = (s["area_sf"] / 43560.0) if s else 0.1
        tc = max(10.0, s["tc"] if s else 10.0)
        i = P1 * (60.0 / tc) ** 0.65
        p["q_local"] = 0.9 * i * a_ac
    # accumulate downstream
    child = {}
    for p in pipes:
        child.setdefault(p["to"], []).append(p)
    def total_q(nid):
        q = sum(total_q(p["from"]) + p["q_local"] * 0 for p in child.get(nid, []))
        for p in child.get(nid, []):
            pass
        return q
    def acc(p):
        return p["q_local"] + sum(acc(c) for c in child.get(p["from"], []))
    sm = {s["id"]: s for s in structs}
    def set_inverts(nid, inv_dn):
        for p in child.get(nid, []):
            L_ft = p["line"].length
            q = acc(p)
            s_pipe = 0.005
            d = pipe_dia(q, s_pipe)
            # steeper where the cover is available, within limits
            up = sm[p["from"]]
            for s_try in (0.005, 0.0075, 0.01, 0.015, 0.02, 0.03):      # flattest slope that scours (>= 2.5 fps) keeps the system shallow
                d_try = pipe_dia(q, s_try)
                v = 1.486 / 0.012 * (d_try / 48.0) ** (2.0 / 3.0) * math.sqrt(s_try)
                if v >= 2.5:
                    s_pipe, d = s_try, d_try
                    break
            else:
                s_pipe, d = 0.03, pipe_dia(q, 0.03)
            inv_up = inv_dn + s_pipe * L_ft + 0.1         # 0.1 ft drop through each structure
            p.update({"len": L_ft, "dia": d, "slope": s_pipe, "q10": q, "inv_dn": round(inv_dn, 2), "inv_up": round(inv_up, 2),
                      "cap": full_flow(d, s_pipe), "mat": "HDPE" if d <= 36 else "RCP"})
            up["inv_out"] = round(inv_up, 2)
            up["cover"] = round(up["rim"] - (inv_up + d / 12.0), 1)
            up["dia_out"] = d
            set_inverts(p["from"], inv_up)
    set_inverts("FES-1", structs[0]["inv_in"])
    warns = []
    for s in structs:
        if s["kind"] in ("CB", "DMH") and s.get("cover") is not None and s["cover"] < 2.0:
            if s["cover"] < 0:
                warns.append(f"{s['id']}: the pond outfall is higher than this inlet, so gravity drainage does not work as drawn; lower the pond, regrade, or add a pump / forebay (engineer to resolve)")
            else:
                warns.append(f"{s['id']}: only {s['cover']} ft of cover over the outlet pipe; use RCP or raise grades")
        if s["kind"] in ("CB", "DMH") and s.get("inv_out") is not None and s["rim"] - s["inv_out"] > 12:
            warns.append(f"{s['id']}: structure deeper than 12 ft")
    return {"structs": structs, "pipes": pipes, "warnings": warns}


def site_pond(g, z_ex, z_prop, G, L, inlets, area_sf=9000.0, depth_below=5.0):
    """A pond seed (circle) next to the development where the ground sits about `depth_below` ft under the lowest inlet, so
    the storm sewer stays shallow. Returns a polygon or None."""
    allowed = g.mask(L["parcel"].buffer(-12)) & ~g.mask(L["dev"].buffer(10)) & ~G["bmask"]
    if L.get("wet") is not None and not L["wet"].is_empty:
        allowed &= ~g.mask(L["wet"].buffer(25))
    if L.get("flood") is not None:
        allowed &= ~g.mask(L["flood"])
    sl = 100.0 * np.hypot(*np.gradient(z_ex, g.cell))
    allowed &= sl < 18.0
    r = math.sqrt(area_sf / math.pi)
    room = ndi.distance_transform_edt(allowed) * g.cell >= r
    if not room.any():
        return None
    zlow = min(float(g.at(z_prop, [x], [y])[0]) for x, y in inlets)
    tree = cKDTree(np.array(inlets))
    jj, ii = np.nonzero(room)
    pts = np.c_[g.xs[ii], g.ys[jj]]
    d, _ = tree.query(pts)
    zz = z_ex[jj, ii]
    score = 3.0 * np.abs(zz - (zlow - depth_below)) + d / 25.0
    k = int(np.argmin(score))
    return Point(pts[k]).buffer(r, 48)
