"""The sheets: T1 title, C1 existing conditions, C2 site, C3 erosion/demo, C4 grading, C5 drainage, C6 profiles, C7 utilities,
C8 fire, C9 landscape, C10 lighting. Every function takes (meta, X) and returns a Sheet (not yet saved)."""
import math

import numpy as np
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from . import draw
from .base import lines, polys
from .draw import corner, table
from .sheetbase import BLUE, BROWN, CYAN, GREEN, GREY, INK, LGREY, RED, Sheet

PRELIM = ("PRELIMINARY — FOR PLANNING AND PERMIT-PREPARATION REVIEW ONLY. NOT SEALED, NOT FOR CONSTRUCTION. Existing conditions are "
          "compiled from public GIS data and have not been field surveyed; a licensed surveyor and the engineer of record must verify "
          "before any design is relied on.")


def _site_geom(X, extra=()):
    L = X["L"]
    gs = [L["dev"], L["pond"], X["G"]["spine"].buffer(30), X["ER"]["lod"]] + list(extra)
    return unary_union([g for g in gs if g is not None and not g.is_empty])


def _site(meta, X, no, title, extra=(), margin=45, **kw):
    return Sheet(meta, no, title, (0, 0), fit_geom=_site_geom(X, extra), margin_ft=margin, **kw)


def _legend(S, fx, fy, items, title="LEGEND", w=2.7):
    """items: [(kind, style, label)] kind in line|poly|text. Drawn as a boxed legend at a fraction of the view."""
    X0, Y0 = corner(S, fx, fy)
    sc = S.scale
    rh = 0.2 * sc
    S.ax.add_patch(draw.plt_rect(X0, Y0 - (len(items) + 1.1) * rh, w * sc, (len(items) + 1.1) * rh, "white"))
    S.ax.plot([X0, X0 + w * sc, X0 + w * sc, X0, X0], [Y0, Y0, Y0 - (len(items) + 1.1) * rh, Y0 - (len(items) + 1.1) * rh, Y0], color=INK, lw=0.8, zorder=62)
    S.txt(X0 + 0.08 * sc, Y0 - 0.5 * rh, title, 8, ha="left", weight="bold", z=63)
    for i, (kind, st, lab) in enumerate(items):
        y = Y0 - (i + 1.6) * rh
        x1, x2 = X0 + 0.1 * sc, X0 + 0.65 * sc
        if kind == "line":
            S.ax.plot([x1, x2], [y, y], zorder=63, **st)
        elif kind == "poly":
            from matplotlib.patches import Rectangle
            S.ax.add_patch(Rectangle((x1, y - rh * 0.3), x2 - x1, rh * 0.6, zorder=63, **st))
        elif kind == "mark":
            S.ax.plot([(x1 + x2) / 2], [y], zorder=63, linestyle="none", **st)
        S.txt(X0 + 0.75 * sc, y, lab, 5.6, ha="left", z=63)


def _prelim_note(S, fx=0.02, fy=0.06):
    x, y = corner(S, fx, fy)
    S.txt(x, y, "PRELIMINARY — NOT SEALED — NOT FOR CONSTRUCTION", 9, ha="left", weight="bold", color=RED, z=60)


def _apply_notes(S, items, fx, fy, title="GENERAL NOTES", width=8.6):
    return S.text_block((fx, fy), title, items, width_in=width)


def _bldg_labels(S, X, with_ffe=False):
    fac = X["facts"]
    for i, b in enumerate(X["L"]["bldgs"]):
        c = b.representative_point()
        t = f"PROPOSED BUILDING\n{b.area:,.0f} SF"
        if with_ffe:
            t += f"\nFFE = {X['G']['ffe'][i]:.2f}"
        S.txt(c.x, c.y, t, 6.6, weight="bold", halo=True, z=25)


# ======================================================================== T1
def t1(meta, X):
    S = Sheet(meta, "T1", "TITLE SHEET", (0, 0), scale=1, scale_text="N.T.S.")
    W, H = S.W_in, S.H_in
    S.ax.set_xlim(0, W)
    S.ax.set_ylim(0, H)
    S.view = (0, 0, W, H)
    S.north = 0
    m = meta
    S.txt(W * 0.5, H - 1.0, "PRELIMINARY CIVIL SITE PLANS", 36, weight="bold", family="Carlito")
    S.txt(W * 0.5, H - 1.85, m["project_lines"][0], 26, weight="bold", family="Carlito")
    for i, ln in enumerate(m["project_lines"][1:4]):
        S.txt(W * 0.5, H - 2.5 - i * 0.42, ln, 15, family="Carlito")
    # aerial with subject lot
    if m.get("site_img") is not None:
        ax0, ay0, aw, ah = W * 0.30, 0.8, W * 0.40, H * 0.52
        S.ax.imshow(m["site_img"], extent=(ax0, ax0 + aw, ay0, ay0 + ah), zorder=3, aspect="auto")
        S.ax.plot([ax0, ax0 + aw, ax0 + aw, ax0, ax0], [ay0, ay0, ay0 + ah, ay0 + ah, ay0], color=INK, lw=1.2, zorder=4)
        pts = [(ax0 + fx * aw, ay0 + fy * ah) for fx, fy in m["site_poly"]]
        S.ax.plot([p[0] for p in pts] + [pts[0][0]], [p[1] for p in pts] + [pts[0][1]], color="#ffd400", lw=2.4, zorder=5)
        S.txt(ax0 + aw / 2, ay0 + ah + 0.25, "SITE LOCATION — SUBJECT PARCEL OUTLINED", 10, weight="bold")
    # sheet index
    rows = [["SHEET", "TITLE"]] + [[n, t] for n, t in m["index"]]
    table(S, 0.9, H - 4.4, [0.8, 4.4], rows, title="SHEET INDEX", size=7, row_h=0.28)
    # project data
    f = X["facts"]
    data = [["PROJECT DATA", ""], ["Parcel area", f"{f['parcel_ac']:.2f} ac"], ["Zoning", m.get("zone_line") or "—"],
            ["Proposed building", f"{f['bldg_sf']:,.0f} SF"], ["Parking provided", f"{f['stalls']} stalls ({f['ada']} ADA)"],
            ["Impervious (new)", f"{f['imp_sf']:,.0f} SF"], ["Disturbed area", f"{f['dist_ac']:.2f} ac"],
            ["Detention", f"{f['pond_cf']:,.0f} CF"], ["Water / Sewer", "Public (assumed)"]]
    table(S, W * 0.72, H - 4.4, [1.9, 3.1], data, size=7, row_h=0.28)
    # legend
    leg = [["LEGEND", ""], ["Property line", "heavy solid"], ["Building setback", "dash-dot"], ["Existing contour", "grey dashed"],
           ["Proposed contour", "solid"], ["Storm sewer", "—SD—  (CB, FES)"], ["Sanitary sewer", "—SS—  (SMH)"], ["Water main", "—W—  (HYD, GV)"],
           ["Electric / Gas", "—E—   —G—"], ["Silt fence / sock", "—SF—"]]
    table(S, 0.9, H - 9.0, [2.0, 3.2], leg, size=7, row_h=0.28)
    # notes + approval box
    _apply_notes(S, [
        "These plans were generated by FORMA from a concept site layout and public data. They are a preliminary design for planning, "
        "budgeting and agency pre-application discussions.",
        "Existing topography is from USGS 3DEP; buildings from OSM / Microsoft footprints; wetlands from NWI; flood zones from FEMA. None "
        "of it is a field survey.",
        "Utility connections assume public water and sanitary sewer in the adjacent right-of-way. Existing main depth and location are "
        "assumed and must be confirmed with the utility.",
        "Rainfall depths: " + X["rain"]["source"] + ". Soil group: " + X["soils"]["source"] + ".",
        "No sheet in this set is signed or sealed. The engineer of record must review, revise and seal before submission."],
        0.72, 0.52, title="NOTES", width=9.0)
    ax, ay = W * 0.72, 0.8
    S.ax.add_patch(draw.plt_rect(ax, ay, 5.0, 2.2, "white"))
    S.ax.plot([ax, ax + 5, ax + 5, ax, ax], [ay, ay, ay + 2.2, ay + 2.2, ay], color=INK, lw=0.8, zorder=62)
    S.txt(ax + 2.5, ay + 1.95, "AGENCY APPROVAL", 9, weight="bold", z=63)
    for i, lab in enumerate(["REVIEWED BY: ______________________", "DATE: ________________", "APPROVED: ____________________"]):
        S.txt(ax + 0.2, ay + 1.4 - i * 0.45, lab, 7, ha="left", z=63)
    S.call811((0.5), ) if False else None
    S.txt(W * 0.5, 0.35, PRELIM, 6.0, color=GREY, z=60)
    return S


