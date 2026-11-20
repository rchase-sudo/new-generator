"""CAD / survey exports of the full design: a layered DXF (state plane feet, real elevations) and a LandXML 1.2 file with the existing and
proposed surfaces (TIN), the drive alignment + profile, and the storm and sanitary pipe networks, for import into Civil 3D / Carlson / OpenRoads."""
import datetime as dt
import math
from xml.sax.saxutils import quoteattr

import ezdxf
import numpy as np

from . import grading
from .base import lines, polys

LAYERS = {                      # name: (ACI colour, linetype)
    "V-PROP-LINE": (7, "PHANTOM2"), "V-PROP-SBCK": (8, "DASHED2"),
    "V-TOPO-MINR": (8, "DASHED2"), "V-TOPO-MAJR": (8, "DASHED"), "V-TOPO-TEXT": (8, "CONTINUOUS"), "V-ROAD-EDGE": (8, "CONTINUOUS"), "V-BLDG": (8, "CONTINUOUS"),
    "C-TOPO-MINR": (7, "CONTINUOUS"), "C-TOPO-MAJR": (7, "CONTINUOUS"), "C-TOPO-TEXT": (7, "CONTINUOUS"), "C-TOPO-SPOT": (2, "CONTINUOUS"),
    "C-BLDG": (7, "CONTINUOUS"), "C-PVMT-EDGE": (7, "CONTINUOUS"), "C-PKNG-STRP": (9, "CONTINUOUS"), "C-WALK": (9, "CONTINUOUS"),
    "C-ROAD-CNTR": (1, "CENTER2"), "C-ROAD-STAN": (1, "CONTINUOUS"),
    "C-STRM-PIPE": (3, "CONTINUOUS"), "C-STRM-STRC": (3, "CONTINUOUS"), "C-STRM-TEXT": (3, "CONTINUOUS"), "C-STRM-POND": (5, "CONTINUOUS"),
    "C-SSWR-PIPE": (30, "CONTINUOUS"), "C-SSWR-FMAN": (30, "DASHED2"), "C-SSWR-STRC": (30, "CONTINUOUS"), "C-SSWR-TEXT": (30, "CONTINUOUS"), "C-SSWR-LATL": (30, "DASHED2"),
    "C-WATR-PIPE": (5, "CONTINUOUS"), "C-WATR-LATL": (5, "DASHED2"), "C-WATR-STRC": (5, "CONTINUOUS"), "C-POWR-UGND": (1, "DASHED2"), "C-NGAS-UGND": (40, "DASHED2"),
    "C-EROS-LOD": (1, "DASHDOT2"), "C-EROS-SILT": (30, "CONTINUOUS"),
    "C-EROS-MATT": (94, "CONTINUOUS"), "C-SITE-RIPR": (8, "CONTINUOUS"), "C-SITE-WALL": (7, "CONTINUOUS"),
    "C-TINN-PROP": (4, "CONTINUOUS"),
}


def _tin(g, z, mask=None, step=1):
    """Regular-grid triangulation of grid surface z (local ft). Returns (points[n,3] local, faces[m,3] 0-based)."""
    ok = np.isfinite(z)
    if mask is not None:
        ok &= mask
    ny, nx = z.shape
    idx = -np.ones((ny, nx), int)
    jj, ii = np.nonzero(ok)
    idx[jj, ii] = np.arange(len(jj))
    pts = np.column_stack([g.X[jj, ii], g.Y[jj, ii], z[jj, ii]])
    a, b, c, d = idx[:-1, :-1], idx[:-1, 1:], idx[1:, 1:], idx[1:, :-1]
    f1 = (a >= 0) & (b >= 0) & (c >= 0)
    f2 = (a >= 0) & (c >= 0) & (d >= 0)
    faces = np.vstack([np.column_stack([a[f1], b[f1], c[f1]]), np.column_stack([a[f2], c[f2], d[f2]])])
    return pts, faces


