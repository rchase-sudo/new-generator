"""Job steps. Each step reads the job row, does its work, writes results back and sets the next status.

Status flow:
  queued -> parsing -> awaiting_parcel                      (checkpoint 1: user picks / confirms the parcel)
  parcel_approved -> building_ec -> ec_ready -> researching_zoning -> awaiting_zoning
                                                           (checkpoint 2: user confirms setbacks + program)
  zoning_approved -> building_layout -> awaiting_layout     (checkpoint 3: user approves the layout)
  layout_approved                                           (step 3: sheet / render / package / website)
Any failure -> error (message in `error`, step in `step`).
"""
import io
import math
import os
import pickle
import tempfile
import traceback

from shapely.geometry import box, mapping, shape

from . import context, supa
from .ec_drawing import build as build_ec
from .geo import geocode, state_plane_epsg, to_crs, units_to_ft
from .listing import parse_listing
from .parcel import find_parcels
from . import zoning as zoning_mod
from .layout2 import generate as generate_layout
from .concept_drawing import write_dxf as write_concept_dxf, write_sheet as write_concept_sheet
from .ec_drawing import classify_edges, envelope


def run(job_id, step):
    try:
        if step == "parse":
            step_parse(job_id)
        elif step == "ec":
            step_ec(job_id)
            step_zoning(job_id)
        elif step == "zoning":
            step_zoning(job_id)
        elif step == "layout":
            step_layout(job_id)
        elif step == "render":
            step_render(job_id)
        elif step == "render_composite":
            step_render_composite(job_id)
        else:
            raise ValueError(f"unknown step {step}")
    except Exception as e:  # noqa: BLE001
        if step in ("render", "render_composite"):   # a failed render never invalidates an approved plan
            supa.update_job(job_id, render_status="failed", render_error=f"{type(e).__name__}: {e}"[:1500])
        else:
            supa.update_job(job_id, status="error", error=f"{type(e).__name__}: {e}", step=step,
                            progress=traceback.format_exc()[-1500:])
        raise


def step_parse(job_id):
    job = supa.get_job(job_id)
    supa.update_job(job_id, status="parsing", step="parse", progress="Reading listing", error=None)
    listing = job.get("listing") or parse_listing(job["listing_text"])
    supa.update_job(job_id, listing=listing, progress="Locating property")
    lat, lon, label = geocode(listing)
    epsg = state_plane_epsg(lat, lon)
    k = units_to_ft(epsg)

    # roads around the geocode point help find the parcel that fronts the listing's road
    road_ll = None  # only needed (and only fetched) when the listing has no street number, below

    # No street number (e.g. "Pocahontas Trl, Lot 6"): the geocode is just somewhere on the road, so search the
    # whole named road near the town/ZIP and let acreage + frontage pick the parcel.
    corridor = None
    street = listing.get("street_address") or ""
    if not (street and street.split()[0][:1].isdigit()):
        supa.update_job(job_id, progress="Tracing the road named in the listing")
        town = geocode({"city": listing.get("city"), "state": listing.get("state"), "zip": listing.get("zip"),
                        "road_name": None, "street_address": None, "county": listing.get("county")})
        road_all = context.named_road_near(listing.get("road_name") or street, town[0], town[1], radius_m=15000)
        if road_all is not None:
            corridor = road_all.buffer(0.0018)  # ~150-200 m either side
            road_ll = road_all
            lat, lon = town[0], town[1]

    supa.update_job(job_id, progress="Searching parcels")
    res = find_parcels(listing, lat, lon, road_geom_ll=road_ll, county_layer=job.get("county_layer"), corridor_ll=corridor)
    if not res["candidates"]:
        raise RuntimeError("No parcels found near the listing location. Paste the parcel ID (APN) or adjust the address.")
    supa.update_job(job_id, status="awaiting_parcel", step="parse", progress="Confirm the parcel",
                    geocode={"lat": lat, "lon": lon, "label": label}, epsg=epsg,
                    candidates=res["candidates"], parcel_source=res["source"], county_layer=res["county_layer"], debug=res.get("debug"))


