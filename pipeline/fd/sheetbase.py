"""Plan-set sheet frame in the style of the reference set (SFC Engineering): ANSI D landscape, drawing area left, title
block column right (logo, seal box, revisions, location plan, project, data, sheet title + number), north arrow,
graphic scale, notes blocks. Colours are CAD black on white; existing work is light grey."""
import math
import os
import textwrap

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import PathPatch, Polygon as MPoly, Rectangle  # noqa: E402
from matplotlib.path import Path as MPath  # noqa: E402
from PIL import Image  # noqa: E402

from ..concept_drawing import ASSETS, TB_DRAW, TB_H, TB_W
from .base import lines, polys

SHEET_W, SHEET_H = 34.0, 22.0
FONT = "Liberation Sans"
FONT_T = "Carlito"
for _f in ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "Carlito-Regular.ttf", "Carlito-Bold.ttf"):
    try:
        font_manager.fontManager.addfont(os.path.join(ASSETS, _f))
    except Exception:  # noqa: BLE001
        pass

INK = "#111111"
GREY = "#8a8f96"
LGREY = "#c5c9ce"
RED = "#d4222a"
BLUE = "#1f4fd8"
GREEN = "#1b8a3a"
CYAN = "#0a8ea6"
EXIST = "#828282"      # CAD colour 253: existing work that stays after development
BROWN = "#8a5a2b"

PX_W = SHEET_W / TB_W      # inches per title-block pixel
PX_H = SHEET_H / TB_H
STD_SCALES = [10, 20, 30, 40, 50, 60, 80, 100, 150, 200, 300, 400, 500]


