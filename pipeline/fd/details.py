"""Construction details sheet: standard details drawn parametrically (not to scale), sized from the design where it matters
(pipe sizes, pond outlet elevations, pavement section)."""
import math

import numpy as np
from matplotlib.patches import Circle, Polygon as MPoly, Rectangle

from .sheetbase import GREY, INK, LGREY, Sheet

HATCH = "#9aa0a6"


class _D:
    """A detail cell with its own inch origin."""

    def __init__(self, S, x, y, w, h, title, sub="NOT TO SCALE"):
        self.S, self.x, self.y, self.w, self.h = S, x, y, w, h
        S.txt(x + w / 2, y + 0.42, title, 10.5, weight="bold", z=63)
        S.ax.plot([x + w / 2 - len(title) * 0.062, x + w / 2 + len(title) * 0.062], [y + 0.28] * 2, color=INK, lw=1.0, zorder=63)
        S.txt(x + w / 2, y + 0.15, sub, 5.6, z=63)
        self.y = y + 1.1                       # drawing origin sits above the title

    def P(self, pts, fc="none", ec=INK, lw=0.8, hatch=None, z=20, ls="-"):
        self.S.ax.add_patch(MPoly([(self.x + a, self.y + b) for a, b in pts], closed=True, fc=fc, ec=ec, lw=lw, hatch=hatch, zorder=z, ls=ls))

    def R(self, x, y, w, h, **kw):
        self.P([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], **kw)

    def L(self, pts, lw=0.8, ls="-", color=INK, z=22):
        self.S.ax.plot([self.x + a for a, _ in pts], [self.y + b for _, b in pts], color=color, lw=lw, ls=ls, zorder=z)

    def C(self, x, y, r, fc="white", lw=0.9, z=24):
        self.S.ax.add_patch(Circle((self.x + x, self.y + y), r, fc=fc, ec=INK, lw=lw, zorder=z))

    def T(self, x, y, s, size=5.6, ha="left", **kw):
        self.S.txt(self.x + x, self.y + y, s, size, ha=ha, z=64, **kw)

    def note(self, xy, txy, s, size=5.4, ha="left"):
        self.S.ax.annotate(s, xy=(self.x + xy[0], self.y + xy[1]), xytext=(self.x + txy[0], self.y + txy[1]), fontsize=size, ha=ha, va="center", zorder=64,
                           arrowprops=dict(arrowstyle="-|>", lw=0.5, color=INK, shrinkA=1, shrinkB=0))

    def dim(self, a, b, s, off=0.12, size=5.2):
        (x0, y0), (x1, y1) = a, b
        self.L([a, b], lw=0.5)
        vert = abs(x1 - x0) < 1e-6
        for (px, py) in (a, b):
            self.L([(px - (off if vert else 0), py - (0 if vert else off)), (px + (off if vert else 0), py + (0 if vert else off))], lw=0.5)
        self.T((x0 + x1) / 2 + (0.08 if vert else 0), (y0 + y1) / 2 + (0 if vert else 0.1), s, size, ha="left" if vert else "center")

    def ground(self, x0, x1, y, n=10):
        self.L([(x0, y), (x1, y)], lw=1.0)
        for k in range(n):
            xx = x0 + (x1 - x0) * (k + 0.5) / n
            self.L([(xx, y), (xx - 0.08, y - 0.1)], lw=0.4)

    def notes(self, x, y, items, title="NOTES:", size=5.2, wrap=62):
        import textwrap
        self.T(x, y, title, size + 1, weight="bold")
        yy = y - 0.17
        for n, it in enumerate(items, 1):
            for k, ln in enumerate(textwrap.wrap(it.upper(), wrap)):
                self.T(x + (0.0 if k == 0 else 0.18), yy, (f"{n}.  " if k == 0 else "") + ln, size)
                yy -= 0.125
            yy -= 0.03


