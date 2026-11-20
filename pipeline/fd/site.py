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


# sanitary sewer slope rules (all jobs): never steeper than 12 %; services at least 2 % (4 in) / 1 % (6 in); 8 in mains 0.4 %
SEWER_MAX = 0.12
LAT_MIN = {4: 0.02, 6: 0.01}
LAT_DIA = 6

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


def _longest(g):
    if g is None or g.is_empty:
        return None
    ls = lines(g)
    return max(ls, key=lambda q: q.length) if ls else None


def _in_parcel(line, parcel, inset=1.5):
    """Part of a line that is inside the parcel (utility lines stay on our own property until the tap in the road)."""
    c = _longest(line.intersection(parcel.buffer(-inset)))
    return c if c is not None and c.length > 5 else line


def _sel_road(L, key):
    from ..ec_drawing import road_key
    sel = [cl for r, cl, pav in (L.get("roads") or []) if key and road_key(r) == key]
    return unary_union(sel) if sel else None


def _service_corridor(g, L, road_sel, road_all, targets):
    """Property-line connection on the selected road's frontage and parallel routes inside the parcel to the first building
    (sewer, water, electric, gas). Returns (rl, cp, routes, building_index)."""
    from .drainage import corridor_route, _bend_points
    P = L["parcel"]
    rl = road_sel if road_sel is not None and not road_sel.is_empty else road_all
    bl = L["bldgs"]
    allb = unary_union(bl)
    ring = P.exterior
    cands = [ring.interpolate(d) for d in np.arange(0, ring.length, 8.0)]
    near = [c for c in cands if c.distance(rl) <= 90.0] or sorted(cands, key=lambda c: c.distance(rl))[:6]
    cp = min(near, key=lambda c: c.distance(allb) + 0.6 * c.distance(rl))
    Pin = P.buffer(-5.0)
    inner0 = nearest_points(Pin, cp)[0] if not Pin.is_empty else cp
    bi = min(range(len(bl)), key=lambda i: inner0.distance(bl[i]))
    bring = bl[bi].buffer(10.0).exterior
    t0 = nearest_points(bring, inner0)[0]
    base = ~g.mask(P.buffer(-3.0))
    base |= g.mask(unary_union([b.buffer(6) for b in bl]))
    if L.get("pond") is not None and not L["pond"].is_empty:
        base |= g.mask(L["pond"].buffer(3))
    if L.get("wet") is not None and not L["wet"].is_empty:
        base |= g.mask(L["wet"])
    for e in (L.get("existing") or []):
        if e.get("kind") != "pavement" and e.get("action") == "keep":
            base |= g.mask(e["geom"].buffer(8))
    bkeep = unary_union([b.buffer(2) for b in bl])
    routes = {}
    used = None
    s_cp = ring.project(cp)
    s_t = bring.project(t0)
    for name, k in (("sewer", 0), ("water", 1), ("elec", -1), ("gas", -2)):
        a = ring.interpolate((s_cp + 12.0 * k) % ring.length)
        a_in = nearest_points(Pin, a)[0] if not Pin.is_empty else a
        t = bring.interpolate((s_t + 12.0 * k) % bring.length)
        r = None
        for buf in ((6.0, 3.5, 0.0) if used is not None else (0.0,)):
            blocked = base.copy()
            if used is not None and buf > 0:
                blocked |= g.mask(used.buffer(buf))
                blocked &= ~g.mask(Point(a_in).buffer(16.0)) & ~g.mask(Point(t).buffer(16.0))
            r = corridor_route(g, (a_in.x, a_in.y), (t.x, t.y), blocked)
            if r is not None:
                break
        if r is None:       # tighter fallback: only buildings, pond and wetland stay blocked (never run a line under a building)
            tight = g.mask(unary_union([b.buffer(2) for b in bl]))
            for k_ in ("pond", "wet"):
                if L.get(k_) is not None and not L[k_].is_empty:
                    tight |= g.mask(L[k_])
            r = corridor_route(g, (a_in.x, a_in.y), (t.x, t.y), tight)
        if r is None:
            r = LineString([(a_in.x, a_in.y), (t.x, t.y)])
        bp = _bend_points(r, keepout=bkeep)
        r = LineString([(a.x, a.y)] + bp) if a.distance(Point(bp[0])) > 0.5 else LineString(bp)
        routes[name] = r
        used = r if used is None else unary_union([used, r])
    return rl, cp, routes, bi


def _road_mains(rl, cp, side_pt):
    """Existing (assumed) water and sewer mains in the selected road, 250 ft either side of the tap."""
    s0 = rl.project(cp) if rl.geom_type == "LineString" else min(lines(rl), key=lambda q: q.distance(cp)).project(cp)
    base = rl if rl.geom_type == "LineString" else min(lines(rl), key=lambda q: q.distance(cp))
    seg = substring(base, max(0.0, s0 - 250.0), min(base.length, s0 + 250.0))
    # water main on the property side of the road centerline, sewer on the centerline
    sign = 1.0
    try:
        left = seg.offset_curve(10.0)
        right = seg.offset_curve(-10.0)
        sign = 1.0 if (_longest(left).distance(cp) if _longest(left) is not None else 1e9) <= (_longest(right).distance(cp) if _longest(right) is not None else 1e9) else -1.0
    except Exception:  # noqa: BLE001
        pass
    wmain = _off(seg, 10.0 * sign)
    return seg, wmain


def _from_tie(tie, route):
    """A dry utility from the point the user picked (Redline) to the service corridor, then along it."""
    from shapely.ops import substring
    s0 = route.project(tie)
    rest = substring(route, s0, route.length) if s0 < route.length - 1.0 else route
    return LineString([(tie.x, tie.y)] + list(rest.coords))


def _strip(out, keep=()):
    """Leave a utility (or all of them) out of the design entirely."""
    blank = {"sewer": dict(sewer_main=None, sewer_lat=[], sewer_pipes=[], smh=[], sewer_lat_rows=[], pump_station=None, road_sewer=None),
             "water": dict(water_main=None, water_lat=[], hydrants=[], valves=[], road_water=None),
             "elec": dict(elec=None, elec_lat=[], xfmr=[], pole=None),
             "gas": dict(gas=None, gas_lat=[])}
    for k_, v_ in blank.items():
        if k_ not in keep:
            out.update(v_)
    return out