# ======================================================================== C1 existing conditions
def c1(meta, X):
    L = X["L"]
    S = Sheet(meta, "C1", "EXISTING CONDITIONS PLAN", (0, 0), fit_geom=L["parcel"].buffer(60), margin_ft=40)
    draw.neighbor_buildings(S, X)
    draw.road_layers(S, X)
    draw.contours(S, X, "ex")
    for e in L.get("existing") or []:
        if e.get("kind") == "pavement":
            S.poly(e["geom"], fc="#f0f1f3", ec=GREY, lw=0.7, z=3)
            q = e["geom"].representative_point()
            S.txt(q.x, q.y, "EXISTING PAVEMENT", 5.0, color=GREY, halo=True, z=20)
        else:
            S.poly(e["geom"], fc="#e6e8eb", ec=INK, lw=1.0, z=4)
            q = e["geom"].representative_point()
            S.txt(q.x, q.y, f"EXISTING BUILDING {e['id']}\n{e['geom'].area:,.0f} SF", 5.6, halo=True, z=20)
    draw.constraints(S, X)
    draw.parcel_labels(S, X, X["parcel_sp"], X["frame"])
    c = L["parcel"].representative_point()
    S.txt(c.x, c.y - 40, f"SUBJECT PARCEL\n{X['facts']['parcel_ac']:.2f} ACRES (GIS)", 9, weight="bold", halo=True, z=30)
    S.north_arrow(); S.scale_bar()
    _legend(S, 0.015, 0.98, [("line", dict(color=INK, lw=2.0), "Parcel boundary (GIS, not surveyed)"), ("line", dict(color=GREY, lw=0.9, ls=(0, (7, 2.5))), "Existing index contour (10 ft)"),
                              ("line", dict(color=GREY, lw=0.45, ls=(0, (4, 3))), "Existing contour (2 ft)"), ("line", dict(color=INK, lw=0.8, ls=(0, (9, 4, 2, 4))), "Building setback line"),
                              ("line", dict(color=INK, lw=1.0, ls=(0, (14, 4, 2, 4, 2, 4))), "Wetland (NWI)"), ("line", dict(color=BLUE, lw=0.9, ls=(0, (8, 3))), "FEMA special flood hazard area"),
                              ("poly", dict(fc="#e6e8eb", ec=INK, lw=0.8), "Existing building")])
    f = X["facts"]
    sb = X["L"]["setbacks_ft"]
    _apply_notes(S, [
        "Boundary shown is from county parcel GIS. It is not a boundary survey and carries no legal description.",
        "Topography is 3DEP lidar-derived, 2 ft interval, NAVD 88. Verify with a field topographic survey.",
        "Building footprints, wetlands (NWI) and flood zones (FEMA) are from public datasets and are approximate.",
        "Existing utilities are not shown. Locate by the 811 one-call and a subsurface utility engineering survey before final design.",
        f"Zoning: {meta.get('zone_line') or 'verify'}. Setbacks used: front {sb.get('front')} ft, side {sb.get('side')} ft, rear {sb.get('rear')} ft (confirm against the ordinance)."],
        0.015, 0.74, title="EXISTING CONDITIONS NOTES", width=7.2)
    X0, Y0 = corner(S, 0.72, 0.98)
    table(S, X0, Y0, [1.9, 1.7], [["SITE DATA", ""], ["Parcel area", f"{f['parcel_ac']:.2f} ac"], ["Existing buildings", f"{f['ex_bldgs']}"],
                                    ["Existing pavement", f"{f['ex_pave_sf']:,.0f} SF"], ["Wetland on parcel", "yes" if f["wet"] else "none mapped"],
                                    ["Flood zone on parcel", "SFHA" if f["flood"] else "none mapped"], ["Elevation range", f"{f['zmin']:.1f} – {f['zmax']:.1f} ft"]], size=6, row_h=0.19)
    _prelim_note(S)
    return S


