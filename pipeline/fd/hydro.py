"""Hydrology and pond design: rainfall (NOAA Atlas 14 where reachable, regional defaults otherwise), SCS curve numbers,
time of concentration, SCS unit hydrograph, and storage routing through a pond with an orifice + weir outlet."""
import io
import math

import numpy as np

STORMS = (2, 10, 25, 50, 100)

# SCS 24-hr cumulative rainfall distributions (fraction vs hours) -- standard Type II and Type III shapes
TYPE_II = [(0, 0), (2, .022), (4, .048), (6, .080), (7, .098), (8, .120), (8.5, .133), (9, .147), (9.5, .163), (9.75, .172), (10, .181), (10.5, .204),
           (11, .235), (11.5, .283), (11.75, .357), (12, .663), (12.5, .735), (13, .772), (13.5, .799), (14, .820), (16, .880), (20, .952), (24, 1.0)]
TYPE_III = [(0, 0), (2, .020), (4, .043), (6, .072), (7, .089), (8, .115), (8.5, .130), (9, .148), (9.5, .167), (9.75, .178), (10, .189), (10.5, .216),
            (11, .250), (11.5, .298), (11.75, .339), (12, .500), (12.5, .702), (13, .751), (13.5, .785), (14, .811), (16, .886), (20, .957), (24, 1.0)]


def rainfall(lat, lon):
    """24-hr depths (in) for STORMS, 1-hr depth for intensity, and a source note. NOAA Atlas 14 point estimate when the
    service answers; otherwise a coarse regional default (flagged in the report)."""
    try:
        from ..http import get
        r = get("https://hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv",
                params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "data": "depth", "units": "english", "series": "pds"}, timeout=30)
        rows = [ln.split(",") for ln in r.text.splitlines() if ln and ln[0] in "0123456789" or ln.startswith("5-min") or ln.startswith("1-hr") or ln.startswith("24-hr")]
        tbl = {}
        for ln in r.text.splitlines():
            p = [x.strip() for x in ln.split(",")]
            if p and p[0].rstrip(":") in ("5-min", "10-min", "15-min", "30-min", "60-min", "2-hr", "3-hr", "6-hr", "12-hr", "24-hr"):
                tbl[p[0].rstrip(":")] = [float(x) for x in p[1:] if x.replace(".", "", 1).isdigit()]
        # columns: 1,2,5,10,25,50,100,200,500,1000 yr
        cols = {1: 0, 2: 1, 5: 2, 10: 3, 25: 4, 50: 5, 100: 6}
        d24 = {s: tbl["24-hr"][cols[s]] for s in STORMS}
        d1 = {s: tbl["60-min"][cols[s]] for s in STORMS}
        return {"d24": d24, "d1": d1, "source": "NOAA Atlas 14 (point precipitation frequency estimates)"}
    except Exception:  # noqa: BLE001
        pass
    # regional defaults (inches): wetter along the Gulf coast, drier in the interior west
    if lat < 31:
        d24 = {2: 4.9, 10: 7.6, 25: 9.4, 50: 10.8, 100: 12.4}
    elif lat < 37:
        d24 = {2: 3.9, 10: 5.8, 25: 7.0, 50: 8.0, 100: 9.0}
    elif lon < -104:
        d24 = {2: 1.6, 10: 2.5, 25: 3.1, 50: 3.6, 100: 4.1}
    else:
        d24 = {2: 3.1, 10: 4.6, 25: 5.6, 50: 6.5, 100: 7.4}
    d1 = {s: d24[s] * 0.36 for s in STORMS}
    return {"d24": d24, "d1": d1, "source": "regional default depths (NOAA Atlas 14 was not reachable): verify before use"}


def storm_type(lat, lon):
    return "III" if (lat < 33.5 and lon > -100) or (lat < 41 and lon > -78 and lon < -65 and lat < 38) else "II"


def cn_runoff(P, cn):
    S = 1000.0 / cn - 10.0
    Ia = 0.2 * S
    return 0.0 if P <= Ia else (P - Ia) ** 2 / (P + 0.8 * S)


