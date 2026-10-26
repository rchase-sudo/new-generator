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
TXT_H = 0.08                    # plan label cap height, inches (CAD text height)
CAP = 0.716                     # Arial cap height / em
LFS = TXT_H * 72 / CAP          # -> font size in points
LSPACE = (TXT_H * 72 * 5 / 3) / LFS   # AutoCAD MText line spacing 1.0 (1.667 x height)
T = SHEET_W / 17.0              # title-block type / line scale vs. the 17x11 layout

P_LAYERS = {"P-BLDG": 1, "P-BLDG-HATCH": 8, "P-PAVE-EDGE": 7, "P-CURB": 9, "P-PARKING-STRIPE": 7, "P-ADA": 5,
            "P-WALK": 9, "P-LANDSCAPE": 3, "P-POND": 4, "P-TEXT": 7, "P-EXIST-KEEP": 30, "P-EXIST-DEMO": 1, "P-EXIST-PAVE": 8, "P-STEEP": 1}

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
def _clip_demo(existing, sp, k_ft):
    """Existing work marked for demolition is only flagged where the proposed development does not replace it: a red
    demolition overlay on top of the proposed buildings and pavement is noise on the layout plan."""
    if not existing:
        return existing
    parts = [sp.get(n) for n in ("bldgs", "pave", "pond", "walk", "drive")]
    flat = []
    for p in parts:
        flat += list(p) if isinstance(p, (list, tuple)) else ([p] if p is not None else [])
    flat = [g for g in flat if g is not None and not g.is_empty]
    if not flat:
        return existing
    prop = unary_union(flat).buffer(2.0 / k_ft)
    out = []
    for e in existing:
        if e.get("action") != "demo":
            out.append(e)
            continue
        g = e["geom"].difference(prop)
        if g.is_empty or g.area < 0.35 * e["geom"].area:
            continue
        out.append(dict(e, geom=g, sf=round(e.get("sf", 0) * g.area / e["geom"].area, -1)))
    return out