def _pavement(d, sec):
    d.R(0.4, 2.6, 2.6, 0.14, fc="#3a3a3a")
    d.R(0.4, 2.36, 2.6, 0.24, fc="#6f6f6f")
    d.R(0.4, 1.76, 2.6, 0.6, fc="white", hatch="ooo", ec=HATCH)
    d.R(0.4, 1.76, 2.6, 0.6)
    d.R(0.4, 0.96, 2.6, 0.8, fc="white", hatch="...", ec=HATCH)
    d.R(0.4, 0.96, 2.6, 0.8)
    d.ground(0.4, 3.0, 0.96, 6)
    d.note((2.6, 2.67), (3.3, 3.1), f"{sec['wear']}\" WEARING COURSE\nBIT. CONC. (1/2\" AGG.)")
    d.note((2.6, 2.48), (3.3, 2.6), f"{sec['base']}\" BINDER COURSE\nBIT. CONC. (3/4\" AGG.)")
    d.note((2.6, 2.05), (3.3, 2.1), f"{sec['agg']}\" CRUSHED AGGREGATE BASE,\nCOMPACTED TO 95%")
    d.note((2.6, 1.35), (3.3, 1.5), f"{sec['sub']}\" BANK-RUN GRAVEL SUBBASE,\nCOMPACTED TO 95%")
    d.note((2.6, 0.93), (3.3, 0.9), "COMPACTED SUBGRADE (PROOF-ROLL)")
    d.T(0.4, 0.3, "HEAVY-DUTY SECTION IN DRIVE AISLES. THE GEOTECHNICAL\nENGINEER'S SECTION GOVERNS WHERE PROVIDED.", 5.0)


def _curb(d):
    d.P([(2.0, 1.2), (2.0, 2.5), (2.45, 2.5), (2.6, 2.05), (2.6, 1.2)], fc="#d9d9d9")
    d.R(2.6, 1.85, 3.0, 0.2, fc="#3a3a3a")
    d.R(2.6, 1.35, 3.0, 0.5, fc="white", hatch="ooo", ec=HATCH)
    d.R(1.3, 0.9, 1.6, 0.3, fc="white", hatch="...", ec=HATCH)
    d.L([(0.5, 2.5), (2.0, 2.5)], lw=1.0)
    d.dim((1.8, 2.05), (1.8, 2.5), "6\" REVEAL")
    d.dim((2.0, 2.65), (2.6, 2.65), "6\"")
    d.note((2.3, 1.6), (0.3, 1.6), "CONCRETE CURB\n4,000 PSI, AIR-ENTRAINED", ha="left")
    d.note((4.5, 1.95), (4.2, 2.6), "PAVEMENT (SEE SECTION)")
    d.note((2.0, 1.05), (3.4, 0.75), "6\" COMPACTED GRAVEL BASE")
    d.T(0.5, 2.62, "LOAM & SEED / WALK", 5.0)
    d.T(0.5, 0.3, "EXPANSION JOINTS AT 30 FT; CONTROL JOINTS AT 10 FT.", 5.0)


def _trench(d, dia, label, cover, mat):
    d.P([(1.6, 3.6), (2.3, 0.7), (4.3, 0.7), (5.0, 3.6)], fc="white")
    d.P([(2.3, 0.7), (4.3, 0.7), (4.52, 1.6), (2.08, 1.6)], fc="white", hatch="...", ec=HATCH)
    d.ground(0.6, 1.6, 3.6, 4)
    d.ground(5.0, 6.0, 3.6, 4)
    d.L([(1.6, 3.6), (5.0, 3.6)], lw=1.0)
    d.C(3.3, 1.2, 0.3)
    d.T(3.3, 1.2, f"{dia}\"", 5.2, ha="center")
    d.dim((5.3, 1.5), (5.3, 3.6), f"{cover} MIN. COVER")
    d.dim((2.3, 0.55), (4.3, 0.55), "PIPE O.D. + 2 FT")
    d.note((3.3, 2.6), (0.2, 2.9), "SUITABLE BACKFILL IN 12\" LIFTS,\n95% COMPACTION UNDER PAVEMENT")
    d.note((2.6, 1.3), (0.2, 1.9), "3/4\" CRUSHED STONE OR SAND\nBEDDING, 6\" UNDER / 12\" OVER PIPE")
    d.note((3.3, 2.0), (5.4, 1.2), "WARNING TAPE\n12\" ABOVE PIPE")
    d.L([(2.9, 2.0), (3.7, 2.0)], lw=0.8, ls=(0, (3, 2)))
    d.note((3.55, 1.05), (5.4, 0.8), f"{label}\n{mat}")