def _extent_around(geom, k_ft, pad_ft=250, min_ft=900):
    minx, miny, maxx, maxy = geom.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    w = max(maxx - minx + 2 * pad_ft / k_ft, min_ft / k_ft)
    h = max(maxy - miny + 2 * pad_ft / k_ft, min_ft / k_ft)
    # 17x11 drawing area aspect (~0.76*17 : 0.92*11)
    aspect = (0.76 * 17) / (0.92 * 11)
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def step_ec(job_id):
    job = supa.get_job(job_id)
    parcel = job.get("parcel")
    if not parcel:
        raise RuntimeError("No parcel selected")
    epsg = job.get("epsg") or state_plane_epsg(job["geocode"]["lat"], job["geocode"]["lon"])
    k = units_to_ft(epsg)
    supa.update_job(job_id, status="building_ec", step="ec", progress="Pulling aerial and site data", error=None)
    if parcel.get("combine"):  # several tax parcels picked on the approval screen -> one site
        from shapely.ops import unary_union
        geom = unary_union([shape(p["geometry"]) for p in parcel["parts"]]).buffer(0)
        parcel = {"source": parcel["source"], "geometry": mapping(geom), "assemblage": True,
                  "apn": ", ".join(str(p.get("apn")) for p in parcel["parts"]), "acres": sum(p.get("acres") or 0 for p in parcel["parts"])}
        supa.update_job(job_id, parcel=parcel)
    parcel_sp = to_crs(shape(parcel["geometry"]), 4326, epsg)
    ext = _extent_around(parcel_sp, k)
    aerial = context.aerial(ext, epsg, k)
    supa.update_job(job_id, progress="Wetlands, flood zones, roads")
    wet = context.wetlands(ext, epsg)
    fl = context.flood(ext, epsg)
    rds = context.roads(ext, epsg)
    warnings = [f"{n}: {v['error']}" for n, v in (("wetlands", wet), ("flood", fl), ("roads", rds)) if isinstance(v, dict)]

    supa.update_job(job_id, progress="Drawing")
    out = tempfile.mkdtemp()
    summary = build_ec(out, parcel_sp, epsg, k, aerial, wet, fl, rds, setbacks_ft=job.get("setbacks"), listing=job.get("listing"))
    summary["warnings"] = warnings
    files = {}
    for name in ("existing_conditions.dxf", "existing_conditions.pdf", "existing_conditions.png", "aerial.jpg", "aerial.jgw"):
        files[name] = supa.upload(job_id, os.path.join(out, name))
    # cache the site context so later steps (layout, re-layouts) don't re-fetch it
    ctx = {"parcel_sp": parcel_sp, "epsg": epsg, "k": k, "wet": wet, "fl": fl, "rds": rds, "warnings": warnings,
           "aerial": {"extent": aerial["extent"], "source": aerial.get("source"), "jpg": _jpg(aerial["image"])}}
    with open(os.path.join(out, "context.pkl"), "wb") as f:
        pickle.dump(ctx, f)
    supa.upload(job_id, os.path.join(out, "context.pkl"))
    outputs = {**(job.get("outputs") or {}), "ec": files}
    supa.update_job(job_id, status="ec_ready", step="ec", progress="Existing conditions ready", summary=summary, outputs=outputs)


def _jpg(img):
    b = io.BytesIO()
    img.save(b, "JPEG", quality=90)
    return b.getvalue()


def _load_ctx(job_id):
    from PIL import Image
    ctx = pickle.loads(supa.download(job_id, "context.pkl"))
    ctx["aerial"]["image"] = Image.open(io.BytesIO(ctx["aerial"]["jpg"])).convert("RGB")
    return ctx


def step_zoning(job_id):
    job = supa.get_job(job_id)
    supa.update_job(job_id, status="researching_zoning", step="zoning", progress="Researching the zoning ordinance", error=None)
    listing, summary = job.get("listing") or {}, job.get("summary") or {}
    rules = zoning_mod.research(listing, job.get("parcel"), summary)
    setbacks = job.get("setbacks") or zoning_mod.setbacks_from(rules)
    # buildable area with the researched setbacks, so the program is sized to the real envelope
    ctx = _load_ctx(job_id)
    edges = classify_edges(ctx["parcel_sp"], [r["centerline"] for r in (ctx["rds"] if isinstance(ctx["rds"], list) else []) if r["class"] != "service"], ctx["k"])
    env = envelope(ctx["parcel_sp"], edges, setbacks, ctx["k"])
    from shapely.ops import unary_union
    cons = [w["geom"] for w in (ctx["wet"] if isinstance(ctx["wet"], list) else [])] + \
           [f["geom"] for f in (ctx["fl"] if isinstance(ctx["fl"], list) else []) if f.get("sfha") == "T"]
    bld = env.difference(unary_union(cons)) if cons else env
    # the buildable piece that reaches the road (wetlands / streams can cut off the back of a site)
    from .ec_drawing import _polys
    road = unary_union([r["centerline"] for r in (ctx["rds"] if isinstance(ctx["rds"], list) else [])]) if ctx["rds"] else None
    pieces = [g for g in _polys(bld) if g.area * ctx["k"] ** 2 > 4000]
    front = min(pieces, key=lambda g: (round(g.distance(road) * ctx["k"] / 50), -g.area)) if pieces and road is not None and not road.is_empty else (max(pieces, key=lambda g: g.area) if pieces else bld)
    ac = lambda g: round(g.area * ctx["k"] ** 2 / 43560, 3)  # noqa: E731
    summary = {**summary, "setbacks_ft": setbacks, "envelope_acres": ac(env), "buildable_acres": ac(bld),
               "frontage_buildable_acres": ac(front)}
    rules = {**rules, "pavement_setbacks_ft": zoning_mod.pavement_setbacks(rules, setbacks)}
    supa.update_job(job_id, zoning=rules, setbacks=setbacks, summary=summary, progress="Choosing the development program")
    prog = job.get("program") if (job.get("program") or {}).get("locked") else zoning_mod.program(listing, summary, rules)
    supa.update_job(job_id, status="awaiting_zoning", step="zoning", program=prog, progress="Confirm zoning, setbacks and program")


