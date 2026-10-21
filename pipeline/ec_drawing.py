"""Existing-conditions drawing: scaled DXF (true state-plane coordinates) + PDF sheet + summary.

Layer names follow Forma's CAD standard so the DXF drops straight into Civil 3D / the layout step.
"""
import math
import os

import ezdxf
import matplotlib
import numpy as np
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.ops import unary_union

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Polygon as MplPoly  # noqa: E402

LAYERS = {
    "E-PL-LINE": 6,        # property line (magenta)
    "E-SETBACK-PL": 2,     # zoning setback (yellow)
    "E-BLDG-ENV": 3,       # buildable envelope (green)
    "E-WET-EDGE": 3,       # wetland limit
    "E-WET-HATCH": 3,
    "E-FLOOD": 5,          # FEMA flood zone (blue)
    "E-ROAD-CL": 1,        # road centerline (red)
    "E-PAVE-EDGE": 8,      # existing edge of pavement (approximate)
    "E-TEXT": 7,
    "E-AERIAL": 7,
}


# ---------------------------------------------------------------- geometry analysis
def _polys(g):
    if g.is_empty:
        return []
    return list(g.geoms) if isinstance(g, MultiPolygon) else [g] if isinstance(g, Polygon) else [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]


def classify_edges(parcel, road_lines, k_ft):
    """Label each exterior edge front / side / rear. Front = within 150 ft of a road and roughly parallel to it."""
    coords = list(parcel.exterior.coords)
    if parcel.exterior.is_ccw:
        coords = coords[::-1]  # clockwise: outward normal is to the left of travel
    edges = []
    for a, b in zip(coords, coords[1:]):
        seg = LineString([a, b])
        if seg.length * k_ft < 1:
            continue
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        n = (-dy / L, dx / L)  # outward normal for clockwise ring
        e = {"line": seg, "normal": n, "len_ft": L * k_ft, "kind": "side"}
        mid = seg.interpolate(0.5, normalized=True)
        for rl in road_lines:
            if mid.distance(rl) * k_ft < 150:
                p = rl.interpolate(rl.project(mid))
                q = rl.interpolate(min(rl.length, rl.project(mid) + 1.0))
                tx, ty = q.x - p.x, q.y - p.y
                tl = math.hypot(tx, ty) or 1
                if abs((dx * tx + dy * ty) / (L * tl)) > 0.8:
                    e["kind"] = "front"
                    break
        edges.append(e)
    fronts = [e for e in edges if e["kind"] == "front"]
    if fronts:
        fx = sum(e["normal"][0] * e["len_ft"] for e in fronts)
        fy = sum(e["normal"][1] * e["len_ft"] for e in fronts)
        fl = math.hypot(fx, fy) or 1
        for e in edges:
            if e["kind"] != "front" and (e["normal"][0] * fx + e["normal"][1] * fy) / fl < -0.6:
                e["kind"] = "rear"
    return edges


def envelope(parcel, edges, setbacks_ft, k_ft):
    """Buildable envelope = parcel minus per-edge setback strips."""
    cuts = []
    for e in edges:
        d = (setbacks_ft or {}).get(e["kind"])
        if d:
            # extend each edge past its ends so neighbouring strips overlap at vertices (no slivers, sharp corners)
            (x0, y0), (x1, y1) = e["line"].coords[0], e["line"].coords[-1]
            L = math.hypot(x1 - x0, y1 - y0) or 1
            ux, uy, ext = (x1 - x0) / L, (y1 - y0) / L, d / k_ft
            seg = LineString([(x0 - ux * ext, y0 - uy * ext), (x1 + ux * ext, y1 + uy * ext)])
            cuts.append(seg.buffer(d / k_ft, cap_style=2, join_style=2))
    env = parcel.difference(unary_union(cuts)) if cuts else parcel
    return env.buffer(0)


