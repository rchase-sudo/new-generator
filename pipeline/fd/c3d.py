"""Civil 3D export package of the full design: everything the Drafted plugin (DRAFTEDIMPORT) needs to build native Civil 3D objects
in the firm's own template, plus the same design as plain files for anyone without the plugin.

drafted_c3d.zip
  manifest.json       surfaces (EG / FG points + boundary), gravity pipe networks (storm, sanitary) with structures, pipes, inverts, rims,
                      feature lines with elevations, ponds as contour sets (native Civil 3D 2027 pond objects), the drive alignment + design
                      profile, COGO points, pressure lines (water, force main) as polylines, and the firm's layer mapping
  linework.dxf        everything that is NOT a Civil 3D object (property, setbacks, buildings, pavement, striping, walks, water, laterals,
                      electric, gas, erosion control), on the firm's layers
  design.dxf          the complete design as plain DXF (contours, pipes and text included), on the firm's layers
  surfaces_pipes.xml  LandXML 1.2: surfaces, alignment + profile, pipe networks, feature lines (for Carlson / OpenRoads / manual import)
  README.txt
"""
import json
import math
import os
import re
import zipfile

import ezdxf
import numpy as np

from . import export
from .base import polys

# Drafted's standard layers: what each holds and the words a firm's layer name for it is likely to contain
ROLES = {
    "V-PROP-LINE": ("Property line", ["PROP", "BNDY", "BOUNDARY", "PL", "LOT", "PARCEL"], "existing"),
    "V-PROP-SBCK": ("Setback lines", ["SBCK", "SETBACK", "SETB", "BSL", "ESMT"], "existing"),
    "V-TOPO-MINR": ("Existing minor contours", ["TOPO", "CONT", "MINR", "MINOR"], "existing"),
    "V-TOPO-MAJR": ("Existing major contours", ["TOPO", "CONT", "MAJR", "MAJOR", "INDX"], "existing"),
    "V-TOPO-TEXT": ("Existing contour labels", ["TOPO", "CONT", "TEXT", "LABL", "ANNO"], "existing"),
    "V-ROAD-EDGE": ("Existing road edge", ["ROAD", "EOP", "PVMT", "EDGE", "CURB"], "existing"),
    "V-BLDG": ("Existing buildings", ["BLDG", "BUILDING", "STRC"], "existing"),
    "C-TOPO-MINR": ("Proposed minor contours", ["TOPO", "CONT", "GRAD", "MINR", "MINOR"], "proposed"),
    "C-TOPO-MAJR": ("Proposed major contours", ["TOPO", "CONT", "GRAD", "MAJR", "MAJOR", "INDX"], "proposed"),
    "C-TOPO-TEXT": ("Proposed contour labels", ["TOPO", "CONT", "GRAD", "TEXT", "LABL", "ANNO"], "proposed"),
    "C-TOPO-SPOT": ("Spot grades / COGO points", ["SPOT", "ELEV", "POINT", "PNTS", "COGO", "GRAD"], "proposed"),
    "C-TOPO": ("Proposed surface (FG)", ["SURF", "TOPO", "FG", "FINISH", "GRAD", "TIN"], "proposed"),
    "V-TOPO": ("Existing surface (EG)", ["SURF", "TOPO", "EG", "EXIST", "TIN"], "existing"),
    "C-GRAD-FEAT": ("Grading feature lines", ["FEAT", "FTLN", "GRAD", "BRKL", "BREAK"], "proposed"),
    "C-BLDG": ("Proposed buildings", ["BLDG", "BUILDING", "STRC", "FTPT"], "proposed"),
    "C-PVMT-EDGE": ("Pavement edge / curb", ["PVMT", "PAVE", "CURB", "EOP", "EDGE", "ASPH"], "proposed"),
    "C-PKNG-STRP": ("Parking striping", ["STRP", "STRIPE", "PKNG", "PARK", "PAINT", "MRKG"], "proposed"),
    "C-WALK": ("Sidewalks", ["WALK", "SDWK", "SIDEWALK", "CONC"], "proposed"),
    "C-ROAD-CNTR": ("Drive centerline (alignment)", ["CNTR", "CL", "CENTER", "ALIGN", "ALGN"], "proposed"),
    "C-ROAD-STAN": ("Stationing text", ["STAN", "STA", "STATION", "ALIGN"], "proposed"),
    "C-STRM-PIPE": ("Storm pipes", ["STRM", "STORM", "SD", "DRAN", "DRAIN", "PIPE"], "proposed"),
    "C-STRM-STRC": ("Storm structures", ["STRM", "STORM", "SD", "DRAN", "STRC", "STRUCT", "CB", "MH"], "proposed"),
    "C-STRM-TEXT": ("Storm labels", ["STRM", "STORM", "SD", "TEXT", "LABL", "ANNO"], "proposed"),
    "C-STRM-POND": ("Detention pond", ["POND", "BASN", "BASIN", "DETN", "SWM", "STRM"], "proposed"),
    "C-SSWR-PIPE": ("Sanitary pipes", ["SSWR", "SAN", "SANI", "SEWR", "SEWER", "SS", "PIPE"], "proposed"),
    "C-SSWR-FMAN": ("Sanitary force main", ["FMAN", "FM", "FORCE", "SSWR", "SAN"], "proposed"),
    "C-SSWR-STRC": ("Sanitary manholes", ["SSWR", "SAN", "SEWR", "STRC", "MH", "SMH"], "proposed"),
    "C-SSWR-TEXT": ("Sanitary labels", ["SSWR", "SAN", "SEWR", "TEXT", "LABL", "ANNO"], "proposed"),
    "C-SSWR-LATL": ("Sanitary laterals", ["LATL", "LAT", "SERV", "SVC", "SSWR", "SAN"], "proposed"),
    "C-WATR-PIPE": ("Water main", ["WATR", "WATER", "WTR", "WM", "DOM"], "proposed"),
    "C-WATR-LATL": ("Water services", ["WATR", "WATER", "WTR", "LATL", "SERV", "SVC"], "proposed"),
    "C-WATR-STRC": ("Hydrants and valves", ["WATR", "WATER", "HYDR", "HYD", "VALV", "VALVE", "FH"], "proposed"),
    "C-POWR-UGND": ("Underground electric", ["POWR", "ELEC", "ELECTRIC", "UGND", "UE"], "proposed"),
    "C-NGAS-UGND": ("Gas", ["NGAS", "GAS"], "proposed"),
    "C-EROS-LOD": ("Limit of disturbance", ["LOD", "LIMIT", "DIST", "EROS", "ESC"], "proposed"),
    "C-EROS-SILT": ("Silt fence", ["SILT", "SF", "EROS", "ESC", "FENC"], "proposed"),
    "C-TINN-PROP": ("Proposed surface 3D faces", ["TIN", "SURF", "FACE", "3D"], "proposed"),
}
# what becomes a native Civil 3D object (left out of linework.dxf so nothing is drawn twice)
C3D_OWNED = {"V-TOPO-MINR", "V-TOPO-MAJR", "V-TOPO-TEXT", "C-TOPO-MINR", "C-TOPO-MAJR", "C-TOPO-TEXT", "C-TOPO-SPOT", "C-TINN-PROP",
             "C-STRM-PIPE", "C-STRM-STRC", "C-STRM-TEXT", "C-STRM-POND", "C-SSWR-PIPE", "C-SSWR-STRC", "C-SSWR-TEXT", "C-SSWR-LATL", "C-ROAD-CNTR", "C-ROAD-STAN"}