def _structure(d, kind, cb):
    d.R(2.2, 0.8, 2.2, 0.2, fc="#d9d9d9")
    d.R(2.2, 1.0, 0.2, 2.2, fc="#d9d9d9")
    d.R(4.2, 1.0, 0.2, 2.2, fc="#d9d9d9")
    d.P([(2.2, 3.2), (2.4, 3.2), (2.9, 3.7), (2.9, 3.85), (2.7, 3.85)], fc="#d9d9d9")
    d.P([(4.4, 3.2), (4.2, 3.2), (3.7, 3.7), (3.7, 3.85), (3.9, 3.85)], fc="#d9d9d9")
    d.R(2.7, 3.85, 1.2, 0.1, fc="#555555")
    d.ground(0.6, 2.7, 3.95, 5)
    d.ground(3.9, 6.0, 3.95, 5)
    inv = 1.7 if cb else 1.0
    d.C(2.3, inv + 0.25, 0.25)
    d.C(4.3, inv + 0.2, 0.25)
    if cb:
        d.dim((4.8, 1.0), (4.8, inv - 0.05), "2 FT SUMP")
        d.P([(4.2, 2.1), (3.85, 2.1), (3.85, 1.45), (4.0, 1.45), (4.0, 1.95), (4.2, 1.95)], fc="white")
        d.note((3.9, 1.6), (5.2, 2.6), "OIL / DEBRIS HOOD\nON OUTLET")
        d.note((3.3, 3.9), (4.6, 4.3), "FRAME & GRATE, H-20\n(BICYCLE SAFE)")
    else:
        d.P([(2.4, 1.0), (2.4, inv + 0.2), (3.3, inv), (4.2, inv), (4.2, 1.0)], fc="#eeeeee")
        d.note((3.3, 1.1), (5.2, 0.6), "SHAPED CONCRETE INVERT\n(BENCH AT 1\":1 FT)")
        d.note((3.3, 3.9), (4.6, 4.3), "FRAME & SOLID COVER, H-20\n" + ("\"SEWER\"" if kind == "SMH" else "\"DRAIN\""))
        d.L([(2.6, 1.6), (2.6, 3.0)], lw=0.5)
        for yy in np.arange(1.7, 3.0, 0.3):
            d.L([(2.4, yy), (2.6, yy)], lw=0.7)
        d.note((2.5, 2.4), (0.2, 2.6), "STEPS AT 12\" O.C.")
    d.dim((2.4, 0.55), (4.2, 0.55), "4 FT I.D. (MIN.)")
    d.note((2.25, 2.9), (0.2, 3.3), "PRECAST CONC. (ASTM C478)\nBUTYL JOINT SEALANT")
    d.note((3.3, 0.9), (0.2, 0.9), "12\" CRUSHED STONE BASE")
    d.note((2.15, inv + 0.25), (0.2, 1.7), "FLEXIBLE BOOT\n(ASTM C923)")