def utilities(g, z_prop, ex_z, G, L, road_all, prog, fr, road_key=None, mode="municipal", has_sewer=None, has_water=None, has_gas=True, has_elec=True):
    """Service corridor from the selected main road, inside our own property only, then the tap into the road mains."""
    sp = G["spine"]
    if has_sewer is None:
        has_sewer = mode == "municipal"
    if has_water is None:
        has_water = mode == "municipal"
    out = {"warnings": [], "notes": [], "mode": "municipal" if (has_sewer and has_water) else ("onsite" if not (has_sewer or has_water) else "mixed"),
           "has_sewer": bool(has_sewer), "has_water": bool(has_water)}
    bl = L["bldgs"]
    P = L["parcel"]
    road_sel = _sel_road(L, road_key)
    st_, wt_ = L.get("sewer_tie") if has_sewer else None, L.get("water_tie") if has_water else None
    if st_ is not None or wt_ is not None:
        # the user picked where the mains tie in (Redline): the corridor leaves the property nearest those points
        tie = st_ if st_ is not None else wt_
        road_sel = tie.buffer(2.0).exterior
    rl, cp, routes, bi = _service_corridor(g, L, road_sel, road_all, None)
    R = routes["sewer"]
    out["road_key"] = road_key
    out["cp"] = (cp.x, cp.y)
    if st_ is not None or wt_ is not None:
        def _stub(p_):
            dx, dy = cp.x - p_.x, cp.y - p_.y
            m_ = math.hypot(dx, dy) or 1.0
            ux, uy = -dy / m_, dx / m_
            return LineString([(p_.x - ux * 15, p_.y - uy * 15), (p_.x + ux * 15, p_.y + uy * 15)])
        seg = _stub(st_ if st_ is not None else wt_)
        wroad = _stub(wt_ if wt_ is not None else st_)
        out["notes"].append("Sewer and water tie in at the points picked on the existing-conditions plan; the existing main inverts are assumed and must be confirmed.")
    else:
        seg, wroad = _road_mains(rl, cp, cp)
    out["road_water"], out["road_sewer"] = wroad, seg
    ex_pt = st_ if st_ is not None else nearest_points(seg, cp)[0]
    z_road = float(g.at(ex_z, [ex_pt.x], [ex_pt.y])[0])
    gpd = design_flow(prog)
    out["sewer_gpd"] = round(gpd, -1)
    out["water_gpd"] = round(gpd * 1.2, -1)
    out["corridor"] = R
    # --- dry utilities: electric and gas ride the same corridor, offset, and stay inside the parcel
    elec_c, gas_c = routes["elec"], routes["gas"]
    if L.get("electric_tie") is not None:            # picked on the EC plan (Redline): the service starts there
        pole = L["electric_tie"]
        out["elec"] = _from_tie(pole, elec_c)
    else:
        pole = nearest_points(seg, Point(elec_c.coords[0]))[0]
        out["elec"] = LineString([(pole.x, pole.y)] + list(elec_c.coords))
    if L.get("gas_tie") is not None:
        out["gas"] = _from_tie(L["gas_tie"], gas_c)
    else:
        gtap = nearest_points(seg, Point(gas_c.coords[0]))[0]
        out["gas"] = LineString([(gtap.x, gtap.y)] + list(gas_c.coords))
    out["pole"] = (pole.x, pole.y)
    out["xfmr"] = []
    out["elec_lat"], out["gas_lat"] = [], []
    for b in bl:
        a, c = nearest_points(out["elec"], b)
        out["xfmr"].append((a.x, a.y))
        out["elec_lat"].append(LineString([a, c]))
        a2, c2 = nearest_points(out["gas"], b)
        out["gas_lat"].append(LineString([a2, c2]))
    out["tap"] = (ex_pt.x, ex_pt.y)
    out.update({"water_main": None, "sewer_main": None, "water_lat": [], "sewer_lat": [], "svc": [], "hydrants": [], "valves": [],
                "smh": [], "sewer_lat_rows": [], "sewer_slope_pct": 0.0})
    if not (has_sewer and has_water):
        # any service that is NOT municipal is private: septic (no sewer) and / or a well + fire cistern (no water)
        ons = onsite(g, z_prop, ex_z, G, L, gpd, R, septic=not has_sewer)
        septic_keys = ("flow_gpd", "loading_rate", "bottom_sf", "hsg", "tank_gal", "field", "reserve", "trenches", "field_sf", "tank", "tank_xy", "building_sewer",
                       "dbox", "tank_to_field", "inv_bldg", "inv_tank_in", "inv_tank_out", "trench_inv", "field_ground", "pump")
        well_keys = ("well_ring", "well", "well_line", "cistern", "cistern_xy", "cistern_gal", "hydrants")
        drop = (septic_keys if has_sewer else ()) + (well_keys if has_water else ())
        for k_, v_ in ons.items():
            if k_ in ("warnings", "notes") or k_ in drop:
                continue
            out[k_] = v_
        for w_ in ons["warnings"]:
            wl_ = w_.lower()
            if has_sewer and any(t in wl_ for t in ("septic", "drainfield", "dosing")):
                continue
            if has_water and any(t in wl_ for t in ("well", "cistern")):
                continue
            out["warnings"].append(w_)
        for n_ in ons["notes"]:
            if has_sewer:
                n_ = "Private well: a drilled well permit, a well yield test and a water quality test are required."
            elif has_water:
                n_ = "Private septic: soil loading rates are ASSUMED from the hydrologic soil group. A percolation test, test pits (seasonal high water table, restrictive layers) and a licensed designer's septic plan are required."
            out["notes"].append(n_)
            if has_sewer or has_water:
                break
        if not has_sewer and not has_water:
            return out
    wc = routes["water"]
    wtap = wt_ if wt_ is not None else nearest_points(wroad, Point(wc.coords[0]))[0]
    S = LineString([(ex_pt.x, ex_pt.y)] + list(R.coords)) if has_sewer else None
    W = LineString([(wtap.x, wtap.y)] + list(wc.coords)) if has_water else None
    out["sewer_main"], out["water_main"] = S, W
    wl, sl, svc, jpos = [], [], [], []
    for n, b in enumerate(bl, 1):
        a = nearest_points(W, b) if W is not None else None
        if a is not None:
            wl.append(LineString([a[0], a[1]]))
        c = nearest_points(S, b) if S is not None else None
        if c is not None:
            sl.append(LineString([c[0], c[1]]))
            jpos.append(S.project(c[0]))
        svc.append({"bldg": n, "w_xy": (a[1].x, a[1].y) if a else None, "s_xy": (c[1].x, c[1].y) if c else None})
    out["water_lat"], out["sewer_lat"], out["svc"] = wl, sl, svc
    # hydrants: within 250 ft along the main and 150 ft of every building face
    if has_water:
        hyd = []
        s = 60.0
        while s < W.length:
            hyd.append(W.interpolate(s))
            s += 250.0
        if not hyd:
            hyd.append(W.interpolate(min(W.length, 40.0)))
        for b in bl:
            if all(h.distance(b) > 150 for h in hyd):
                hyd.append(nearest_points(W, b)[0])
        out["hydrants"] = []          # standing rule: Drafted never proposes fire hydrants (fire protection is the fire marshal's call)
        out["valves"] = [(wtap.x, wtap.y)] + [(l.coords[0][0], l.coords[0][1]) for l in wl]
    if not has_sewer:
        out["notes"].append(f"Water service corridor enters the site from {road_key or 'the nearest road'} at the property line and stays inside the parcel; the water main taps the road main there.")
        return out
    # manholes: at the tap, at every bend, at lateral junctions, at least every 300 ft, and at the end
    cs = list(S.coords)
    stas = [0.0]
    for p in cs[1:]:
        stas.append(stas[-1] + math.hypot(p[0] - cs[len(stas) - 1][0], p[1] - cs[len(stas) - 1][1]))
    pos = set(stas) | set(jpos)
    allp = sorted(pos)
    full = [allp[0]]
    for p in allp[1:]:
        while p - full[-1] > 300.0:
            full.append(full[-1] + 300.0)
        if p - full[-1] > 20.0:
            full.append(p)
    full[-1] = S.length
    inv_ex = z_road - 8.0                                      # assumed invert of the existing main (to be confirmed by test pit / record drawings)
    # ---- gravity design, upstream to downstream: under every building lateral, at least 4 ft of cover, never flatter than 0.40 % (8 in)
    DIA, COVER, SMIN, SMAX, DROP, NMAN = 8, 4.0, 0.004, SEWER_MAX, 0.1, 0.013
    n_mh = len(full)
    pts = [_along(S, p_) for p_ in full]
    rims = [float(g.at(z_prop if k else ex_z, [pt.x], [pt.y])[0]) for k, pt in enumerate(pts)]
    lat_at = {}
    for k, (b, l) in enumerate(zip(bl, sl)):
        j = min(range(1, n_mh), key=lambda q: abs(full[q] - jpos[k])) if n_mh > 1 else 0
        lat_at.setdefault(j, []).append(k)
    def lat_inv_main(k):                                       # highest invert the main may have where building k's 6 in lateral (2 %) joins
        return G["ffe"][k] - 4.5 - 0.02 * sl[k].length - DROP
    top = max(lat_at) if lat_at else n_mh - 1                  # the main starts at the manhole serving the farthest building
    inv_out, inv_in, slopes = {}, {}, {}
    for k in range(top, 0, -1):
        v = rims[k] - COVER - DIA / 12.0
        for b_ in lat_at.get(k, []):
            v = min(v, lat_inv_main(b_))
        if k + 1 in inv_in:
            v = min(v, inv_in[k + 1] - DROP)
        inv_out[k] = v
        L_ft = max(full[k] - full[k - 1], 1.0)
        tgt = (rims[k - 1] - COVER - DIA / 12.0) if k - 1 >= 1 else inv_ex
        s_ = min(max(SMIN, (v - tgt) / L_ft), SMAX)
        s_ = math.ceil(s_ * 2000.0 - 1e-9) / 2000.0
        slopes[k] = s_
        inv_in[k] = v - s_ * L_ft                              # invert arriving at manhole k-1
    arrive = inv_in.get(1, inv_ex)
    depth = {k: rims[k] - inv_out[k] for k in inv_out}
    ps = None
    if arrive < inv_ex - 0.05 or any(d_ > 14.0 for d_ in depth.values()):
        # gravity does not reach the main: pump station at the most downstream manhole that is still a reasonable depth
        ps = top
        for k in range(top, 0, -1):
            if depth[k] > 14.0:
                break
            ps = k
    qavg = gpd / 1440.0
    qpeak = qavg * 4.0
    cap_cfs = lambda s_: 0.463 / NMAN * (DIA / 12.0) ** (8.0 / 3.0) * math.sqrt(s_)     # noqa: E731
    mh, spipes = [], []
    mh.append({"id": "EX-SMH", "xy": (pts[0].x, pts[0].y), "sta": full[0], "rim": round(rims[0], 2), "inv": round(inv_ex if ps else min(inv_ex, arrive), 2),
               "depth": round(rims[0] - inv_ex, 1), "existing": True, "kind": "EX"})
    if not ps and arrive - inv_ex > 2.0:
        mh[0]["drop"] = round(arrive - inv_ex, 1)
    first = ps or 1
    for k in range(first, top + 1):
        is_ps = ps == k
        mh.append({"id": "PS-1" if is_ps else f"SMH-{k - first + (0 if ps else 1)}", "xy": (pts[k].x, pts[k].y), "sta": full[k], "rim": round(rims[k], 2), "inv": round(inv_out[k], 2),
                   "inv_in": round(inv_in[k + 1], 2) if k + 1 in inv_in else None, "depth": round(rims[k] - inv_out[k], 1), "existing": False, "kind": "PS" if is_ps else "SMH"})
    ids = {round(m["sta"], 2): m["id"] for m in mh}
    for k in range(top, first, -1) if ps else range(top, 0, -1):
        c_ = cap_cfs(slopes[k])
        spipes.append({"from": ids[round(full[k], 2)], "to": ids[round(full[k - 1], 2)], "len": full[k] - full[k - 1], "dia": DIA, "slope": slopes[k], "inv_up": round(inv_out[k], 2),
                       "inv_dn": round(inv_in[k], 2), "q_gpm": round(qpeak, 1), "cap_gpm": round(c_ * 448.8), "v": round(c_ / (math.pi * (DIA / 24.0) ** 2), 1), "mat": "PVC SDR 35"})
    if ps:
        fm_len = full[ps] - full[0]
        d_fm = 2.0 if qpeak <= 60 else 4.0
        q_p = max(qpeak, 20.0 if d_fm == 2.0 else 80.0)
        v_fm = q_p * 0.4085 / d_fm ** 2
        wet_d = 6.0
        work_ft = max(1.0, (q_p * 10.0 / 4.0) / 7.48 / (math.pi * wet_d ** 2 / 4))        # 10 minute cycle
        on_el = inv_out[ps] - 0.5
        off_el = on_el - work_ft
        floor = off_el - 2.0
        hi = max([rims[k] for k in range(0, ps + 1)]) - 5.0                               # force main at 5 ft cover: its high point
        static = max(hi, inv_ex + 1.0) - off_el
        hf = 10.44 * fm_len * q_p ** 1.852 / (120.0 ** 1.852 * d_fm ** 4.87)
        tdh = static + hf * 1.1 + 5.0
        out["pump_station"] = {"xy": mh[1]["xy"], "id": "PS-1", "rim": mh[1]["rim"], "inv_in": round(inv_out[ps], 2), "on_el": round(on_el, 2), "off_el": round(off_el, 2),
                               "floor": round(floor, 2), "wet_dia_ft": wet_d, "q_gpm": round(q_p), "tdh_ft": round(tdh, 1), "static_ft": round(static, 1), "friction_ft": round(hf * 1.1, 1),
                               "fm_dia_in": d_fm, "fm_len": round(fm_len), "fm_v": round(v_fm, 1), "duplex": True,
                               "fm_line": substring(S, full[0], full[ps]) if fm_len > 1 else None}
        mh[1]["depth"] = round(mh[1]["rim"] - floor, 1)
        out["notes"].append(f"Gravity sewer cannot reach the assumed main invert ({inv_ex:.1f}), so a duplex submersible pump station (PS-1) and a {d_fm:.0f}\" force main "
                            f"({fm_len:.0f} LF) discharge to the existing manhole. Pumps: {q_p:.0f} gpm at {tdh:.0f} ft TDH each. Confirm the main invert before final design: gravity service may be possible.")
    out["smh"] = mh
    out["sewer_pipes"] = spipes
    out["sewer_peak_gpm"] = round(qpeak, 1)
    out["sewer_slope_pct"] = (min(p_["slope"] for p_ in spipes) * 100) if spipes else SMIN * 100
    byi = {}
    for j, ks in lat_at.items():
        for k in ks:
            byi[k] = j
    lat_rows = []
    for k, (b, l) in enumerate(zip(bl, sl)):
        L_ft = max(l.length, 1.0)
        j = byi.get(k, 1)
        inv_j = inv_out.get(max(j, first), inv_ex)
        inv_b = G["ffe"][k] - 4.5
        s_lat = (inv_b - (inv_j + DROP)) / L_ft
        drop_conn = False
        if s_lat > SEWER_MAX:                                   # never steeper than 12 %: run at 12 % and drop into the manhole / main
            s_lat, drop_conn = SEWER_MAX, True
        lmin = LAT_MIN.get(LAT_DIA, 0.01)
        lat_rows.append({"bldg": k + 1, "len": round(L_ft), "inv_main": round(inv_b - s_lat * L_ft if drop_conn else inv_j + DROP, 2), "inv_bldg": round(inv_b, 2),
                         "ok": s_lat >= lmin - 1e-4, "bldg_out_inv": round(inv_b, 2), "slope": s_lat, "dia": LAT_DIA, "drop": drop_conn})
        if drop_conn:
            out["notes"].append(f"Building {k + 1}: the sewer service is held to the 12% maximum and enters the main through a drop connection")
        if s_lat < lmin - 1e-4:
            out["warnings"].append(f"Building {k + 1}: the sewer lateral has only {s_lat * 100:.1f}% of fall to the main ({lmin * 100:.0f}% minimum for {LAT_DIA}\"); raise the floor or use an ejector pump")
    out["sewer_lat_rows"] = lat_rows
    out["notes"].append(f"Service corridor enters the site from {road_key or 'the nearest road'} at the property line and stays inside the parcel; " +
                        ("water and sewer tap the road mains there." if has_water else "the sewer taps the road main there."))
    return out