GENERIC = {"PIPE", "TEXT", "LABL", "ANNO", "STRC", "STRUCT", "EDGE", "LAT", "LATL", "SERV", "SVC", "CONC", "ELEV", "POINT", "MH", "SD", "SS", "SF", "CL", "PL", "FM", "UE", "FH"}
EXISTING_HINT = ("EX", "EXIST", "EXST", "V-", "EXT")


def _toks(name):
    return [t for t in re.split(r"[^A-Z0-9]+", name.upper()) if t]


def automap(firm_layers):
    """Best guess of the firm's layer for each Drafted layer: exact name, then shared words, existing/proposed kept apart. {std: firm}"""
    names = [l["name"] if isinstance(l, dict) else str(l) for l in firm_layers or []]
    up = {n.upper(): n for n in names}
    out = {}
    for std, (_, words, kind) in ROLES.items():
        if std.upper() in up:
            out[std] = up[std.upper()]
            continue
        best, bs = None, 0.0
        sw = set(_toks(std))
        for n in names:
            t = _toks(n)
            ts = set(t)
            hit = sum(2.0 for w in words if w in ts) + sum(0.5 for w in words if len(w) > 2 and any(w in x and w != x for x in ts))
            if not any(w in ts or (len(w) > 2 and any(w in x for x in ts)) for w in words if w not in GENERIC):
                continue                     # must share at least one word that means this layer's content (not just PIPE / TEXT)
            s = hit + 0.5 * len(sw & ts)
            ex = any(n.upper().startswith(h) for h in EXISTING_HINT) or "EXIST" in n.upper()
            if kind == "existing":
                s += 1.5 if ex else -1.0
            elif ex:
                s -= 2.5
            if s > bs:
                best, bs = n, s
        if best and bs >= 2.0:
            out[std] = best
    return out