class Sheet:
    def __init__(self, meta, no, title, view_center, scale=None, fit_geom=None, rotate_north=0.0, margin_ft=40, right_notes=True, scale_text=None, panel=False, panel_w=8.6):
        """meta: project info dict. view_center ft (x,y). scale: ft per inch, or None to fit `fit_geom`."""
        self.meta = meta
        self.no, self.title = no, title
        self.fig = plt.figure(figsize=(SHEET_W, SHEET_H), dpi=100)
        self.bg = self.fig.add_axes([0, 0, 1, 1])
        self.bg.axis("off")
        tb = Image.open(os.path.join(ASSETS, "forma_titleblock.jpg"))
        self.bg.imshow(np.asarray(tb), extent=(0, TB_W, TB_H, 0), interpolation="lanczos")
        self.bg.set_xlim(0, TB_W)
        self.bg.set_ylim(TB_H, 0)
        x0, y0, x1, y1 = TB_DRAW
        self.ax = self.fig.add_axes([x0 / TB_W + 0.0006, 1 - y1 / TB_H + 0.0006, (x1 - x0) / TB_W - 0.0012, (y1 - y0) / TB_H - 0.0012])
        self.ax.set_aspect("equal")
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        for sp in self.ax.spines.values():
            sp.set_visible(False)
        self.W_in = (x1 - x0) * PX_W
        self.H_in = (y1 - y0) * PX_H
        self.panel, self.PW = bool(panel), panel_w
        fit_w = self.W_in - (panel_w if panel else 0)
        if scale is None and fit_geom is not None:
            minx, miny, maxx, maxy = fit_geom.bounds
            need = max((maxx - minx + 2 * margin_ft) / fit_w, (maxy - miny + 2 * margin_ft) / self.H_in)
            scale = next((s for s in STD_SCALES if s >= need), STD_SCALES[-1])
            view_center = ((minx + maxx) / 2, (miny + maxy) / 2)
        self.scale = scale or 40
        self.scale_text = scale_text
        cx, cy = view_center
        hw, hh = self.W_in * self.scale / 2, self.H_in * self.scale / 2
        if self.panel:
            cx += (self.PW * self.scale) / 2          # site sits in the left region; the panel takes the right
        self.ax.set_xlim(cx - hw, cx + hw)
        self.ax.set_ylim(cy - hh, cy + hh)
        self.view = (cx - hw, cy - hh, cx + hw, cy + hh)
        self.left_view = (self.view[0], self.view[1], self.view[2] - (self.PW * self.scale if self.panel else 0), self.view[3])
        if self.panel:
            self.px = self.view[2] - (self.PW - 0.3) * self.scale
            self.cur = self.view[3] - 0.35 * self.scale
            self.ax.add_patch(Rectangle((self.view[2] - self.PW * self.scale, self.view[1]), self.PW * self.scale, self.view[3] - self.view[1],
                                        fc="white", ec="none", zorder=58))
            self.ax.plot([self.view[2] - self.PW * self.scale] * 2, [self.view[1], self.view[3]], color=INK, lw=1.4, zorder=59)
        self.pt_per_ft = 72.0 / self.scale            # 1 ft on the drawing in points
        self.north = rotate_north
        self._chrome()

    # ---------------------------------------------------------- drawing helpers
    def lw(self, ft):
        """Line width in points for a physical width in feet on the ground (pen weights are in points, so this is for ground-scale items)."""
        return ft * self.pt_per_ft

    def txt(self, x, y, s, size=7.0, rot=0, ha="center", va="center", weight="normal", color=INK, halo=False, z=30, family=FONT, box=True, **kw):
        t = self.ax.text(x, y, s, fontsize=size, rotation=rot, ha=ha, va=va, fontweight=weight, color=color, family=family, zorder=z, **kw)
        if halo:
            import matplotlib.patheffects as pe
            t.set_path_effects([pe.withStroke(linewidth=2.2, foreground="white")])
        if box:
            t.set_bbox(dict(boxstyle="square,pad=0.15", fc="white", ec="none"))
        return t

    def poly(self, g, fc="none", ec=INK, lw=0.8, ls="-", z=5, alpha=None, hatch=None):
        for p in polys(g):
            verts, codes = [], []
            for r in [p.exterior, *p.interiors]:
                cs = np.asarray(r.coords)
                verts += cs.tolist()
                codes += [MPath.MOVETO] + [MPath.LINETO] * (len(cs) - 2) + [MPath.CLOSEPOLY]
            self.ax.add_patch(PathPatch(MPath(verts, codes), fc=fc, ec=ec, lw=lw, ls=ls, zorder=z, alpha=alpha, hatch=hatch))

    def line(self, g, color=INK, lw=0.8, ls="-", z=6, alpha=1.0, **kw):
        for ln in lines(g) if g.geom_type != "LinearRing" else [g]:
            xs, ys = ln.xy
            self.ax.plot(xs, ys, color=color, lw=lw, ls=ls, zorder=z, alpha=alpha, solid_capstyle="butt", **kw)

    def curb(self, g, z=7, lw=1.3):
        """Pavement edge as a curb: heavy outline plus a thin inner offset line."""
        for p in polys(g):
            self.poly(p, ec=INK, lw=lw, z=z)
            try:
                inner = p.buffer(-0.9)
                self.poly(inner, ec=INK, lw=0.4, z=z)
            except Exception:  # noqa: BLE001
                pass

    def leader(self, xy, text_xy, text, size=6.5, ha="left"):
        self.ax.annotate(text, xy=xy, xytext=text_xy, fontsize=size, family=FONT, ha=ha, va="center", zorder=40,
                         arrowprops=dict(arrowstyle="-|>", lw=0.5, color=INK, mutation_scale=5, shrinkA=0, shrinkB=0),
                         bbox=dict(boxstyle="square,pad=0.12", fc="white", ec="none"))

    def clip_view(self, g):
        from shapely.geometry import box
        return g.intersection(box(*self.view))

    # ---------------------------------------------------------- chrome
    def _px(self, x, y):
        return x, y

    def T(self, x, y, s, size=8, ha="left", va="center", weight="normal", color=INK, rot=0, family=FONT_T, z=10):
        return self.bg.text(x, y, s, fontsize=size, ha=ha, va=va, fontweight=weight, color=color, rotation=rot, family=family, zorder=z)

    def _chrome(self):
        m = self.meta
        bg = self.bg
        # --- rebuild the right column below the logo
        bg.add_patch(Rectangle((2284, 228), 244, 1394, fc="white", ec="none", zorder=2))
        def rule(y, lw=3.2):
            bg.plot([2284, 2528], [y, y], color="black", lw=lw, zorder=3, solid_capstyle="butt")
        rule(232)
        # seal block
        cx = 2406
        bg.add_patch(plt.Circle((cx, 395), 62, fc="none", ec=GREY, lw=1.0, ls=(0, (4, 3)), zorder=3))
        self.T(cx, 372, "PRELIMINARY", 8, "center", weight="bold", color=GREY)
        self.T(cx, 392, "NOT SEALED", 8, "center", weight="bold", color=GREY)
        self.T(cx, 412, "FOR PE SEAL", 7, "center", color=GREY)
        rule(478)
        # revisions
        self.T(2290, 494, "REVISIONS", 6.5)
        rows = 10
        top, bot = 508, 790
        bg.plot([2290, 2522], [top, top], color="black", lw=0.8, zorder=3)
        self.T(2296, top + 9, "NO.", 5.5)
        self.T(2326, top + 9, "DESCRIPTION", 5.5)
        self.T(2476, top + 9, "DATE", 5.5)
        for r in range(rows):
            y = top + 18 + r * (bot - top - 18) / rows
            bg.plot([2290, 2522], [y, y], color=GREY, lw=0.5, zorder=3)
        for x in (2321, 2472):
            bg.plot([x, x], [top, bot], color=GREY, lw=0.5, zorder=3)
        # revision 0 line
        self.T(2296, top + 27, "0", 5.5)
        self.T(2326, top + 27, "ISSUED FOR PRELIMINARY REVIEW", 4.4)
        self.T(2476, top + 27, m["date_short"], 5)
        rule(800)
        # location plan thumbnail
        if m.get("loc_img") is not None:
            bg.imshow(m["loc_img"], extent=(2292, 2520, 1030, 812), zorder=3, interpolation="bilinear", aspect="auto")
            bg.plot([2292, 2520, 2520, 2292, 2292], [812, 812, 1030, 1030, 812], color="black", lw=0.6, zorder=4)
            bg.plot([m["loc_star"][0] * 228 + 2292], [812 + m["loc_star"][1] * 218], marker="*", ms=11, color="black", zorder=5)
            self.T(cx, 1042, "Location Plan", 8.5, "center", family=FONT_T)
            self.T(cx, 1058, "Aerial imagery: " + m.get("aerial_src", "NAIP"), 5.2, "center", family=FONT_T)
        rule(1072)
        # project
        self.T(cx, 1090, "Project:", 7, "center", family=FONT_T)
        y = 1110
        for i, ln in enumerate(m["project_lines"][:6]):
            self.T(cx, y, ln, 9.5 if i == 0 else 7.6, "center", weight="bold" if i == 0 else "normal", family="DejaVu Serif" if i == 0 else FONT_T)
            y += 19 if i == 0 else 15
        if m.get("zone_line"):
            self.T(cx, y + 8, m["zone_line"], 6.4, "center")
            y += 26
        self.T(cx, y + 12, "Prepared For:", 7, "center", family=FONT_T)
        for i, ln in enumerate(m["client_lines"][:3]):
            self.T(cx, y + 34 + i * 15, ln, 7.6, "center", family=FONT_T)
        rule(1378)
        d = [("Designed By:", m["designer"]), ("Drawn By:", "FORMA"), ("Checked By:", "—")]
        yy = 1396
        for k_, v_ in d:
            self.T(2294, yy, k_, 5.4)
            self.T(2362, yy, v_, 5.4)
            yy += 14
        self.T(2418, 1396, "Date:", 5.4)
        self.T(2452, 1396, m["date_short"], 5.4)
        self.T(2418, 1410, "Scale:", 5.4)
        self.T(2452, 1410, self.scale_text or f'1" = {self.scale:g}\'', 5.4)
        self.T(2418, 1424, "Project No:", 5.4)
        self.T(2466, 1424, m["job_no"], 5.4)
        rule(1450)
        self.T(cx, 1470, "Sheet Title:", 6.4, "center", family=FONT_T)
        for i, ln in enumerate(textwrap.wrap(self.title, 24)[:2]):
            self.T(cx, 1492 + i * 17, ln, 10.5, "center", weight="bold", family="DejaVu Serif")
        self.T(cx, 1590, self.no, 30, "center", weight="bold", family=FONT_T)
        # drawing name (vertical, left margin)
        self.bg.text(70, 760, f"Drawing: {m['job_no']} {self.title}, Layout: {self.title} - {self.no}", fontsize=5.0, rotation=90, ha="center", va="center", family=FONT, color="#444", zorder=5)
        self.bg.text(2290, 1632, f"© FORMA DESIGNS {m['year']} — PRELIMINARY, NOT FOR CONSTRUCTION", fontsize=4.2, ha="left", va="top", family=FONT, color="#555", zorder=5)

    # ---------------------------------------------------------- annotation blocks
    def north_arrow(self, xy_frac=(0.90, 0.12), size_in=1.5):
        x0, y0, x1, y1 = self.left_view
        cx = x0 + (x1 - x0) * xy_frac[0]
        cy = y0 + (y1 - y0) * xy_frac[1]
        L = size_in * self.scale / 2
        a = math.radians(self.north)
        ux, uy = math.cos(a), math.sin(a)
        tip = (cx + ux * L, cy + uy * L)
        tail = (cx - ux * L, cy - uy * L)
        self.ax.plot([tail[0], tip[0]], [tail[1], tip[1]], color=INK, lw=1.2, zorder=50)
        px, py = -uy, ux
        head = [tip, (tip[0] - ux * L * 0.28 + px * L * 0.1, tip[1] - uy * L * 0.28 + py * L * 0.1), (tip[0] - ux * L * 0.2, tip[1] - uy * L * 0.2),
                (tip[0] - ux * L * 0.28 - px * L * 0.1, tip[1] - uy * L * 0.28 - py * L * 0.1)]
        self.ax.add_patch(MPoly(head, closed=True, fc=INK, ec=INK, zorder=51))
        self.ax.add_patch(plt.Circle((cx, cy), L * 0.2, fc="white", ec=INK, lw=0.8, zorder=51))
        self.txt(cx + ux * (L * 1.25), cy + uy * (L * 1.25), "N", 11, weight="bold", z=52)
        self.txt(cx - ux * L * 1.25 - px * L * 0.5, cy - uy * L * 1.25 - py * L * 0.5, "NAD 83 / MAG. N. N/A", 4.2, rot=self.north - 90, z=52)

    def scale_bar(self, xy_frac=(0.55, 0.035)):
        x0, y0, x1, y1 = self.left_view
        seg = {10: 10, 20: 10, 30: 30, 40: 20, 50: 25, 60: 30, 80: 40, 100: 50}.get(self.scale, self.scale * 0.8)
        n = 3
        bx = x0 + (x1 - x0) * xy_frac[0]
        by = y0 + (y1 - y0) * xy_frac[1]
        h = 0.14 * self.scale
        for i in range(n):
            self.ax.add_patch(Rectangle((bx + i * seg, by), seg, h / 2, fc=INK if i % 2 == 0 else "white", ec=INK, lw=0.6, zorder=50))
            self.ax.add_patch(Rectangle((bx + i * seg, by + h / 2), seg, h / 2, fc="white" if i % 2 == 0 else INK, ec=INK, lw=0.6, zorder=50))
        for i in range(n + 1):
            self.txt(bx + i * seg, by - 0.12 * self.scale, f"{i * seg:g}", 6.5, z=52)
        self.txt(bx + n * seg / 2, by + h + 0.16 * self.scale, f'SCALE:  1" = {self.scale:g}\'', 6.8, z=52)

    def text_block(self, xy_frac, title, items, width_in=8.6, size=5.8, title_size=11, numbered=True):
        """Notes block anchored at its top-left (fraction of the drawing area; in panel mode, at the panel cursor)."""
        x0, y0, x1, y1 = self.view
        X = x0 + (x1 - x0) * xy_frac[0]
        Y = y0 + (y1 - y0) * xy_frac[1]
        ftin = self.scale
        if self.panel:
            X, Y, width_in, size, title_size = self.px, self.cur, self.PW - 0.9, 7.4, 13
        self.txt(X, Y, title, title_size, ha="left", va="top", weight="bold", z=60, family=FONT)
        y = Y - 0.26 * ftin
        chars = int(width_in / (size * 0.64 / 72))
        for n, it in enumerate(items, 1):
            lines_ = textwrap.wrap(it.upper(), chars) or [""]
            lab = f"{n}." if numbered else "•"
            self.txt(X, y, lab, size, ha="left", va="top", z=60)
            for k, ln in enumerate(lines_):
                self.txt(X + 0.22 * ftin, y - k * size * 1.35 / 72 * ftin, ln, size, ha="left", va="top", z=60)
            y -= (len(lines_) * size * 1.35 / 72 + 0.09) * ftin
        if self.panel:
            self.cur = y - 0.3 * ftin
        return y

    def call811(self, xy_frac=(0.80, 0.12)):
        self._811 = True
        x0, y0, x1, y1 = self.view
        X = x0 + (x1 - x0) * xy_frac[0]
        Y = y0 + (y1 - y0) * xy_frac[1]
        r = 0.5 * self.scale
        if self.panel:
            X, Y = x1 - self.PW * self.scale / 2, y0 + 0.9 * self.scale
        self.ax.add_patch(plt.Circle((X, Y), r, fc="white", ec=RED, lw=2.4, zorder=60))
        self.txt(X, Y + 0.08 * self.scale, "811", 17, weight="bold", z=61)
        self.txt(X, Y - 0.22 * self.scale, "CALL BEFORE\nYOU DIG", 5.4, weight="bold", z=61, linespacing=1.0)
        self.ax.plot([X - r * 0.7, X + r * 0.7], [Y - r * 0.7, Y + r * 0.7], color=RED, lw=2.4, zorder=62)

    def save(self, pdf=None, png=None, dpi=60):
        if png:
            self.fig.savefig(png, dpi=dpi)
        if pdf is not None:
            pdf.savefig(self.fig)
        plt.close(self.fig)