def _silt(d):
    d.ground(0.5, 6.0, 1.5, 12)
    d.R(3.0, 0.8, 0.08, 2.4, fc="#8a5a2b")
    d.L([(3.08, 3.0), (3.08, 1.3), (3.6, 1.1)], lw=1.4)
    d.P([(3.08, 1.5), (3.6, 1.5), (3.6, 1.1), (3.08, 1.1)], fc="white", ls=(0, (3, 2)))
    d.note((3.08, 2.5), (4.0, 2.9), "FILTER FABRIC, STAPLED TO POSTS")
    d.note((3.04, 3.1), (0.6, 3.3), "2\" x 2\" HARDWOOD POST\nAT 6 FT O.C. (MAX.)")
    d.note((3.4, 1.25), (4.0, 0.8), "6\" x 6\" TRENCH, FABRIC TOE\nBURIED AND BACKFILLED")
    d.dim((2.7, 1.5), (2.7, 3.0), "30\" MIN.")
    d.T(4.2, 1.9, "FLOW", 6, weight="bold")
    d.L([(5.2, 1.95), (4.7, 1.95)], lw=1.0)
    d.T(0.5, 0.3, "REMOVE SEDIMENT AT 1/3 FENCE HEIGHT.\nFILTER SOCK (12\" MIN.) MAY BE SUBSTITUTED.", 5.0)


def _entrance(d):
    d.R(1.0, 1.4, 4.2, 0.35, fc="white", hatch="ooo", ec=HATCH)
    d.R(1.0, 1.4, 4.2, 0.35)
    d.L([(1.0, 1.38), (5.2, 1.38)], lw=1.2, ls=(0, (4, 2)))
    d.ground(0.3, 1.0, 1.75, 3)
    d.R(5.2, 1.55, 0.9, 0.2, fc="#3a3a3a")
    d.dim((1.0, 2.05), (5.2, 2.05), "50 FT MIN. (75 FT WHERE SOILS ARE SILT / CLAY)")
    d.note((3.0, 1.6), (1.2, 2.8), "3\" CRUSHED STONE, 6\" THICK MIN.")
    d.note((3.4, 1.38), (2.6, 0.8), "GEOTEXTILE FILTER FABRIC")
    d.note((5.6, 1.7), (4.6, 2.8), "EXISTING ROAD")
    d.T(0.3, 0.3, "WIDTH = FULL WIDTH OF ACCESS, 12 FT MIN.\nTOP-DRESS WITH STONE AS NEEDED; SWEEP ROAD DAILY.", 5.0)


def _outlet(d, pond):
    d.P([(0.4, 1.0), (1.4, 1.0), (2.6, 3.0), (3.4, 3.0), (4.6, 1.0), (6.2, 1.0), (6.2, 0.7), (0.4, 0.7)], fc="#f1f1f1")
    d.L([(0.4, 2.55), (2.33, 2.55)], lw=0.8, color="#1f4fd8")
    d.R(1.9, 1.0, 0.5, 1.7, fc="#d9d9d9")
    d.R(1.85, 2.7, 0.6, 0.08, fc="#555555")
    d.L([(2.4, 1.15), (5.0, 1.0)], lw=3.0, color="#777777")
    d.C(1.9, 1.3, 0.07, fc=INK)
    d.note((1.9, 1.3), (0.2, 1.7), f"{pond['orifice_in']:.1f}\" ORIFICE\nINV. {pond['bottom_el']:.2f}")
    d.note((2.15, 2.78), (0.2, 3.3), "OUTLET CONTROL STRUCTURE\n4 FT PRECAST, TRASH-RACK TOP")
    d.note((3.0, 3.0), (3.6, 3.5), f"BERM TOP {pond['top_el']:.2f}, 6 FT WIDE (MIN.)")
    d.note((2.2, 2.55), (3.9, 2.4), f"EMERGENCY WEIR CREST {pond['weir_crest']:.2f}\n{pond['weir_len_ft']:.0f} FT WIDE, RIPRAP-LINED")
    d.note((4.2, 1.05), (4.4, 1.7), "OUTLET PIPE WITH ANTI-SEEP\nCOLLAR AND RIPRAP APRON")
    d.note((1.0, 1.0), (0.2, 0.3), f"POND BOTTOM {pond['bottom_el']:.2f}")
    d.T(2.6, 0.3, f"INTERIOR SLOPES {pond.get('side', 3.0):g}H:1V; 4\" LOAM AND SEED.", 5.0)