def _mapped(mapping, std):
    return (mapping or {}).get(std) or std


def relayer(src, dst, mapping, firm_layers=None, drop=()):
    """Copy of DXF src with every entity on the firm's layer (mapping std -> firm), firm layer colours / linetypes where known,
    and entities on `drop` layers removed."""
    doc = ezdxf.readfile(src)
    props = {l["name"]: l for l in firm_layers or [] if isinstance(l, dict) and l.get("name")}
    ms = doc.modelspace()
    for e in list(ms):
        lyr = e.dxf.layer
        if lyr in drop:
            ms.delete_entity(e)
            continue
        new = _mapped(mapping, lyr)
        if new != lyr:
            if new not in doc.layers:
                std = doc.layers.get(lyr) if lyr in doc.layers else None
                p = props.get(new) or {}
                lt = p.get("linetype") or (std.dxf.linetype if std else "CONTINUOUS")
                doc.layers.add(new, color=int(p.get("color") or (std.color if std else 7)), linetype=lt if lt in doc.linetypes else "CONTINUOUS")
            e.dxf.layer = new
    used = {e.dxf.layer for e in ms}
    for l in list(doc.layers):
        if l.dxf.name not in used and l.dxf.name not in ("0", "Defpoints") and l.dxf.name in ROLES:
            try:
                doc.layers.remove(l.dxf.name)
            except Exception:  # noqa: BLE001
                pass
    doc.saveas(dst)
    return dst


def _r(v, n=3):
    return round(float(v), n)