def onsite(g, z_prop, ex_z, G, L, gpd, R, septic=True):
    """Private water well and septic system (tank + conventional drainfield + reserve area)."""
    out = {"warnings": [], "notes": []}
    P, bl, dev = L["parcel"], L["bldgs"], L["dev"]
    hsg = L.get("hsg") or "B"
    rate = {"A": 1.2, "B": 0.8, "C": 0.5, "D": 0.3}.get(hsg, 0.8)        # gpd/sf of trench bottom (assumed from soil group)
    flow = max(gpd, 300.0)
    bottom = flow / rate
    foot = bottom * 3.0                                                    # 3 ft trenches on 9 ft centers
    A2 = 2.0 * foot                                                        # primary + 100 % reserve
    ll = math.sqrt(2.0 * A2)
    ww = A2 / ll
    allowed = g.mask(P.buffer(-12)) & ~g.mask(dev.buffer(15)) & ~g.mask(unary_union(bl).buffer(25))
    keepx = [e["geom"] for e in (L.get("existing") or []) if e.get("action") == "keep"]
    if keepx:
        allowed &= ~g.mask(unary_union(keepx).buffer(15))
    if L.get("wet") is not None and not L["wet"].is_empty:
        allowed &= ~g.mask(L["wet"].buffer(75))
    if L.get("pond") is not None and not L["pond"].is_empty:
        allowed &= ~g.mask(L["pond"].buffer(75))
    if L.get("flood") is not None and not L["flood"].is_empty:
        allowed &= ~g.mask(L["flood"])
    sl = 100.0 * np.hypot(*np.gradient(ex_z, g.cell))
    allowed &= np.nan_to_num(sl, nan=99.0) < 12.0
    bc = unary_union(bl).centroid
    best = None
    for (lw, lh) in ((ll, ww), (ww, ll)):
        nx, ny = int(math.ceil(lw / g.cell)), int(math.ceil(lh / g.cell))
        fit = ndi.binary_erosion(allowed, structure=np.ones((ny, nx), bool), border_value=0)
        jj, ii = np.nonzero(fit)
        if not len(ii):
            continue
        d = np.hypot(g.xs[ii] - bc.x, g.ys[jj] - bc.y)
        z = ex_z[jj, ii]
        ffe = float(np.mean(G["ffe"]))
        cost = d + 3.0 * np.maximum(0, np.nan_to_num(z, nan=ffe) - (ffe - 3.0))
        k = int(np.argmin(cost))
        if best is None or cost[k] < best[0]:
            best = (float(cost[k]), float(g.xs[ii[k]]), float(g.ys[jj[k]]), lw, lh)
    out["flow_gpd"] = round(flow, -1)
    out["loading_rate"] = rate
    out["bottom_sf"] = round(bottom)
    out["hsg"] = hsg
    tank_gal = next((x for x in (1000, 1250, 1500, 2000, 2500, 3000, 4000, 5000) if x >= max(1000.0, 2.0 * flow)), 5000)
    out["tank_gal"] = tank_gal
    tk_a = tank_gal * 0.1337 / 5.0
    tw = math.sqrt(tk_a / 2.0)
    tl = 2.0 * tw
    if best is None:
        out["field"] = out["reserve"] = out["trenches"] = None
        out["warnings"].append("No area on the site is large enough for a septic drainfield and reserve with the required setbacks; use a shared or advanced treatment system or municipal sewer")
        fcen = None
    else:
        _, fx, fy, lw, lh = best
        if lw >= lh:
            prim = box(fx - lw / 2, fy - lh / 2, fx + lw / 2, fy)
            res = box(fx - lw / 2, fy, fx + lw / 2, fy + lh / 2)
            tr = [LineString([(fx - lw / 2 + 5, y), (fx + lw / 2 - 5, y)]) for y in np.arange(fy - lh / 2 + 4.5, fy - 1, 9.0)]
        else:
            prim = box(fx - lw / 2, fy - lh / 2, fx, fy + lh / 2)
            res = box(fx, fy - lh / 2, fx + lw / 2, fy + lh / 2)
            tr = [LineString([(x, fy - lh / 2 + 5), (x, fy + lh / 2 - 5)]) for x in np.arange(fx - lw / 2 + 4.5, fx - 1, 9.0)]
        out["field"], out["reserve"], out["trenches"] = prim, res, tr
        fcen = prim.centroid
        out["field_sf"] = round(prim.area)
    # tank: on the line from the building to the field, just outside the paving
    if fcen is not None:
        line = LineString([bc, fcen])
        open_part = line.difference(dev.buffer(10))
        seg = _longest(open_part)
        tp = Point(seg.coords[0]) if seg is not None else Point(line.interpolate(0.5, normalized=True))
        ok = nearest_points(unary_union(bl).buffer(12).exterior, fcen)[0] if tp.distance(unary_union(bl)) < 10 else tp
        tp = ok if ok.distance(dev) > 0 else tp
    else:
        tp = nearest_points(unary_union(bl).buffer(20).exterior, Point(P.centroid))[0]
    out["tank"] = box(tp.x - tl / 2, tp.y - tw / 2, tp.x + tl / 2, tp.y + tw / 2)
    out["tank_xy"] = (tp.x, tp.y)
    ib = min(range(len(bl)), key=lambda i: bl[i].distance(tp))
    a, c = nearest_points(bl[ib], tp)
    out["building_sewer"] = LineString([a, c])
    if fcen is not None:
        db = Point(out["field"].centroid.x, out["field"].bounds[3] if out["field"].bounds[3] < fcen.y + 1e9 else fcen.y)
        out["dbox"] = (fcen.x, fcen.y)
        out["tank_to_field"] = LineString([tp, fcen])
    else:
        out["dbox"] = None
        out["tank_to_field"] = None
    # elevations: building outlet -> tank -> field trench
    ffe_b = G["ffe"][ib]
    inv_out = ffe_b - 4.5
    inv_tank_in = inv_out - 0.02 * out["building_sewer"].length
    inv_tank_out = inv_tank_in - 0.17
    out["inv_bldg"], out["inv_tank_in"], out["inv_tank_out"] = round(inv_out, 2), round(inv_tank_in, 2), round(inv_tank_out, 2)
    if fcen is not None:
        zf = float(g.at(z_prop if np.isfinite(g.at(z_prop, [fcen.x], [fcen.y])[0]) else ex_z, [fcen.x], [fcen.y])[0])
        inv_trench = zf - 2.0
        run_ft = out["tank_to_field"].length
        inv_field = inv_tank_out - 0.01 * run_ft
        out["trench_inv"] = round(min(inv_trench, inv_field), 2)
        out["field_ground"] = round(zf, 2)
        if inv_trench > inv_field + 0.01:
            out["pump"] = True
            out["warnings"].append(f"Drainfield trench invert ({inv_trench:.1f}) is above what gravity from the tank allows ({inv_field:.1f}); a dosing pump chamber is needed")
        else:
            out["pump"] = False
    # well: >= 100 ft from the drainfield / reserve, >= 50 ft from the tank, up-gradient where possible
    wallow = g.mask(P.buffer(-25)) & ~g.mask(dev.buffer(20))
    if keepx:
        wallow &= ~g.mask(unary_union(keepx).buffer(20))
    if L.get("flood") is not None and not L["flood"].is_empty:
        wallow &= ~g.mask(L["flood"])
    if septic and out.get("field") is not None:
        wallow &= ~g.mask(unary_union([out["field"], out["reserve"]]).buffer(100))
    if septic:
        wallow &= ~g.mask(out["tank"].buffer(50))
    jj, ii = np.nonzero(wallow)
    out["well_ring"] = 100.0
    if len(ii):
        d = np.hypot(g.xs[ii] - bc.x, g.ys[jj] - bc.y)
        z = np.nan_to_num(ex_z[jj, ii], nan=0.0)
        zf0 = out.get("field_ground", float(np.nanmean(ex_z[g.mask(P)]))) if septic else float(np.nanmean(ex_z[g.mask(P)]))
        cost = d - 4.0 * (z - zf0)
        k = int(np.argmin(cost))
        w = (float(g.xs[ii[k]]), float(g.ys[jj[k]]))
    else:
        out["warnings"].append("A private well cannot be placed 100 ft from the drainfield and clear of the development area on this parcel; adjust the layout, use a shared well, or connect to municipal water")
        cand = Point(P.centroid)
        w = (cand.x, cand.y)
    out["well"] = w
    wp = Point(w)
    a2, c2 = nearest_points(unary_union(bl), wp)
    out["well_line"] = LineString([wp, a2])
    # cistern for fire protection (no hydrants without a public main)
    sp = G["spine"]
    mid = sp.interpolate(0.5, normalized=True)
    cp_ = nearest_points(dev.buffer(14).exterior, mid)[0]
    out["cistern"] = box(cp_.x - 15, cp_.y - 6, cp_.x + 15, cp_.y + 6)
    out["cistern_xy"] = (cp_.x, cp_.y)
    out["cistern_gal"] = 30000
    out["notes"].append("Private well and septic: soil loading rates are ASSUMED from the hydrologic soil group. A percolation test, test pits (seasonal high water table, restrictive layers), a licensed designer's septic plan and a well permit are required.")
    out["hydrants"] = []          # no hydrants proposed (standing rule); the cistern stays
    return out