def _fes(d):
    d.P([(0.8, 1.6), (2.6, 1.6), (3.3, 1.2), (3.3, 2.5), (2.6, 2.1), (0.8, 2.1)], fc="#e5e5e5")
    d.P([(3.3, 0.9), (5.9, 0.6), (5.9, 3.1), (3.3, 2.8)], fc="white", hatch="ooo", ec=HATCH)
    d.P([(3.3, 0.9), (5.9, 0.6), (5.9, 3.1), (3.3, 2.8)])
    d.note((1.6, 1.85), (0.3, 2.9), "STORM DRAIN")
    d.note((3.0, 2.2), (1.8, 3.3), "FLARED END SECTION\nWITH TOE WALL")
    d.note((4.8, 1.8), (4.0, 3.5), "RIPRAP APRON d50 = 6\", 18\" THICK\nOVER GEOTEXTILE (PLAN VIEW)")
    d.dim((3.3, 0.45), (5.9, 0.45), "La = 6 x PIPE DIA. (10 FT MIN.)")
    d.dim((6.05, 0.6), (6.05, 3.1), "W = 3D + La")


def sheet(meta, X, no="C9"):
    S = Sheet(meta, no, "CONSTRUCTION DETAILS", (0, 0), scale=1, scale_text="AS NOTED")
    W, H = S.W_in, S.H_in
    S.ax.set_xlim(0, W)
    S.ax.set_ylim(0, H)
    S.view = (0, 0, W, H)
    net, U, pond = X["net"], X["U"], X["D"]["pond"]
    dmax = max([p["dia"] for p in net.get("pipes") or []] or [15])
    cw, ch = W / 4.0, H / 3.0 - 0.1
    def cell(c, r, title):
        return _D(S, 0.4 + c * cw, H - (r + 1) * ch - 0.2, cw - 0.4, ch, title)
    _pavement(cell(0, 0, "BITUMINOUS PAVEMENT SECTION"), {"wear": 1.5, "base": 2.5, "agg": 6, "sub": 12})
    _curb(cell(1, 0, "CONCRETE CURB"))
    _structure(cell(2, 0, "CATCH BASIN"), "CB", True)
    _structure(cell(3, 0, "DRAIN / SEWER MANHOLE"), "SMH" if U.get("sewer_pipes") else "DMH", False)
    _trench(cell(0, 1, "STORM DRAIN TRENCH"), dmax, "STORM DRAIN", "2 FT", "HDPE (AASHTO M294) OR RCP CL. III")
    _trench(cell(1, 1, "SANITARY SEWER TRENCH"), 8, "GRAVITY SEWER", "4 FT", "PVC SDR 35 (ASTM D3034)")
    _trench(cell(2, 1, "WATER MAIN TRENCH"), 8, "WATER MAIN", "5 FT", "PVC C900 DR 18 / DUCTILE IRON CL. 52")
    _fes(cell(3, 1, "FLARED END SECTION & RIPRAP APRON"))
    _silt(cell(0, 2, "SILT FENCE"))
    _entrance(cell(1, 2, "STABILIZED CONSTRUCTION ENTRANCE"))
    if pond.get("kind") != "chambers":
        _outlet(cell(2, 2, "DETENTION POND OUTLET"), pond)
    d = cell(3, 2, "DETAIL NOTES")
    d.notes(0.3, 4.6, ["Details are typical. Where the municipality or utility publishes a standard detail, that standard governs.",
                       "Maintain 10 ft horizontal and 18 in vertical separation between water and sewer; where not possible, sleeve the sewer for 10 ft each side of the crossing.",
                       "All structures in pavement are rated H-20. Set rims to finished grade.",
                       "Pipe bedding and backfill are compacted to 95 percent of maximum dry density (ASTM D1557) under pavement and 90 percent elsewhere.",
                       "Pavement section is a planning section; the geotechnical report governs.",
                       "Thrust blocks or restrained joints at all water main bends, tees and dead ends."], wrap=64)
    S.txt(W * 0.5, 0.3, "PRELIMINARY - NOT SEALED - NOT FOR CONSTRUCTION", 7, color=GREY, z=60)
    return S
