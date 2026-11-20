import os
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


STORM_SMIN = 0.004       # storm pipes: never flatter than 0.40 %, no maximum slope

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



# ---------------------------------------------------------------------------------------------- underground chambers
CH_STONE_BASE, CH_HT, CH_STONE_TOP, CH_COVER = 0.75, 5.0, 0.5, 2.0       # ft: stone under, chamber (MC-4500 class), stone over, cover to finished grade
CH_SF_PER_UNIT = 36.1                                                      # installed footprint of one chamber with its stone (sf)
CH_CF_PER_SF = 4.5                                                         # chamber + stone storage per sf of bed (MC-4500 class: 162.6 CF / 36.1 sf)


def _max_rect(m):
    """Largest all-True axis-aligned rectangle in a boolean grid: (area, i0, j0, i1, j1) with i = column, j = row, inclusive."""
    h, w = m.shape
    best = (0, 0, 0, 0, 0)
    hs = np.zeros(w, int)
    for r in range(h):
        hs = np.where(m[r], hs + 1, 0)
        stack = []
        for c in range(w + 1):
            cur = int(hs[c]) if c < w else 0
            start = c
            while stack and stack[-1][1] >= cur:
                s0, hh = stack.pop()
                area = hh * (c - s0)
                if area > best[0]:
                    best = (area, s0, r - hh + 1, c - 1, r)
                start = s0
            stack.append((start, cur))
    return best


def chamber_stage(A, bottom):
    """Stage / storage tables for a chamber bed of footprint A (sf) with its stone base at `bottom`."""
    hs = [0.0, CH_STONE_BASE, CH_STONE_BASE + CH_HT, CH_STONE_BASE + CH_HT + CH_STONE_TOP]
    per = [0.4, (CH_CF_PER_SF - 0.4 * (CH_STONE_BASE + CH_STONE_TOP)) / CH_HT, 0.4]
    stage, stor = [], []
    for k in range(3):
        for t in (0.0, 0.5, 1.0):
            h = hs[k] + (hs[k + 1] - hs[k]) * t
            if stage and abs(stage[-1] - (bottom + h)) < 1e-9:
                continue
            v = (stor[-1] if stor else 0.0) + (0.0 if not stage else per[k] * A * (bottom + h - stage[-1]))
            stage.append(bottom + h)
            stor.append(v)
    return stage, stor