class _W:
    """Local feet -> world feet (state plane coordinates expressed in feet)."""

    def __init__(self, fr):
        self.fr = fr

    def xy(self, x, y):
        X, Y = self.fr.pts_inv(np.asarray(x, float), np.asarray(y, float))
        return X * self.fr.k, Y * self.fr.k

    def pt(self, p):
        X, Y = self.xy(p[0], p[1])
        return float(X), float(Y)

    def coords(self, geom):
        cs = np.asarray(geom.coords)
        X, Y = self.xy(cs[:, 0], cs[:, 1])
        return list(zip(X.tolist(), Y.tolist()))


def surfaces(X):
    g = X["grid"]
    L = X["L"]
    eg = _tin(g, X["ex_z"])
    fg = _tin(g, X["z_prop"], mask=g.mask(L["parcel"].buffer(15)))
    return {"EG": eg, "FG": fg}


def landxml(X, path, name="Site"):
    W = _W(X["frame"])
    now = dt.datetime.now()
    o = ['<?xml version="1.0" encoding="UTF-8"?>',
         f'<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
         f'xsi:schemaLocation="http://www.landxml.org/schema/LandXML-1.2 http://www.landxml.org/schema/LandXML-1.2/LandXML-1.2.xsd" version="1.2" date="{now:%Y-%m-%d}" time="{now:%H:%M:%S}" language="English" readOnly="false">',
         '<Units><Imperial areaUnit="squareFoot" linearUnit="foot" volumeUnit="cubicYard" temperatureUnit="fahrenheit" pressureUnit="inHG" diameterUnit="inch" angularUnit="decimal degrees" directionUnit="decimal degrees"/></Units>',
         f'<Project name={quoteattr(name)}/>', '<Application name="Drafted" manufacturer="Apollo Development" version="1"/>', "<Surfaces>"]
    for sname, desc in (("EG", "Existing ground (USGS 3DEP)"), ("FG", "Proposed finished grade")):
        pts, faces = surfaces(X)[sname]
        E, N = W.xy(pts[:, 0], pts[:, 1])
        o.append(f'<Surface name="{sname}" desc={quoteattr(desc)}><Definition surfType="TIN" elevMin="{pts[:, 2].min():.2f}" elevMax="{pts[:, 2].max():.2f}"><Pnts>')
        o.extend(f'<P id="{i + 1}">{n:.3f} {e:.3f} {z:.3f}</P>' for i, (n, e, z) in enumerate(zip(N, E, pts[:, 2])))
        o.append("</Pnts><Faces>")
        o.extend(f"<F>{a + 1} {b + 1} {c + 1}</F>" for a, b, c in faces)
        o.append("</Faces></Definition></Surface>")
    o.append("</Surfaces>")
    # ---- drive centreline alignment + design profile
    G = X["G"]
    sp0 = G.get("spine")
    if sp0 is not None and sp0.length > 5:
        sp, remap = grading.clean_alignment(sp0)
        cs = W.coords(sp)
        o.append(f'<Alignments><Alignment name="Drive CL" length="{sp.length:.3f}" staStart="0.000"><CoordGeom>')
        for (e0, n0), (e1, n1) in zip(cs[:-1], cs[1:]):
            o.append(f'<Line length="{math.hypot(e1 - e0, n1 - n0):.3f}"><Start>{n0:.3f} {e0:.3f}</Start><End>{n1:.3f} {e1:.3f}</End></Line>')
        o.append('</CoordGeom><Profile name="Drive CL"><ProfSurf name="EG"><PntList2D>' +
                 " ".join(f"{s:.2f} {z:.2f}" for s, z in sorted({round(remap(s_), 2): z_ for s_, z_ in zip(G["stations"], G["ex_profile"]) if np.isfinite(z_)}.items())) + '</PntList2D></ProfSurf><ProfAlign name="FG">')
        cv = {round(c["pvi_sta"], 2): c for c in (G.get("curves") or [])}
        last = -1.0
        for s0, z in (G.get("pvi") or []):
            c = cv.get(round(s0, 2))
            s = remap(s0)
            if s <= last + 1.0:
                continue
            last = s
            o.append(f'<ParaCurve length="{c["L"]:.2f}">{s:.3f} {z:.3f}</ParaCurve>' if c and c.get("L") else f"<PVI>{s:.3f} {z:.3f}</PVI>")
        o.append("</ProfAlign></Profile></Alignment></Alignments>")
    # ---- pipe networks
    o.append("<PipeNetworks>")
    net = X["net"]
    if net.get("pipes"):
        o.append('<PipeNetwork name="Storm" pipeNetType="storm"><Structs>')
        pid = {id(p): f"ST-P{i}" for i, p in enumerate(net["pipes"], 1)}
        for s in net["structs"]:
            e, n = W.pt(s["xy"])
            inv = s.get("inv_out") if s.get("inv_out") is not None else s.get("inv_in")
            sump = (inv - (2.0 if s["kind"] == "CB" else 0.0)) if inv is not None else s["rim"] - 4
            o.append(f'<Struct name="{s["id"]}" desc="{ {"CB": "Catch basin", "DMH": "Drain manhole", "FES": "Flared end section"}.get(s["kind"], s["kind"]) }" elevRim="{s["rim"]:.2f}" elevSump="{sump:.2f}"><Center>{n:.3f} {e:.3f}</Center>')
            o.append('<CircStruct diameter="48" material="Precast concrete"/>' if s["kind"] != "FES" else '<OutletStruct/>' if False else '<CircStruct diameter="24" material="HDPE"/>')
            for p in net["pipes"]:
                if p["from"] == s["id"]:
                    o.append(f'<Invert elev="{p["inv_up"]:.2f}" flowDir="out" refPipe="{pid[id(p)]}"/>')
                if p["to"] == s["id"]:
                    o.append(f'<Invert elev="{p["inv_dn"]:.2f}" flowDir="in" refPipe="{pid[id(p)]}"/>')
            o.append("</Struct>")
        o.append("</Structs><Pipes>")
        for p in net["pipes"]:
            o.append(f'<Pipe name="{pid[id(p)]}" refStart="{p["from"]}" refEnd="{p["to"]}" length="{p["len"]:.2f}" slope="{p["slope"]:.4f}"><CircPipe diameter="{p["dia"]}" material="{p["mat"]}"/></Pipe>')
        o.append("</Pipes></PipeNetwork>")
    SN = sanitary_network(X)
    if SN["pipes"]:
        o.append('<PipeNetwork name="Sanitary" pipeNetType="sanitary"><Structs>')
        for m in SN["structs"]:
            e, n = W.pt(m["xy"])
            o.append(f'<Struct name="{m["id"]}" desc="{m["desc"]}" elevRim="{m["rim"]:.2f}" elevSump="{m["sump"]:.2f}"><Center>{n:.3f} {e:.3f}</Center>'
                     f'<CircStruct diameter="{m["dia_in"]}" material="{"PVC" if m["kind"] in ("CO", "TAP") else "Precast concrete"}"/>')
            for p in SN["pipes"]:
                if p["from"] == m["id"]:
                    o.append(f'<Invert elev="{p["inv_up"]:.2f}" flowDir="out" refPipe="{p["name"]}"/>')
                if p["to"] == m["id"]:
                    o.append(f'<Invert elev="{p["inv_dn"]:.2f}" flowDir="in" refPipe="{p["name"]}"/>')
            o.append("</Struct>")
        o.append("</Structs><Pipes>")
        for p in SN["pipes"]:
            o.append(f'<Pipe name="{p["name"]}" refStart="{p["from"]}" refEnd="{p["to"]}" length="{p["len"]:.2f}" slope="{p["slope"]:.4f}"><CircPipe diameter="{p["dia"]}" material="PVC"/></Pipe>')
        o.append("</Pipes></PipeNetwork>")
    o.append("</PipeNetworks></LandXML>")
    with open(path, "w") as f:
        f.write("\n".join(o))
    return path


