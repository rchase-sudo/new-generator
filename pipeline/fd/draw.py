"""Layer helpers shared by the sheets."""
import math

import numpy as np
from shapely.geometry import LineString, Point, box
from shapely.ops import substring, unary_union

from . import grading
from .base import bearing_text, lines, polys
from .sheetbase import BLUE, BROWN, CYAN, GREEN, GREY, INK, LGREY, RED


def parcel_labels(S, X, ctx_parcel_sp, fr, dim=True, name=True):
    L = X["L"]
    P = L["parcel"]
    S.poly(P, ec=INK, lw=2.2, z=12)
    if not dim:
        return
    # bearings / distances on every straight run longer than 40 ft (computed in state plane so bearings are true)
    ring = list(ctx_parcel_sp.exterior.coords)
    loc = list(P.exterior.coords)
    cc = P.centroid
    for (a, b), (la, lb) in zip(zip(ring, ring[1:]), zip(loc, loc[1:])):
        d = math.hypot(lb[0] - la[0], lb[1] - la[1])
        if d < 45:
            continue
        mx, my = (la[0] + lb[0]) / 2, (la[1] + lb[1]) / 2
        ang = math.degrees(math.atan2(lb[1] - la[1], lb[0] - la[0]))
        if ang > 90 or ang < -90:
            ang += 180
        nx, ny = -(lb[1] - la[1]) / d, (lb[0] - la[0]) / d
        if (mx + nx - cc.x) ** 2 + (my + ny - cc.y) ** 2 < (mx - cc.x) ** 2 + (my - cc.y) ** 2:
            nx, ny = -nx, -ny           # outside the parcel
        off = 7.0 * S.scale / 30.0
        bt = bearing_text(a, b)
        S.txt(mx + nx * off, my + ny * off, f"{bt}  {d:,.2f}'", 5.4, rot=ang, z=45, halo=True)


def road_layers(S, X, emph=False):
    L = X["L"]
    for r, cl, pav in L["roads"]:
        g = pav.intersection(box(*S.view)) if pav is not None and not pav.is_empty else None
        if g is not None and not g.is_empty:
            for p in polys(g):
                S.line(p.exterior, color=GREY, lw=0.9, z=3)
        c = cl.intersection(box(*S.view))
        if not c.is_empty:
            S.line(c, color=GREY, lw=0.6, ls=(0, (10, 4, 2, 4)), z=3)


def neighbor_buildings(S, X):
    for b in X["L"]["nbld"]:
        if b.intersects(box(*S.view)):
            S.poly(b, fc="#f1f2f4", ec=LGREY, lw=0.5, z=2)


def existing_features(S, X, demo_marks=False, label_ids=True):
    """Everything existing on the parcel that we know about: kept buildings and pavement are shown, removals are hatched red."""
    L = X["L"]
    labelled = {"keep_pave": False, "demo_pave": False}
    for e in L.get("existing") or []:
        if not e["geom"].intersects(box(*S.view)):
            continue
        if e.get("kind") == "pavement":
            if e["action"] == "demo":
                S.poly(e["geom"], fc=(1, 0.2, 0.2, 0.10), ec=RED, lw=0.9, ls=(0, (5, 3)), z=3, hatch="//")
                if not labelled["demo_pave"] or e["geom"].area > 20000:
                    q = e["geom"].representative_point()
                    S.txt(q.x, q.y, "EXISTING PAVEMENT\nTO BE REMOVED", 5.2, color=RED, halo=True, z=20)
                    labelled["demo_pave"] = True
            else:
                S.poly(e["geom"], fc="#e9ebee", ec=GREY, lw=0.9, z=3)
                if not labelled["keep_pave"] or e["geom"].area > 20000:
                    q = e["geom"].representative_point()
                    S.txt(q.x, q.y, "EXISTING PAVEMENT\n(TO REMAIN)", 5.2, color="#555", halo=True, z=20)
                    labelled["keep_pave"] = True
            continue
        if e["action"] == "keep":
            S.poly(e["geom"], fc="#e6e8eb", ec=GREY, lw=1.0, z=4)
            c = e["geom"].representative_point()
            S.txt(c.x, c.y, "EXISTING BUILDING\n(TO REMAIN)", 5.2, halo=True, z=20)
        else:
            S.poly(e["geom"], fc=(1, .2, .2, .08), ec=RED if demo_marks else GREY, lw=1.0, ls=(0, (5, 3)), z=4)
            c = e["geom"].representative_point()
            if demo_marks:
                x0, y0, x1, y1 = e["geom"].bounds
                S.line(LineString([(x0, y0), (x1, y1)]), color=RED, lw=0.8, z=9)
                S.line(LineString([(x0, y1), (x1, y0)]), color=RED, lw=0.8, z=9)
                S.txt(c.x, c.y, "EXISTING BUILDING\nTO BE DEMOLISHED", 5.2, color=RED, halo=True, z=20)
            else:
                S.txt(c.x, c.y, "EXISTING BUILDING\n(TO BE DEMOLISHED)", 5.2, halo=True, z=20)