# ---------------------------------------------------------------- DXF
def write_dxf(path, parcel, env, setback_line, wet, fl, rds, aerial_img_name, aerial_extent, aerial_px, labels):
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 2  # feet
    doc.header["$MEASUREMENT"] = 0
    for name, color in LAYERS.items():
        lay = doc.layers.add(name, color=color)
        if name in ("E-SETBACK-PL", "E-WET-EDGE"):
            lay.dxf.linetype = "DASHED"
        if name == "E-ROAD-CL":
            lay.dxf.linetype = "CENTER"
    msp = doc.modelspace()

    if aerial_img_name:
        minx, miny, maxx, maxy = aerial_extent
        idef = doc.add_image_def(filename=aerial_img_name, size_in_pixel=aerial_px)
        img = msp.add_image(idef, insert=(minx, miny), size_in_units=(maxx - minx, maxy - miny), rotation=0, dxfattribs={"layer": "E-AERIAL"})
        img.dxf.flags = 15  # show image, show when not aligned, use clipping, transparency
        msp_img_order = img  # noqa: F841

    def ring(r, layer, width=0.0, closed=True):
        pts = list(r.coords)[:-1] if closed else list(r.coords)
        msp.add_lwpolyline(pts, close=closed, dxfattribs={"layer": layer, "const_width": width})

    for p in _polys(parcel):
        ring(p.exterior, "E-PL-LINE", width=2.0)
    if setback_line is not None:
        for p in _polys(setback_line):
            ring(p.exterior, "E-SETBACK-PL", width=1.0)
    for p in _polys(env):
        ring(p.exterior, "E-BLDG-ENV")
    for w in wet:
        for p in _polys(w["geom"]):
            ring(p.exterior, "E-WET-EDGE")
            h = msp.add_hatch(color=3, dxfattribs={"layer": "E-WET-HATCH"})
            h.set_pattern_fill("DOTS", scale=4.0)
            h.paths.add_polyline_path(list(p.exterior.coords)[:-1], is_closed=True)
    for f in fl:
        for p in _polys(f["geom"]):
            ring(p.exterior, "E-FLOOD")
    for r in rds:
        cl = r["centerline"]
        for ln in getattr(cl, "geoms", [cl]):
            msp.add_lwpolyline(list(ln.coords), dxfattribs={"layer": "E-ROAD-CL"})
        for p in _polys(r["pavement_clip"]):
            ring(p.exterior, "E-PAVE-EDGE")
    for t in labels:
        msp.add_mtext(t["text"], dxfattribs={"layer": "E-TEXT", "char_height": t.get("h", 4.0), "insert": t["at"],
                                              "attachment_point": 5, "rotation": t.get("rot", 0)})
    doc.saveas(path)