def fire(g, G, L, util):
    from . import firefix
    sp = L.get("fire_route") or G["spine"]
    out = {"warnings": []}
    # clear width along the fire route (20 ft required)
    pv = L["pave"].union(L["drive"]) if L.get("drive") is not None else L["pave"]
    ok_w = 0
    n = 0
    for s in np.arange(15.0, sp.length - 10.0, 10.0):
        p = sp.interpolate(s)
        n += 1
        if p.distance(pv.boundary) >= 9.5:
            ok_w += 1
    out["lane_ok_pct"] = round(100.0 * ok_w / max(n, 1))
    if out["lane_ok_pct"] < 90:
        out["warnings"].append(f"The fire lane is narrower than 20 ft clear along about {100 - out['lane_ok_pct']}% of the access route (buildings or the property line leave no room to widen it)")
    r_cl = L.get("fire_rmin_cl")
    out["min_radius_ft"] = None if r_cl is None else round(r_cl - firefix.LANE / 2)
    if r_cl is not None and r_cl - firefix.LANE / 2 < firefix.R_IN - 1.0:
        out["warnings"].append(f"One turn on the fire route has an inside radius of about {max(r_cl - firefix.LANE / 2, 0):.0f} ft (25 ft needed for a ladder truck); the drive legs there are too short to open it further")
    out["truck_envelope"] = (_off(sp, 6.5), _off(sp, -6.5))
    out["hydrants"] = util["hydrants"]
    out["cistern"] = util.get("cistern")
    if not util.get("has_water", util.get("mode") != "onsite"):
        out["warnings"].append("No public water main: fire flow is from an on-site cistern (30,000 gal assumed); confirm the required volume (NFPA 1142 / sprinkler demand) with the fire marshal")
    out["fdc"] = [nearest_points(sp, b)[1] for b in L["bldgs"]]
    out["fdc"] = [(p.x, p.y) for p in out["fdc"]]
    # dead end
    loop = firefix.is_loop(pv, L["bldgs"])
    out["dead_end_ft"] = 0 if loop else round(sp.length)
    out["turnaround"] = L.get("turnaround")
    if sp.length > 150 and not loop and L.get("turnaround") is None:
        out["warnings"].append("The access drive is a dead end longer than 150 ft and there is no room for a hammerhead turnaround; provide an approved alternative with the fire marshal")
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
    if L.get("parcel") is not None and not lod.is_empty:
        lod = lod.intersection(L["parcel"].buffer(-1.5))      # never outside the property line
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
    if big is None or big.area <= 5000:
        # no big lawn inside the disturbance: use the largest open piece of the LOD, else stage beside the entrance
        try:
            open_ = lod.difference(unary_union([L["pave"]] + [b for b in L["bldgs"] if b is not None]))
            cand = [p for p in polys(open_) if p.area > 900]
            big = max(cand, key=lambda p: p.area) if cand else None
        except Exception:  # noqa: BLE001
            big = None
    if big is not None:
        c = big.representative_point()
    else:
        c = Point(a.x, a.y)
        c = affinity.translate(Point(a.x, a.y), 70 * math.cos(math.radians(ang + 90)), 70 * math.sin(math.radians(ang + 90)))
    out["stockpile"] = box(c.x - 25, c.y - 15, c.x + 25, c.y + 15)
    out["washout"] = box(c.x + 30, c.y - 6, c.x + 42, c.y + 6)
    out["inlet_prot"] = [s["xy"] for s in net["structs"] if s["kind"] == "CB"]
    out["npdes"] = out["lod_ac"] >= 1.0
    return out