def step_layout(job_id):
    job = supa.get_job(job_id)
    supa.update_job(job_id, status="building_layout", step="layout", progress="Laying out the site", error=None)
    ctx = _load_ctx(job_id)
    rules, prog, setbacks = job.get("zoning") or {}, job.get("program") or {}, job.get("setbacks") or {"front": 25, "side": 10, "rear": 10}
    out = tempfile.mkdtemp()
    # refresh the existing-conditions drawing with the confirmed setbacks
    summary = build_ec(out, ctx["parcel_sp"], ctx["epsg"], ctx["k"], ctx["aerial"], ctx["wet"], ctx["fl"], ctx["rds"],
                       setbacks_ft=setbacks, listing=job.get("listing"))
    summary["warnings"] = ctx.get("warnings", [])
    psb = rules.get("pavement_setbacks_ft") or zoning_mod.pavement_setbacks(rules, setbacks)
    psb = {k: float(psb[k]) for k in ("front", "side", "rear") if isinstance(psb.get(k), (int, float))}
    res = generate_layout(ctx["parcel_sp"], ctx["k"], ctx["rds"], ctx["wet"], ctx["fl"], setbacks, prog, rules=rules,
                          parking_setbacks_ft=psb)
    supa.update_job(job_id, progress="Drawing the concept plan")
    L = job.get("listing") or {}
    title = [x for x in [L.get("street_address") and f"{L.get('lot') + ', ' if L.get('lot') else ''}{L.get('street_address')}",
                         ", ".join(x for x in [L.get("city"), L.get("state")] if x),
                         L.get("county") and (L["county"] if "county" in L["county"].lower() else f"{L['county']} County")] if x]
    project = [prog.get("use") or "Development"] + title
    edges = classify_edges(ctx["parcel_sp"], [r["centerline"] for r in (ctx["rds"] if isinstance(ctx["rds"], list) else []) if r["class"] != "service"], ctx["k"])
    env = envelope(ctx["parcel_sp"], edges, setbacks, ctx["k"])
    wet = ctx["wet"] if isinstance(ctx["wet"], list) else []
    fl = ctx["fl"] if isinstance(ctx["fl"], list) else []
    rds = ctx["rds"] if isinstance(ctx["rds"], list) else []
    write_concept_sheet(os.path.join(out, "concept_plan.pdf"), os.path.join(out, "concept_plan.png"), ctx["parcel_sp"], env, wet, fl, rds,
                        ctx["aerial"], ctx["k"], res["sp"], res["labels"], title, res["metrics"], prog, rules, project=project)
    write_concept_dxf(os.path.join(out, "existing_conditions.dxf"), os.path.join(out, "concept_layout.dxf"), res["sp"], res["labels"], ctx["k"])
    with open(os.path.join(out, "layout.pkl"), "wb") as f:   # geometry for the 3D model / render step
        pickle.dump({"sp": res["sp"], "metrics": res["metrics"], "labels": res["labels"], "frame_angle_deg": res["frame_angle_deg"]}, f)
    supa.upload(job_id, os.path.join(out, "layout.pkl"))
    files = {n: supa.upload(job_id, os.path.join(out, n)) for n in ("existing_conditions.dxf", "existing_conditions.pdf", "existing_conditions.png")}
    lay = {n: supa.upload(job_id, os.path.join(out, n)) for n in ("concept_plan.pdf", "concept_plan.png", "concept_layout.dxf")}
    outputs = {**(job.get("outputs") or {}), "ec": {**((job.get("outputs") or {}).get("ec") or {}), **files}, "layout": lay}
    supa.update_job(job_id, status="awaiting_layout", step="layout", progress="Review the concept layout",
                    summary={**(job.get("summary") or {}), **summary}, layout=res["metrics"], outputs=outputs)