# ======================================================================== C2 site development
def c2(meta, X):
    L = X["L"]
    S = _site(meta, X, "C2", "SITE DEVELOPMENT PLAN")
    draw.neighbor_buildings(S, X)
    draw.road_layers(S, X)
    draw.parcel_labels(S, X, X["parcel_sp"], X["frame"], dim=False)
    draw.constraints(S, X)
    draw.existing_features(S, X)
    draw.proposed_base(S, X)
    _bldg_labels(S, X)
    # dimensions: building edges, drive width, stall depth
    for b in L["bldgs"]:
        mrr = b.minimum_rotated_rectangle
        cs = list(mrr.exterior.coords)
        for a, c in zip(cs, cs[1:3]):
            d = math.hypot(c[0] - a[0], c[1] - a[1])
            ang = math.degrees(math.atan2(c[1] - a[1], c[0] - a[0]))
            if ang > 90 or ang < -90:
                ang += 180
            mx, my = (a[0] + c[0]) / 2, (a[1] + c[1]) / 2
            S.txt(mx, my + 7 * S.scale / 30, f"{d:.0f}'", 6, rot=ang, halo=True, z=30)
    if L["stalls"]:
        s0 = L["stalls"][0]
        x0, y0, x1, y1 = s0.bounds
        S.leader((x0, y0), (x0 - 40, y0 - 35), f"TYP. STALL {min(x1-x0, y1-y0):.0f}'×{max(x1-x0, y1-y0):.0f}'", 6)
    if L.get("drive") is not None:
        c = L["drive"].representative_point()
        S.leader((c.x, c.y), (c.x - 60, c.y + 38), f"ACCESS DRIVE — {X['facts']['drive_w']:.0f}' PAVED", 6)
    for st in X["G"]["spine"].coords[:1]:
        S.txt(st[0] + 10, st[1] + 14, "CONNECTION TO EXISTING PAVEMENT / ROAD", 5.6, ha="left", halo=True, z=30)
    S.north_arrow(); S.scale_bar()
    f = X["facts"]
    X0, Y0 = corner(S, 0.015, 0.97)
    table(S, X0, Y0, [2.1, 1.7], [["SITE DATA", ""], ["Parcel area", f"{f['parcel_ac']:.2f} ac"], ["Proposed building", f"{f['bldg_sf']:,.0f} SF"],
                                    ["Parking provided", f"{f['stalls']} ({f['ada']} ADA)"], ["Parking ratio", f"{f['ratio']:.1f} / 1,000 SF"],
                                    ["New impervious", f"{f['imp_sf']:,.0f} SF"], ["Reused pavement", f"{f['reuse_sf']:,.0f} SF"],
                                    ["Open space / landscape", f"{f['green_sf']:,.0f} SF"], ["Stormwater pond", f"{f['pond_ac']:.2f} ac top"]], size=6, row_h=0.2)
    _legend(S, 0.015, 0.74, [("line", dict(color=INK, lw=1.6), "Proposed building"), ("line", dict(color=INK, lw=1.2), "Proposed curb / edge of pavement"),
                              ("line", dict(color=INK, lw=0.8, ls=(0, (9, 4, 2, 4))), "Building setback line"), ("line", dict(color=INK, lw=2.0), "Property line")])
    _apply_notes(S, ["All dimensions are to face of curb unless noted.", "Parking stalls per concept layout; verify count and ADA requirements with the local code.",
                     "Proposed pavement section, curb and sidewalk details to be provided in final plans.", "Site lighting, landscaping, utilities and drainage are shown on sheets C5 – C10."],
                 0.015, 0.50, title="SITE NOTES", width=5.6)
    _prelim_note(S)
    return S


# ======================================================================== C3 erosion control & demolition
def c3(meta, X):
    L, ER = X["L"], X["ER"]
    S = _site(meta, X, "C3", "EROSION CONTROL & DEMOLITION PLAN")
    draw.road_layers(S, X)
    draw.contours(S, X, "ex", labels=True)
    draw.parcel_labels(S, X, X["parcel_sp"], X["frame"], dim=False)
    draw.existing_features(S, X, demo_marks=True)
    for p in polys(ER["lod"]):
        S.line(p.exterior, color=RED, lw=1.2, ls=(0, (10, 3, 2, 3)), z=12)
    S.poly(L["dev"].buffer(0.1), ec=GREY, lw=0.5, ls=(0, (4, 3)), z=8)
    for b in L["bldgs"]:
        S.poly(b, ec=GREY, lw=0.8, ls=(0, (4, 3)), z=8)
    for ln in ER["silt"]:
        S.line(ln, color=BROWN, lw=1.4, z=13)
    for ln in ER["silt"][::6]:
        c = ln.interpolate(0.5, normalized=True)
        S.txt(c.x, c.y, "SF", 5.4, color=BROWN, halo=True, z=30)
    S.poly(ER["exit"], fc=(0.5, 0.4, 0.2, 0.25), ec=BROWN, lw=1.0, z=12)
    c = ER["exit"].centroid
    S.leader((c.x, c.y), (c.x + 30, c.y - 40), "STABILIZED CONSTRUCTION ENTRANCE\n(50' MIN. 3\" – 6\" STONE)", 5.8)
    S.poly(ER["stockpile"], fc=(0.6, 0.6, 0.6, 0.2), ec=INK, lw=0.8, ls=(0, (6, 3)), z=12)
    c = ER["stockpile"].centroid
    S.leader((c.x, c.y), (c.x + 25, c.y + 30), "STOCKPILE / STAGING (SILT FENCE AROUND)", 5.8)
    S.poly(ER["washout"], fc=(0.3, 0.6, 0.9, 0.25), ec=BLUE, lw=0.8, z=12)
    c = ER["washout"].centroid
    S.leader((c.x, c.y), (c.x + 25, c.y - 30), "CONCRETE WASHOUT", 5.8)
    for (x, y) in ER["inlet_prot"]:
        S.ax.plot([x], [y], marker="s", ms=6, mfc="none", mec=BROWN, mew=1.4, zorder=30)
    if ER["inlet_prot"]:
        x, y = ER["inlet_prot"][0]
        S.leader((x, y), (x + 20, y + 25), "INLET PROTECTION (TYP.)", 5.8)
    c = ER["lod"].representative_point()
    S.txt(c.x, c.y + 20, f"LIMITS OF DISTURBANCE\n{ER['lod_ac']:.2f} AC", 7, color=RED, weight="bold", halo=True, z=30)
    S.north_arrow(); S.scale_bar()
    _legend(S, 0.015, 0.97, [("line", dict(color=RED, lw=1.2, ls=(0, (10, 3, 2, 3))), "Limits of disturbance"), ("line", dict(color=BROWN, lw=1.6), "Silt fence / filter sock"),
                              ("poly", dict(fc=(0.5, 0.4, 0.2, 0.25), ec=BROWN, lw=0.8), "Construction entrance"), ("mark", dict(marker="s", mfc="none", mec=BROWN, mew=1.4, ms=6), "Inlet protection"),
                              ("poly", dict(fc=(1, .2, .2, .08), ec=RED, lw=0.8), "Demolition"), ("line", dict(color=GREY, lw=0.9, ls=(0, (7, 2.5))), "Existing contour")])
    _apply_notes(S, [
        "Install perimeter sediment control and the construction entrance before any clearing or grading.",
        "Silt fence or filter sock to be maintained until the site is stabilized; inspect weekly and after each ½\" rain.",
        "Protect storm inlets and the pond outfall with approved inlet protection as they are built.",
        "Stabilize all disturbed areas within 7 days of final grade (seed and mulch, sod or pavement).",
        "Pond to serve as a sediment basin during construction; clean out before final stabilization.",
        ("Disturbance is ≥ 1 acre: an NPDES Construction General Permit / NOI and a SWPPP are required." if ER["npdes"] else "Disturbance is under 1 acre; confirm local erosion control permit requirements."),
        "Demolition: remove existing structures, slabs and utilities as marked; cap utilities at the main. Haul and dispose per local rules."],
        0.015, 0.70, title="EROSION CONTROL & DEMOLITION NOTES", width=7.4)
    S.call811()
    _prelim_note(S)
    return S


