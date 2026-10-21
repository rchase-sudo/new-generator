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
        else:
            raise ValueError(f"unknown step {step}")
    except Exception as e:  # noqa: BLE001
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
    summary = {**summary, "setbacks_ft": setbacks, "envelope_acres": round(env.area * ctx["k"] ** 2 / 43560, 3),
               "buildable_acres": round(bld.area * ctx["k"] ** 2 / 43560, 3)}
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
    res = generate_layout(ctx["parcel_sp"], ctx["k"], ctx["rds"], ctx["wet"], ctx["fl"], setbacks, prog, rules=rules)
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
    files = {n: supa.upload(job_id, os.path.join(out, n)) for n in ("existing_conditions.dxf", "existing_conditions.pdf", "existing_conditions.png")}
    lay = {n: supa.upload(job_id, os.path.join(out, n)) for n in ("concept_plan.pdf", "concept_plan.png", "concept_layout.dxf")}
    outputs = {**(job.get("outputs") or {}), "ec": {**((job.get("outputs") or {}).get("ec") or {}), **files}, "layout": lay}
    supa.update_job(job_id, status="awaiting_layout", step="layout", progress="Review the concept layout",
                    summary={**(job.get("summary") or {}), **summary}, layout=res["metrics"], outputs=outputs)