def manifest(X, meta, job, mapping=None, firm=None):
    W = export._W(X["frame"])
    g, L, G, D, net, U = X["grid"], X["L"], X["G"], X["D"], X["net"], X["U"]
    zp = X["z_prop"]

    def zat(x, y):
        return float(g.at(zp, [x], [y])[0])

    def en(p):
        e, n = W.pt(p)
        return [_r(e), _r(n)]

    def ring3(coords, zfun, step=10.0):
        """Ring / line vertices densified to `step` ft, each with its elevation."""
        cs = list(coords)
        out = []
        for a, b in zip(cs[:-1], cs[1:]):
            dl = math.dist(a, b)
            k = max(1, int(dl // step))
            for i in range(k):
                x, y = a[0] + (b[0] - a[0]) * i / k, a[1] + (b[1] - a[1]) * i / k
                out.append(en((x, y)) + [_r(zfun(x, y), 2)])
        out.append(en(cs[-1]) + [_r(zfun(*cs[-1]), 2)])
        return out

    # ---- surfaces (grid points, boundary = the TIN's extent)
    surfs = []
    for name, desc, z, mask in (("EG", "Existing ground (USGS 3DEP)", X["ex_z"], None),
                                ("FG", "Proposed finished grade", zp, g.mask(L["parcel"].buffer(15)))):
        pts, _ = export._tin(g, z, mask=mask)
        E, N = W.xy(pts[:, 0], pts[:, 1])
        arr = np.round(np.column_stack([E, N, pts[:, 2]]), 3)
        bnd = L["parcel"].buffer(15) if name == "FG" else None
        surfs.append({"name": name, "desc": desc, "layer": "C-TOPO" if name == "FG" else "V-TOPO",
                      "points": arr.reshape(-1).tolist(), "boundary": [en(c) for c in bnd.exterior.coords] if bnd is not None else None})
    # ---- gravity networks
    nets = []
    if net.get("pipes"):
        S = []
        for s in net["structs"]:
            inv = s.get("inv_out") if s.get("inv_out") is not None else s.get("inv_in")
            sump = (inv - (2.0 if s["kind"] == "CB" else 0.0)) if inv is not None else s["rim"] - 4
            S.append({"name": s["id"], "kind": s["kind"], "xy": en(s["xy"]), "rim": _r(s["rim"], 2), "sump": _r(sump, 2),
                      "dia_in": 24 if s["kind"] == "FES" else 48, "desc": {"CB": "Catch basin", "DMH": "Drain manhole", "FES": "Flared end section"}.get(s["kind"], s["kind"]),
                      "pond": s.get("pond")})
        byid = {s["id"]: s for s in net["structs"]}
        P = []
        for i, p in enumerate(net["pipes"], 1):
            a, b = p["line"].coords[0], p["line"].coords[-1]
            P.append({"name": f"ST-P{i}", "from": p["from"], "to": p["to"], "dia_in": p["dia"], "material": p["mat"], "inv_up": _r(p["inv_up"], 2), "inv_dn": _r(p["inv_dn"], 2),
                      "start": en(byid[p["from"]]["xy"] if p["from"] in byid else a), "end": en(byid[p["to"]]["xy"] if p["to"] in byid else b),
                      "len": _r(p["len"], 2), "slope": _r(p["slope"], 5), "shape": "circular"})
        nets.append({"name": "Storm", "kind": "storm", "pipe_layer": "C-STRM-PIPE", "struct_layer": "C-STRM-STRC", "structures": S, "pipes": P})
    SN = export.sanitary_network(X)
    if SN["pipes"]:
        byid = {m["id"]: m for m in SN["structs"]}
        S = [{"name": m["id"], "kind": m["kind"], "xy": en(m["xy"]), "rim": _r(m["rim"], 2), "sump": _r(m["sump"], 2), "dia_in": m["dia_in"],
              "existing": bool(m.get("existing")), "desc": m["desc"]} for m in SN["structs"]]
        P = [{"name": p["name"], "from": p["from"], "to": p["to"], "dia_in": p["dia"], "material": p["mat"], "inv_up": _r(p["inv_up"], 2), "inv_dn": _r(p["inv_dn"], 2),
              "start": en(byid[p["from"]]["xy"]), "end": en(byid[p["to"]]["xy"]), "len": _r(p["len"], 2), "slope": _r(p["slope"], 5), "shape": "circular"}
             for p in SN["pipes"]]
        nets.append({"name": "Sanitary", "kind": "sanitary", "pipe_layer": "C-SSWR-PIPE", "struct_layer": "C-SSWR-STRC", "structures": S, "pipes": P})
    # ---- pressure lines: polylines (water, force main)
    press = []
    if U.get("water_main") is not None:
        press.append({"name": "Water main", "kind": "water", "layer": "C-WATR-PIPE", "lines": [[en(c) for c in U["water_main"].coords]]})
        if U.get("water_lat"):
            press.append({"name": "Water services", "kind": "water", "layer": "C-WATR-LATL", "lines": [[en(c) for c in l.coords] for l in U["water_lat"]]})
    ps = U.get("pump_station") or {}
    if ps.get("fm_line") is not None:
        press.append({"name": f"Force main {ps.get('fm_dia_in', '')}\"", "kind": "force_main", "layer": "C-SSWR-FMAN", "lines": [[en(c) for c in ps["fm_line"].coords]]})
    # ---- feature lines: 6" granite curb on every pavement edge as bottom (BC, the gutter) and top (TC = BC + 0.50) of curb
    feats = []
    from scipy import ndimage as ndi
    from shapely.geometry import LineString as _LS, MultiLineString as _MLS
    from shapely.ops import linemerge, unary_union as _uu
    zpv = G.get("z_pave")
    if zpv is not None:
        fin = np.isfinite(zpv)
        _, ix = ndi.distance_transform_edt(~fin, return_indices=True)
        zpf = zpv[ix[0], ix[1]]

        def zbc(x, y):
            return float(g.at(zpf, [x], [y])[0])
    else:
        zbc = zat
    roads = [r[2] for r in (L.get("roads") or []) if r[2] is not None and not r[2].is_empty]
    no_curb = _uu(roads).buffer(6.0) if roads else None

    def runs(ring):
        ln = _LS(ring.coords)
        if no_curb is not None:
            ln = ln.difference(no_curb)
        if ln.is_empty:
            return []
        parts = list(ln.geoms) if isinstance(ln, _MLS) or hasattr(ln, "geoms") else [ln]
        m = linemerge([p_ for p_ in parts if p_.length > 1.0]) if len(parts) > 1 else parts[0]
        return [p_.simplify(0.05) for p_ in (m.geoms if hasattr(m, "geoms") else [m]) if p_.length > 2.0]

    def outward(q, ln, d=0.1):
        """The curb line moved d ft off the pavement (so TC never sits on top of BC in the TIN)."""
        o = ln.parallel_offset(d, "left", join_style=2)
        if o.is_empty or q.contains(o.interpolate(0.5, normalized=True)):
            o = ln.parallel_offset(d, "right", join_style=2)
        return o if not o.is_empty and o.geom_type == "LineString" else ln

    n = 0
    for q in polys(L["pave"]):
        for ring in [q.exterior] + list(q.interiors):
            for ln in runs(ring):
                n += 1
                closed = ln.is_ring
                bc = ring3(ln.coords, zbc)
                feats.append({"name": f"Curb {n} BC", "kind": "curb_bc", "layer": "C-GRAD-FEAT", "closed": closed, "pts": bc})
                tc = ring3(outward(q, ln).coords, lambda x, y: zbc(x, y) + 0.5)
                feats.append({"name": f"Curb {n} TC", "kind": "curb_tc", "layer": "C-GRAD-FEAT", "closed": closed, "pts": tc})
    # ADA spaces + aisles: their plane (1-1.5%, never over 2.08%)
    for k, a in enumerate(G.get("ada") or [], 1):
        pl = lambda x, y, a=a: a["c0"] + a["gx"] * (x - a["cx"]) + a["gy"] * (y - a["cy"])
        feats.append({"name": f"ADA parking {k} ({a['slope_pct']:.1f}%)", "kind": "ada", "layer": "C-GRAD-FEAT", "closed": True,
                      "pts": ring3(a["poly"].exterior.coords, pl, step=5.0)})
    for k, b in enumerate(L["bldgs"]):
        ffe = float(G["ffe"][k])
        feats.append({"name": f"Building {k + 1} pad (FFE {ffe:.2f})", "kind": "building", "layer": "C-GRAD-FEAT", "closed": True,
                      "pts": [en(c) + [_r(ffe, 2)] for c in b.exterior.coords]})
    # ---- ponds: contours every foot from the top down at 3:1 (the pond object's own geometry)
    ponds = []
    if (D.get("pond") or {}).get("kind") != "chambers":
        for q in D.get("ponds") or [D["pond"]]:
            top, bot = float(q["top_el"]), float(q["bottom_el"])
            side = float(q.get("side") or 3.0)
            levels = [top] + [e for e in np.arange(math.floor(top), bot, -1.0) if bot < e < top] + [bot]
            cons = []
            for el in levels:
                c = q["poly"].buffer(-(top - el) * side, join_style=1)
                c = max(polys(c), key=lambda p_: p_.area) if polys(c) else None
                if c is not None and c.area > 20:
                    cons.append({"elev": _r(el, 2), "pts": [en(xy) for xy in c.simplify(0.25).exterior.coords]})
            ponds.append({"name": q.get("id") or "POND", "layer": "C-STRM-POND", "top": _r(top, 2), "bottom": _r(bot, 2), "side_slope": side,
                          "storage_cf": round(float(q.get("storage_cf") or 0)), "orifice_in": q.get("orifice_in"), "weir_crest": q.get("weir_crest"),
                          "weir_len_ft": q.get("weir_len_ft"), "contours": cons})
    # ---- drive alignment + design profile
    aligns = []
    sp = G.get("spine")
    if sp is not None and sp.length > 5:
        cv = {round(c["pvi_sta"], 2): c for c in (G.get("curves") or [])}
        pvis = [[_r(s, 3), _r(z, 3), _r((cv.get(round(s, 2)) or {}).get("L") or 0, 2)] for s, z in (G.get("pvi") or [])]
        aligns.append({"name": "Drive CL", "layer": "C-ROAD-CNTR", "pts": [en(c) for c in sp.coords], "profile": {"name": "Drive CL - FG", "pvis": pvis},
                       "eg_profile": "Drive CL - EG"})
    # ---- COGO points: building corners at FFE, structure rims
    pts = []
    for k, b in enumerate(L["bldgs"]):
        for c in list(b.exterior.coords)[:-1][:8]:
            pts.append({"xy": en(c), "z": _r(G["ffe"][k], 2), "desc": f"FFE BLDG {k + 1}"})
    epsg = job.get("epsg")
    return {
        "format": "drafted-c3d", "version": 1, "units": "feet",
        "crs": {"epsg": epsg}, "project": {"name": " ".join(meta.get("project_lines", [])[:2]), "job_id": job.get("id")},
        "layers": {"mapping": mapping or {}, "roles": {k: v[0] for k, v in ROLES.items()}, "firm": [l.get("name") for l in (firm or {}).get("layers") or [] if isinstance(l, dict)][:3000]},
        "styles": (firm or {}).get("styles") or {},
        "surfaces": surfs, "networks": nets, "pressure": press, "feature_lines": feats, "ponds": ponds, "alignments": aligns, "points": pts,
        "property": [[en(c) for c in q.exterior.coords] for q in polys(L["parcel"])],
        "linework": "linework.dxf",
    }


README = """Drafted - Civil 3D export
=========================

With the Drafted plugin (Civil 3D 2027 or later)
  1. Start a new drawing from your firm's template (.dwt), with its coordinate system set to the one in manifest.json (crs.epsg).
  2. Run DRAFTEDIMPORT and pick this zip (or its manifest.json).
  3. Choose the styles, parts lists and layers in the dialog (your template's defaults are preselected) and press Import.
  You get: EG and FG TIN surfaces, the storm and sanitary gravity pipe networks on your parts lists, native pond objects,
  grading feature lines, the drive alignment with its EG and design profiles, COGO points, and the linework (property,
  buildings, pavement, striping, water, force main, electric, gas, erosion control) on your layers.

Without the plugin
  surfaces_pipes.xml  Insert > LandXML: surfaces, alignment + profile, storm and sanitary pipe networks, feature lines.
  design.dxf          The whole design as plain linework on your layers (contours, pipes, labels included).
  linework.dxf        Only what is not a Civil 3D object, for use alongside the LandXML import.

Coordinates are state plane feet (see manifest.json crs). Elevations are NAVD88 feet from USGS 3DEP.
This is a preliminary design produced by Drafted; it must be checked and sealed by a licensed professional engineer.
"""


def _plan_features(path, man):
    """Feature lines (pavement edges, building pads, pond top / bottom) into the LandXML as PlanFeatures (Civil 3D imports them as feature lines)."""
    from xml.sax.saxutils import quoteattr
    o = ['<PlanFeatures name="Drafted grading">']
    items = [(f["name"], f["pts"]) for f in man["feature_lines"]]
    for p in man["ponds"]:
        for c in (p["contours"][:1] + p["contours"][-1:]):
            items.append((f"{p['name']} {c['elev']:.2f}", [xy + [c["elev"]] for xy in c["pts"]]))
    for name, pts in items:
        if len(pts) < 2:
            continue
        o.append(f"<PlanFeature name={quoteattr(name)}><CoordGeom>")
        o.extend(f"<Line><Start>{a[1]:.3f} {a[0]:.3f} {a[2]:.3f}</Start><End>{b[1]:.3f} {b[0]:.3f} {b[2]:.3f}</End></Line>" for a, b in zip(pts[:-1], pts[1:]))
        o.append("</CoordGeom></PlanFeature>")
    o.append("</PlanFeatures>")
    s = open(path).read()
    if "<PlanFeatures" not in s:
        s = s.replace("</LandXML>", "\n".join(o) + "</LandXML>")
        open(path, "w").write(s)


def package(X, meta, job, out_dir, design_dxf, landxml, template=None):
    """Write drafted_c3d.zip next to the plan set. template: the firm's cad_templates row (mapping, layers, styles) or None."""
    mapping = (template or {}).get("mapping") or {}
    firm_layers = (template or {}).get("layers") or []
    if firm_layers and not mapping:
        mapping = automap(firm_layers)
    man = manifest(X, meta, job, mapping=mapping, firm=template)
    lw = relayer(design_dxf, os.path.join(out_dir, "linework.dxf"), mapping, firm_layers, drop=C3D_OWNED)
    full = relayer(design_dxf, os.path.join(out_dir, "design_firm.dxf"), mapping, firm_layers) if mapping else design_dxf
    _plan_features(landxml, man)
    zp = os.path.join(out_dir, "drafted_c3d.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(man, separators=(",", ":")))
        z.write(lw, "linework.dxf")
        z.write(full, "design.dxf")
        z.write(landxml, "surfaces_pipes.xml")
        z.writestr("README.txt", README)
    return zp
