"""Concept plan outputs: DXF (existing-conditions DXF + proposed layers) and a 17x11 colour plan sheet."""
import math
import textwrap

import ezdxf
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Polygon as MplPoly  # noqa: E402

from .ec_drawing import _polys

P_LAYERS = {"P-BLDG": 1, "P-BLDG-HATCH": 8, "P-PAVE-EDGE": 7, "P-PARKING-STRIPE": 7, "P-ADA": 5, "P-WALK": 9,
            "P-POND": 4, "P-TEXT": 7}


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
    ring(sp["walk"], "P-WALK")
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
                                                          "attachment_point": 5, "rotation": _upright(t["rot"]), "bg_fill": 1})
    doc.saveas(out_path)


def _upright(rot):
    rot = (rot + 180) % 360 - 180
    return rot - 180 if rot > 90 else rot + 180 if rot < -90 else rot


def write_sheet(pdf_path, png_path, parcel, env, wet, fl, rds, aerial, k_ft, sp, labels, title_lines, metrics, program, rules):
    minx, miny, maxx, maxy = aerial["extent"]
    fig = plt.figure(figsize=(17, 11), dpi=150)
    ax = fig.add_axes([0.02, 0.04, 0.76, 0.92])
    ax.imshow(np.asarray(aerial["image"]), extent=(minx, maxx, miny, maxy), interpolation="bilinear")
    ax.add_patch(plt.Rectangle((minx, miny), maxx - minx, maxy - miny, color=(1, 1, 1, 0.32), zorder=1))
    # zoom to the parcel (+ ~120 ft) at the sheet's aspect, inside the aerial's extent
    aspect = (0.76 * 17) / (0.92 * 11)
    px0, py0, px1, py1 = parcel.bounds
    pad = 120 / k_ft
    cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
    w, h = px1 - px0 + 2 * pad, py1 - py0 + 2 * pad
    w, h = (h * aspect, h) if w / h < aspect else (w, w / aspect)
    w, h = min(w, maxx - minx), min(h, maxy - miny)
    cx, cy = min(max(cx, minx + w / 2), maxx - w / 2), min(max(cy, miny + h / 2), maxy - h / 2)
    minx, maxx, miny, maxy = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])

    def draw(g, z=2, **kw):
        for p in _polys(g):
            ax.add_patch(MplPoly(np.asarray(p.exterior.coords), closed=True, zorder=z, **kw))
            for h in p.interiors:
                ax.add_patch(MplPoly(np.asarray(h.coords), closed=True, zorder=z, facecolor=(1, 1, 1, 0), edgecolor=kw.get("edgecolor", "k"), lw=kw.get("lw", 1)))

    for w in wet:
        draw(w["geom"], facecolor=(0.25, 0.75, 0.5, 0.35), edgecolor="#1f8a55", lw=1.0, ls="--")
    for f in fl:
        if f.get("sfha") == "T":
            draw(f["geom"], facecolor=(0.2, 0.45, 1, 0.15), edgecolor="#3a6fe0", lw=1.0, ls="--")
    if env is not None:
        draw(env, fill=False, edgecolor="#c9a400", lw=1.0, ls=(0, (6, 4)), z=3)
    draw(parcel, fill=False, edgecolor="#d020d8", lw=2.4, z=6)
    draw(sp["pave"], facecolor="#5d6166", edgecolor="#2a2c2f", lw=0.8, z=4)
    draw(sp["walk"], facecolor="#d9d4c7", edgecolor="#8c877a", lw=0.5, z=4)
    for a in sp["ada"]:
        draw(a, facecolor="#2f6fd0", edgecolor="w", lw=0.4, z=5)
    for ln in sp["stripes"]:
        xs, ys = ln.xy
        ax.plot(xs, ys, color="w", lw=0.45, zorder=5)
    if sp.get("pond") is not None:
        draw(sp["pond"], facecolor="#7fb7d9", edgecolor="#2d6f99", lw=1.2, z=4)
    for b in sp["bldgs"]:
        draw(b, facecolor="#f2c49b", edgecolor="#7a3e12", lw=1.4, z=6)

    span = maxx - minx
    ppu = 0.76 * 17 * 72 / span  # points per drawing unit
    dev = sp["pave"].centroid
    placed = []
    for t in labels:
        txt = "\n".join(t["lines"])
        chars = max(len(x) for x in t["lines"])
        if t.get("small"):
            ax.text(t["at"][0], t["at"][1], txt, ha="center", va="center", fontsize=5.5, weight="bold", color="#14445f", zorder=8)
            continue
        w_pt, d_pt = t["w_ft"] / k_ft * ppu, t["d_ft"] / k_ft * ppu
        long_pt, short_pt = max(w_pt, d_pt), min(w_pt, d_pt)
        fs = min(8.0, long_pt / (chars * 0.62), short_pt / (len(t["lines"]) * 1.35))
        if fs >= 5.0:
            rot = _upright(t["rot"] if w_pt >= d_pt else t["rot"] + 90)
            ax.text(t["at"][0], t["at"][1], txt, ha="center", va="center", fontsize=fs, weight="bold", rotation=rot,
                    rotation_mode="anchor", zorder=8, linespacing=1.15)
        else:  # too small to hold its label: callout with a leader, pushed away from the development
            dx, dy = t["at"][0] - dev.x, t["at"][1] - dev.y
            L = math.hypot(dx, dy) or 1
            off = span * 0.09
            tx, ty = t["at"][0] + dx / L * off, t["at"][1] + dy / L * off
            for px, py in placed:
                if math.hypot(tx - px, ty - py) < span * 0.06:
                    ty += span * 0.06
            placed.append((tx, ty))
            ax.annotate(txt, xy=t["at"], xytext=(tx, ty), ha="center", va="center", fontsize=7, weight="bold", zorder=9,
                        arrowprops=dict(arrowstyle="-", color="k", lw=0.8, shrinkA=0, shrinkB=2),
                        bbox=dict(boxstyle="square,pad=0.3", fc="w", ec="k", lw=0.6))

    # north arrow + scale bar
    span_ft = span * k_ft
    bar_ft = [50, 100, 200, 250, 500, 1000][int(np.searchsorted([300, 700, 1400, 2000, 4000, 1e9], span_ft))]
    bx, by = minx + span * 0.04, miny + (maxy - miny) * 0.04
    blen, hh = bar_ft / k_ft, (maxy - miny) * 0.045
    ax.add_patch(plt.Rectangle((bx - blen * 0.08, by - hh * 0.35), blen * 1.16, hh, color=(1, 1, 1, 0.9), zorder=9))
    ax.plot([bx, bx + blen], [by, by], color="k", lw=3, zorder=10)
    ax.plot([bx, bx + blen / 2], [by, by], color="w", lw=1.6, zorder=11)
    ax.text(bx + blen / 2, by + hh * 0.2, f"0 — {bar_ft} FT", ha="center", va="bottom", fontsize=8, zorder=11)
    nx, ny = maxx - span * 0.05, maxy - (maxy - miny) * 0.09
    ax.annotate("N", xy=(nx, ny + (maxy - miny) * 0.05), xytext=(nx, ny), ha="center", fontsize=12, color="k", weight="bold",
                arrowprops=dict(arrowstyle="-|>", color="k", lw=2), zorder=11)

    # info panel
    ip = fig.add_axes([0.795, 0.04, 0.19, 0.92])
    ip.axis("off")
    ip.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ip.transAxes, fill=False, lw=1.0, color="#333"))
    y = 0.97
    ip.text(0.06, y, "CONCEPT SITE PLAN", fontsize=11, weight="bold", va="top", transform=ip.transAxes)
    y -= 0.04
    for t in title_lines:
        ip.text(0.06, y, t, fontsize=9, va="top", transform=ip.transAxes)
        y -= 0.026
    y -= 0.015
    m = metrics
    rows = [("Concept", program.get("use") or "—")]
    for b in m["buildings"]:
        rows.append((b["name"] or "Building", f"{b['use']} · {b['w_ft']}' x {b['d_ft']}' · {b['footprint_sf']:,.0f} SF"
                     + (" · drive-thru" if b["drive_thru"] else "")))
    zon = " ".join(x for x in [rules.get("district"), rules.get("jurisdiction") and f"({rules['jurisdiction']})"] if x) or "—"
    sb = m.get("setbacks_ft") or {}
    rows += [("Zoning", zon), ("Building setbacks F / S / R", f"{sb.get('front', '—')}' / {sb.get('side', '—')}' / {sb.get('rear', '—')}'"),
             ("Parking provided / required", f"{m['stalls']} / {m['stalls_required']} (incl. {m['ada_stalls']} ADA)"),
             ("Total building area", f"{m['gfa_sf']:,.0f} SF"),
             ("Impervious", f"{m['impervious_sf']:,.0f} SF ({m['impervious_pct']}%)"),
             ("Lot coverage · FAR", f"{m['lot_coverage_pct']}% · {m['far']}"),
             ("Stormwater (approx.)", f"{m['pond_sf']:,.0f} SF"),
             ("Remaining developable land", f"{m['remaining_developable_acres']} ac")]
    for k, v in rows:
        ip.text(0.06, y, k.upper(), fontsize=6.3, color="#666", va="top", transform=ip.transAxes)
        lines = textwrap.wrap(str(v), 32) or [""]
        ip.text(0.06, y - 0.016, "\n".join(lines[:3]), fontsize=8.8, weight="bold", va="top", transform=ip.transAxes, linespacing=1.2)
        y -= 0.045 + 0.018 * (min(len(lines), 3) - 1)
    if m.get("warnings"):
        ip.text(0.06, y, "\n".join(textwrap.wrap("NOTE: " + "; ".join(m["warnings"]), 40)), fontsize=7, color="#b03000",
                va="top", transform=ip.transAxes)
    ip.text(0.06, 0.04, "Concept only — not a survey or engineered plan.\nParcel: county GIS · Aerial: USDA NAIP\nWetlands: USFWS NWI · Flood: FEMA NFHL\n"
            "Zoning per ordinance research; verify with\nthe jurisdiction before relying on it.",
            fontsize=6.3, color="#555", va="bottom", transform=ip.transAxes)
    fig.savefig(pdf_path)
    fig.savefig(png_path, dpi=90)
    plt.close(fig)
