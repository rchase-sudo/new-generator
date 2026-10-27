"""3D model + render inputs for the approved layout (same process as the Pocahontas Trail model):

  layout geometry + a wide NAIP aerial  ->  scene.json + textures (this module, plain Python)
  ->  Blender / Cycles scene (blender_scene.py, run as a separate process with bpy)
  ->  front 3/4 aerial view PNG  ->  Nano Banana (edge function `pipeline-banana`) for the photoreal pass.

Everything is in metres around the development centroid; +x east, +y north.
"""
import json
import math
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from .ec_drawing import _polys

FT = 0.3048


def _ring_coords(g):
    out = []
    for p in _polys(g):
        out.append(list(p.exterior.coords))
        out += [list(r.coords) for r in p.interiors]
    return out


def forest_mask(img):
    """Tree canopy in an aerial: darker-than-typical pixels that lean green (NAIP or other natural colour)."""
    a = np.asarray(img.filter(ImageFilter.GaussianBlur(2.5))).astype(float)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    lum = (r + g + b) / 3
    thr = min(85.0, np.percentile(lum, 50))
    m = ((lum < thr) & (g >= r * 0.92) & (g >= b * 0.9)).astype(np.uint8) * 255
    im = Image.fromarray(m).filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(7))
    return np.asarray(im) > 127