def constraints(S, X, setback=True):
    L = X["L"]
    if setback and L.get("setback") is not None:
        S.line(L["setback"].boundary, color=INK, lw=0.8, ls=(0, (9, 4, 2, 4)), z=6)
        c = L["setback"].exterior.coords[0]
        S.txt(c[0], c[1], "BUILDING SETBACK LINE (TYP.)", 5.0, rot=0, halo=True, z=21)
    if L.get("wet") is not None and not L["wet"].is_empty:
        for p in polys(L["wet"].intersection(box(*S.view))):
            S.line(p.exterior, color=INK, lw=1.0, ls=(0, (14, 4, 2, 4, 2, 4)), z=6)
        pt = L["wet"].intersection(L["parcel"])
        if not pt.is_empty:
            q = pt.representative_point()
            S.txt(q.x, q.y, "EDGE OF WETLAND (NWI, APPROX.)", 5.2, halo=True, z=21)
    if L.get("flood") is not None and not L["flood"].is_empty:
        for p in polys(L["flood"].intersection(box(*S.view))):
            S.line(p.exterior, color=BLUE, lw=0.9, ls=(0, (8, 3)), z=6)
        q = L["flood"].intersection(L["parcel"])
        if not q.is_empty:
            r = q.representative_point()
            S.txt(r.x, r.y, "FEMA SFHA (1% ANNUAL CHANCE FLOODPLAIN)", 5.2, color=BLUE, halo=True, z=21)


def contours(S, X, which="ex", labels=True, color=GREY, alpha=0.9):
    g = X["grid"]
    z = X["ex_z"] if which == "ex" else X["z_prop"]
    key = "_c_" + which
    if key not in X:
        X[key] = grading.contour_lines(g, z, 2.0, 10.0, clip=box(*S.view).buffer(300))
    for lev, ln, idx in X[key]:
        if which == "ex":
            S.line(ln, color=GREY, lw=0.9 if idx else 0.45, ls=(0, (7, 2.5)) if idx else (0, (4, 3)), z=2, alpha=alpha)
        else:
            S.line(ln, color=INK, lw=0.95 if idx else 0.45, z=6)
    if not labels:
        return
    vb = box(*S.view).buffer(-25)
    for lev, ln, idx in X[key]:
        if not idx or ln.length < 70:
            continue
        for frac in (0.5,):
            p = ln.interpolate(frac, normalized=True)
            if not vb.contains(p):
                continue
            q = ln.interpolate(min(frac + 0.02, 1.0), normalized=True)
            a = math.degrees(math.atan2(q.y - p.y, q.x - p.x))
            if a > 90 or a < -90:
                a += 180
            S.txt(p.x, p.y, f"{lev:g}", 5.4, rot=a, color=GREY if which == "ex" else INK, halo=True, z=7)