def _chambers(g, z_prop, G, L, inlets, d_total, pre, warns):
    """Underground detention (chambers in a stone bed) under pavement, clear of buildings. Footprint comes from the paved area."""
    c = g.cell
    pave = L["pave"]
    blds = [b for b in L["bldgs"] if b is not None] + [b for b in (L.get("keep_bldgs") or []) if b is not None]
    allowed_poly = pave.buffer(-2.0)
    if blds:
        allowed_poly = allowed_poly.difference(unary_union(blds).buffer(10.0))
    allowed_poly = allowed_poly.intersection(L["parcel"].buffer(-10.0))
    if L.get("wet") is not None and not L["wet"].is_empty:
        allowed_poly = allowed_poly.difference(L["wet"].buffer(10.0))
    base_mask = g.mask(allowed_poly) if not allowed_poly.is_empty else np.zeros(g.X.shape, bool)
    rims = []
    for x, y in inlets:
        r_ = float(g.at(z_prop, [x], [y])[0])
        if np.isfinite(r_):
            rims.append(r_)
    bound = (min(rims) - 0.3 - 0.005 * 150.0) if rims else float(np.nanmax(z_prop))
    zfin = np.where(np.isfinite(z_prop), z_prop, 1e9)

    def collect(m0, n_max=16):
        m = m0.copy()
        out = []
        for _ in range(n_max):
            area, i0, j0, i1, j1 = _max_rect(m)
            if area == 0 or min(i1 - i0 + 1, j1 - j0 + 1) * c < 12.0 or area * c * c < 500.0:
                break
            out.append((i0, j0, i1, j1))
            m[max(j0 - 1, 0):j1 + 2, max(i0 - 1, 0):i1 + 2] = False
        return out

    # prefer the lowest paved ground (the storm sewer has to reach the chambers by gravity); widen the search if the volume needs it
    rects, thr_used = [], None
    for thr in (bound, bound + 2.0, bound + 5.0, 1e9):
        r_ = collect(base_mask & (zfin <= thr))
        if sum((q[2] - q[0] + 1) * (q[3] - q[1] + 1) for q in r_) * c * c >= 500.0:
            rects, thr_used = r_, thr
            if sum((q[2] - q[0] + 1) * (q[3] - q[1] + 1) for q in rects) * c * c >= 6000.0:
                break
    if not rects:
        raise RuntimeError("No paved area clear of buildings is large enough for underground detention chambers")
    areas = [(q[2] - q[0] + 1) * (q[3] - q[1] + 1) * c * c for q in rects]
    A_avail = float(sum(areas))
    cells = np.zeros(g.X.shape, bool)
    for (i0, j0, i1, j1) in rects:
        cells[j0:j1 + 1, i0:i1 + 1] = True
    top_el = round(float(np.nanmin(np.where(cells, z_prop, np.nan))), 2)
    stone_top = top_el - CH_COVER
    bottom = round(stone_top - (CH_STONE_BASE + CH_HT + CH_STONE_TOP), 2)
    crest = round(bottom + CH_STONE_BASE + CH_HT - 0.25, 2)
    res = None
    last = None
    for A in np.arange(400.0, A_avail + 1.0, 100.0):
        stage, stor = chamber_stage(float(A), bottom)
        last = float(A)
        for dia in (0.25, 0.33, 0.5, 0.67, 0.83, 1.0, 1.25, 1.5, 2.0):
            ok, out = True, {}
            for st in hydro.STORMS:
                t, q = d_total[st]
                hp, qp, qo, hs_ = hydro.route_pond(t, q, stage, stor, (bottom + 0.3, dia), (crest, 10.0))
                out[st] = (hp, qp, float(q.max()))
                if st <= 50 and qp > pre[st]:
                    ok = False
            if ok:
                break
        if ok and out[100][0] <= bottom + CH_STONE_BASE + CH_HT + 0.2:
            res = (float(A), dia, out, stage, stor)
            break
    if res is None:
        A = A_avail
        stage, stor = chamber_stage(A, bottom)
        dia = 0.5
        out = {}
        for st in hydro.STORMS:
            t, q = d_total[st]
            hp, qp, qo, hs_ = hydro.route_pond(t, q, stage, stor, (bottom + 0.3, dia), (crest, 10.0))
            out[st] = (hp, qp, float(q.max()))
        res = (A, dia, out, stage, stor)
        warns.append(f"The paved area clear of buildings ({A_avail:,.0f} SF) cannot hold enough chambers to bring post-development peaks down to pre-development; reduce impervious area or add storage elsewhere")
    A, dia, out, stage, stor = res
    # allocate the needed footprint across the beds, trimming the last one along its long side
    beds, left = [], A
    for (i0, j0, i1, j1), ar in sorted(zip(rects, areas), key=lambda t: -t[1]):
        if left <= 50:
            break
        w_c, h_c = i1 - i0 + 1, j1 - j0 + 1
        take = min(ar, left)
        if take < ar:
            if w_c >= h_c:
                w_c = max(3, int(math.ceil(take / (c * c) / h_c)))
                i1 = i0 + w_c - 1
            else:
                h_c = max(3, int(math.ceil(take / (c * c) / w_c)))
                j1 = j0 + h_c - 1
            take = w_c * h_c * c * c
        x0, x1 = g.xs[i0] - c / 2, g.xs[i1] + c / 2
        y0, y1 = g.ys[j0] - c / 2, g.ys[j1] + c / 2
        from shapely.geometry import box as _box
        beds.append(_box(x0, y0, x1, y1))
        left -= take
    A_real = float(sum(b.area for b in beds))
    stage, stor = chamber_stage(A_real, bottom)
    n_ch = int(round(A_real / CH_SF_PER_UNIT))
    poly = unary_union(beds)
    warns.append(f"No detention pond in the concept layout: stormwater is held in underground chambers (MC-4500 class, {n_ch} units in {len(beds)} bed{'s' if len(beds) != 1 else ''}, {A_real:,.0f} SF) "
                 f"under the parking, clear of buildings ({CH_COVER:g} ft minimum cover to finished grade); size, manufacturer and traffic loading to be confirmed with the engineer of record")
    if thr_used is not None and thr_used > bound + 0.01:
        warns.append("The chambers could not all be placed at the low end of the paving, so part of the storm sewer reaches them with little fall")
    pond = {"kind": "chambers", "poly": poly, "beds": beds, "n_chambers": n_ch, "top_el": top_el, "stone_top_el": round(stone_top, 2), "bottom_el": bottom,
            "depth_ft": round(top_el - bottom, 2), "footprint_fixed": False, "side": 0.0, "orifice_in": round(dia * 12, 1), "weir_crest": crest, "weir_len_ft": 10.0,
            "top_area_sf": float(A_real), "storage_cf": float(stor[-1]), "cover_ft": CH_COVER,
            "table": [{"storm": st, "pre_cfs": round(pre[st], 2), "post_cfs": round(out[st][2], 2), "out_cfs": round(out[st][1], 2), "stage": round(out[st][0], 2)} for st in hydro.STORMS]}
    return pond