def prepare(work, parcel_sp, k_ft, aerial, rds, lay, program=None, seed=3):
    """Write scene.json, ground.jpg, lawn.png and site masks into `work`. Returns the scene dict."""
    os.makedirs(work, exist_ok=True)
    sp = lay["sp"]
    mpu = k_ft * FT                                   # metres per state-plane unit
    dev = sp["dev"]
    ox, oy = dev.centroid.x, dev.centroid.y
    W = lambda x, y: [round((x - ox) * mpu, 3), round((y - oy) * mpu, 3)]  # noqa: E731
    Wg = lambda g: [[W(x, y) for x, y in ring] for ring in _ring_coords(g)]  # noqa: E731

    # ---------------- ground: the aerial as texture, lawn where the development clears the woods
    img = aerial["image"].convert("RGB")
    minx, miny, maxx, maxy = aerial["extent"]
    AW, AH = img.size
    px = lambda x, y: ((x - minx) / (maxx - minx) * AW, (maxy - y) / (maxy - miny) * AH)  # noqa: E731
    # demolished buildings disappear from the draped aerial: fill each footprint with the colour of the ground around it
    demo = [e["geom"] for e in lay.get("existing") or [] if e["action"] == "demo"]
    if demo:
        a = np.asarray(img).astype(float)
        for g in demo:
            ring = g.buffer(14 / k_ft).difference(g.buffer(5 / k_ft))
            m_in, m_ring = Image.new("L", (AW, AH), 0), Image.new("L", (AW, AH), 0)
            for p_ in _polys(g.buffer(4 / k_ft)):
                ImageDraw.Draw(m_in).polygon([px(x, y) for x, y in p_.exterior.coords], fill=255)
            for p_ in _polys(ring):
                ImageDraw.Draw(m_ring).polygon([px(x, y) for x, y in p_.exterior.coords], fill=255)
            ri = np.asarray(m_ring) > 127
            if ri.sum() < 10:
                continue
            fill = np.median(a[ri], axis=0)
            rng_ = np.random.default_rng(7)
            soft = np.asarray(m_in.filter(ImageFilter.GaussianBlur(2))).astype(float)[..., None] / 255.0
            noise = rng_.normal(0, 4.0, a.shape[:2])[..., None]
            a = a * (1 - soft) + (fill[None, None, :] + noise) * soft
        img = Image.fromarray(a.clip(0, 255).astype("uint8"))
    img.save(os.path.join(work, "ground.jpg"), quality=92)
    pad = sp.get("pad") or sp.get("grass")
    clear = unary_union([dev.buffer(45 / k_ft), pad if pad is not None else dev]).intersection(parcel_sp.buffer(5 / k_ft))
    lawn = Image.new("L", (AW, AH), 0)
    d = ImageDraw.Draw(lawn)
    for p in _polys(clear):
        d.polygon([px(x, y) for x, y in p.exterior.coords], fill=255)
    lawn.filter(ImageFilter.GaussianBlur(3)).save(os.path.join(work, "lawn.png"))

    # ---------------- trees: woods from the aerial + the plan's ornamental trees
    fm = forest_mask(img)
    road_u = unary_union([r["pavement"].buffer(15 / k_ft) for r in rds]) if rds else None
    keep_out = clear.buffer(8 / k_ft)
    if road_u is not None:
        keep_out = keep_out.union(road_u)
    rng = np.random.default_rng(seed)
    step_ft = 26.0
    step_px = step_ft / k_ft / (maxx - minx) * AW
    trees = []
    arr = np.asarray(img)
    from shapely.prepared import prep
    ko = prep(keep_out)
    for v in np.arange(0, AH, step_px):
        for u in np.arange(0, AW, step_px):
            uu, vv = u + rng.uniform(0, step_px), v + rng.uniform(0, step_px)
            ui, vi = int(min(uu, AW - 1)), int(min(vv, AH - 1))
            if not fm[vi, ui]:
                continue
            x = minx + uu / AW * (maxx - minx)
            y = maxy - vv / AH * (maxy - miny)
            if ko.contains(Point(x, y)):
                continue
            c = arr[vi, ui]
            trees.append(W(x, y) + [int(c[0]), int(c[1]), int(c[2]), round(float(rng.uniform(2.6, 5.4)), 2)])
    orn = []
    for t in sp.get("trees") or []:
        c = t.centroid
        r_ft = math.sqrt(t.area / math.pi) * k_ft
        orn.append(W(c.x, c.y) + [round(max(1.4, r_ft / 4.2), 2)])

    # ---------------- site surface masks (asphalt / concrete / islands / paint / water)
    sx0, sy0, sx1, sy1 = dev.buffer(30 / k_ft).bounds
    span_ft = max(sx1 - sx0, sy1 - sy0) * k_ft
    res_ft = max(0.12, span_ft / 5000.0)              # ft per pixel
    SW_, SH_ = int((sx1 - sx0) * k_ft / res_ft), int((sy1 - sy0) * k_ft / res_ft)
    sp_px = lambda x, y: ((x - sx0) * k_ft / res_ft, (sy1 - y) * k_ft / res_ft)  # noqa: E731

    def mask(geoms, name, lines=None, lw_ft=0.33):
        m = Image.new("L", (SW_, SH_), 0)
        dd = ImageDraw.Draw(m)
        for g in geoms:
            for p in _polys(g):
                dd.polygon([sp_px(x, y) for x, y in p.exterior.coords], fill=255)
                for h in p.interiors:
                    dd.polygon([sp_px(x, y) for x, y in h.coords], fill=0)
        for ln in lines or []:
            dd.line([sp_px(x, y) for x, y in ln.coords], fill=255, width=max(2, int(round(lw_ft / res_ft))))
        m.save(os.path.join(work, name))

    bldg_u = unary_union([b for b in sp["bldgs"] if b is not None])
    pave, walk = sp["pave"], sp["walk"]
    island = (pad if pad is not None else dev).difference(pave).difference(walk).difference(bldg_u)
    mask([pave], "m_asphalt.png")
    mask([walk], "m_concrete.png")
    mask([island], "m_island.png")
    mask([], "m_stripe.png", lines=sp.get("stripes") or [])

    # ---------------- buildings: storefront on the faces that look at the parking
    stalls = unary_union(sp.get("stalls") or []) if sp.get("stalls") else pave
    blds = []
    gfa = {b.get("name"): b for b in (lay["metrics"].get("buildings") or [])}
    for i, b in enumerate(sp["bldgs"]):
        if b is None:
            continue
        poly = list(b.exterior.coords)[:-1]
        cx, cy = b.centroid.x, b.centroid.y
        near = stalls.buffer(0).centroid if not stalls.is_empty else pave.centroid
        # face direction: toward the nearest walk/stall area
        tgt = b.buffer(25 / k_ft).difference(b).intersection(pave)
        tp = tgt.centroid if not tgt.is_empty else near
        fv = np.array([tp.x - cx, tp.y - cy])
        fv = (fv / (np.linalg.norm(fv) or 1)).tolist()
        stories = 1
        mb = list(gfa.values())[i] if i < len(gfa) else {}
        if isinstance(mb, dict):
            stories = int(mb.get("stories") or 1)
        H = 6.6 + 4.2 * (stories - 1)
        rb = (sp.get("roof_blocks") or [None] * 99)[i] if i < len(sp.get("roof_blocks") or []) else None
        blds.append({"poly": [W(x, y) for x, y in poly], "H": H, "fronts": [fv],
                     "entry": [W(x, y) for x, y in list(rb.exterior.coords)[:-1]] if rb is not None else None,
                     "entry_H": H + 2.2})

    kept = []
    for e in lay.get("existing") or []:
        if e["action"] == "keep" and e.get("kind") != "pavement":
            kept.append({"poly": [W(x, y) for x, y in list(e["geom"].exterior.coords)[:-1]], "H": 4.8})

    # ---------------- camera: from the road side, 3/4 aerial like the Pocahontas front view
    a = math.radians(lay.get("frame_angle_deg") or 0.0)
    dx0, dy0, dx1, dy1 = dev.bounds
    S = max(dx1 - dx0, dy1 - dy0) * mpu
    # a long, narrow development (a strip along a road or rail line): turn the camera so the strip runs ACROSS the frame and
    # fill the frame with it. Framed like a square site it is a thin sliver in a sea of surroundings, and the photoreal pass
    # then invents the whole scene instead of following the model.
    try:
        mc = list(dev.minimum_rotated_rectangle.exterior.coords)[:4]
        e0 = (mc[1][0] - mc[0][0], mc[1][1] - mc[0][1])
        e1 = (mc[2][0] - mc[1][0], mc[2][1] - mc[1][1])
        l0, l1 = math.hypot(*e0), math.hypot(*e1)
        long_v, L_, W_ = (e0, l0, l1) if l0 >= l1 else (e1, l1, l0)
        if W_ > 0 and L_ / W_ >= 2.5:
            a = math.atan2(long_v[1], long_v[0])
            if rds:
                ru = unary_union([r["pavement"] for r in rds])
                rp = min(_polys(ru) or [ru], key=lambda q: q.distance(dev.centroid)) if not ru.is_empty else None
                if rp is not None:
                    from shapely.ops import nearest_points as _np2
                    q_ = _np2(rp, dev.centroid)[0]
                    if (-math.sin(a)) * (q_.x - dev.centroid.x) + math.cos(a) * (q_.y - dev.centroid.y) > 0:
                        a += math.pi          # the road must be on the camera side
            S = max(0.62 * L_ * mpu, W_ * mpu * 3.0)
    except Exception:  # noqa: BLE001
        pass
    n_in = np.array([-math.sin(a), math.cos(a)])        # into the site
    t_al = np.array([math.cos(a), math.sin(a)])         # along the frontage
    dist = 1.75 * S + 45.0
    el = math.radians(40)           # steep enough that no horizon shows: the frame is all ground, like a real drone shot
    cam_xy = -n_in * dist * math.cos(el) + t_al * dist * 0.28
    # the lot: only this area may change in the photoreal pass (parcel + the driveway out to the road)
    # only the developed area (plus a margin and the driveway), never the whole parcel: on a long parcel the rest stays
    # exactly as the model draws it (woods), so the photoreal pass cannot invent buildings or parking there
    lot_parts = [dev.buffer(30 / k_ft).intersection(parcel_sp.buffer(5 / k_ft))]
    if sp.get("drive") is not None:
        lot_parts.append(sp["drive"].buffer(6 / k_ft))
    lot = unary_union(lot_parts).buffer(0)
    scene = {
        "lot": Wg(lot),
        "ground": {"corners": [W(minx, miny), W(maxx, miny), W(maxx, maxy), W(minx, maxy)]},
        "site_rect": [W(sx0, sy0), W(sx1, sy1)],
        "curbs": Wg(pave),
        "buildings": blds,
        "existing": kept,
        "pond": Wg(sp["pond"]) if sp.get("pond") is not None else [],
        "trees": trees,
        "ornamental": orn,
        "camera": {"pos": [round(float(cam_xy[0]), 2), round(float(cam_xy[1]), 2), round(dist * math.sin(el), 2)],
                   "target": [0.0, 0.0, 0.0], "lens": 32},
        "sun": {"az_deg": (math.degrees(math.atan2(-n_in[0], -n_in[1])) + 35) % 360, "el_deg": 24},   # low golden-hour sun: long soft shadows
        "program": (program or {}).get("use"),
    }
    with open(os.path.join(work, "scene.json"), "w") as f:
        json.dump(scene, f)
    return scene


def render(work, width=1600, height=1000, samples=48, timeout=1500):
    """Run the Blender scene script in its own process (bpy wants a fresh interpreter)."""
    script = os.path.join(os.path.dirname(__file__), "blender_scene.py")
    out = os.path.join(work, "model_front.png")
    cmd = [sys.executable, script, work, str(width), str(height), str(samples), out]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 or not os.path.exists(out):
        raise RuntimeError(f"Blender render failed: {(p.stderr or p.stdout)[-1500:]}")
    return out