# ======================================================================== C4 grading
def c4(meta, X):
    L, G = X["L"], X["G"]
    S = _site(meta, X, "C4", "GRADING & PAVING PLAN")
    draw.road_layers(S, X)
    draw.contours(S, X, "ex", labels=False)
    draw.contours(S, X, "pr")
    draw.parcel_labels(S, X, X["parcel_sp"], X["frame"], dim=False)
    draw.proposed_base(S, X, fill=False)
    for p in polys(X["ER"]["lod"]):
        S.line(p.exterior, color=RED, lw=0.8, ls=(0, (10, 3, 2, 3)), z=12)
    _bldg_labels(S, X, with_ffe=True)
    draw.stationing(S, X)
    # spot elevations along the pavement edge
    grid, z = X["grid"], X["z_prop"]
    seen = []
    for p in polys(L["pave"]):
        ring = p.exterior
        for d in np.arange(0, ring.length, 55.0):
            pt = ring.interpolate(d)
            i, j = grid.ij(pt.x, pt.y)
            if i is None or not (0 <= i < z.shape[0] and 0 <= j < z.shape[1]) or np.isnan(z[i, j]):
                continue
            if any(math.hypot(pt.x - a, pt.y - b) < 40 for a, b in seen):
                continue
            seen.append((pt.x, pt.y))
            S.ax.plot([pt.x], [pt.y], marker="+", ms=4, color=INK, zorder=30)
            S.txt(pt.x + 4, pt.y + 4, f"{z[i, j]:.2f}", 5.2, ha="left", halo=True, z=31)
    # flow arrows from the pavement
    pond = X["D"]["pond"]
    c = pond["poly"].centroid
    S.txt(c.x, c.y, f"DETENTION POND\nTOP {pond['top_el']:.1f}  BOT {pond['bottom_el']:.1f}\nWEIR {pond['weir_crest']:.1f}\n3:1 SIDE SLOPES", 5.8, weight="bold", halo=True, z=30)
    for s in X["net"]["structs"]:
        if s["kind"] == "CB":
            S.ax.plot([s["xy"][0]], [s["xy"][1]], marker="s", ms=5, mfc="white", mec=INK, zorder=32)
    S.north_arrow(); S.scale_bar()
    ew = X["earthwork"]
    X0, Y0 = corner(S, 0.015, 0.97)
    table(S, X0, Y0, [2.0, 1.4], [["EARTHWORK (PRELIM.)", ""], ["Cut", f"{ew['cut_cy']:,.0f} CY"], ["Fill", f"{ew['fill_cy']:,.0f} CY"],
                                    ["Net", f"{ew['net_cy']:+,.0f} CY ({'import' if ew['net_cy'] > 0 else 'export'})"], ["Max cut / fill", f"{ew['max_cut_ft']:.1f} / {ew['max_fill_ft']:.1f} ft"],
                                    ["Disturbed", f"{ew['disturbed_ac']:.2f} ac"]], size=6, row_h=0.2)
    _legend(S, 0.015, 0.77, [("line", dict(color=INK, lw=0.95), "Proposed index contour (10 ft)"), ("line", dict(color=INK, lw=0.45), "Proposed contour (2 ft)"),
                              ("line", dict(color=GREY, lw=0.45, ls=(0, (4, 3))), "Existing contour"), ("line", dict(color=RED, lw=0.8, ls=(0, (10, 3, 2, 3))), "Limits of disturbance"),
                              ("mark", dict(marker="+", color=INK, ms=5), "Spot elevation")])
    _apply_notes(S, [f"Pavement: max {meta['facts_max_slope']}% along the drive; cross-slope 1.5% crown off the drive centerline; ADA stalls and routes ≤ 2%.",
                     "Slab finished floor set 0.5 ft above the highest adjacent pavement.", "Cut and fill slopes 3H:1V maximum; stabilize with seed and mulch or sod.",
                     "Earthwork quantities are from a 5 ft grid surface and are not adjusted for shrink, swell, topsoil stripping or pavement sections.",
                     "Verify vertical datum NAVD 88 with a field survey before use."],
                 0.015, 0.57, title="GRADING NOTES", width=7.0)
    _prelim_note(S)
    return S