def dxf(X, path):
    W = _W(X["frame"])
    L, G, D, net, U, g = X["L"], X["G"], X["D"], X["net"], X["U"], X["grid"]
    doc = ezdxf.new("R2018", setup=True)
    doc.units = ezdxf.units.FT
    for name, (col, lt) in LAYERS.items():
        doc.layers.add(name, color=col, linetype=lt if lt in doc.linetypes else "CONTINUOUS")
    doc.layers.get("C-TINN-PROP").off()
    ms = doc.modelspace()
    rot = -X["frame"].rot                  # text rotation from local to world

    def pl(geom, layer, elev=0.0, close=None):
        for q in polys(geom) if geom.geom_type in ("Polygon", "MultiPolygon", "GeometryCollection") and polys(geom) else []:
            for ring in [q.exterior] + list(q.interiors):
                ms.add_lwpolyline(W.coords(ring), close=True, dxfattribs={"layer": layer, "elevation": elev})
        for q in lines(geom) if geom.geom_type in ("LineString", "MultiLineString", "LinearRing") or (geom.geom_type == "GeometryCollection" and not polys(geom)) else []:
            ms.add_lwpolyline(W.coords(q), dxfattribs={"layer": layer, "elevation": elev})

    def tx(p, s, layer, h=2.0, r=0.0):
        t = ms.add_mtext(s, dxfattribs={"layer": layer, "char_height": h, "rotation": (r + rot) % 360, "attachment_point": 7})
        t.set_location(W.pt(p))

    def circ(p, r, layer):
        ms.add_circle(W.pt(p), r, dxfattribs={"layer": layer})

    pl(L["parcel"], "V-PROP-LINE")
    if L.get("setback") is not None and not L["setback"].is_empty:
        pl(L["setback"], "V-PROP-SBCK")
    for _, _, pv in L.get("roads") or []:
        if pv is not None and not pv.is_empty:
            pl(pv, "V-ROAD-EDGE")
    for b in L.get("nbld") or []:
        pl(b, "V-BLDG")
    view = L["parcel"].buffer(60)
    for z, lyr_mn, lyr_mj, lyr_tx in ((X["ex_z"], "V-TOPO-MINR", "V-TOPO-MAJR", "V-TOPO-TEXT"), (X["z_prop"], "C-TOPO-MINR", "C-TOPO-MAJR", "C-TOPO-TEXT")):
        clip = view if lyr_mn.startswith("V") else g.cell_poly(G["lod_mask"]).buffer(2) if G.get("lod_mask") is not None and G["lod_mask"].any() else L["dev"].buffer(20)
        rel = float(np.nanmax(z) - np.nanmin(z))
        iv = 1.0 if rel < 12 else 2.0
        for lev, ln, is_idx in grading.contour_lines(g, z, interval=iv, index=iv * 5, clip=clip, fine=2):
            ms.add_lwpolyline(W.coords(ln), dxfattribs={"layer": lyr_mj if is_idx else lyr_mn, "elevation": lev})
            if is_idx and ln.length > 80:
                tx(ln.interpolate(0.5, normalized=True).coords[0], f"{lev:.0f}", lyr_tx, h=2.5)
    for k, b in enumerate(L["bldgs"]):
        pl(b, "C-BLDG")
        c = b.representative_point()
        tx((c.x - 18, c.y), f"PROPOSED BUILDING\\P{b.area:,.0f} SF\\PFFE {G['ffe'][k]:.2f}", "C-BLDG", h=3.0)
    pl(L["pave"], "C-PVMT-EDGE")
    if L.get("walk") is not None and not L["walk"].is_empty:
        pl(L["walk"], "C-WALK")
    for s in (L.get("stripes") or L.get("stalls") or []):
        pl(s if s.geom_type != "Polygon" else s.exterior, "C-PKNG-STRP")
    if G.get("spine") is not None:
        cl_, _ = grading.clean_alignment(G["spine"])
        pl(cl_, "C-ROAD-CNTR")
        st = 0.0
        while st <= cl_.length:
            p = cl_.interpolate(st)
            tx((p.x, p.y), f"{int(st // 100)}+{st % 100:02.0f}", "C-ROAD-STAN", h=2.0)
            st += 50.0
    pond = D["pond"]
    if pond.get("kind") == "chambers":
        for b in pond.get("beds") or []:
            pl(b, "C-STRM-POND")
    else:
        for q_ in D.get("ponds") or [pond]:
            pl(q_["poly"], "C-STRM-POND", elev=q_["top_el"])
            fl = q_["poly"].buffer(-q_["depth_ft"] * 3.0)
            if not fl.is_empty:
                pl(fl, "C-STRM-POND", elev=q_["bottom_el"])
            c = q_["poly"].representative_point()
            tx((c.x, c.y), f"DETENTION {q_.get('id', 'POND')}\\PTOP {q_['top_el']:.2f}  BOT {q_['bottom_el']:.2f}\\P{q_['storage_cf']:,.0f} CF, 3:1", "C-STRM-TEXT", h=2.5)
    for i, p in enumerate(net.get("pipes") or [], 1):
        a, b = W.pt(p["line"].coords[0]), W.pt(p["line"].coords[-1])
        ms.add_line((a[0], a[1], p["inv_up"]), (b[0], b[1], p["inv_dn"]), dxfattribs={"layer": "C-STRM-PIPE"})
        c = p["line"].interpolate(0.5, normalized=True)
        tx((c.x, c.y), f"P-{i}  {p['len']:.0f} LF {p['dia']}\" {p['mat']} @ {p['slope'] * 100:.2f}%", "C-STRM-TEXT", h=1.8)
    for s in net.get("structs") or []:
        circ(s["xy"], 2.0 if s["kind"] != "FES" else 1.2, "C-STRM-STRC")
        inv = s.get("inv_out") if s.get("inv_out") is not None else s.get("inv_in")
        tx((s["xy"][0] + 3, s["xy"][1] - 3), f"{s['id']}\\PRIM {s['rim']:.2f}\\PINV {inv:.2f}" if inv is not None else s["id"], "C-STRM-TEXT", h=1.8)
    if U.get("sewer_main") is not None:
        if U.get("pump_station") and U["pump_station"].get("fm_line") is not None:
            pl(U["pump_station"]["fm_line"], "C-SSWR-FMAN")
        byid = {m["id"]: m for m in U.get("smh") or []}
        for p in U.get("sewer_pipes") or []:
            a, b = W.pt(byid[p["from"]]["xy"]), W.pt(byid[p["to"]]["xy"])
            ms.add_line((a[0], a[1], p["inv_up"]), (b[0], b[1], p["inv_dn"]), dxfattribs={"layer": "C-SSWR-PIPE"})
            mx, my = (byid[p["from"]]["xy"][0] + byid[p["to"]]["xy"][0]) / 2, (byid[p["from"]]["xy"][1] + byid[p["to"]]["xy"][1]) / 2
            tx((mx, my), f"{p['len']:.0f} LF 8\" PVC SDR 35 @ {p['slope'] * 100:.2f}%", "C-SSWR-TEXT", h=1.8)
        for m in U.get("smh") or []:
            circ(m["xy"], 3.0 if m.get("kind") == "PS" else 2.0, "C-SSWR-STRC")
            tx((m["xy"][0] + 3, m["xy"][1] - 3), f"{m['id']}\\PRIM {m['rim']:.2f}\\PINV {m['inv']:.2f}", "C-SSWR-TEXT", h=1.8)
        for l in U.get("sewer_lat") or []:
            pl(l, "C-SSWR-LATL")
    if U.get("water_main") is not None:
        pl(U["water_main"], "C-WATR-PIPE")
        for l in U.get("water_lat") or []:
            pl(l, "C-WATR-LATL")
        for h in U.get("hydrants") or []:
            circ(h, 1.5, "C-WATR-STRC")
            tx((h[0] + 2, h[1] + 2), "FH", "C-WATR-STRC", h=1.8)
        for v in U.get("valves") or []:
            circ(v, 0.8, "C-WATR-STRC")
    for key, lyr in (("elec", "C-POWR-UGND"), ("gas", "C-NGAS-UGND")):
        if U.get(key) is not None:
            pl(U[key], lyr)
    if G.get("lod_mask") is not None and G["lod_mask"].any() and not X.get("no_esc"):      # (no erosion control on Redline jobs)
        pl(g.cell_poly(G["lod_mask"]).simplify(2.0), "C-EROS-LOD")
    ER = (X.get("ER") or {}) if not X.get("no_esc") else {}
    for key in ("silt", "silt_fence"):
        if ER.get(key) is not None and hasattr(ER[key], "geom_type"):
            pl(ER[key], "C-EROS-SILT")
    # slope treatment: 2:1 slopes with erosion control matting, 1:1 slopes with riprap, retaining walls (always shown)
    SLP = X.get("slopes") or {}
    for key, lyr, pat, sc, lab in (("matting", "C-EROS-MATT", "NET", 4.0, "2:1 SLOPE\\PEROSION CONTROL MATTING"), ("riprap", "C-SITE-RIPR", "GRAVEL", 6.0, "1:1 SLOPE\\PRIPRAP SLOPE PROTECTION")):
        for q in SLP.get(key) or []:
            pl(q, lyr)
            try:
                hz = ms.add_hatch(dxfattribs={"layer": lyr})
                hz.set_pattern_fill(pat, scale=sc)
                hz.paths.add_polyline_path(W.coords(q.exterior), is_closed=True)
                for r_ in q.interiors:
                    hz.paths.add_polyline_path(W.coords(r_), is_closed=True)
            except Exception:  # noqa: BLE001
                pass
            c_ = q.representative_point()
            tx((c_.x, c_.y), lab, lyr, h=2.0)
    for k, w in enumerate(SLP.get("walls") or [], 1):
        pl(w["line"], "C-SITE-WALL")
        off = w["line"].parallel_offset(1.0, "left", join_style=2)
        if not off.is_empty and off.geom_type == "LineString":
            pl(off, "C-SITE-WALL")
        m_ = w["line"].interpolate(0.5, normalized=True)
        tw = max(t[1] for t in w["top"]); bw = min(b[1] for b in w["bot"])
        tx((m_.x + 4, m_.y + 4), f"RETAINING WALL\\P{w['len']:.0f} LF, {w['h_max']:.1f}' MAX HT\\PTW {tw:.1f} / BW {bw:.1f}", "C-SITE-WALL", h=2.0)
    # spot grades at structures and building corners
    for k, b in enumerate(L["bldgs"]):
        for x, y in list(b.exterior.coords)[:-1][:8]:
            z = float(g.at(X["z_prop"], [x], [y])[0])
            ms.add_point((*W.pt((x, y)), z), dxfattribs={"layer": "C-TOPO-SPOT"})
    # proposed surface as 3D faces inside the limit of disturbance (layer off by default); the full surfaces are in the LandXML
    if G.get("lod_mask") is not None and G["lod_mask"].any():
        from scipy import ndimage as ndi
        pts, faces = _tin(g, X["z_prop"], mask=ndi.binary_dilation(G["lod_mask"], iterations=2))
        E, N = W.xy(pts[:, 0], pts[:, 1])
        P3 = np.column_stack([E, N, pts[:, 2]])
        for a_, b_, c_ in faces:
            ms.add_3dface([tuple(P3[a_]), tuple(P3[b_]), tuple(P3[c_]), tuple(P3[c_])], dxfattribs={"layer": "C-TINN-PROP"})
    doc.saveas(path)
    return path