def composite_cn(areas):
    """areas: [(sf, cn)] -> area-weighted CN."""
    tot = sum(a for a, _ in areas)
    return sum(a * c for a, c in areas) / tot if tot else 98.0


def tc_min(sheet_ft, sheet_slope, n, shallow_ft, shallow_slope, paved, p2_in):
    """TR-55 sheet flow (<=100 ft) + shallow concentrated flow, minutes (min 6 min)."""
    sheet_ft = min(sheet_ft, 100.0)
    t1 = 0.007 * (n * sheet_ft) ** 0.8 / (p2_in ** 0.5 * max(sheet_slope, 0.003) ** 0.4) * 60.0
    v = (20.3282 if paved else 16.1345) * math.sqrt(max(shallow_slope, 0.003))
    t2 = shallow_ft / v / 60.0 if shallow_ft > 0 else 0.0
    return max(6.0, t1 + t2)


def hydrograph(area_ac, cn, tc, P24, stype="II", dt=0.05):
    """SCS triangular unit hydrograph convolved with incremental runoff of the 24-hr storm. Returns (t_hr, q_cfs)."""
    pts = TYPE_III if stype == "III" else TYPE_II
    t = np.arange(0, 48 + dt, dt)
    frac = np.interp(t, [a for a, _ in pts] + [48], [b for _, b in pts] + [1.0])
    P = frac * P24
    Q = np.array([cn_runoff(p, cn) for p in P])
    dQ = np.diff(Q, prepend=0.0)
    tc_hr = tc / 60.0
    tp = dt / 2 + 0.6 * tc_hr
    Tb = 2.67 * tp
    A_sqmi = area_ac / 640.0
    qp = 484.0 * A_sqmi / tp                      # cfs per inch of runoff
    tu = np.arange(0, Tb + dt, dt)
    uh = np.where(tu <= tp, qp * tu / tp, qp * (Tb - tu) / (Tb - tp))
    uh = np.clip(uh, 0, None)
    q = np.convolve(dQ, uh)[: len(t)]
    return t, q


def route_pond(t, qin, stage, storage, orifice, weir, dt=0.05):
    """Modified-Puls style routing. stage[], storage[] (ft, cf) tables; orifice = (invert, dia_ft); weir = (crest, len_ft).
    Returns (stage_peak, q_out_peak, q_out series, stage series)."""
    def qout(h):
        q = 0.0
        inv, d = orifice
        if h > inv:
            head = h - (inv + d / 2) if h > inv + d else (h - inv) / 2 if h > inv else 0
            a = math.pi * d * d / 4
            q += 0.6 * a * math.sqrt(2 * 32.2 * max(head, 0.0))
        c, L = weir
        if h > c:
            q += 3.3 * L * (h - c) ** 1.5
        return q

    n = len(t)
    S = 0.0
    out = np.zeros(n)
    hs = np.zeros(n)
    s_tab = np.array(storage)
    h_tab = np.array(stage)
    sec = dt * 3600.0
    h = h_tab[0]
    for i in range(1, n):
        qi = 0.5 * (qin[i] + qin[i - 1])
        h = float(np.interp(S, s_tab, h_tab))
        qo = qout(h)
        S = max(0.0, S + (qi - qo) * sec)
        h = float(np.interp(S, s_tab, h_tab))
        out[i] = qout(h)
        hs[i] = h
    return float(hs.max()), float(out.max()), out, hs


def pond_geometry(poly_top, top_el, depth, side=3.0):
    """Stage / area table for a pond with `side`:1 slopes from polygon at the top of bank. Elevations absolute."""
    rows = []
    bottom = top_el - depth
    steps = 12
    for k in range(steps + 1):
        h = bottom + depth * k / steps
        inset = (top_el - h) * side
        g = poly_top.buffer(-inset) if inset > 0 else poly_top
        rows.append((h, 0.0 if g.is_empty else g.area))
    stage = [r[0] for r in rows]
    area = [r[1] for r in rows]
    stor = [0.0]
    for k in range(1, len(rows)):
        stor.append(stor[-1] + 0.5 * (area[k] + area[k - 1]) * (stage[k] - stage[k - 1]))
    return stage, area, stor
