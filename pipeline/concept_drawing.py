"""Concept plan outputs: DXF (existing-conditions DXF + proposed layers) and the Forma preliminary development plan
sheet (17x11, Forma title block) styled like the client renders: aerial, landscaped pad, curbed asphalt, walks,
roofs with shadows, white label boxes."""
import math
import os
import textwrap

import ezdxf
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402
from PIL import Image  # noqa: E402
from shapely import affinity  # noqa: E402

from .ec_drawing import _polys

ASSETS = os.environ.get("FORMA_ASSETS") or os.path.join(os.path.dirname(__file__), "assets")
for _f in ("Carlito-Regular.ttf", "Carlito-Bold.ttf", "LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"):
    try:
        font_manager.fontManager.addfont(os.path.join(ASSETS, _f))
    except Exception:  # noqa: BLE001
        pass
FONT = "Carlito"              # title block (matches the Forma template)
LFONT = "Liberation Sans"     # plan labels (Arial metrics, like the CAD text style)

SHEET_W, SHEET_H = 34.0, 22.0   # ANSI D
TXT_H = 0.16                    # plan label cap height, inches (CAD text height)
CAP = 0.716                     # Arial cap height / em
LFS = TXT_H * 72 / CAP          # -> font size in points
LSPACE = (TXT_H * 72 * 5 / 3) / LFS   # AutoCAD MText line spacing 1.0 (1.667 x height)
T = SHEET_W / 17.0              # title-block type / line scale vs. the 17x11 layout

P_LAYERS = {"P-BLDG": 1, "P-BLDG-HATCH": 8, "P-PAVE-EDGE": 7, "P-CURB": 9, "P-PARKING-STRIPE": 7, "P-ADA": 5,
            "P-WALK": 9, "P-LANDSCAPE": 3, "P-POND": 4, "P-TEXT": 7}

# title block geometry (pixels of forma_titleblock.jpg, 2576 x 1667)
TB_W, TB_H = 2576, 1667
TB_DRAW = (85, 48, 2278, 1618)          # drawing frame
TB_PROJECT = (2293, 1190, 2521, 1527)   # project name block (vertical text)
TB_DATA = (2293, 296, 2521, 470)        # empty block under the logo -> site data

C_GRASS, C_GRASS_EDGE = "#78a64b", "#5d8a36"
C_ASPHALT, C_CURB = "#40454a", "#dcdbd6"
C_WALK, C_WALK_EDGE = "#d3d0c8", "#aeaaa1"
C_ROOF, C_ROOF_EDGE, C_PARAPET = "#e9eef2", "#7f8890", "#b9c1c8"
C_WATER, C_WATER_EDGE = "#5f9cc2", "#2f6b93"


# ---------------------------------------------------------------- DXF
def write_dxf(ec_dxf, out_path, sp, labels, k_ft):
    doc = ezdxf.readfile(ec_dxf)
    for n, c in P_LAYERS.items():
        if n not in doc.layers:
            doc.layers.add(n, color=c)
    msp = doc.modelspace()

    def ring(g, layer, width=0.0):
        for p in _polys(g):
            for r in [p.exterior, *p.interiors]:
                msp.add_lwpolyline(list(r.coords)[:-1], close=True, dxfattribs={"layer": layer, "const_width": width})

    ring(sp["pave"], "P-PAVE-EDGE")
    ring(sp["pave"].buffer(0.5 / k_ft, join_style=2), "P-CURB")
    ring(sp["walk"], "P-WALK")
    if sp.get("grass") is not None:
        ring(sp["grass"], "P-LANDSCAPE")
    for b in sp["bldgs"]:
        ring(b, "P-BLDG", width=0.5 / k_ft)
        for p in _polys(b):
            h = msp.add_hatch(color=8, dxfattribs={"layer": "P-BLDG-HATCH"})
            h.set_pattern_fill("ANSI31", scale=2.0 / k_ft)
            h.paths.add_polyline_path(list(p.exterior.coords)[:-1], is_closed=True)
    for ln in sp["stripes"]:
        msp.add_line(ln.coords[0], ln.coords[1], dxfattribs={"layer": "P-PARKING-STRIPE"})
    for a in sp["ada"]:
        ring(a, "P-ADA")
    if sp.get("pond") is not None:
        ring(sp["pond"], "P-POND", width=0.3 / k_ft)
    for t in labels:
        h = (2.0 if t.get("small") else 3.0) / k_ft
        msp.add_mtext("\\P".join(t["lines"]), dxfattribs={"layer": "P-TEXT", "char_height": h, "insert": t["at"],
                                                          "attachment_point": 5, "rotation": 0, "bg_fill": 1})
    doc.saveas(out_path)