def design(g, z_ex, z_prop, G, L, rain, soils, stype, parcel_area_sf, pond_override=None, floor_max=None, n_sys=None):
    """G: grading result; L: local geometries. Returns the stormwater design dict (JSON-safe parts + geometries)."""
    warns = []
    pave_poly = L["pave"]
    roofs = L["bldgs"]
    hsg = soils.get("hsg", "B")
    cn_lawn, cn_pre = CN["lawn"][hsg], CN["pre"][hsg]

    prof_lows = [c["low_sta"] for c in G["curves"] if c.get("low_sta") is not None]
    inlets = place_inlets(g, z_prop, G["pave_mask"], pave_poly, prof_lows, G["spine"])
    area_p, area_r, cells, roof_of = assign_catchments(g, z_prop, G["pave_mask"], pave_poly, inlets, roofs)
    # no catch basin takes more than about 0.7 ac of pavement and roof: an overloaded inlet gets help at the far end of what drains to it
    for _ in range(40):
        k_big = int(np.argmax(area_p + area_r)) if len(inlets) else -1
        if k_big < 0 or area_p[k_big] + area_r[k_big] <= 30000.0 or len(inlets) >= 45 or not cells[k_big]:
            break
        cj = np.array(cells[k_big])
        cx_, cy_ = g.xs[cj[:, 1]], g.ys[cj[:, 0]]
        dmin_ = np.min([np.hypot(cx_ - a, cy_ - b) for a, b in inlets], axis=0)
        far_i = int(np.argmax(dmin_))
        if dmin_[far_i] < 80.0:
            break
        nr = nearest_points(Point(float(cx_[far_i]), float(cy_[far_i])), pave_poly.boundary)[1]
        if min(math.hypot(nr.x - a, nr.y - b) for a, b in inlets) < 60.0:
            break
        inlets = list(inlets) + [(nr.x, nr.y)]
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
    if pond0 is None or pond0.is_empty:
        d_tot = {st: hydro.hydrograph(area_ac, cn_post, tc_post, rain["d24"][st], stype) for st in hydro.STORMS}
        pond = _chambers(g, z_prop, G, L, inlets, d_tot, pre, warns)
        imp = float(sum(s_["pave_sf"] + s_["roof_sf"] for s_ in sc))
        return {"inlets": inlets, "subcatchments": sc, "cells": cells, "area_ac": area_ac, "cn_post": cn_post, "cn_pre": cn_pre_area, "tc_pre": tc_pre, "tc_post": tc_post,
                "pond": pond, "ponds": [pond], "impervious_sf": imp, "bypass_sf": area_g_bypass, "hsg": hsg, "warnings": warns, "rain": rain, "stype": stype,
                "roof_of": roof_of, "area_p": area_p, "area_r": area_r}
    # ---- stormwater ponds. At this (preliminary) stage a pond is sized for the water quality volume only: WQV = 1" x Rv x A,
    # Rv = 0.05 + 0.9 x impervious fraction, held below a 0.5 ft freeboard. Rules: 3:1 interior slopes always; 2 ft deep as the
    # target, never deeper than 3 ft; no larger than it needs to be; no closer than 5 ft to a property line. One pond at the
    # concept location when that works; where the storm sewer would have to run so far that the pond ends up dug well below the
    # ground around it (long sites), the site is split into several drainage systems, each with its own pond.
    from shapely.ops import polylabel
    SIDE, DMIN, DMAX, DTARGET, EDGE, FREEBOARD = 3.0, 1.5, 3.0, 2.0, 5.0, 0.5
    imp_sc = {s_["inlet"]: s_ for s_ in sc if s_["inlet"] is not None}
    imp_total = sum(s_["area_sf"] for s_ in imp_sc.values()) or 1.0
    keep = [x for x in [L.get("pave"), L.get("walk")] + list(L.get("bldgs") or []) if x is not None and not x.is_empty]
    free = L["parcel"].buffer(-EDGE).difference(unary_union(keep).buffer(10.0)) if keep else L["parcel"].buffer(-EDGE)
    if L.get("wet") is not None and not L["wet"].is_empty:
        free = free.difference(L["wet"].buffer(25.0))
    top_polys = polys(pond0)
    concept = max(top_polys, key=lambda p: p.area) if top_polys else None
    if concept is None:
        concept = Point(L["parcel"].centroid).buffer(60).intersection(L["parcel"].buffer(-EDGE))
        warns.append("No pond area in the layout; a detention basin footprint was assumed")
    concept = max(polys(concept.intersection(L["parcel"].buffer(-EDGE))) or [concept], key=lambda p: p.area)
    rim_of = {}
    for k_, (x, y) in enumerate(inlets):
        v = float(g.at(z_prop, [x], [y])[0])
        rim_of[k_] = v if np.isfinite(v) else float(np.nanmean(z_prop[G["pave_mask"]]))
    live = [k_ for k_ in range(len(inlets)) if k_ in imp_sc]

    def site_for(group, taken):
        """Open land for a new pond near the low end of a group of inlets."""
        region = free.difference(unary_union(taken)) if taken else free
        low = min(group, key=lambda k_: rim_of[k_])
        lx, ly = inlets[low]
        cx_, cy_ = np.mean([inlets[k_][0] for k_ in group]), np.mean([inlets[k_][1] for k_ in group])
        best = None
        for er in (22.0, 16.0, 11.0):
            core = region.buffer(-er)
            if core.is_empty:
                continue
            x0, y0, x1, y1 = core.bounds
            st = max(12.0, max(x1 - x0, y1 - y0) / 60.0)
            for xx in np.arange(x0, x1 + 0.1, st):
                for yy in np.arange(y0, y1 + 0.1, st):
                    pt = Point(xx, yy)
                    if not core.contains(pt):
                        continue
                    zg = float(g.at(z_ex, [xx], [yy])[0])
                    if not np.isfinite(zg):
                        continue
                    sc_ = 0.6 * math.hypot(xx - lx, yy - ly) + 0.4 * math.hypot(xx - cx_, yy - cy_) + 25.0 * max(0.0, zg - (rim_of[low] - 2.0))
                    if best is None or sc_ < best[0]:
                        best = (sc_, pt, er)
            if best is not None:
                break
        if best is None:
            return None
        seed = best[1].buffer(best[2] + 8.0).intersection(region)
        ps_ = [q for q in polys(seed) if q.contains(best[1])]
        return ps_[0] if ps_ and ps_[0].area > 500 else None

    def size_one(seed, group, taken, fm):
        """Size one pond for the inlets in `group`. Returns the pond dict (with 'ok' and 'lowered')."""
        share = sum(imp_sc[k_]["area_sf"] for k_ in group) / imp_total
        parts = [(imp_sc[k_]["area_sf"], imp_sc[k_]["cn"], imp_sc[k_]["tc"]) for k_ in group]
        if area_g_to_pond > 0:
            parts.append((area_g_to_pond * share, cn_lawn, 14.0))
        a_sf = sum(p_[0] for p_ in parts)
        a_ac = a_sf / 43560.0
        imp_k = sum(imp_sc[k_]["pave_sf"] + imp_sc[k_]["roof_sf"] for k_ in group)
        rv = 0.05 + 0.9 * min(1.0, imp_k / max(a_sf, 1.0))
        wqv = 1.0 / 12.0 * rv * a_sf
        cn_k = hydro.composite_cn([(p_[0], p_[1]) for p_ in parts])
        tc_k = sum(p_[0] * p_[2] for p_ in parts) / a_sf
        d_tot = {st: hydro.hydrograph(a_ac, cn_k, tc_k, rain["d24"][st], stype) for st in hydro.STORMS}
        pre_k = {st: pre[st] * a_sf / max(total_to_pond, 1.0) for st in hydro.STORMS}
        ring = seed.buffer(8).difference(seed.buffer(1))
        rmask = g.mask(ring)
        ground_low = float(np.percentile(z_prop[rmask], 15)) if rmask.any() else float(np.nanmin(z_ex))
        m10 = g.mask(seed.buffer(10))
        if m10.any():
            ground_low = min(ground_low, float(np.percentile(z_ex[m10], 20)))
        top0 = round(ground_low + 0.5, 1)                  # top of bank about at the low side of the ground it sits in: the basin is dug, not bermed up
        cpp = seed.centroid
        # where the storm sewer will arrive: 2 ft cover + pipe at each inlet, then about 0.3 % along the run
        arrive = min(rim_of[k_] - 3.5 - 0.003 * 1.25 * math.hypot(inlets[k_][0] - cpp.x, inlets[k_][1] - cpp.y) for k_ in group) if fm is None else fm
        top_el = round(min(top0, arrive - 0.5 + DMAX), 1)             # the floor (top - depth, depth <= 3) must reach under the pipe
        dmin = float(min(DMAX, max(DMIN, math.ceil((top_el - (arrive - 0.5)) * 2.0) / 2.0)))
        lowered = max(0.0, top0 - top_el)
        if os.environ.get("FD_DEBUG"):
            print("   site", [round(v, 1) for v in (cpp.x, cpp.y)], "ground_low", round(ground_low, 1), "min rim", round(min(rim_of[k_] for k_ in group), 1), "arrive", round(arrive, 1), "top", top_el, "dmin", dmin,
                  "dist", [round(math.hypot(inlets[k_][0] - cpp.x, inlets[k_][1] - cpp.y)) for k_ in group])
        region = free.difference(unary_union(taken)) if taken else free
        region = region.union(seed)
        cands = [seed]
        # smaller than the concept footprint too: a pond is only as big as its water quality volume needs
        shr = []
        for k_ in range(1, 30):
            c_ = seed.buffer(-3.0 * k_).buffer(1.0)
            ps0 = polys(c_)
            if not ps0:
                break
            c_ = max(ps0, key=lambda q_: q_.area)
            if c_.area < 300:
                break
            shr.append(c_)
        for k_ in range(1, 20):
            cand = seed.buffer(8.0 * k_).intersection(region).buffer(-6.0).buffer(6.0)
            cps = [c_ for c_ in polys(cand) if c_.intersects(seed)]
            if not cps:
                break
            cand = max(polys(max(cps, key=lambda c_: c_.area).union(seed).buffer(0)), key=lambda c_: c_.area).simplify(1.0)
            if cand.area < cands[-1].area * 1.03:
                break
            cands.append(cand)
        cands = shr[::-1] + cands                                        # smallest first

        def route(poly, depth, dia):
            bottom = round(top_el - depth, 1)
            stage, area, stor = hydro.pond_geometry(poly, top_el, depth, SIDE)
            out = {}
            for st in hydro.STORMS:
                t, q = d_tot[st]
                hp, qp, qo, hs = hydro.route_pond(t, q, stage, stor, (bottom + 0.5, dia), (top_el - 1.5, 10.0))
                out[st] = (hp, qp, float(q.max()))
            return stage, stor, out

        def works(poly, depth):
            try:
                inr = poly.exterior.distance(polylabel(poly, 1.0))
            except Exception:  # noqa: BLE001
                inr = 20.0
            if inr < depth * SIDE * 0.5:                  # the 3:1 banks would meet before reaching this depth over most of the basin
                return None
            stage, area, stor = hydro.pond_geometry(poly, top_el, depth, SIDE)
            if float(np.interp(top_el - FREEBOARD, stage, stor)) < wqv:
                return None
            stage, stor, out = route(poly, depth, 0.25)   # outlet shown for reference only: the pond is sized on the WQV
            return {"poly": poly, "stage": stage, "stor": stor, "orifice_ft": 0.25, "out": out, "depth": float(depth)}

        res = None
        # 2 ft deep (or what the storm sewer needs, up to 3 ft) with the smallest footprint that holds the WQV; deeper only if no footprint will
        depths = sorted({max(dmin, DTARGET)} | {float(d_) for d_ in np.arange(max(dmin, DTARGET) + 0.5, DMAX + 0.01, 0.5)})
        for depth in depths:
            for cand in cands:
                res = works(cand, depth)
                if res is not None:
                    break
            if res is not None:
                break
        ok = res is not None
        if res is None:
            poly = cands[-1]
            stage, stor, out = route(poly, DMAX, 0.25)
            res = {"poly": poly, "stage": stage, "stor": stor, "orifice_ft": 0.25, "out": out, "depth": DMAX}
        depth = res["depth"]
        return {"poly": res["poly"], "top_el": top_el, "bottom_el": round(top_el - depth, 1), "depth_ft": depth, "footprint_fixed": res["poly"].equals(concept), "side": SIDE,
                "orifice_in": round(res["orifice_ft"] * 12, 1), "weir_crest": round(top_el - 1.5, 2), "weir_len_ft": 10.0, "top_area_sf": float(res["poly"].area),
                "storage_cf": float(res["stor"][-1]), "wqv_cf": round(wqv), "inlets": list(group), "area_ac": a_ac, "cn_post": cn_k, "tc_post": tc_k, "ok": ok, "lowered": lowered,
                "freeboard_ok": True,
                "table": [{"storm": st, "pre_cfs": round(pre_k[st], 2), "post_cfs": round(res["out"][st][2], 2), "out_cfs": round(res["out"][st][1], 2),
                           "stage": round(res["out"][st][0], 2)} for st in hydro.STORMS]}

    def build(k):
        # contiguous groups of inlets along the drive, balanced by the impervious area they collect
        order = sorted(live, key=lambda q: G["spine"].project(Point(inlets[q])))
        groups, acc_, tot = [[] for _ in range(k)], 0.0, sum(imp_sc[q]["area_sf"] for q in order) or 1.0
        for q in order:
            gi = min(k - 1, int(acc_ / tot * k))
            groups[gi].append(q)
            acc_ += imp_sc[q]["area_sf"]
        groups = [gr for gr in groups if gr]
        cc = concept.centroid
        near = min(range(len(groups)), key=lambda i_: math.hypot(np.mean([inlets[q][0] for q in groups[i_]]) - cc.x, np.mean([inlets[q][1] for q in groups[i_]]) - cc.y))
        ponds_, taken = [None] * len(groups), []
        for i_ in [near] + [j_ for j_ in range(len(groups)) if j_ != near]:
            seed = concept if i_ == near else site_for(groups[i_], taken)
            if seed is None:
                return None
            fm = floor_max.get(i_) if isinstance(floor_max, dict) else (floor_max if len(groups) == 1 else None)
            ponds_[i_] = size_one(seed, groups[i_], taken, fm)
            taken.append(ponds_[i_]["poly"].buffer(12.0))
        return ponds_

    def bad(ps_):                                                    # lower is better: all ponds work, none dug far below its surroundings, fewest ponds
        return (sum(1 for q in ps_ if not q["ok"]), round(max(0.0, max(q["lowered"] for q in ps_) - 1.5)), len(ps_))

    best = None
    if not live:
        live = list(range(len(inlets)))
    for k in ([n_sys] if n_sys else range(1, min(5, max(1, len(live))) + 1)):
        ps_ = build(k)
        if os.environ.get("FD_DEBUG"):
            print("ponds k", k, None if ps_ is None else [(q["ok"], round(q["lowered"], 1), q["depth_ft"], round(q["top_area_sf"]), q["inlets"]) for q in ps_], "free", round(free.area))
        if ps_ is None:
            continue
        if best is None or bad(ps_) < bad(best):
            best = ps_
        if bad(ps_)[:2] == (0, 0):
            break
    ponds = best
    for i_, q in enumerate(ponds, 1):
        q["id"] = f"POND-{i_}" if len(ponds) > 1 else "POND"
        if not q["ok"]:
            warns.append(f"{q['id'].title()}: the open land available ({q['top_area_sf']:,.0f} SF at 3:1, 3 ft deep) does not hold the water quality volume ({q['wqv_cf']:,} CF); "
                         "reduce impervious area or add underground storage")
        elif not q["footprint_fixed"] and len(ponds) == 1:
            warns.append(f"Pond sized for the water quality volume ({q['wqv_cf']:,} CF): {q['top_area_sf']:,.0f} SF at the top of bank, 3:1 slopes, {q['depth_ft']:.1f} ft deep")
        if not q["freeboard_ok"]:
            warns.append(f"{q['id'].title()}: the 100-yr stage is within 1 ft of the berm; add freeboard")
        if q["lowered"] > 1.5:
            warns.append(f"{q['id'].title()}: top of bank set {q['lowered']:.1f} ft below the ground around it so the storm sewer can drain to it by gravity")
    if len(ponds) > 1:
        warns.append(f"The site drains to {len(ponds)} detention ponds spread along the development so each storm sewer run stays short and each pond stays shallow "
                     "(" + ", ".join(f"{q['depth_ft']:.1f}" for q in ponds) + " ft deep, 3:1 slopes)")
    L["pond"] = unary_union([q["poly"] for q in ponds])
    pond = max(ponds, key=lambda q: q["storage_cf"])
    imp = float(sum(s["pave_sf"] + s["roof_sf"] for s in sc))
    return {"inlets": inlets, "subcatchments": sc, "cells": cells, "area_ac": area_ac, "cn_post": cn_post, "cn_pre": cn_pre_area, "tc_pre": tc_pre, "tc_post": tc_post,
            "pond": pond, "ponds": ponds, "impervious_sf": imp, "bypass_sf": area_g_bypass, "hsg": hsg, "warnings": warns, "rain": rain, "stype": stype,
            "roof_of": roof_of, "area_p": area_p, "area_r": area_r}