def proposed_base(S, X, labels=True, fill=True, existing=True):
    L = X["L"]
    if existing:
        existing_features(S, X)
    for b, ffe in zip(L["bldgs"], X["G"]["ffe"]):
        S.poly(b, fc="#dfe2e6" if fill else "none", ec=INK, lw=1.6, z=10)
    S.curb(L["pave"], z=9)
    if L.get("walk") is not None and not L["walk"].is_empty:
        S.poly(L["walk"], ec=INK, lw=0.4, z=8)
    for st in L.get("stripes") or []:
        S.line(st, color=INK, lw=0.35, z=8)
    for a in L.get("ada") or []:
        S.poly(a, ec=BLUE, lw=0.6, z=8)
    pond = X["D"]["pond"]["poly"]
    S.poly(pond, ec=INK, lw=1.1, z=9)
    S.poly(pond.buffer(-(X["D"]["pond"]["depth_ft"] * 3.0)), ec=INK, lw=0.6, z=9)


def stationing(S, X, step=50.0, size=5.2):
    sp = X["G"]["spine"]
    for s in np.arange(0, sp.length + 1, step):
        p = sp.interpolate(s)
        q = sp.interpolate(min(s + 1.0, sp.length))
        a = math.degrees(math.atan2(q.y - p.y, q.x - p.x))
        nx, ny = -math.sin(math.radians(a)), math.cos(math.radians(a))
        S.line(LineString([(p.x - nx * 2, p.y - ny * 2), (p.x + nx * 2, p.y + ny * 2)]), color=INK, lw=0.6, z=11)
        rot = a if -90 <= a <= 90 else a + 180
        S.txt(p.x + nx * 5.5, p.y + ny * 5.5, f"{int(s // 100)}+{int(s % 100):02d}", size, rot=rot, halo=True, z=22)
    S.line(sp, color=INK, lw=0.7, ls=(0, (10, 3, 2, 3)), z=11)


def table(S, X0, Y0, col_w, rows, title=None, size=5.2, row_h=0.19, header=True, widths_in=True, fill_header="#e9ecef"):
    """Grid table with its top-left at ground coords (X0, Y0); col_w in inches, row_h in inches."""
    sc = S.scale
    y = Y0
    if title:
        S.txt(X0, y, title, 9, ha="left", va="bottom", weight="bold", z=61)
    x_edges = np.concatenate([[0], np.cumsum(col_w)]) * sc + X0
    n = len(rows)
    for r, row in enumerate(rows):
        y0 = Y0 - r * row_h * sc
        y1 = y0 - row_h * sc
        if r == 0 and header:
            S.ax.add_patch(plt_rect(x_edges[0], y1, x_edges[-1] - x_edges[0], row_h * sc, fill_header))
        else:
            S.ax.add_patch(plt_rect(x_edges[0], y1, x_edges[-1] - x_edges[0], row_h * sc, "white"))
        S.ax.plot([x_edges[0], x_edges[-1]], [y1, y1], color=INK, lw=0.5, zorder=61)
        for c, cell in enumerate(row):
            S.txt(x_edges[c] + 0.04 * sc, (y0 + y1) / 2, str(cell), size, ha="left", weight="bold" if (r == 0 and header) else "normal", z=63)
    S.ax.plot([x_edges[0], x_edges[-1]], [Y0, Y0], color=INK, lw=0.8, zorder=61)
    for e in x_edges:
        S.ax.plot([e, e], [Y0, Y0 - n * row_h * sc], color=INK, lw=0.5, zorder=62)
    S.ax.plot([x_edges[0], x_edges[-1]], [Y0 - n * row_h * sc] * 2, color=INK, lw=0.8, zorder=61)
    return Y0 - n * row_h * sc


def plt_rect(x, y, w, h, fc):
    from matplotlib.patches import Rectangle
    return Rectangle((x, y), w, h, fc=fc, ec="none", zorder=60)


def corner(S, fx, fy):
    x0, y0, x1, y1 = S.view
    return x0 + (x1 - x0) * fx, y0 + (y1 - y0) * fy