# ---------------------------------------------------------------- sheet
def _patch(ax, g, z, **kw):
    """Polygon(s) with holes as one path each, so islands inside pavement stay open."""
    for p in _polys(g):
        verts, codes = [], []
        for r in [p.exterior, *p.interiors]:
            cs = np.asarray(r.coords)
            verts += cs.tolist()
            codes += [MPath.MOVETO] + [MPath.LINETO] * (len(cs) - 2) + [MPath.CLOSEPOLY]
        ax.add_patch(PathPatch(MPath(verts, codes), zorder=z, **kw))


def _view(sp, parcel, aerial, k_ft, aspect):
    minx, miny, maxx, maxy = aerial["extent"]
    dev = sp["dev"]
    dx0, dy0, dx1, dy1 = dev.bounds
    size = max(dx1 - dx0, dy1 - dy0)
    pad = max(size * 0.75, 160 / k_ft)
    vx0, vy0, vx1, vy1 = dx0 - pad, dy0 - pad, dx1 + pad, dy1 + pad
    px0, py0, px1, py1 = parcel.bounds
    if (px1 - px0) < (vx1 - vx0) * 1.6 and (py1 - py0) < (vy1 - vy0) * 1.6:   # small parcel: show all of it
        vx0, vy0, vx1, vy1 = min(vx0, px0 - 60 / k_ft), min(vy0, py0 - 60 / k_ft), max(vx1, px1 + 60 / k_ft), max(vy1, py1 + 60 / k_ft)
    cx, cy = (vx0 + vx1) / 2, (vy0 + vy1) / 2
    w, h = vx1 - vx0, vy1 - vy0
    w, h = (h * aspect, h) if w / h < aspect else (w, w / aspect)
    w, h = min(w, maxx - minx), min(h, maxy - miny)
    cx, cy = min(max(cx, minx + w / 2), maxx - w / 2), min(max(cy, miny + h / 2), maxy - h / 2)
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def write_sheet(pdf_path, png_path, parcel, env, wet, fl, rds, aerial, k_ft, sp, labels, title_lines, metrics, program, rules,
                project=None):
    fig = plt.figure(figsize=(SHEET_W, SHEET_H), dpi=100)
    bg = fig.add_axes([0, 0, 1, 1])
    bg.axis("off")
    tb = Image.open(os.path.join(ASSETS, "forma_titleblock.jpg"))
    bg.imshow(np.asarray(tb), extent=(0, TB_W, TB_H, 0), interpolation="lanczos")
    bg.set_xlim(0, TB_W)
    bg.set_ylim(TB_H, 0)

    x0, y0, x1, y1 = TB_DRAW
    ax = fig.add_axes([x0 / TB_W + 0.0006, 1 - y1 / TB_H + 0.0006, (x1 - x0) / TB_W - 0.0012, (y1 - y0) / TB_H - 0.0012])
    aspect = ((x1 - x0) * SHEET_W / TB_W) / ((y1 - y0) * SHEET_H / TB_H)
    vx0, vy0, vx1, vy1 = _view(sp, parcel, aerial, k_ft, aspect)
    minx, miny, maxx, maxy = aerial["extent"]
    ax.imshow(np.asarray(aerial["image"]), extent=(minx, maxx, miny, maxy), interpolation="bilinear", zorder=0)
    ax.set_xlim(vx0, vx1)
    ax.set_ylim(vy0, vy1)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    ppu = (x1 - x0) / TB_W * SHEET_W * 72 / (vx1 - vx0)   # points per drawing unit
    lw_ft = lambda ft: max(0.4, ft / k_ft * ppu)       # noqa: E731

    # existing: wetlands (dashed outline), parcel, setback
    for w in wet:
        _patch(ax, w["geom"], 2, facecolor="none", edgecolor="#4fdc5a", lw=1.1 * T, ls=(0, (6, 4)))
    for f in fl:
        if f.get("sfha") == "T":
            _patch(ax, f["geom"], 2, facecolor=(0.3, 0.55, 1, 0.12), edgecolor="#4d8cff", lw=1.0 * T, ls=(0, (6, 4)))

    # proposed: landscape pad, pond, pavement + curbs, walks, stripes, ADA, buildings w/ shadow
    if sp.get("grass") is not None:
        _patch(ax, sp["grass"], 3, facecolor=C_GRASS, edgecolor=C_GRASS_EDGE, lw=0.6)
    if sp.get("pond") is not None:
        _patch(ax, sp["pond"].buffer(6 / k_ft), 3, facecolor=C_GRASS, edgecolor=C_GRASS_EDGE, lw=0.6)
        _patch(ax, sp["pond"], 4, facecolor=C_WATER, edgecolor=C_WATER_EDGE, lw=1.0 * T)
    _patch(ax, sp["pave"], 5, facecolor=C_ASPHALT, edgecolor=C_CURB, lw=lw_ft(1.0), joinstyle="round")
    _patch(ax, sp["walk"], 6, facecolor=C_WALK, edgecolor=C_WALK_EDGE, lw=0.5)
    for a in sp["ada"]:
        _patch(ax, a, 7, facecolor="#2f6fd0", edgecolor="w", lw=0.5)
    for ln in sp["stripes"]:
        xs, ys = ln.xy
        ax.plot(xs, ys, color="w", lw=lw_ft(0.45), zorder=8, solid_capstyle="butt")
    sh = 4.0 / k_ft
    for b in sp["bldgs"]:
        _patch(ax, affinity.translate(b, sh, -sh), 8, facecolor=(0, 0, 0, 0.38), edgecolor="none")
    for b in sp["bldgs"]:
        _patch(ax, b, 9, facecolor=C_ROOF, edgecolor=C_ROOF_EDGE, lw=1.0 * T)
        inner = b.buffer(-2.0 / k_ft, join_style=2)
        if not inner.is_empty:
            _patch(ax, inner, 10, facecolor="none", edgecolor=C_PARAPET, lw=0.6)

    _patch(ax, parcel, 11, facecolor="none", edgecolor="#ff19ff", lw=2.6 * T)
    if env is not None:
        _patch(ax, env, 11, facecolor="none", edgecolor="#ffe600", lw=1.8 * T, ls=(0, (9, 6)))

    # labels: white boxes, regular text, centred on the building
    box_kw = dict(boxstyle=f"square,pad={TXT_H * 72 * 0.5 / LFS:.3f}", fc="w", ec="none")
    placed = []
    for t in labels:
        if t.get("small"):
            ax.text(t["at"][0], t["at"][1], "\n".join(t["lines"]), ha="center", va="center", fontsize=LFS, family=LFONT,
                    color="#10324a", zorder=12, linespacing=LSPACE)
            continue
        fs = LFS
        bw = (max(len(x) for x in t["lines"]) * fs * 0.6 + fs) / ppu
        bh = ((len(t["lines"]) - 1) * LSPACE * fs + TXT_H * 72 + fs) / ppu
        cx, cy = t["at"]
        gap = 8 / ppu
        cands = [(0, 0), (0, bh + gap), (0, -bh - gap), (bw + gap, 0), (-bw - gap, 0),
                 (bw + gap, bh + gap), (-bw - gap, bh + gap), (bw + gap, -bh - gap), (-bw - gap, -bh - gap),
                 (0, 2 * (bh + gap)), (0, -2 * (bh + gap))]
        best = None
        for dx, dy in cands:
            r = (cx + dx - bw / 2, cy + dy - bh / 2, cx + dx + bw / 2, cy + dy + bh / 2)
            if not any(r[0] < q[2] and q[0] < r[2] and r[1] < q[3] and q[1] < r[3] for q in placed):
                best = (dx, dy, r)
                break
        dx, dy, r = best or (0, 0, (cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2))
        placed.append(r)
        if dx or dy:   # moved off its building: leader line
            ax.plot([cx, cx + dx], [cy, cy + dy], color="w", lw=1.5, zorder=11.5)
        ax.text(cx + dx, cy + dy, "\n".join(t["lines"]), ha="center", va="center", fontsize=fs, family=LFONT,
                zorder=12, linespacing=LSPACE, bbox=box_kw)
    for w in wet:
        g = w["geom"].intersection(parcel)
        if g.is_empty or g.area < 2000 / k_ft ** 2:
            continue
        c = g.representative_point()
        if vx0 < c.x < vx1 and vy0 < c.y < vy1:
            ax.text(c.x, c.y, "WETLANDS", ha="center", va="center", fontsize=LFS, family=LFONT, zorder=12, bbox=box_kw)
            break

    # project block (vertical text, reading bottom-to-top) over the template's sample text
    px0, py0, px1, py1 = TB_PROJECT
    bg.add_patch(plt.Rectangle((px0, py0), px1 - px0, py1 - py0, color="w", zorder=2))
    proj = project or [program.get("use") or "Development"] + list(title_lines)
    cyb = (py0 + py1) / 2
    x = 2346 + 6
    for i, line in enumerate(proj[:4]):
        fs = (17 if i == 0 else 13.5) * T
        n = max(len(line), 1)
        fs = min(fs, (py1 - py0 - 24) / (n * 0.5) * 72 / (TB_H / SHEET_H))   # keep inside the block
        bg.text(x, cyb, line, rotation=90, ha="center", va="center", fontsize=fs, family=FONT,
                weight="bold" if i == 0 else "normal", zorder=3)
        x += 31 if i == 0 else 29

    # site data under the logo
    dx0, dy0, dx1, dy1 = TB_DATA
    m = metrics
    zon = rules.get("district") or "—"
    sb = m.get("setbacks_ft") or {}
    rows = [("ZONING", zon),
            ("SETBACKS F/S/R", f"{sb.get('front', '—'):g}' / {sb.get('side', '—'):g}' / {sb.get('rear', '—'):g}'" if sb else "—"),
            ("BUILDING AREA", f"{m['gfa_sf']:,.0f} SF"),
            ("PARKING REQ / PROV", f"{m['stalls_required']} / {m['stalls']}  ({m['ada_stalls']} ADA)"),
            ("IMPERVIOUS", f"{m['impervious_pct']}% of site"),
            ("REMAINING LAND", f"{m['remaining_developable_acres']} AC ±")]
    bg.text(dx0 + 8, dy0 + 4, "SITE DATA", fontsize=6.5 * T, family=FONT, weight="bold", va="top", zorder=3)
    yy = dy0 + 24
    for k_, v in rows:
        bg.text(dx0 + 8, yy, k_, fontsize=5.2 * T, family=FONT, color="#555", va="top", zorder=3)
        bg.text(dx1 - 6, yy, v, fontsize=5.6 * T, family=FONT, va="top", ha="right", zorder=3)
        yy += 21
    if m.get("warnings"):
        bg.text(dx0 + 8, yy, "\n".join(textwrap.wrap("; ".join(m["warnings"]), 46)[:2]), fontsize=4.8 * T, family=FONT,
                color="#b42318", va="top", zorder=3)

    fig.savefig(pdf_path)
    fig.savefig(png_path, dpi=55)
    plt.close(fig)