# ---------------------------------------------------------------- PDF / PNG sheet
def write_sheet(pdf_path, png_path, parcel, env, setback_line, wet, fl, rds, aerial, k_ft, title_lines, facts):
    minx, miny, maxx, maxy = aerial["extent"]
    W_in, H_in = 17.0, 11.0
    fig = plt.figure(figsize=(W_in, H_in), dpi=150)
    ax = fig.add_axes([0.02, 0.04, 0.76, 0.92])
    ax.imshow(np.asarray(aerial["image"]), extent=(minx, maxx, miny, maxy), interpolation="bilinear")
    ax.set_xlim(minx, maxx)
    ax.set_ylim(miny, maxy)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])

    def draw(g, **kw):
        for p in _polys(g):
            ax.add_patch(MplPoly(np.asarray(p.exterior.coords), closed=True, **kw))

    for f in fl:
        if f.get("sfha") == "T":
            draw(f["geom"], fill=True, facecolor=(0.2, 0.45, 1, 0.18), edgecolor=(0.3, 0.55, 1, 0.9), lw=1.0, ls="--")
    for w in wet:
        draw(w["geom"], fill=True, facecolor=(0.2, 0.9, 0.6, 0.22), edgecolor=(0.3, 1, 0.65, 0.95), lw=1.2, ls="--")
    for r in rds:
        for ln in getattr(r["centerline"], "geoms", [r["centerline"]]):
            xs, ys = ln.xy
            ax.plot(xs, ys, color=(1, 0.35, 0.3, 0.8), lw=0.8, ls=(0, (10, 3, 2, 3)))
    if setback_line is not None:
        draw(setback_line, fill=False, edgecolor="#f5d33c", lw=1.4, ls=(0, (6, 4)))
    draw(parcel, fill=False, edgecolor="#e040e8", lw=2.6)

    # north arrow + scale bar
    span_ft = (maxx - minx) * k_ft
    bar_ft = [50, 100, 200, 250, 500, 1000][int(np.searchsorted([300, 700, 1400, 2000, 4000, 1e9], span_ft))]
    bx, by = minx + (maxx - minx) * 0.04, miny + (maxy - miny) * 0.04
    blen = bar_ft / k_ft
    hh = (maxy - miny) * 0.045
    ax.add_patch(plt.Rectangle((bx - blen * 0.08, by - hh * 0.35), blen * 1.16, hh, color=(1, 1, 1, 0.85), zorder=5))
    ax.plot([bx, bx + blen], [by, by], color="k", lw=3, zorder=6)
    ax.plot([bx, bx + blen / 2], [by, by], color="w", lw=1.6, zorder=7)
    ax.text(bx + blen / 2, by + hh * 0.2, f"0 — {bar_ft} FT", ha="center", va="bottom", fontsize=8, zorder=7)
    nx, ny = maxx - (maxx - minx) * 0.05, maxy - (maxy - miny) * 0.09
    ax.annotate("N", xy=(nx, ny + (maxy - miny) * 0.05), xytext=(nx, ny), ha="center", fontsize=12, color="w", weight="bold",
                arrowprops=dict(arrowstyle="-|>", color="w", lw=2))

    # info panel
    ip = fig.add_axes([0.795, 0.04, 0.19, 0.92])
    ip.axis("off")
    ip.add_patch(plt.Rectangle((0, 0), 1, 1, transform=ip.transAxes, fill=False, lw=1.0, color="#333"))
    y = 0.97
    ip.text(0.06, y, "EXISTING CONDITIONS", fontsize=11, weight="bold", va="top", transform=ip.transAxes)
    y -= 0.04
    for t in title_lines:
        ip.text(0.06, y, t, fontsize=9, va="top", transform=ip.transAxes)
        y -= 0.028
    y -= 0.02
    for k, v in facts:
        ip.text(0.06, y, k.upper(), fontsize=6.5, color="#666", va="top", transform=ip.transAxes)
        ip.text(0.06, y - 0.017, str(v), fontsize=9.5, weight="bold", va="top", transform=ip.transAxes)
        y -= 0.05
    y -= 0.01
    leg = [("#e040e8", "-", "Property line"), ("#f5d33c", "--", "Zoning setback"), ("#3cf0a0", "--", "Wetlands (NWI)"),
           ("#4d8cff", "--", "FEMA flood zone (SFHA)"), ("#ff5a4d", "-.", "Road centerline (OSM)")]
    for c, ls, lab in leg:
        ip.plot([0.06, 0.18], [y, y], color=c, lw=2, ls=ls, transform=ip.transAxes)
        ip.text(0.22, y, lab, fontsize=8, va="center", transform=ip.transAxes)
        y -= 0.03
    ip.text(0.06, 0.05, f"Aerial: {aerial.get('source','NAIP')} (USDA NAIP)\nWetlands: USFWS NWI (not a delineation)\n"
            "Flood: FEMA NFHL · Roads: OpenStreetMap\nPreliminary — not a survey. Not for construction.",
            fontsize=6.5, color="#555", va="bottom", transform=ip.transAxes)
    fig.savefig(pdf_path)
    fig.savefig(png_path, dpi=80)
    plt.close(fig)