def step_render(job_id):
    """3D model of the approved layout (Blender, same build as the Pocahontas Trail model) -> front aerial view
    -> handed to the `pipeline-banana` edge function for the Nano Banana photoreal pass."""
    import requests
    from . import render3d
    job = supa.get_job(job_id)
    supa.update_job(job_id, render_status="modeling", render_error=None)
    ctx = _load_ctx(job_id)
    lay = pickle.loads(supa.download(job_id, "layout.pkl"))
    k, epsg = ctx["k"], ctx["epsg"]
    c = lay["sp"]["dev"].centroid
    half = 1900.0 / k                                   # wide aerial so the camera sees real ground to the horizon
    ext = (c.x - half, c.y - half, c.x + half, c.y + half)
    try:
        aer = context.aerial(ext, epsg, k, ft_per_px=1.0, max_px=4000)
    except Exception:  # noqa: BLE001
        aer = ctx["aerial"]
    try:
        rds = context.roads(ext, epsg)
        rds = rds if isinstance(rds, list) else ctx["rds"]
    except Exception:  # noqa: BLE001
        rds = ctx["rds"] if isinstance(ctx["rds"], list) else []
    work = tempfile.mkdtemp()
    render3d.prepare(work, ctx["parcel_sp"], k, aer, rds, lay, job.get("program"))
    supa.update_job(job_id, render_status="rendering")
    out = render3d.render(work, width=1600, height=1000, samples=64)
    key = supa.upload(job_id, out, "model_front.png")
    supa.upload(job_id, out.replace(".png", "_mask.json"), "model_front_mask.json")
    outputs = supa.get_job(job_id).get("outputs") or {}
    outputs["render"] = {**(outputs.get("render") or {}), "model_front.png": key}
    supa.update_job(job_id, outputs=outputs, render_status="nanobanana")
    r = requests.post(f"{supa.URL}/functions/v1/pipeline-banana", json={"job_id": job_id},
                      headers=supa._h({"Content-Type": "application/json"}), timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"Nano Banana hand-off failed: {r.status_code} {r.text[:300]}")


def step_render_composite(job_id):
    """Keep the Nano Banana pass inside the lot only: its output is used inside the property (and the building
    volumes standing on it); everything outside comes from the 3D model render, i.e. the real surroundings."""
    import json as _json
    from PIL import Image, ImageDraw, ImageFilter
    from shapely.geometry import MultiPoint, Polygon as _Poly
    from shapely.ops import unary_union as _uu
    job = supa.get_job(job_id)
    raw_name = next((n for n in ("render_raw.png", "render_raw.jpg") if n in ((job.get("outputs") or {}).get("render") or {})), None)
    if not raw_name:
        raise RuntimeError("Nano Banana output missing")
    base = Image.open(io.BytesIO(supa.download(job_id, "model_front.png"))).convert("RGB")
    raw = Image.open(io.BytesIO(supa.download(job_id, raw_name))).convert("RGB").resize(base.size, Image.LANCZOS)
    m = _json.loads(supa.download(job_id, "model_front_mask.json"))
    sx, sy = base.size[0] / m["w"], base.size[1] / m["h"]
    shapes = [_Poly([(x * sx, y * sy) for x, y in m["lot"][0]]).buffer(0)] if m.get("lot") else []
    shapes += [MultiPoint([(x * sx, y * sy) for x, y in b]).convex_hull for b in m.get("buildings", [])]
    region = _uu(shapes).buffer(4)
    mask = Image.new("L", base.size, 0)
    d = ImageDraw.Draw(mask)
    for g in getattr(region, "geoms", [region]):
        d.polygon(list(g.exterior.coords), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(3))
    # the surroundings come from the draped aerial: grade them to the photoreal pass so the seam disappears
    import numpy as _np
    b = _np.asarray(base).astype(float)
    r = _np.asarray(raw).astype(float)
    q = _np.linspace(0, 100, 257)
    for c in range(3):
        b[..., c] = _np.interp(b[..., c], _np.percentile(b[..., c], q), _np.percentile(r[..., c], q))
    base = Image.fromarray(b.clip(0, 255).astype("uint8")).filter(ImageFilter.UnsharpMask(1.6, 70, 2))
    out = Image.composite(raw, base, mask)
    path = os.path.join(tempfile.mkdtemp(), "render.jpg")
    out.save(path, quality=93)
    key = supa.upload(job_id, path, "render.jpg")
    outputs = supa.get_job(job_id).get("outputs") or {}
    outputs["render"] = {**(outputs.get("render") or {}), "render.jpg": key}
    outputs["render"].pop("render.png", None)
    supa.update_job(job_id, outputs=outputs, render_status="completed", render_error=None)