def write_dxf(ec_dxf, out_path, sp, labels, k_ft, existing=None, steep=None):
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
    if "P-TREE" not in doc.layers:
        doc.layers.add("P-TREE", color=3)
    for t in sp.get("trees") or []:
        c = t.centroid
        msp.add_circle((c.x, c.y), math.sqrt(t.area / math.pi), dxfattribs={"layer": "P-TREE"})
    if sp.get("pond") is not None:
        ring(sp["pond"], "P-POND", width=0.3 / k_ft)
    for g in steep or []:
        ring(g, "P-STEEP", width=1.0 / k_ft)
    existing = _clip_demo(existing, sp, k_ft)
    for e in existing or []:
        pv = e.get("kind") == "pavement"
        if pv and e["action"] == "keep":
            continue      # reused pavement is backend knowledge only; the drawing shows proposed work
        ring(e["geom"], "P-EXIST-PAVE" if (pv and e["action"] == "keep") else ("P-EXIST-KEEP" if e["action"] == "keep" else "P-EXIST-DEMO"), width=0.5 / k_ft)
        if e["action"] == "demo" and not pv:   # X through the footprint, the usual demolition mark
            for p in _polys(e["geom"]):
                x0, y0, x1, y1 = p.minimum_rotated_rectangle.bounds
                msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": "P-EXIST-DEMO"})
                msp.add_line((x0, y1), (x1, y0), dxfattribs={"layer": "P-EXIST-DEMO"})
        c = e["geom"].representative_point()
        mt = msp.add_mtext(f"EXISTING {'PAVEMENT ' if pv else ''}{e['id']} TO BE {'REMOVED' if pv and e['action'] == 'demo' else 'DEMOLISHED' if e['action'] == 'demo' else 'RETAINED'}\\P{e['sf']:,.0f} SF",
                           dxfattribs={"layer": "P-TEXT", "char_height": 2.5 / k_ft, "insert": (c.x, c.y), "attachment_point": 5, "rotation": 0})
        mt.set_bg_color("canvas", scale=1.3)
    for t in labels:
        h = (2.0 if t.get("small") else 3.0) / k_ft
        mt = msp.add_mtext("\\P".join(t["lines"]), dxfattribs={"layer": "P-TEXT", "char_height": h, "insert": t["at"],
                                                               "attachment_point": 5, "rotation": 0})
        # background mask: must be written with its scale (45) and colour (63) codes -- a bare bg_fill flag (90)
        # makes AutoCAD abort the open ("Press ENTER to continue" -> back to the start page)
        mt.set_bg_color("canvas", scale=1.3)
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
    for pi_, p in enumerate(_polys(g)):
        verts, codes = [], []
        for r in [p.exterior, *p.interiors]:
            cs = np.asarray(r.coords)
            verts += cs.tolist()
            codes += [MPath.MOVETO] + [MPath.LINETO] * (len(cs) - 2) + [MPath.CLOSEPOLY]
        clip = PathPatch(MPath(verts, codes), transform=ax.transData, facecolor="none", edgecolor="none")
        ax.add_patch(clip)
        im2 = im if pi_ == 0 else ax.imshow(img, extent=(x0, x1, y0, y1), origin="upper", zorder=z, interpolation="bilinear")
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
    if sp.get("reuse") is not None and not sp["reuse"].is_empty:      # keep the existing pavement the site ties into in view
        dev = dev.union(sp["reuse"])
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
    w, h = min(w, h * aspect), min(h, w / aspect)    # keep the sheet's aspect when the aerial is the limit
    cx, cy = min(max(cx, minx + w / 2), maxx - w / 2), min(max(cy, miny + h / 2), maxy - h / 2)
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def write_sheet(pdf_path, png_path, parcel, env, wet, fl, rds, aerial, k_ft, sp, labels, title_lines, metrics, program, rules,
                project=None, existing=None, topo=None, steep=None):
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

    if topo:
        from .ec_drawing import draw_topo
        draw_topo(ax, topo, T / 2, shade=False, labels=False, alpha=0.7, lw=0.45)
    for g in steep or []:
        _patch(ax, g, 2, facecolor=(1, 0.25, 0.2, 0.12), edgecolor="#ff4d4d", lw=1.0 * T, ls=(0, (3, 3)))
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

    existing = _clip_demo(existing, sp, k_ft)
    for e in existing or []:
        if e.get("kind") == "pavement":
            if e["action"] == "demo":
                _patch(ax, e["geom"], 5.4, facecolor=(1, 0.2, 0.2, 0.18), edgecolor="#ff3b30", lw=lw_ft(0.9), ls=(0, (5, 3)), hatch="//")
            continue
        if e["action"] == "keep":
            _patch(ax, e["geom"], 9.2, facecolor="#aeb4bb", edgecolor=C_DARK, lw=lw_ft(1.2), joinstyle="miter")
        else:
            _patch(ax, e["geom"], 9.2, facecolor=(1, 0.2, 0.2, 0.22), edgecolor="#ff3b30", lw=lw_ft(1.2), ls=(0, (5, 3)))
            for p in _polys(e["geom"]):
                x0_, y0_, x1_, y1_ = p.minimum_rotated_rectangle.bounds
                ax.plot([x0_, x1_], [y0_, y1_], color="#ff3b30", lw=lw_ft(0.8), zorder=9.3)
                ax.plot([x0_, x1_], [y1_, y0_], color="#ff3b30", lw=lw_ft(0.8), zorder=9.3)

    # ornamental trees: NE shadow, canopy with a lit south-west side
    from matplotlib.patches import Circle
    for t in sp.get("trees") or []:
        c = t.centroid
        r = math.sqrt(t.area / math.pi)
        ax.add_patch(Circle((c.x + 0.45 * r, c.y + 0.45 * r), r * 0.98, facecolor=(0.08, 0.12, 0.10, 0.38), edgecolor="none", zorder=9.75))
        ax.add_patch(Circle((c.x, c.y), r, facecolor="#4a6630", edgecolor="#34481f", lw=0.6, zorder=9.8))
        ax.add_patch(Circle((c.x - 0.22 * r, c.y - 0.22 * r), r * 0.62, facecolor="#6a8a42", edgecolor="none", alpha=0.9, zorder=9.81))
        ax.add_patch(Circle((c.x - 0.35 * r, c.y - 0.35 * r), r * 0.28, facecolor="#86a457", edgecolor="none", alpha=0.85, zorder=9.82))

    _patch(ax, parcel, 11, facecolor="none", edgecolor="#ff19ff", lw=2.6 * T)
    if env is not None:
        _patch(ax, env, 11, facecolor="none", edgecolor="#ffe600", lw=1.8 * T, ls=(0, (9, 6)))

    # labels: white boxes with regular text, always placed OFF the buildings (the client has to see the roofs),
    # as close as they fit, with a short leader line back to the building
    from shapely.geometry import LineString as _LS, box as _box
    from shapely.ops import nearest_points
    box_kw = dict(boxstyle=f"square,pad={TXT_H * 72 * 0.5 / LFS:.3f}", fc="w", ec="none")
    bldgs = [b for b in sp["bldgs"] if b is not None]
    hard = unary_union([b.buffer(3 / k_ft) for b in bldgs]) if bldgs else None
    ex_keep = [e for e in existing or [] if e["action"] == "keep" and e.get("kind") != "pavement"]
    if ex_keep:
        eh = unary_union([e["geom"].buffer(3 / k_ft) for e in ex_keep])
        hard = eh if hard is None else hard.union(eh)
    if sp.get("pond") is not None:
        hard = sp["pond"] if hard is None else hard.union(sp["pond"])
    placed = []
    view_box = _box(vx0, vy0, vx1, vy1).buffer(-20 / ppu)

    def place(lines, anchor, target, fs):
        bw = (max(len(x) for x in lines) * fs * 0.6 + fs) / ppu
        bh = ((len(lines) - 1) * LSPACE * fs + TXT_H * 72 + fs) / ppu
        ax_, ay_ = anchor
        best = None
        dirs = [(0, 1), (0, -1), (1, 0), (-1, 0), (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7)]
        step = 3 / k_ft
        for dx, dy in dirs:
            for i in range(0, 120):
                cx, cy = ax_ + dx * i * step, ay_ + dy * i * step
                rb = _box(cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2)
                if hard is not None and rb.intersects(hard):
                    continue
                if any(rb.intersects(q) for q in placed) or not view_box.contains(rb):
                    break
                d = i * step
                if best is None or d < best[0]:
                    best = (d, cx, cy, rb)
                break
        if best is None:
            best = (0, ax_, ay_, _box(ax_ - bw / 2, ay_ - bh / 2, ax_ + bw / 2, ay_ + bh / 2))
        _, cx, cy, rb = best
        placed.append(rb.buffer(4 / ppu))
        if target is not None and rb.distance(target) > 1.5 / k_ft:
            p1, p2 = nearest_points(rb, target)
            ax.plot([p1.x, p2.x], [p1.y, p2.y], color="w", lw=1.2, zorder=11.5, solid_capstyle="round")
            ax.plot([p2.x], [p2.y], marker="o", ms=2.2, color="w", zorder=11.6)
        ax.text(cx, cy, "\n".join(lines), ha="center", va="center", fontsize=fs, family=LFONT, zorder=12,
                linespacing=LSPACE, bbox=box_kw)

    bi = 0
    for t in labels:
        if t.get("small"):
            ax.text(t["at"][0], t["at"][1], "\n".join(t["lines"]), ha="center", va="center", fontsize=LFS, family=LFONT,
                    color="#10324a", zorder=12, linespacing=LSPACE)
            continue
        target = bldgs[bi] if bi < len(bldgs) else None
        bi += 1
        place(t["lines"], t["at"], target, LFS)
    for e in existing or []:
        c = e["geom"].representative_point()
        if not (vx0 < c.x < vx1 and vy0 < c.y < vy1):
            continue
        if e.get("kind") == "pavement" and e["action"] == "keep":
            continue
        if e.get("kind") == "pavement":
            place([f"EXISTING PAVEMENT {e['id']}", "TO BE REMOVED" if e["action"] == "demo" else "TO REMAIN", f"{e['label']}, {e['sf']:,.0f} SF"], (c.x, c.y), e["geom"], LFS)
            continue
        place([f"EXISTING {e['id']}", "TO BE DEMOLISHED" if e["action"] == "demo" else "TO REMAIN", f"{e['sf']:,.0f} SF"], (c.x, c.y), e["geom"], LFS)
    for w in wet:
        g = w["geom"].intersection(parcel)
        if g.is_empty or g.area < 2000 / k_ft ** 2:
            continue
        c = g.representative_point()
        if vx0 < c.x < vx1 and vy0 < c.y < vy1:
            place(["WETLANDS"], (c.x, c.y), None, LFS)
            break

    for g in sorted(steep or [], key=lambda g: -g.area)[:1]:
        c = g.intersection(parcel).representative_point() if g.intersects(parcel) else None
        if c is not None and vx0 < c.x < vx1 and vy0 < c.y < vy1:
            place(["STEEP SLOPES", "NO DEVELOPMENT"], (c.x, c.y), None, LFS)

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