# ------------------------------------------------------------------------------------------------ campgrounds
CAMP_MAIN_DIA, CAMP_LAT_DIA, CAMP_COVER, CAMP_DROP = 6, 4, 4.0, 0.1


def _camp_graph(lines_, pts, tol=2.0):
    """The drive centrelines as a graph (vertices every ~10 ft, junctions merged) with each service point's nearest spot on
    them added as a node. Returns (nx graph, xy by node, node of each point)."""
    import networkx as nx
    from shapely.ops import linemerge
    net = unary_union([l for l in lines_ if l is not None and l.length > 1])
    segs = [l for l in lines(net)] if not net.is_empty else []
    G_ = nx.Graph()
    xy, key = {}, {}

    def node(p):
        k = (round(p[0] / tol), round(p[1] / tol))
        if k not in key:
            key[k] = len(key)
            xy[key[k]] = (float(p[0]), float(p[1]))
        return key[k]
    # where each point meets the network (the main passes it there)
    snaps = []
    for q in pts:
        if q is None or not segs:
            snaps.append(None)
            continue
        i = min(range(len(segs)), key=lambda j: segs[j].distance(q))
        snaps.append((i, segs[i].project(q)))
    for i, ln in enumerate(segs):
        L_ = ln.length
        st = set(np.linspace(0, L_, max(2, int(L_ // 10) + 1)).tolist())
        cs = list(ln.coords)
        acc = 0.0
        for a, b in zip(cs, cs[1:]):
            st.add(acc)
            acc += math.dist(a, b)
        st |= {s_[1] for s_ in snaps if s_ is not None and s_[0] == i}
        st = sorted(st)
        prev = None
        for s_ in st:
            p_ = ln.interpolate(s_)
            n_ = node((p_.x, p_.y))
            if prev is not None and prev != n_:
                G_.add_edge(prev, n_, w=math.dist(xy[prev], xy[n_]))
            prev = n_
    at = []
    for q, s_ in zip(pts, snaps):
        if s_ is None:
            at.append(None)
            continue
        p_ = segs[s_[0]].interpolate(s_[1])
        at.append(node((p_.x, p_.y)))
    return G_, xy, at


def _camp_tree(G_, xy, root, targets):
    """Shortest paths from the root to every target along the drives, as a tree: parent of each node, and the nodes kept as
    structures (root, ends, branches, bends and at least every 300 ft)."""
    import networkx as nx
    par = {}
    dist, paths = nx.single_source_dijkstra(G_, root, weight="w")
    used = set([root])
    for t in targets:
        if t is None or t not in paths:
            continue
        pth = paths[t]
        for a, b in zip(pth, pth[1:]):
            par.setdefault(b, a)
            used.add(b)
    kids = {}
    for b, a in par.items():
        kids.setdefault(a, []).append(b)
    keep = {root}
    for n_ in used:
        if n_ != root and (len(kids.get(n_, [])) != 1):
            keep.add(n_)                                    # ends and branch points
    # along each run between those: a structure at every real bend (not every vertex of a rounded curve), none closer than
    # 30 ft to the next, and at least every 300 ft
    base = set(keep)
    for k0 in list(base):
        for c in kids.get(k0, []):
            run = [k0, c]
            while run[-1] not in base:
                run.append(kids[run[-1]][0])
            cs = [xy[n_] for n_ in run]
            acc = [0.0]
            for a_, b_ in zip(cs, cs[1:]):
                acc.append(acc[-1] + math.dist(a_, b_))
            sc = list(LineString(cs).simplify(4.0).coords)[1:-1] if len(cs) > 2 else []
            bends = [i for i in range(1, len(run) - 1) if any(math.dist(cs[i], s_) < 1e-6 for s_ in sc)]
            last, total = 0.0, acc[-1]
            for i in range(1, len(run) - 1):
                if (i in bends and acc[i] - last >= 30.0 and total - acc[i] >= 30.0) or acc[i] - last > 290.0:
                    keep.add(run[i])
                    last = acc[i]
    return par, keep, kids, dist


def camp_utilities(g, z_prop, ex_z, G, L, rules):
    """Campground services: a 6" gravity collection main along the drives with a 4" service to every sewer pedestal (and
    cabin), to a septic tank and leach field sized on the campground flow (or to the public sewer tie-in); water along the
    drives to every water pedestal; electric to every electric pedestal. Slopes: services 2 % min, mains 1 % min, 12 % max."""
    out = {"warnings": [], "notes": [], "hydrants": [], "valves": [], "water_main": None, "sewer_main": None, "water_lat": [], "sewer_lat": [], "svc": [],
           "smh": [], "sewer_pipes": [], "sewer_lat_rows": [], "elec_lat": [], "gas_lat": [], "xfmr": [], "elec": None, "gas": None, "camp": True}
    P = L["parcel"]
    meta = L.get("camp_meta") or []
    svc = L.get("camp_svc") or []
    gpd = float(L.get("camp_gpd") or 0) + design_flow({"buildings": []})
    sewage = L.get("camp_sewage") or "septic"
    tie = L.get("sewer_tie")
    septic = sewage != "sewer" or tie is None and not (L.get("roads"))
    out.update({"mode": "onsite" if septic else "mixed", "has_sewer": not septic, "has_water": L.get("water_tie") is not None})
    if sewage == "sewer" and tie is None:
        out["notes"].append("Public sewer was chosen for the campground but no sewer tie-in was picked; the collection main is carried to the frontage road")
    zat = lambda p_: float(g.at(z_prop if np.isfinite(g.at(z_prop, [p_[0]], [p_[1]])[0]) else ex_z, [p_[0]], [p_[1]])[0])     # noqa: E731
    sew_i = [i for i, (m, q) in enumerate(zip(meta, svc)) if q is not None and ("s" in (m.get("hook") or "") or m.get("kind") == "cabin")]
    wat_i = [i for i, (m, q) in enumerate(zip(meta, svc)) if q is not None and ("w" in (m.get("hook") or "") or m.get("kind") == "cabin")]
    ele_i = [i for i, (m, q) in enumerate(zip(meta, svc)) if q is not None and ("e" in (m.get("hook") or "") or m.get("kind") == "cabin")]
    cls = [c for c in (L.get("drive_cl") or []) if c is not None]
    if not cls and G.get("spine") is not None:
        cls = [G["spine"]]
    dev = L["dev"]
    # ---- the outlet: septic tank + leach field, or the sewer tie-in
    rate = {"A": 1.2, "B": 0.8, "C": 0.5, "D": 0.3}.get(L.get("hsg") or "B", 0.8)
    flow = max(gpd, 300.0)
    out["sewer_gpd"] = round(gpd, -1)
    out["water_gpd"] = round(gpd * 1.2, -1)
    field = None
    if septic:
        bottom = flow / rate
        foot = bottom * 3.0
        A2 = 2.0 * foot
        ll = math.sqrt(2.0 * A2)
        ww = A2 / ll
        allowed = g.mask(P.buffer(-12)) & ~g.mask(dev.buffer(15))
        lots = [q for q in (L.get("camp_lots") or []) if q is not None]
        if lots:
            allowed &= ~g.mask(unary_union(lots).buffer(10))
        for k_, d_ in (("wet", 75), ("pond", 50), ("flood", 0)):
            if L.get(k_) is not None and not L[k_].is_empty:
                allowed &= ~g.mask(L[k_].buffer(d_) if d_ else L[k_])
        if L.get("nobuild") is not None and not L["nobuild"].is_empty:
            allowed &= ~g.mask(L["nobuild"])
        sl = 100.0 * np.hypot(*np.gradient(ex_z, g.cell))
        allowed &= np.nan_to_num(sl, nan=99.0) < 12.0
        cen = unary_union([svc[i] for i in sew_i]).centroid if sew_i else dev.centroid
        best = None
        for (lw, lh) in ((ll, ww), (ww, ll)):
            nx_, ny_ = int(math.ceil(lw / g.cell)), int(math.ceil(lh / g.cell))
            fit = ndi.binary_erosion(allowed, structure=np.ones((max(ny_, 1), max(nx_, 1)), bool), border_value=0)
            jj, ii = np.nonzero(fit)
            if not len(ii):
                continue
            d = np.hypot(g.xs[ii] - cen.x, g.ys[jj] - cen.y)
            z = np.nan_to_num(ex_z[jj, ii], nan=0.0)
            zs = float(np.nanmedian([zat((svc[i].x, svc[i].y)) for i in sew_i])) if sew_i else float(np.nanmean(z))
            cost = d + 6.0 * np.maximum(0, z - (zs - 4.0))              # downhill of the sites, close to them
            k = int(np.argmin(cost))
            if best is None or cost[k] < best[0]:
                best = (float(cost[k]), float(g.xs[ii[k]]), float(g.ys[jj[k]]), lw, lh)
        tank_gal = next((x for x in (1000, 1250, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 8000, 10000) if x >= max(1000.0, 2.0 * flow)), 10000)
        out.update({"flow_gpd": round(flow, -1), "loading_rate": rate, "bottom_sf": round(bottom), "hsg": L.get("hsg") or "B", "tank_gal": tank_gal})
        if best is None:
            out["warnings"].append(f"No area on the site fits a leach field and reserve for the campground's {flow:,.0f} GPD with the required setbacks; consider an advanced treatment system or public sewer")
            out["field"] = out["reserve"] = out["trenches"] = None
            outlet = Point(dev.centroid)
        else:
            _, fx, fy, lw, lh = best
            if lw >= lh:
                prim, res = box(fx - lw / 2, fy - lh / 2, fx + lw / 2, fy), box(fx - lw / 2, fy, fx + lw / 2, fy + lh / 2)
                tr = [LineString([(fx - lw / 2 + 5, y), (fx + lw / 2 - 5, y)]) for y in np.arange(fy - lh / 2 + 4.5, fy - 1, 9.0)]
            else:
                prim, res = box(fx - lw / 2, fy - lh / 2, fx, fy + lh / 2), box(fx, fy - lh / 2, fx + lw / 2, fy + lh / 2)
                tr = [LineString([(x, fy - lh / 2 + 5), (x, fy + lh / 2 - 5)]) for x in np.arange(fx - lw / 2 + 4.5, fx - 1, 9.0)]
            field = prim
            out.update({"field": prim, "reserve": res, "trenches": tr, "field_sf": round(prim.area)})
            outlet = prim.centroid
        out["notes"].append(f"Campground septic: {flow:,.0f} GPD design flow ({len(meta)} sites), {tank_gal:,} gal septic tank, leach field on an ASSUMED {rate} GPD/SF loading rate "
                            f"(soil group {out['hsg']}). Test pits, a percolation test and a licensed designer's septic plan are required.")
    else:
        if tie is not None:
            outlet = tie
        else:
            rd = [r_[2] for r_ in (L.get("roads") or []) if r_[2] is not None]
            outlet = nearest_points(unary_union(rd), dev)[0] if rd else Point(P.exterior.coords[0])
    # ---- graph along the drives, the sewer tree from its outlet
    pts_all = [svc[i] for i in range(len(svc))] + [outlet]
    Gr, xy, at = _camp_graph(cls, pts_all)
    root = at[-1]
    if root is None or not sew_i:
        if sew_i:
            out["warnings"].append("The campsites' services could not be routed (no drive to follow)")
        return out
    par, keep, kids, dist = _camp_tree(Gr, xy, root, [at[i] for i in sew_i])
    # tank (or the existing manhole) just off the network at the outlet end
    rnode = xy[root]
    if septic:
        dir_ = np.array([outlet.x - rnode[0], outlet.y - rnode[1]])
        dn = float(np.linalg.norm(dir_)) or 1.0
        tdist = min(max(20.0, dn * 0.35), max(dn - 20.0, 20.0)) if field is not None else 20.0
        tp = (rnode[0] + dir_[0] / dn * tdist, rnode[1] + dir_[1] / dn * tdist)
        tk_a = out["tank_gal"] * 0.1337 / 5.0
        tw = math.sqrt(tk_a / 2.0)
        out["tank"] = box(tp[0] - tw, tp[1] - tw / 2, tp[0] + tw, tp[1] + tw / 2)
        out["tank_xy"] = tp
        out_pt = tp
    else:
        out_pt = (outlet.x, outlet.y)
    # ---- gravity design, leaves to the outlet
    DIA, SMIN, SMAX = CAMP_MAIN_DIA, LAT_MIN[CAMP_MAIN_DIA], SEWER_MAX
    # structures and the pipes between them (each run follows the drive; its pipe is straight between kept nodes)
    sp_par = {}
    for n_ in keep:
        if n_ == root:
            continue
        a = par[n_]
        L_ = math.dist(xy[n_], xy[a])
        while a not in keep:
            L_ += math.dist(xy[a], xy[par[a]])
            a = par[a]
        sp_par[n_] = a
    rim = {n_: zat(xy[n_]) for n_ in keep}
    # services: each site's lateral meets the main at its own node, on the run from the next structure up
    lat_req = {}
    lat_info = []
    for i in sew_i:
        n_ = at[i]
        q = svc[i]
        wye = xy[n_]
        Llat = max(math.dist((q.x, q.y), wye), 3.0)
        z_site = zat((q.x, q.y))
        inv_site = z_site - (2.0 if meta[i].get("kind") == "cabin" else 1.5)
        need = inv_site - LAT_MIN[CAMP_LAT_DIA] * Llat - CAMP_DROP
        # the structure upstream of the wye on its run (the wye node itself when it is one)
        up = n_
        t_ = 0.0
        while up not in keep:
            c = [k_ for k_ in kids.get(up, []) if k_ in par]
            nxt = c[0] if c else None
            if nxt is None:
                break
            t_ += math.dist(xy[up], xy[nxt])
            up = nxt
        lat_req[up] = min(lat_req.get(up, 1e9), need + SMIN * t_)
        lat_info.append((i, n_, Llat, inv_site))
    order = sorted([n_ for n_ in keep], key=lambda n_: -dist.get(n_, 0))
    inv_out, arr = {}, {}
    for n_ in order:
        v = rim[n_] - CAMP_COVER - DIA / 12.0
        if n_ in lat_req:
            v = min(v, lat_req[n_])
        if n_ in arr:
            v = min(v, min(arr[n_]) - CAMP_DROP)
        inv_out[n_] = v
        if n_ == root:
            continue
        a = sp_par[n_]
        Lp = max(math.dist(xy[n_], xy[a]), 1.0)
        tgt = rim[a] - CAMP_COVER - DIA / 12.0
        s_ = min(max(SMIN, (v - tgt) / Lp), SMAX)
        s_ = math.ceil(s_ * 2000.0 - 1e-9) / 2000.0
        arr.setdefault(a, []).append(v - s_ * Lp)
    ids, k = {}, 0
    for n_ in sorted(keep, key=lambda n_: dist.get(n_, 0)):
        k += 1
        ids[n_] = f"SMH-{k}"
    mh, pipes = [], []
    for n_ in keep:
        mh.append({"id": ids[n_], "kind": "SMH", "xy": xy[n_], "rim": round(rim[n_], 2), "inv": round(inv_out[n_], 2),
                   "inv_in": round(min(arr[n_]), 2) if n_ in arr else None, "depth": round(rim[n_] - inv_out[n_], 1), "existing": False})
        if rim[n_] - inv_out[n_] > 14.0:
            out["warnings"].append(f"{ids[n_]} is {rim[n_] - inv_out[n_]:.1f} ft deep; consider a pump or a shallower route")
    qpk = gpd / 1440.0 * 4.0
    cap = lambda s_: 0.463 / 0.013 * (DIA / 12.0) ** (8.0 / 3.0) * math.sqrt(s_) * 448.8     # noqa: E731
    for n_, a in sp_par.items():
        Lp = math.dist(xy[n_], xy[a])
        if Lp < 0.5:
            continue
        v = inv_out[n_]
        s_ = min(max(SMIN, (v - (rim[a] - CAMP_COVER - DIA / 12.0)) / Lp), SMAX)
        s_ = math.ceil(s_ * 2000.0 - 1e-9) / 2000.0
        pipes.append({"from": ids[n_], "to": ids[a], "len": round(Lp, 1), "dia": DIA, "slope": s_, "inv_up": round(v, 2), "inv_dn": round(v - s_ * Lp, 2),
                      "q_gpm": round(qpk, 1), "cap_gpm": round(cap(s_)), "mat": "PVC SDR 35"})
    # outlet run: last structure to the tank / the existing manhole
    z_out = zat(out_pt)
    Lo = max(math.dist(xy[root], out_pt), 1.0)
    s_o = min(max(SMIN, 0.02), SMAX)
    inv_end = inv_out[root] - s_o * Lo
    if septic:
        out["inv_tank_in"] = round(inv_end, 2)
        out["inv_tank_out"] = round(inv_end - 0.17, 2)
        mh.append({"id": "ST-1", "kind": "TANK", "xy": out_pt, "rim": round(z_out, 2), "inv": round(inv_end - 0.17, 2), "depth": round(z_out - (inv_end - 4.5), 1), "existing": False})
        pipes.append({"from": ids[root], "to": "ST-1", "len": round(Lo, 1), "dia": DIA, "slope": s_o, "inv_up": round(inv_out[root], 2), "inv_dn": round(inv_end, 2),
                      "q_gpm": round(qpk, 1), "cap_gpm": round(cap(s_o)), "mat": "PVC SDR 35"})
        out["building_sewer"] = LineString([xy[root], out_pt])
        if out.get("field") is not None:
            fc = out["field"].centroid
            out["dbox"] = (fc.x, fc.y)
            out["tank_to_field"] = LineString([out_pt, (fc.x, fc.y)])
            zf = zat((fc.x, fc.y))
            inv_tr = zf - 2.0
            inv_grav = out["inv_tank_out"] - 0.01 * out["tank_to_field"].length
            out["trench_inv"], out["field_ground"] = round(min(inv_tr, inv_grav), 2), round(zf, 2)
            out["pump"] = inv_tr > inv_grav + 0.01
            if out["pump"]:
                out["warnings"].append(f"The leach field ({inv_tr:.1f}) is above what gravity from the septic tank reaches ({inv_grav:.1f}): a pump chamber and force main to the field are needed")
        else:
            out["dbox"] = out["tank_to_field"] = None
            out["pump"] = False
    else:
        inv_ex = z_out - 8.0
        mh.append({"id": "EX-SMH", "kind": "EX", "xy": out_pt, "rim": round(z_out, 2), "inv": round(min(inv_ex, inv_end), 2), "depth": round(z_out - min(inv_ex, inv_end), 1), "existing": True})
        pipes.append({"from": ids[root], "to": "EX-SMH", "len": round(Lo, 1), "dia": DIA, "slope": s_o, "inv_up": round(inv_out[root], 2), "inv_dn": round(inv_end, 2),
                      "q_gpm": round(qpk, 1), "cap_gpm": round(cap(s_o)), "mat": "PVC SDR 35"})
        if inv_end < inv_ex - 0.05:
            out["warnings"].append(f"The campground sewer arrives at {inv_end:.1f}, below the assumed main invert ({inv_ex:.1f}): a pump station is needed unless the main is deeper (confirm it)")
        out["notes"].append("The campground sewer ties in at the point picked on the existing-conditions plan; the existing main invert is assumed and must be confirmed.")
    out["smh"], out["sewer_pipes"] = mh, pipes
    out["sewer_main"] = unary_union([LineString([xy[n_], xy[a]]) for n_, a in sp_par.items()] + [LineString([xy[root], out_pt])])
    out["sewer_slope_pct"] = min(p_["slope"] for p_ in pipes) * 100 if pipes else SMIN * 100
    out["sewer_peak_gpm"] = round(qpk, 1)
    # services: invert at the main from the main's own profile at the wye
    prof = {}
    for n_, a in sp_par.items():
        prof[(n_, a)] = (inv_out[n_], next(p_["slope"] for p_ in pipes if p_["from"] == ids[n_]) if any(p_["from"] == ids[n_] for p_ in pipes) else SMIN)
    rows_ = []
    for j, (i, n_, Llat, inv_site) in enumerate(lat_info):
        # walk up to the structure above the wye and down the run to it
        up, t_ = n_, 0.0
        while up not in keep:
            c = kids.get(up, [])
            if not c:
                break
            t_ += math.dist(xy[up], xy[c[0]])
            up = c[0]
        if up in sp_par:
            inv_m = inv_out[up] - prof[(up, sp_par[up])][1] * t_
        else:
            inv_m = inv_out.get(up, inv_out[root])
        s_l = (inv_site - (inv_m + CAMP_DROP)) / Llat
        drop = False
        if s_l > SEWER_MAX:
            s_l, drop = SEWER_MAX, True
        q = svc[i]
        out["sewer_lat"].append(LineString([xy[n_], (q.x, q.y)]))
        rows_.append({"bldg": j + 1, "site": meta[i]["label"], "len": round(Llat), "inv_main": round(inv_site - s_l * Llat if drop else inv_m + CAMP_DROP, 2), "inv_bldg": round(inv_site, 2),
                      "ok": s_l >= LAT_MIN[CAMP_LAT_DIA] - 1e-4, "slope": s_l, "dia": CAMP_LAT_DIA, "drop": drop})
        if s_l < LAT_MIN[CAMP_LAT_DIA] - 1e-4:
            out["warnings"].append(f"Site {meta[i]['label']}: its 4\" sewer service has only {max(s_l, 0) * 100:.1f}% fall to the main (2% minimum)")
    out["sewer_lat_rows"] = rows_
    # ---- water and electric along the drives
    def along(idx, src):
        if not idx or src is None:
            return None, []
        G2, xy2, at2 = _camp_graph(cls, [svc[i] for i in idx] + [src])
        if at2[-1] is None:
            return None, []
        par2, _, _, _ = _camp_tree(G2, xy2, at2[-1], at2[:-1])
        segs = [LineString([xy2[b], xy2[a]]) for b, a in par2.items()]
        from shapely.ops import linemerge
        main = linemerge(unary_union(segs + [LineString([(src.x, src.y), xy2[at2[-1]]])])) if segs else None
        if main is not None:
            main = main.simplify(0.5)
        lat = [LineString([xy2[at2[j]], (svc[i].x, svc[i].y)]) for j, i in enumerate(idx) if at2[j] is not None]
        return main, lat
    wsrc = L.get("water_tie")
    if wsrc is None:
        rd = [r_[2] for r_ in (L.get("roads") or []) if r_[2] is not None]
        wsrc = nearest_points(unary_union(rd), dev)[0] if rd else None
        out["notes"].append("Campground water: no water tie-in was picked, so the main is shown from the frontage road; if there is no public main, a well and a pump house serve it.")
    out["water_main"], out["water_lat"] = along(wat_i, wsrc)
    if out["water_main"] is not None:
        out["valves"] = [(wsrc.x, wsrc.y)]
        out["has_water"] = True
    esrc = L.get("electric_tie") or wsrc
    out["elec"], out["elec_lat"] = along(ele_i, esrc)
    out["svc"] = [{"site": meta[i]["label"], "s_xy": (svc[i].x, svc[i].y)} for i in sew_i]
    out["notes"].append(f"Campground services: {len(sew_i)} sewer, {len(wat_i)} water and {len(ele_i)} electric connections at the site pedestals; "
                        f"6\" PVC SDR 35 collection main at 1% minimum, 4\" services at 2% minimum, nothing steeper than 12%.")
    return out