# ---------------------------------------------------------------- main entry
def build(out_dir, parcel_sp, epsg, k_ft, aerial, wet, fl, rds, setbacks_ft=None, listing=None):
    os.makedirs(out_dir, exist_ok=True)
    wet = [] if isinstance(wet, dict) else wet
    fl = [] if isinstance(fl, dict) else fl
    rds = [] if isinstance(rds, dict) else rds
    ext = box(*aerial["extent"])
    for r in rds:
        r["pavement_clip"] = r["pavement"].intersection(ext)
    road_lines = [r["centerline"] for r in rds if r["class"] not in ("service",)]
    edges = classify_edges(parcel_sp, road_lines, k_ft)
    env = envelope(parcel_sp, edges, setbacks_ft, k_ft)
    setback_line = env if setbacks_ft else None
    wet_in = unary_union([w["geom"] for w in wet]).intersection(parcel_sp) if wet else Polygon()
    sfha = unary_union([f["geom"] for f in fl if f.get("sfha") == "T"]).intersection(parcel_sp) if fl else Polygon()
    buildable = env.difference(wet_in).difference(sfha)
    ac = lambda g: g.area * k_ft * k_ft / 43560.0  # noqa: E731
    front_ft = sum(e["len_ft"] for e in edges if e["kind"] == "front")
    fronting = sorted({r["name"] for r in rds if r["name"] and r["centerline"].distance(parcel_sp) * k_ft < 150})

    aerial_name = "aerial.jpg"
    aerial["image"].save(os.path.join(out_dir, aerial_name), quality=90)
    with open(os.path.join(out_dir, "aerial.jgw"), "w") as f:  # world file so GIS/CAD can also place the JPG
        minx, miny, maxx, maxy = aerial["extent"]
        W, H = aerial["image"].size
        px, py = (maxx - minx) / W, (maxy - miny) / H
        f.write(f"{px}\n0\n0\n{-py}\n{minx + px / 2}\n{maxy - py / 2}\n")

    c = parcel_sp.representative_point()
    labels = [{"text": f"PARCEL\\P{ac(parcel_sp):.2f} AC ±", "at": (c.x, c.y), "h": 8 / k_ft}]
    for r in rds:
        if r["name"] and r["centerline"].distance(parcel_sp) * k_ft < 300:
            ln = r["centerline"] if r["centerline"].geom_type == "LineString" else max(r["centerline"].geoms, key=lambda g: g.length)
            p = ln.interpolate(ln.project(c))
            q = ln.interpolate(min(ln.length, ln.project(c) + 1))
            rot = math.degrees(math.atan2(q.y - p.y, q.x - p.x))
            rot = rot - 180 if rot > 90 else rot + 180 if rot < -90 else rot
            labels.append({"text": r["name"].upper(), "at": (p.x, p.y), "h": 7 / k_ft, "rot": rot})

    dxf_path = os.path.join(out_dir, "existing_conditions.dxf")
    write_dxf(dxf_path, parcel_sp, env, setback_line, wet, fl, rds, aerial_name, aerial["extent"], aerial["image"].size, labels)

    L = listing or {}
    title = [x for x in [L.get("street_address") and f"{L.get('lot') + ', ' if L.get('lot') else ''}{L.get('street_address')}",
                         ", ".join(x for x in [L.get("city"), L.get("state")] if x), L.get("county") and f"{L['county']} County"] if x]
    facts = [("Parcel area (GIS)", f"{ac(parcel_sp):.2f} ac"), ("Listing area", f"{L['acres']} ac" if L.get("acres") else "—"),
             ("Zoning", L.get("zoning") or "—"), ("Road frontage", f"{front_ft:,.0f} ft" + (f" · {', '.join(fronting)}" if fronting else "")),
             ("Mapped wetlands on parcel", f"{ac(wet_in):.2f} ac"), ("FEMA flood (SFHA) on parcel", f"{ac(sfha):.2f} ac"),
             ("Buildable (envelope − wetland − flood)", f"{ac(buildable):.2f} ac" if setbacks_ft else "setbacks pending")]
    write_sheet(os.path.join(out_dir, "existing_conditions.pdf"), os.path.join(out_dir, "existing_conditions.png"),
                parcel_sp, env, setback_line, wet, fl, rds, aerial, k_ft, title, facts)

    summary = {
        "epsg": epsg, "parcel_acres": round(ac(parcel_sp), 3), "frontage_ft": round(front_ft), "fronting_roads": fronting,
        "wetland_acres": round(ac(wet_in), 3), "flood_sfha_acres": round(ac(sfha), 3),
        "envelope_acres": round(ac(env), 3) if setbacks_ft else None, "buildable_acres": round(ac(buildable), 3) if setbacks_ft else None,
        "edges": [{"kind": e["kind"], "len_ft": round(e["len_ft"], 1)} for e in edges],
        "flood_zones": sorted({f["zone"] for f in fl if f.get("zone") and f["geom"].intersects(parcel_sp)}),
        "wetland_types": sorted({w["type"] for w in wet if w.get("type") and w["geom"].intersects(parcel_sp)}),
        "setbacks_ft": setbacks_ft, "aerial_source": aerial.get("source"),
    }
    return summary