# ======================================================================== C5 drainage
def c5(meta, X):
    L, D, net = X["L"], X["D"], X["net"]
    S = _site(meta, X, "C5", "DRAINAGE PLAN & CALCULATIONS")
    draw.road_layers(S, X)
    draw.contours(S, X, "pr", labels=False)
    draw.proposed_base(S, X, fill=False)
    pal = ["#d94f4f", "#3b82c4", "#4aa564", "#c58a1d", "#8a5fc7"]
    grid = X["grid"]
    for sc, cells in zip(D["subcatchments"], [D["cells"][sc["inlet"]] if sc["inlet"] is not None else None for sc in D["subcatchments"]]):
        pass
    col = {}
    for n, sc in enumerate(D["subcatchments"]):
        col[sc["id"]] = pal[n % len(pal)]
    for n, sc in enumerate(D["subcatchments"]):
        if sc["inlet"] is not None and sc["inlet"] < len(D["cells"]):
            cs = D["cells"][sc["inlet"]]
            if len(cs):
                m_ = np.zeros((grid.ny, grid.nx), bool)
                for i, j in cs:
                    m_[i, j] = True
                g = grid.cell_poly(m_)
                S.poly(g, fc=col[sc["id"]] + "22", ec=col[sc["id"]], lw=0.9, ls=(0, (7, 3)), z=4)
                c = g.representative_point()
                S.txt(c.x, c.y + 20, f"{sc['id']}\n{sc['area_sf']/43560:.2f} AC  CN {sc['cn']:.0f}\nTc {sc['tc']:.0f} min", 6, color=col[sc["id"]], weight="bold", halo=True, z=30)
    pond = D["pond"]
    S.poly(pond["poly"], fc="#cfe4f5", ec=BLUE, lw=1.2, z=9)
    c = pond["poly"].centroid
    S.txt(c.x, c.y, f"DETENTION POND\n{pond['storage_cf']:,.0f} CF\nTOP {pond['top_el']:.2f}\nBOT {pond['bottom_el']:.2f}", 6, weight="bold", halo=True, z=30)
    sc3 = [s for s in D["subcatchments"] if s["inlet"] is None]
    if sc3:
        S.txt(c.x, c.y - 50, f"{sc3[0]['id']} (to pond): {sc3[0]['area_sf']/43560:.2f} AC  CN {sc3[0]['cn']:.0f}", 5.8, color=col[sc3[0]["id"]], weight="bold", halo=True, z=30)
    byid = {s["id"]: s for s in net["structs"]}
    for p in net["pipes"]:
        S.line(p["line"], color=GREEN, lw=1.8, z=14)
        c = p["line"].interpolate(0.5, normalized=True)
        a = p["line"]
        ang = math.degrees(math.atan2(a.coords[-1][1] - a.coords[0][1], a.coords[-1][0] - a.coords[0][0]))
        if ang > 90 or ang < -90:
            ang += 180
        S.txt(c.x, c.y + 6 * S.scale / 30, f"{p['len']:.0f} LF – {p['dia']}\" {p['mat']} @ {p['slope']*100:.2f}%", 5.6, rot=ang, color=GREEN, halo=True, z=30)
    for s in net["structs"]:
        x, y = s["xy"]
        if s["kind"] == "CB":
            S.ax.plot([x], [y], marker="s", ms=7, mfc="white", mec=INK, mew=1.2, zorder=32)
            S.txt(x + 6, y - 10, f"{s['id']}\nRIM {s['rim']:.2f}\nINV OUT {s['inv_out']:.2f}", 5.4, ha="left", halo=True, z=33)
        else:
            S.ax.plot([x], [y], marker="^", ms=8, mfc="white", mec=INK, mew=1.2, zorder=32)
            S.txt(x - 6, y + 12, f"{s['id']}\nINV IN {s['inv_in']:.2f}", 5.4, ha="right", halo=True, z=33)
    S.north_arrow(); S.scale_bar()
    # pre/post table
    X0, Y0 = corner(S, 0.015, 0.97)
    rows = [["STORM", "PRE (cfs)", "POST (cfs)", "OUT (cfs)", "STAGE (ft)"]]
    for r in pond["table"]:
        rows.append([f"{r['storm']}-yr", f"{r['pre_cfs']:.2f}", f"{r['post_cfs']:.2f}", f"{r['out_cfs']:.2f}", f"{r['stage']:.2f}"])
    y = table(S, X0, Y0, [0.8, 0.8, 0.9, 0.9, 0.9], rows, title=None, size=6, row_h=0.2)
    S.txt(X0, Y0 + 0.2 * S.scale, "PRE / POST PEAK RUNOFF & POND ROUTING", 7.4, ha="left", weight="bold", z=61)
    info = [["POND DATA", ""], ["Top / bottom", f"{pond['top_el']:.2f} / {pond['bottom_el']:.2f}"], ["Storage at top", f"{pond['storage_cf']:,.0f} CF"],
            ["Orifice", f"{pond['orifice_in']:.1f}\" dia."], ["Weir crest / length", f"{pond['weir_crest']:.2f} / {pond['weir_len_ft']:.0f} ft"],
            ["CN pre / post", f"{D['cn_pre']:.0f} / {D['cn_post']:.0f}"], ["Tc pre / post", f"{D['tc_pre']:.1f} / {D['tc_post']:.1f} min"], ["Soil group", D["hsg"]]]
    y = table(S, X0, y - 0.3 * S.scale, [1.8, 2.1], info, size=6, row_h=0.2)
    prow = [["PIPE", "FROM → TO", "LEN", "DIA", "SLOPE", "Q10", "CAP", "INV UP", "INV DN"]]
    for i, p in enumerate(net["pipes"], 1):
        prow.append([f"P-{i}", f"{p['from']} → {p['to']}", f"{p['len']:.0f}'", f"{p['dia']}\"", f"{p['slope']*100:.2f}%", f"{p['q10']:.1f}", f"{p['cap']:.1f}", f"{p['inv_up']:.2f}", f"{p['inv_dn']:.2f}"])
    table(S, X0, y - 0.3 * S.scale, [0.5, 1.4, 0.5, 0.45, 0.6, 0.5, 0.5, 0.65, 0.65], prow, size=5.6, row_h=0.2)
    _legend(S, 0.015, 0.40, [("line", dict(color=GREEN, lw=1.8), "Storm sewer"), ("mark", dict(marker="s", mfc="white", mec=INK, mew=1.2, ms=7), "Catch basin"),
                              ("mark", dict(marker="^", mfc="white", mec=INK, mew=1.2, ms=8), "Flared end section"), ("poly", dict(fc="#cfe4f5", ec=BLUE, lw=0.8), "Detention pond"),
                              ("line", dict(color="#d94f4f", lw=0.9, ls=(0, (7, 3))), "Subcatchment")])
    notes = ["Hydrology: SCS TR-55 with SCS 24-hr Type " + X["stype"] + " rainfall; " + X["rain"]["source"] + ".",
             "Pond routed by modified Puls through a single orifice and rectangular emergency weir; post-development peaks are held at or below pre-development for the 2- through 50-year storms.",
             "Storm sewer sized by Manning's equation for the 10-year storm, min. 2.5 fps, min. 2 ft cover (to be verified with final structure and pipe details).",
             "Pipes are HDPE unless noted; CB rims and inverts are preliminary."] + list(D["warnings"]) + list(net["warnings"])
    _apply_notes(S, notes, 0.015, 0.31, title="DRAINAGE NOTES", width=8.0)
    _prelim_note(S)
    return S


# ======================================================================== C6 profiles
def _profile(S, x0, y0, w, h, title, st, series, ylab="ELEV (ft)", vex=10, marks=(), xlab="STATION (ft)"):
    """Profile panel in sheet-inch coordinates (scale 1). series: [(station array, elev array, kwargs)]."""
    allz = np.concatenate([np.asarray(z) for _, z, _ in series])
    zmin, zmax = float(np.nanmin(allz)) - 1.5, float(np.nanmax(allz)) + 1.5
    smin, smax = min(float(np.min(s)) for s, _, _ in series), max(float(np.max(s)) for s, _, _ in series)
    fx = lambda s: x0 + (s - smin) / max(smax - smin, 1e-6) * w
    fz = lambda z: y0 + (z - zmin) / max(zmax - zmin, 1e-6) * h
    S.ax.add_patch(draw.plt_rect(x0, y0, w, h, "white"))
    S.ax.plot([x0, x0 + w, x0 + w, x0, x0], [y0, y0, y0 + h, y0 + h, y0], color=INK, lw=0.8, zorder=62)
    for z in np.arange(math.ceil(zmin), zmax, 1.0 if zmax - zmin < 14 else 2.0):
        S.ax.plot([x0, x0 + w], [fz(z)] * 2, color=LGREY, lw=0.3, zorder=61)
        S.txt(x0 - 0.08, fz(z), f"{z:.0f}", 5.4, ha="right", z=63)
    for s in np.arange(math.ceil(smin / 50) * 50, smax, 50):
        S.ax.plot([fx(s)] * 2, [y0, y0 + h], color=LGREY, lw=0.3, zorder=61)
        S.txt(fx(s), y0 - 0.12, f"{int(s // 100)}+{int(s % 100):02d}", 5.4, z=63)
    for s_, z_, kw in series:
        S.ax.plot([fx(a) for a in s_], [fz(b) for b in z_], zorder=64, **kw)
    S.txt(x0, y0 + h + 0.18, title, 8.5, ha="left", weight="bold", z=63)
    S.txt(x0 + w, y0 + h + 0.18, f"HORIZ 1\" = {S.hscale:g}'   VERT 1\" = {S.hscale/vex:g}'", 5.8, ha="right", z=63)
    return fx, fz