def sanitary_network(X):
    """The whole gravity sanitary system as one network: the mains (manholes, pump station wet well, the existing manhole)
    plus every building's 6" service lateral, from a cleanout at the building to its tap on the main (or straight into the
    existing manhole when there is no main). Local coordinates. {"structs": [...], "pipes": [...]}"""
    U, G, L, g, zp = X["U"], X["G"], X["L"], X["grid"], X["z_prop"]
    S, P = [], []
    for m in U.get("smh") or []:
        S.append({"id": m["id"], "kind": m.get("kind") or "SMH", "xy": tuple(m["xy"]), "rim": float(m["rim"]), "sump": float(m["rim"] - m["depth"]),
                  "dia_in": 72 if m.get("kind") == "PS" else 48, "existing": bool(m.get("existing")),
                  "desc": {"PS": "Pump station wet well", "EX": "Existing sewer manhole"}.get(m.get("kind"), "Sewer manhole")})
    for i, p in enumerate(U.get("sewer_pipes") or [], 1):
        P.append({"name": f"SS-P{i}", "from": p["from"], "to": p["to"], "dia": p["dia"], "mat": p.get("mat") or "PVC SDR 35", "inv_up": float(p["inv_up"]),
                  "inv_dn": float(p["inv_dn"]), "len": float(p["len"]), "slope": float(p["slope"])})
    rows = {r["bldg"] - 1: r for r in U.get("sewer_lat_rows") or []}
    from shapely.geometry import Point as _Pt
    for k, ln in enumerate(U.get("sewer_lat") or []):
        if ln is None or ln.length < 1 or k not in rows:
            continue
        r = rows[k]
        cs = list(ln.coords)
        b = L["bldgs"][k] if k < len(L["bldgs"]) else None
        if b is not None and _Pt(cs[0]).distance(b) < _Pt(cs[-1]).distance(b):
            cs = cs[::-1]                                       # building end last
        up_inv, dn_inv = float(r["inv_bldg"]), float(r["inv_main"])
        # downstream: an existing / proposed manhole it reaches, else a tap (wye) on the main
        dn = min(S, key=lambda q: math.dist(q["xy"], cs[0]), default=None)
        if dn is None or math.dist(dn["xy"], cs[0]) > 6.0:
            z0 = float(g.at(zp, [cs[0][0]], [cs[0][1]])[0])
            dn = {"id": f"TAP-{k + 1}", "kind": "TAP", "xy": tuple(cs[0]), "rim": z0, "sump": dn_inv, "dia_in": 6, "desc": f"Wye: building {k + 1} lateral"}
            S.append(dn)
        z1 = float(g.at(zp, [cs[-1][0]], [cs[-1][1]])[0])
        co = {"id": f"CO-{k + 1}", "kind": "CO", "xy": tuple(cs[-1]), "rim": max(z1, up_inv + 3.0), "sump": up_inv, "dia_in": 6, "desc": f"Cleanout: building {k + 1} sewer"}
        S.append(co)
        # one pipe per straight run; bends get a cleanout / bend structure
        Ltot = sum(math.dist(a, c) for a, c in zip(cs[:-1], cs[1:])) or 1.0
        nodes = [dn["id"]]
        acc = [0.0]
        for j in range(1, len(cs) - 1):
            acc.append(acc[-1] + math.dist(cs[j - 1], cs[j]))
            zb = float(g.at(zp, [cs[j][0]], [cs[j][1]])[0])
            iv = dn_inv + (up_inv - dn_inv) * acc[-1] / Ltot
            S.append({"id": f"CO-{k + 1}{chr(65 + j - 1)}", "kind": "CO", "xy": tuple(cs[j]), "rim": max(zb, iv + 3.0), "sump": iv, "dia_in": 6,
                      "desc": f"Bend cleanout: building {k + 1} sewer"})
            nodes.append(S[-1]["id"])
        acc.append(Ltot)
        nodes.append(co["id"])
        for j in range(len(nodes) - 1):
            a_, c_ = acc[j], acc[j + 1]
            i_dn = dn_inv + (up_inv - dn_inv) * a_ / Ltot
            i_up = dn_inv + (up_inv - dn_inv) * c_ / Ltot
            P.append({"name": f"SS-L{k + 1}{'-' + str(j + 1) if len(nodes) > 2 else ''}", "from": nodes[j + 1], "to": nodes[j], "dia": 6, "mat": "PVC SDR 35",
                      "inv_up": round(i_up, 2), "inv_dn": round(i_dn, 2), "len": round(c_ - a_, 2), "slope": round((i_up - i_dn) / max(c_ - a_, 0.1), 5)})
    return {"structs": S, "pipes": P}
