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
from shapely.ops import unary_union  # noqa: E402

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

# palette sampled from the Forma reference render (1120 Lot 6, Pocahontas Trail)
C_GRASS = "#76924f"          # lawn
C_GRASS_SHADE = "#5a7057"    # lawn in shadow / edge
C_ASPHALT = "#4e5969"        # blue-grey asphalt
C_LIGHT = "#c2c9d1"          # curbs, walks, roofs (cool light grey)
C_ROOF_HI = "#d3d9e0"
C_DARK = "#2f3b4b"           # parapets, walls, shadow core
C_WATER, C_WATER_EDGE = "#6f97a8", "#3f6a7c"
SUN = (1.0, 1.0)             # shadows fall to the north-east, like the reference
BLDG_H = 22.0                # ft, for shadow length


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


def _noise(shape, seed, blur):
    from PIL import ImageFilter
    rng = np.random.default_rng(seed)
    n = Image.fromarray((rng.random(shape) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(blur))
    a = np.asarray(n).astype(float) / 255.0
    return (a - a.mean()) / (a.std() + 1e-6)


def _textured(ax, g, view, color, amp, z, k_ft, edge=None, lw=0.0):
    """Fill g with `color` plus soft fine-grain noise (render-like), clipped to g."""
    if g is None or g.is_empty:
        return
    x0, y0, x1, y1 = g.bounds
    x0, y0, x1, y1 = max(x0, view[0]), max(y0, view[1]), min(x1, view[2]), min(y1, view[3])
    if x1 <= x0 or y1 <= y0:
        return
    W = int(min(2400, max(64, (x1 - x0) * k_ft * 2)))
    H = int(min(2400, max(64, (y1 - y0) * k_ft * 2)))
    base = np.array(matplotlib.colors.to_rgb(color))
    n = 0.65 * _noise((H, W), int(z * 10), 1.2) + 0.35 * _noise((H, W), int(z * 10) + 1, 6)
    img = np.clip(base[None, None, :] * (1 + amp * n[..., None]), 0, 1)
    im = ax.imshow(img, extent=(x0, x1, y0, y1), origin="upper", zorder=z, interpolation="bilinear")
    for p in _polys(g):
        verts, codes = [], []
        for r in [p.exterior, *p.interiors]:
            cs = np.asarray(r.coords)
            verts += cs.tolist()
            codes += [MPath.MOVETO] + [MPath.LINETO] * (len(cs) - 2) + [MPath.CLOSEPOLY]
        clip = PathPatch(MPath(verts, codes), transform=ax.transData, facecolor="none", edgecolor="none")
        ax.add_patch(clip)
        im2 = im if p is _polys(g)[0] else ax.imshow(img, extent=(x0, x1, y0, y1), origin="upper", zorder=z, interpolation="bilinear")
        im2.set_clip_path(clip)
    if edge:
        _patch(ax, g, z + 0.1, facecolor="none", edgecolor=edge, lw=lw)


def _gradient(ax, g, view, c_hi, c_lo, z):
    """Roof fill: soft diagonal light-to-shade gradient clipped to the roof."""
    x0, y0, x1, y1 = g.bounds
    hi, lo = np.array(matplotlib.colors.to_rgb(c_hi)), np.array(matplotlib.colors.to_rgb(c_lo))
    gx, gy = np.meshgrid(np.linspace(0, 1, 64), np.linspace(1, 0, 64))
    t = np.clip((gx + gy) / 2, 0, 1)[..., None]
    img = hi * (1 - t) + lo * t
    im = ax.imshow(img, extent=(x0, x1, y0, y1), origin="upper", zorder=z, interpolation="bilinear")
    clip = PathPatch(MPath(np.asarray(g.exterior.coords), closed=True), transform=ax.transData, facecolor="none", edgecolor="none")
    ax.add_patch(clip)
    im.set_clip_path(clip)


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

    # proposed, drawn like the reference render: textured lawn pad, blue-grey asphalt with light curbs, light
    # walks, light roofs with dark parapets, NE shadows, white stripes
    view = (vx0, vy0, vx1, vy1)
    pad = sp.get("pad")
    if pad is None and sp.get("grass") is not None:
        pad = sp["grass"]
    if pad is not None:
        _textured(ax, pad, view, C_GRASS, 0.09, 3, k_ft, edge=C_GRASS_SHADE, lw=0.6)
    if sp.get("pond") is not None:
        _textured(ax, sp["pond"].buffer(6 / k_ft), view, C_GRASS, 0.09, 3.1, k_ft)
        _patch(ax, sp["pond"], 4, facecolor=C_WATER, edgecolor=C_WATER_EDGE, lw=1.0 * T)
    _textured(ax, sp["pave"], view, C_ASPHALT, 0.06, 5, k_ft)
    _patch(ax, sp["pave"], 5.2, facecolor="none", edgecolor=C_LIGHT, lw=lw_ft(0.9), joinstyle="round")   # curb
    _patch(ax, sp["walk"], 6, facecolor=C_LIGHT, edgecolor="none")
    for ln in sp["stripes"]:
        xs, ys = ln.xy
        ax.plot(xs, ys, color="#f2f4f5", lw=lw_ft(0.4), zorder=8, solid_capstyle="butt")
    sv = (SUN[0] * BLDG_H * 0.75 / k_ft, SUN[1] * BLDG_H * 0.75 / k_ft)
    for b in sp["bldgs"]:
        moved = affinity.translate(b, *sv)
        shadow = unary_union([b, moved, unary_union([b, moved]).convex_hull]).difference(b)
        _patch(ax, shadow, 7.5, facecolor=(0.10, 0.15, 0.20, 0.42), edgecolor="none")
        wall = affinity.translate(b, 1.6 / k_ft, -1.6 / k_ft).difference(b)          # visible wall face (SE)
        _patch(ax, wall, 8.5, facecolor=C_DARK, edgecolor="none")
    for b, extra in zip(sp["bldgs"], sp.get("roof_blocks") or [None] * len(sp["bldgs"])):
        _gradient(ax, b, view, C_ROOF_HI, C_LIGHT, 9)
        _patch(ax, b, 9.6, facecolor="none", edgecolor=C_DARK, lw=lw_ft(1.4), joinstyle="miter")
        if extra is not None:
            _patch(ax, extra, 9.7, facecolor="none", edgecolor=C_DARK, lw=lw_ft(1.1), joinstyle="miter")
            _patch(ax, affinity.translate(extra, 0.8 / k_ft, -0.8 / k_ft).difference(extra).intersection(b), 9.65, facecolor=(0.18, 0.23, 0.29, 0.6), edgecolor="none")

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

    fig.savefig(pdf_path)
    fig.savefig(png_path, dpi=55)
    plt.close(fig)