def c6(meta, X):
    G = X["G"]
    S = Sheet(meta, "C6", "PROFILES", (0, 0), scale=1, scale_text="AS NOTED")
    W, H = S.W_in, S.H_in
    S.ax.set_xlim(0, W); S.ax.set_ylim(0, H); S.view = (0, 0, W, H)
    S.hscale = 40
    st, pr, ex = G["stations"], G["profile"], G["ex_profile"]
    y0 = H - 6.2
    fx, fz = _profile(S, 1.3, y0, W * 0.62, 4.6, "ACCESS DRIVE PROFILE (CENTERLINE)", st, [(st, ex, dict(color=GREY, lw=1.0, ls=(0, (6, 3)))), (st, pr, dict(color=INK, lw=1.6))])
    S.hscale = 40
    for (s, z) in G["pvi"]:
        S.ax.plot([fx(s)], [fz(z)], marker="o", ms=3.5, mfc="white", mec=INK, zorder=66)
    S.txt(fx(st[0]) + 1.5, fz(ex[0]) - 0.2, "EXISTING GROUND", 5.4, color=GREY, ha="left", z=65)
    S.txt(fx(st[len(st)//2]), fz(pr[len(pr)//2]) + 0.3, "PROPOSED GRADE", 5.4, z=65)
    # PVI table
    rows = [["PVI", "STATION", "ELEV", "GRADE IN", "GRADE OUT", "VC LEN", "K"]]
    for n, c in enumerate(G["curves"], 1):
        rows.append([f"PVI {n}", f"{int(c['pvi_sta']//100)}+{c['pvi_sta']%100:05.2f}", f"{c['pvi_elev']:.2f}", f"{c['g1']:.2f}%", f"{c['g2']:.2f}%", f"{c['L']:.0f}'", f"{c['K']:.1f}"])
    table(S, W * 0.62 + 2.0, y0 + 4.6, [0.6, 0.9, 0.65, 0.75, 0.8, 0.6, 0.45], rows, size=5.6, row_h=0.22)
    S.txt(W * 0.62 + 2.0, y0 + 4.6 + 0.2, "VERTICAL CURVE DATA", 8, ha="left", weight="bold", z=61)
    # storm profile
    net = X["net"]
    byid = {s["id"]: s for s in net["structs"]}
    y1 = y0 - 5.0
    if net["pipes"]:
        p = net["pipes"][0]
        a, b = byid[p["from"]], byid[p["to"]]
        s_ = np.array([0.0, p["len"]])
        S.hscale = 20
        fx2, fz2 = _profile(S, 1.3, y1, 6.0, 3.6, f"STORM SEWER {p['from']} – {p['to']}", s_,
                            [(s_, [a["rim"], b["rim"]], dict(color=INK, lw=1.0)), (s_, [p["inv_up"], p["inv_dn"]], dict(color=GREEN, lw=1.8)),
                             (s_, [p["inv_up"] + p["dia"] / 12, p["inv_dn"] + p["dia"] / 12], dict(color=GREEN, lw=1.0))], vex=5)
        S.txt(fx2(p["len"] / 2), fz2(a["rim"]) + 0.3, f"{p['len']:.0f} LF – {p['dia']}\" {p['mat']} @ {p['slope']*100:.2f}%", 6, z=66)
        S.txt(fx2(0), fz2(a["rim"]) + 0.2, f"{a['id']}  RIM {a['rim']:.2f}", 5.4, ha="left", z=66)
        S.txt(fx2(p["len"]), fz2(b["rim"]) + 0.2, f"{b['id']}  RIM {b['rim']:.2f}", 5.4, ha="right", z=66)
    # sewer profile
    U = X["U"]
    smh = U["smh"]
    if len(smh) >= 2:
        s_ = np.array([m["sta"] for m in smh], float)
        S.hscale = 100
        fx3, fz3 = _profile(S, 8.6, y1, W - 8.6 - 1.0, 3.6, "SANITARY SEWER PROFILE (ASSUMED CONNECTION DEPTH)", s_,
                            [(s_, [m["rim"] for m in smh], dict(color=INK, lw=1.0)), (s_, [m["inv"] for m in smh], dict(color=BROWN, lw=1.8))], vex=10)
        for m in smh:
            S.txt(fx3(m["sta"]), fz3(m["rim"]) + 0.25, f"{m['id']}\nRIM {m['rim']:.2f}\nINV {m['inv']:.2f}", 5.0, z=66)
        S.txt(fx3(s_[len(s_)//2]), fz3(smh[len(smh)//2]["inv"]) - 0.35, f"8\" PVC SDR 35 @ {U['sewer_slope_pct']:.2f}%", 5.8, color=BROWN, z=66)
    S.txt(W * 0.5, 0.35, PRELIM, 6.0, color=GREY, z=60)
    return S


# ======================================================================== C7 utilities
def c7(meta, X):
    L, U = X["L"], X["U"]
    ext = [U["water_main"].buffer(10), U["sewer_main"].buffer(10)]
    S = _site(meta, X, "C7", "UTILITY PLAN", extra=ext, margin=40)
    draw.neighbor_buildings(S, X)
    draw.road_layers(S, X)
    draw.parcel_labels(S, X, X["parcel_sp"], X["frame"], dim=False)
    draw.proposed_base(S, X, fill=True)
    _bldg_labels(S, X)
    vb = box(*S.view)
    def L_(g, **kw):
        c = g.intersection(vb)
        if not c.is_empty:
            S.line(c, **kw)
    L_(U["water_main"], color=BLUE, lw=1.8, z=14)
    for g in U["water_lat"]:
        L_(g, color=BLUE, lw=1.2, z=14)
    L_(U["sewer_main"], color=BROWN, lw=1.8, ls=(0, (9, 3)), z=14)
    for g in U["sewer_lat"]:
        L_(g, color=BROWN, lw=1.2, ls=(0, (9, 3)), z=14)
    L_(U["elec"], color=RED, lw=1.3, ls=(0, (2, 2)), z=14)
    for g in U["elec_lat"]:
        L_(g, color=RED, lw=1.0, ls=(0, (2, 2)), z=14)
    L_(U["gas"], color="#e08a00", lw=1.3, ls=(0, (6, 2, 1, 2)), z=14)
    for g in U["gas_lat"]:
        L_(g, color="#e08a00", lw=1.0, ls=(0, (6, 2, 1, 2)), z=14)
    for x, y in U["hydrants"]:
        if vb.contains(Point(x, y)):
            S.ax.plot([x], [y], marker="*", ms=9, mfc="white", mec=BLUE, mew=1.2, zorder=33)
            S.txt(x + 5, y + 8, "FH", 5.4, color=BLUE, ha="left", halo=True, z=34)
    for x, y in U["valves"]:
        if vb.contains(Point(x, y)):
            S.ax.plot([x], [y], marker="D", ms=5, mfc=BLUE, mec=BLUE, zorder=33)
            S.txt(x + 5, y - 8, "GV", 5.4, color=BLUE, ha="left", halo=True, z=34)
    for m in U["smh"]:
        x, y = m["xy"]
        if vb.contains(Point(x, y)):
            S.ax.plot([x], [y], marker="o", ms=7, mfc="white", mec=BROWN, mew=1.4, zorder=33)
            S.txt(x + 6, y - 14, f"{m['id']}\nRIM {m['rim']:.2f}\nINV {m['inv']:.2f}", 5.2, ha="left", color=BROWN, halo=True, z=34)
    for x, y in U["xfmr"]:
        S.poly(box(x - 3, y - 3, x + 3, y + 3), fc=RED, ec=RED, z=33)
        S.txt(x, y + 9, "PAD XFMR", 5.2, color=RED, halo=True, z=34)
    S.leader(U["tap"], (U["tap"][0] - 60, U["tap"][1] + 40), "CONNECT TO EXISTING MAINS (LOCATION / DEPTH ASSUMED — VERIFY)", 5.8, ha="right")
    for sv in U["svc"]:
        S.txt(sv["w_xy"][0], sv["w_xy"][1] + 12, f"WATER SVC {X['facts']['w_in']}\"", 5.2, color=BLUE, halo=True, z=34)
    S.north_arrow(); S.scale_bar()
    _legend(S, 0.015, 0.97, [("line", dict(color=BLUE, lw=1.8), "Water main (PVC C900)"), ("line", dict(color=BROWN, lw=1.8, ls=(0, (9, 3))), "Sanitary sewer (PVC SDR 35)"),
                              ("line", dict(color=RED, lw=1.3, ls=(0, (2, 2))), "Underground electric"), ("line", dict(color="#e08a00", lw=1.3, ls=(0, (6, 2, 1, 2))), "Natural gas"),
                              ("mark", dict(marker="*", mfc="white", mec=BLUE, mew=1.2, ms=9), "Fire hydrant"), ("mark", dict(marker="D", mfc=BLUE, mec=BLUE, ms=5), "Gate valve"),
                              ("mark", dict(marker="o", mfc="white", mec=BROWN, mew=1.4, ms=7), "Sanitary manhole")], w=3.2)
    X0, Y0 = corner(S, 0.015, 0.68)
    rows = [["LATERAL", "LEN", "INV AT MAIN", "INV AT BLDG", "OK"]] + [[f"BLDG {r['bldg']}", f"{r['len']}'", f"{r['inv_main']:.2f}", f"{r['inv_bldg']:.2f}", "yes" if r["ok"] else "CHECK"] for r in U["sewer_lat_rows"]]
    table(S, X0, Y0, [0.8, 0.5, 1.0, 1.0, 0.6], rows, size=5.6, row_h=0.2, title=None)
    S.txt(X0, Y0 + 0.2 * S.scale, "SEWER LATERAL SCHEDULE", 7.4, ha="left", weight="bold", z=61)
    X1, Y1 = corner(S, 0.015, 0.56)
    table(S, X1, Y1, [2.1, 1.3], [["DESIGN DEMAND (PLANNING)", ""], ["Sewer", f"{U['sewer_gpd']:,.0f} gpd"], ["Water", f"{U['water_gpd']:,.0f} gpd"], ["Service size", f"{X['facts']['w_in']}\" water / 6\" sewer"]], size=6, row_h=0.2)
    _apply_notes(S, ["Public water and sanitary sewer are ASSUMED to be available in the adjacent right-of-way. Obtain a will-serve letter and verify location, size, depth and capacity.",
                     "Existing main invert is assumed at 8 ft below the road surface; sewer runs at the minimum slope shown. Deep structures or lift-station needs are flagged below.",
                     "Water main 8\" minimum for fire flow; hydrants spaced per the fire plan (C8). Provide thrust restraint, backflow prevention and meter per utility standards.",
                     "Dry utilities (electric, gas, communications) are schematic; coordinate routing, easements and transformer location with the providers.",
                     "Maintain 10 ft horizontal / 18 in vertical separation between water and sewer."] + list(U["warnings"]) + list(U["notes"]),
                 0.015, 0.46, title="UTILITY NOTES", width=7.6)
    S.call811()
    _prelim_note(S)
    return S


# ======================================================================== C8 fire protection
def c8(meta, X):
    L, F, U = X["L"], X["FIRE"], X["U"]
    S = _site(meta, X, "C8", "FIRE PROTECTION PLAN", extra=[Point(*U["tap"]).buffer(1)] if False else [])
    draw.road_layers(S, X)
    draw.parcel_labels(S, X, X["parcel_sp"], X["frame"], dim=False)
    draw.proposed_base(S, X)
    _bldg_labels(S, X)
    te = F["truck_envelope"]
    for g in (te if isinstance(te, (tuple, list)) else [te]):
        if g is not None and not g.is_empty:
            S.line(g, color=RED, lw=1.4, ls=(0, (8, 3)), z=15)
    vb = box(*S.view)
    for x, y in F["hydrants"]:
        if vb.contains(Point(x, y)):
            S.ax.plot([x], [y], marker="*", ms=9, mfc="white", mec=BLUE, mew=1.2, zorder=33)
            S.poly(Point(x, y).buffer(250), ec=BLUE, lw=0.6, ls=(0, (3, 3)), z=2)
    for x, y in F["fdc"]:
        S.ax.plot([x], [y], marker="s", ms=7, mfc=RED, mec=RED, zorder=33)
        S.leader((x, y), (x + 25, y - 30), "FIRE DEPT. CONNECTION", 5.8)
    S.north_arrow(); S.scale_bar()
    X0, Y0 = corner(S, 0.015, 0.97)
    table(S, X0, Y0, [2.3, 1.5], [["FIRE ACCESS SUMMARY", ""], ["Drive ≥ 22 ft clear", f"{F['lane_ok_pct']}% of length"], ["Min. centerline radius", f"{F['min_radius_ft']} ft"],
                                    ["Dead-end length", f"{F['dead_end_ft']} ft"], ["Hydrants in plan", f"{len(F['hydrants'])}"], ["Hydrant coverage (dashed)", "250 ft radius"]], size=6, row_h=0.2)
    _legend(S, 0.015, 0.78, [("line", dict(color=RED, lw=1.4, ls=(0, (8, 3))), "Fire apparatus path (turning envelope)"), ("mark", dict(marker="*", mfc="white", mec=BLUE, mew=1.2, ms=9), "Fire hydrant"),
                              ("mark", dict(marker="s", mfc=RED, mec=RED, ms=7), "Fire dept. connection"), ("line", dict(color=BLUE, lw=0.6, ls=(0, (3, 3))), "250 ft hydrant reach")], w=3.1)
    _apply_notes(S, ["Fire apparatus access roads: 20 ft minimum, 22 ft or more shown here, 13 ft 6 in vertical clearance, designed for 75,000 lb loads (IFC 503).",
                     "Hose-lay reach: all points of the building within 150 ft of the access road where practicable (IFC 503.1.1); confirm with the fire marshal.",
                     "Hydrant spacing and fire flow to be confirmed with the water provider and fire department; automatic sprinklers and FDC location per NFPA 13.",
                     "Dead-end access roads over 150 ft require an approved turnaround."] + list(F["warnings"]),
                 0.015, 0.60, title="FIRE PROTECTION NOTES", width=7.4)
    _prelim_note(S)
    return S


# ======================================================================== C9 landscape
def c9(meta, X):
    L, LS = X["L"], X["LS"]
    S = _site(meta, X, "C9", "LANDSCAPE PLAN")
    draw.road_layers(S, X)
    draw.parcel_labels(S, X, X["parcel_sp"], X["frame"], dim=False)
    if L.get("grass") is not None:
        S.poly(L["grass"], fc="#e7f1e2", ec="none", z=2)
    draw.proposed_base(S, X, fill=True)
    _bldg_labels(S, X)
    cols = {"shade": ("#2f7d32", 1.0), "orn": ("#b2478b", 0.55), "ever": ("#1f5f3a", 0.8)}
    for (x, y, r, kind) in LS["trees"]:
        c, k = cols.get(kind, ("#2f7d32", 1))
        S.poly(Point(x, y).buffer(r), fc=c + "33", ec=c, lw=0.8, z=20)
        S.ax.plot([x], [y], marker="+", ms=4, color=c, zorder=21)
        S.txt(x, y - r - 4, LS["species"][kind][0], 5.2, color=c, weight="bold", halo=True, z=30)
    S.north_arrow(); S.scale_bar()
    X0, Y0 = corner(S, 0.015, 0.97)
    rows = [["KEY", "BOTANICAL NAME", "COMMON NAME", "SIZE", "QTY"]]
    for kind, (code, bot, com, size) in LS["species"].items():
        n = LS["counts"].get(kind, 0)
        if n:
            rows.append([code, bot, com, size, str(n)])
    table(S, X0, Y0, [0.5, 1.7, 1.2, 0.9, 0.4], rows, size=5.8, row_h=0.22)
    S.txt(X0, Y0 + 0.2 * S.scale, "PLANT SCHEDULE", 8, ha="left", weight="bold", z=61)
    _apply_notes(S, [f"Landscaped / open area approx. {LS['landscape_sf']:,.0f} SF; turf (sod) to all disturbed areas not paved or planted.", "Species are Florida-/region-appropriate selections by latitude; confirm against the local landscape ordinance (tree credits, buffers, parking-lot islands).",
                     "Provide automatic irrigation to all planting beds and turf; protect existing trees where practicable.", "Pond side slopes to be seeded with a stormwater-tolerant mix."],
                 0.015, 0.74, title="LANDSCAPE NOTES", width=7.0)
    _prelim_note(S)
    return S


# ======================================================================== C10 lighting
def c10(meta, X):
    L, LT = X["L"], X["LT"]
    S = _site(meta, X, "C10", "SITE LIGHTING PLAN")
    draw.road_layers(S, X)
    draw.parcel_labels(S, X, X["parcel_sp"], X["frame"], dim=False)
    draw.proposed_base(S, X)
    _bldg_labels(S, X)
    g = X["grid"]
    E = LT["E"]
    try:
        import matplotlib.pyplot as plt
        cs = S.ax.contour(g.xs, g.ys, E, levels=[0.5, 1.0, 2.0, 5.0], colors=["#e2a300", "#d98300", "#c85a00", "#a32d00"], linewidths=0.7, zorder=18)
        S.ax.clabel(cs, fontsize=5, fmt="%.1f fc")
    except Exception:  # noqa: BLE001
        pass
    for (x, y) in LT["poles"]:
        S.ax.plot([x], [y], marker="o", ms=7, mfc="#ffd400", mec=INK, mew=1.0, zorder=33)
    S.north_arrow(); S.scale_bar()
    st = LT["stats"]
    X0, Y0 = corner(S, 0.015, 0.97)
    table(S, X0, Y0, [2.3, 1.5], [["PHOTOMETRIC SUMMARY (PARKING / DRIVE)", ""], ["Poles", f"{len(LT['poles'])} @ {LT['mount_ft']:.0f} ft mount"], ["Average", f"{st['avg']:.2f} fc"], ["Maximum", f"{st['max']:.1f} fc"],
                                    ["Minimum", f"{st['min']:.2f} fc"], ["Max : Min", f"{st['max_min']:.1f} : 1"], ["Avg : Min", f"{st['avg_min']:.1f} : 1"]], size=6, row_h=0.2)
    _legend(S, 0.015, 0.78, [("mark", dict(marker="o", mfc="#ffd400", mec=INK, mew=1.0, ms=7), "Pole-mounted LED area light"), ("line", dict(color="#d98300", lw=0.9), "Illuminance contour (foot-candles)")], w=3.2)
    _apply_notes(S, ["Photometrics are a simplified point-source (cosine³) estimate for a typical full-cutoff LED area luminaire; replace with a manufacturer IES calculation for permit submission.",
                     "Target: 1.0 fc average, 0.2 fc minimum over parking and drives (IES RP-20 / local code); confirm the local lighting ordinance (spill, curfew, color temperature).",
                     "Luminaires full cutoff, 3000 K–4000 K, mounted on 20 ft poles on concrete bases (details by others)."],
                 0.015, 0.68, title="LIGHTING NOTES", width=7.2)
    _prelim_note(S)
    return S


ALL = [("T1", "TITLE SHEET", t1), ("C1", "EXISTING CONDITIONS PLAN", c1), ("C2", "SITE DEVELOPMENT PLAN", c2), ("C3", "EROSION CONTROL & DEMOLITION PLAN", c3),
       ("C4", "GRADING & PAVING PLAN", c4), ("C5", "DRAINAGE PLAN & CALCULATIONS", c5), ("C6", "PROFILES", c6), ("C7", "UTILITY PLAN", c7),
       ("C8", "FIRE PROTECTION PLAN", c8), ("C9", "LANDSCAPE PLAN", c9), ("C10", "SITE LIGHTING PLAN", c10)]