def _bend_points(route, tol=8.0, min_seg=25.0, keepout=None):
    """Vertices of the simplified route where the pipe would have to turn; short legs are merged so structures are not stacked.
    keepout (shapely geometry, e.g. building footprints): no leg of the result may cross it. If the coarse simplification
    cuts a corner through it, the tolerance and the merge length are tightened until every leg is clear."""
    def build(t, m):
        pts = list(route.simplify(t).coords)
        out = [pts[0]]
        for p in pts[1:-1]:
            if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= m and math.hypot(p[0] - pts[-1][0], p[1] - pts[-1][1]) >= m:
                out.append(p)
        out.append(pts[-1])
        return out

    def clear(pts):
        return keepout is None or keepout.is_empty or LineString(pts).intersection(keepout).length < 0.5

    out = build(tol, min_seg)
    if clear(out):
        return out
    for t, m in ((4.0, 15.0), (2.0, 8.0), (1.0, 4.0), (0.5, 0.0)):
        out = build(t, m)
        if clear(out):
            return out
    return list(route.coords)


def storm_network(g, z_prop, D, L, rain, blocked_extra=None, pond=None, start=(0, 0, 1)):
    """Storm sewer from each inlet of one drainage system to its pond. Returns {structures, pipes} with rim / invert data.
    start = (catch basins, manholes already numbered, outfall number) so several systems on one site number through."""
    inlets = D["inlets"]
    pond = pond or D["pond"]
    mine = pond.get("inlets") if pond.get("inlets") is not None else list(range(len(inlets)))
    FID = f"FES-{start[2]}"
    pp = pond["poly"]
    # pipe corridor: not under buildings, not in the pond, inside the parcel margin
    bkeep = unary_union([b.buffer(2) for b in L["bldgs"]])
    blocked = g.mask(unary_union([b.buffer(6) for b in L["bldgs"]]))
    blocked |= g.mask(pp.buffer(-1))
    if L.get("wet") is not None and not L["wet"].is_empty:
        blocked |= g.mask(L["wet"])
    if blocked_extra is not None:
        blocked |= blocked_extra
    # outfall point: on the pond rim nearest the first inlet cluster
    cent = unary_union([Point(inlets[k]) for k in mine]).centroid if mine else pp.centroid
    chambers = pond.get("kind") == "chambers"
    fes = nearest_points(pp.boundary, cent)[0]
    if chambers:
        # the sewer enters the chamber bed through its header (crown about 3 ft under the lowest finished grade over the bed)
        structs = [{"id": FID, "kind": "FES", "xy": (fes.x, fes.y), "rim": pond["top_el"], "inv_out": None, "inv_in": round(pond["top_el"] - 3.5, 2), "label": "CHAMBER INLET"}]
    else:
        structs = [{"id": FID, "kind": "FES", "xy": (fes.x, fes.y), "rim": pond["top_el"], "inv_out": None, "inv_in": round(max(pond["bottom_el"] + 0.5, pond["top_el"] - 3.0), 2), "pond": pond.get("id")}]
    pipes = []
    order = sorted(mine, key=lambda k: math.hypot(inlets[k][0] - fes.x, inlets[k][1] - fes.y))
    net_lines = []
    node_pts = [(fes.x, fes.y)]
    node_ids = [FID]
    cb_no, dmh_no = start[0], start[1]
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
        if route is None:       # tighter fallback: only the buildings themselves stay blocked
            route = corridor_route(g, a, b, g.mask(bkeep))
        if route is None:
            route = LineString([a, b])
        # snap CB onto the pavement low point, structure rim from the proposed surface
        rim = float(g.at(z_prop, [x], [y])[0])
        structs.append({"id": cid, "kind": "CB", "xy": (x, y), "rim": round(rim, 2), "inlet": k})
        # a pipe run is straight between structures: every bend gets a manhole (no joints in the pipe itself)
        cs = _bend_points(route, keepout=bkeep)
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
    # ---- hydraulic design, upstream to downstream. Each run follows the finished grade at minimum cover where the ground falls,
    # is never flatter than the self-cleansing slope, never smaller than the pipes feeding it, and matches crowns at every structure.
    COVER, DROP, VMIN, VMAX, NMAN = 2.0, 0.1, 2.5, 10.0, 0.012
    def s_for_v(d, v):
        return (v * NMAN / 1.486 / (d / 48.0) ** (2.0 / 3.0)) ** 2
    fes_target = structs[0]['inv_in']
    done = set()
    def size(p):
        if id(p) in done:
            return
        done.add(id(p))
        ins = child.get(p["from"], [])
        for c in ins:
            size(c)
        up, dn = sm[p["from"]], sm[p["to"]]
        L_ft = max(p["line"].length, 1.0)
        q = acc(p)
        dmin = max([c["dia"] for c in ins] or [12])
        pick = None
        for d in [x for x in PIPE_SIZES if x >= dmin]:
            wall = d / 12.0
            inv_up = up["rim"] - COVER - wall
            for c in ins:                                   # crowns match; at least a 0.1 ft drop through the structure
                inv_up = min(inv_up, c["inv_dn"] + c["dia"] / 12.0 - wall, c["inv_dn"] - DROP)
            if dn["kind"] == "FES":                       # the outfall run falls to the pond / chamber invert
                s_gr = (inv_up - fes_target) / L_ft
            else:
                s_gr = (inv_up - (dn["rim"] - COVER - wall)) / L_ft
            s_use = max(STORM_SMIN, s_gr)                  # storm rule (all jobs): 0.40 % minimum, no maximum — follows the ground at min cover
            s_use = math.ceil(s_use * 2000.0 - 1e-9) / 2000.0     # slopes called out to 0.05%
            if dn["kind"] == "FES" and inv_up - s_use * L_ft > fes_target + 0.5:
                inv_up = fes_target + s_use * L_ft         # the rest of the fall is taken as a drop in the last structure
            pick = (d, inv_up, s_use)
            if full_flow(d, s_use, NMAN) >= q:
                break
        d, inv_up, s_use = pick
        inv_dn = inv_up - s_use * L_ft
        cap = full_flow(d, s_use, NMAN)
        vf = cap / (math.pi * (d / 24.0) ** 2)
        p.update({"len": L_ft, "dia": d, "slope": s_use, "q10": q, "inv_dn": round(inv_dn, 2), "inv_up": round(inv_up, 2), "cap": cap, "v": round(vf, 1),
                  "mat": "HDPE" if d <= 36 else "RCP", "cover_up": round(up["rim"] - inv_up - d / 12.0, 1),
                  "cover_dn": round(dn["rim"] - inv_dn - d / 12.0, 1) if dn["kind"] != "FES" else None})
        up["inv_out"] = round(inv_up, 2)
        up["cover"] = round(up["rim"] - (inv_up + d / 12.0), 1)
        up["dia_out"] = d
        up["inv_ins"] = [(c["from"], c["inv_dn"], c["dia"]) for c in ins]
        if (ins and min(c["inv_dn"] for c in ins) - inv_up > 2.0) or (not ins and up["rim"] - inv_up - d / 12.0 > COVER + 2.0):
            up["drop"] = True
        if dn["kind"] == "FES" and vf > VMAX:
            dn["dissipator"] = True
    for p in child.get(FID, []):
        size(p)
    last = child.get(FID, [])
    warns = []
    if last:
        fes_inv = min(p["inv_dn"] for p in last)
        floor = structs[0]["inv_in"] if chambers else pond["bottom_el"]
        structs[0]["inv_in"] = round(fes_inv, 2)
        if fes_inv < floor - 0.05:
            warns.append(f"The storm sewer arrives at the {'chamber bed' if chambers else 'pond'} at {fes_inv:.2f}, which is {floor - fes_inv:.1f} ft below its floor ({floor:.2f}); "
                         "lower the " + ("bed" if chambers else "pond floor") + " or raise the pavement grades (engineer to resolve)")
    if chambers:
        _chamber_outlet(g, z_prop, D, L, pond, structs, pipes, blocked, dmh_no, warns)
    for s in structs:
        if s["kind"] in ("CB", "DMH") and s.get("inv_out") is not None and s["rim"] - s["inv_out"] > 12:
            warns.append(f"{s['id']}: structure deeper than 12 ft")
    return {"structs": structs, "pipes": pipes, "warnings": warns}


