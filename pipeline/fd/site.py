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
        if r is None:
            r = LineString([(a_in.x, a_in.y), (t.x, t.y)])
        bp = _bend_points(r)
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


def utilities(g, z_prop, ex_z, G, L, road_all, prog, fr, road_key=None, mode="municipal"):
    """Service corridor from the selected main road, inside our own property only, then the tap into the road mains."""
    sp = G["spine"]
    out = {"warnings": [], "notes": [], "mode": mode}
    bl = L["bldgs"]
    P = L["parcel"]
    road_sel = _sel_road(L, road_key)
    rl, cp, routes, bi = _service_corridor(g, L, road_sel, road_all, None)
    R = routes["sewer"]
    out["road_key"] = road_key
    out["cp"] = (cp.x, cp.y)
    seg, wroad = _road_mains(rl, cp, cp)
    out["road_water"], out["road_sewer"] = wroad, seg
    ex_pt = nearest_points(seg, cp)[0]
    z_road = float(g.at(ex_z, [ex_pt.x], [ex_pt.y])[0])
    gpd = design_flow(prog)
    out["sewer_gpd"] = round(gpd, -1)
    out["water_gpd"] = round(gpd * 1.2, -1)
    out["corridor"] = R
    # --- dry utilities: electric and gas ride the same corridor, offset, and stay inside the parcel
    elec_c, gas_c = routes["elec"], routes["gas"]
    pole = nearest_points(seg, Point(elec_c.coords[0]))[0]
    out["elec"] = LineString([(pole.x, pole.y)] + list(elec_c.coords))
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
    if mode != "municipal":
        out.update({"water_main": None, "sewer_main": None, "tap": (ex_pt.x, ex_pt.y), "water_lat": [], "sewer_lat": [], "svc": [], "hydrants": [], "valves": [],
                    "smh": [], "sewer_lat_rows": [], "sewer_slope_pct": 0.0})
        out.update(onsite(g, z_prop, ex_z, G, L, gpd, R))
        return out
    # --- sanitary sewer: from the existing manhole in the road, along the corridor, to the building
    S = LineString([(ex_pt.x, ex_pt.y)] + list(R.coords))
    out["sewer_main"] = S
    out["tap"] = (ex_pt.x, ex_pt.y)
    wc = routes["water"]
    wtap = nearest_points(wroad, Point(wc.coords[0]))[0]
    W = LineString([(wtap.x, wtap.y)] + list(wc.coords))
    out["water_main"] = W
    wl, sl, svc, jpos = [], [], [], []
    for n, b in enumerate(bl, 1):
        a = nearest_points(W, b)
        wl.append(LineString([a[0], a[1]]))
        c = nearest_points(S, b)
        sl.append(LineString([c[0], c[1]]))
        svc.append({"bldg": n, "w_xy": (a[1].x, a[1].y), "s_xy": (c[1].x, c[1].y)})
        jpos.append(S.project(c[0]))
    out["water_lat"], out["sewer_lat"], out["svc"] = wl, sl, svc
    # hydrants: within 250 ft along the main and 150 ft of every building face
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
    out["hydrants"] = [(h.x, h.y) for h in hyd]
    out["valves"] = [(wtap.x, wtap.y)] + [(l.coords[0][0], l.coords[0][1]) for l in wl]
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
    inv_ex = z_road - 8.0                                      # assumed depth of the existing main
    slope = 0.005
    mh = []
    for k, p in enumerate(full):
        pt = _along(S, p)
        inv = inv_ex + slope * p + 0.1 * k                     # 0.1 ft drop across each manhole
        rim = float(g.at(z_prop if k else ex_z, [pt.x], [pt.y])[0])
        mh.append({"id": "EX-SMH" if k == 0 else f"SMH-{k}", "xy": (pt.x, pt.y), "sta": p, "rim": round(rim, 2), "inv": round(inv, 2), "depth": round(rim - inv, 1), "existing": k == 0})
    lift = [m for m in mh[1:] if m["depth"] < 4.0]
    if lift:
        out["warnings"].append(f"Gravity sewer cannot keep 4 ft of cover at {', '.join(m['id'] for m in lift)}; a lift station or grinder pumps are likely required")
    deepm = [m for m in mh[1:] if m["depth"] > 14.0]
    if deepm:
        out["warnings"].append(f"Sewer manholes deeper than 14 ft at {', '.join(m['id'] for m in deepm)}; consider a lift station")
    out["smh"] = mh
    out["sewer_slope_pct"] = slope * 100
    lat_rows = []
    for k, (b, l) in enumerate(zip(bl, sl)):
        ffe = G["ffe"][k]
        L_ft = l.length
        p = jpos[k]
        j = min(range(len(mh)), key=lambda q: abs(mh[q]["sta"] - p))
        inv_j = mh[j]["inv"]
        inv_b_req = inv_j + 0.1 + 0.02 * L_ft
        inv_b_have = ffe - 4.5
        lat_rows.append({"bldg": k + 1, "len": round(L_ft), "inv_main": round(inv_j, 2), "inv_bldg": round(inv_b_req, 2), "ok": inv_b_have >= inv_b_req - 0.01,
                         "bldg_out_inv": round(inv_b_have, 2)})
        if inv_b_have < inv_b_req - 0.01:
            out["warnings"].append(f"Building {k + 1}: sewer lateral invert {inv_b_req:.1f} is above the building outlet {inv_b_have:.1f}; pump or deeper main required")
    out["sewer_lat_rows"] = lat_rows
    out["notes"].append(f"Service corridor enters the site from {road_key or 'the nearest road'} at the property line and stays inside the parcel; water and sewer tap the road mains there.")
    return out


def onsite(g, z_prop, ex_z, G, L, gpd, R):
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
    if out.get("field") is not None:
        wallow &= ~g.mask(unary_union([out["field"], out["reserve"]]).buffer(100))
    wallow &= ~g.mask(out["tank"].buffer(50))
    jj, ii = np.nonzero(wallow)
    out["well_ring"] = 100.0
    if len(ii):
        d = np.hypot(g.xs[ii] - bc.x, g.ys[jj] - bc.y)
        z = np.nan_to_num(ex_z[jj, ii], nan=0.0)
        zf0 = out.get("field_ground", float(np.nanmean(ex_z[g.mask(P)])))
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
    out["hydrants"] = [out["cistern_xy"]]
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
    out["cistern"] = util.get("cistern")
    if util.get("mode") == "onsite":
        out["warnings"].append("No public water main: fire flow is from an on-site cistern (30,000 gal assumed); confirm the required volume (NFPA 1142 / sprinkler demand) with the fire marshal")
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
    out["stockpile"] = None
    if big is not None and big.area > 5000:
        c = big.representative_point()
        out["stockpile"] = box(c.x - 25, c.y - 15, c.x + 25, c.y + 15)
        out["washout"] = box(c.x + 30, c.y - 6, c.x + 42, c.y + 6)
    out["inlet_prot"] = [s["xy"] for s in net["structs"] if s["kind"] == "CB"]
    out["npdes"] = out["lod_ac"] >= 1.0
    return out