def _chamber_outlet(g, z_prop, D, L, pond, structs, pipes, blocked, dmh_no, warns):
    """Outlet control manhole beside the chamber bed and a gravity discharge pipe to a flared end at the low side of the property."""
    pp = pond["poly"]
    cent = pp.centroid
    ring = L["parcel"].buffer(-3.0).boundary
    best, bd = None, 1e18
    for s_ in np.arange(0, ring.length, 10.0):
        p = ring.interpolate(s_)
        dist = math.hypot(p.x - cent.x, p.y - cent.y)
        if dist > 600:
            continue
        zz = float(g.at(z_prop, [p.x], [p.y])[0])
        if not np.isfinite(zz):
            continue
        sc_ = zz + 0.004 * dist
        if sc_ < bd:
            best, bd = (p.x, p.y, zz), sc_
    if best is None:
        warns.append("No discharge point could be found on the low side of the property for the chamber outlet")
        return
    dx, dy, dz = best
    fes1 = Point(structs[0]["xy"])
    target = Point(dx, dy)
    o, od = None, 1e18
    for s_ in np.arange(0, pp.boundary.length, 5.0):
        p = pp.boundary.interpolate(s_)
        if p.distance(fes1) >= 30.0 and p.distance(target) < od:
            o, od = p, p.distance(target)
    if o is None:
        o = nearest_points(pp.boundary, target)[0]
    bkeep = unary_union([b.buffer(2) for b in L["bldgs"]])
    route = corridor_route(g, (o.x, o.y), (dx, dy), blocked)
    if route is None:
        route = corridor_route(g, (o.x, o.y), (dx, dy), g.mask(bkeep))
    if route is None:
        route = LineString([(o.x, o.y), (dx, dy)])
    cs = _bend_points(route, keepout=bkeep)
    q100 = max(pond["table"][-1]["out_cfs"], 0.1)
    s_pipe = 0.01
    d = pipe_dia(q100, s_pipe)
    inv = pond["bottom_el"] + 0.0          # low-flow orifice at the bed floor; the outlet pipe leaves the control manhole at that level
    ocs = {"id": "OCS-1", "kind": "DMH", "xy": (o.x, o.y), "rim": round(float(g.at(z_prop, [o.x], [o.y])[0]), 2), "label": "OUTLET CONTROL MH"}
    chain = [ocs]
    for k, (bx, by) in enumerate(cs[1:-1], 1):
        dmh_no += 1
        chain.append({"id": f"DMH-{dmh_no}", "kind": "DMH", "xy": (bx, by), "rim": round(float(g.at(z_prop, [bx], [by])[0]), 2)})
    fes2 = {"id": "FES-2", "kind": "FES", "xy": (dx, dy), "rim": round(dz, 2), "inv_out": None}
    chain.append(fes2)
    for a_, b_ in zip(chain, chain[1:]):
        ln = LineString([a_["xy"], b_["xy"]])
        L_ft = ln.length
        inv_up = inv
        inv_dn = round(inv_up - s_pipe * L_ft, 2)
        pipes.append({"from": a_["id"], "to": b_["id"], "line": ln, "inlet": None, "q_local": 0.0, "len": L_ft, "dia": d, "slope": s_pipe, "q10": q100,
                      "inv_up": round(inv_up, 2), "inv_dn": inv_dn, "cap": full_flow(d, s_pipe), "mat": "HDPE" if d <= 36 else "RCP", "outlet": True})
        a_["inv_out"] = round(inv_up, 2)
        a_["dia_out"] = d
        a_["cover"] = round(a_["rim"] - (inv_up + d / 12.0), 1)
        b_["inv_in"] = inv_dn
        inv = inv_dn - 0.1
    structs.extend(chain)
    dn = fes2["inv_in"]
    if dn < dz - 2.0:
        warns.append(f"Chamber outlet: the discharge pipe reaches the low side of the property at invert {dn:.1f}, {dz - dn:.1f} ft below the ground there; "
                     "gravity discharge needs a lower discharge point (ditch, swale or downstream storm sewer) or a pumped outlet (engineer to resolve)")
    if pond["table"] and pond["table"][-1]["stage"] > pond["weir_crest"] + 0.01:
        warns.append("The 100-yr event rises above the chamber overflow crest; the overflow weir in the control manhole carries the excess")


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
