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
import json
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


def _flush_usage(job_id, step):
    """Append this step's billable calls (Claude, Google geocoding, Modal compute) to pipeline_jobs.usage."""
    from . import usage
    calls, usage.USAGE[:] = list(usage.USAGE), []
    if not calls:
        return
    import datetime as _dt
    at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        prev = supa.get_job(job_id).get("usage") or []
        supa.update_job(job_id, usage=prev + [{**c, "step": step, "at": at} for c in calls])
    except Exception as e:  # noqa: BLE001  -- usage logging must never break a run
        print("usage log failed:", e)


# container size per step (must match modal_app.py): run_render = 8 cores / 12 GiB, run_step = 2 cores / 4 GiB
STEP_RESOURCES = {"render": (8.0, 12.0)}


def run(job_id, step):
    import time as _t
    from . import usage
    usage.USAGE[:] = []
    t0 = _t.time()
    try:
        _run(job_id, step)
    finally:
        cores, gib = STEP_RESOURCES.get(step, (2.0, 4.0))
        usage.modal_compute(step, _t.time() - t0, cores, gib)
        _flush_usage(job_id, step)


def _run(job_id, step):
    try:
        if step == "parse":
            step_parse(job_id)
        elif step == "ec":
            step_ec(job_id)
            step_zoning(job_id)
        elif step == "zoning":
            step_zoning(job_id)
        elif step == "buildings":
            step_buildings(job_id)
        elif step == "topo":
            step_topo(job_id)
        elif step == "layout":
            step_layout(job_id)
        elif step == "render":
            step_render(job_id)
        elif step == "render_composite":
            step_render_composite(job_id)
        elif step == "package":
            step_package(job_id)
        elif step == "design":
            step_design(job_id)
        else:
            raise ValueError(f"unknown step {step}")
    except Exception as e:  # noqa: BLE001
        if step in ("render", "render_composite"):   # a failed render never invalidates an approved plan
            supa.update_job(job_id, render_status="failed", render_error=f"{type(e).__name__}: {e}"[:1500])
        elif step == "design":   # the full design phase is optional and never touches the approved plan
            supa.update_job(job_id, design_status="failed", design_error=f"{type(e).__name__}: {e}"[:1500])
        elif step == "package":
            supa.update_job(job_id, package_status="failed", package_error=f"{type(e).__name__}: {e}"[:1500])
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
    if not listing.get("county"):   # listings often omit it; the county parcel-GIS fallback needs it
        from .geo import county_at
        cty, st = county_at(lat, lon)
        if cty:
            listing = {**listing, "county": cty, "state": listing.get("state") or st}
            supa.update_job(job_id, listing=listing)
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

    supa.update_job(job_id, progress="Searching parcels", geocode={"lat": lat, "lon": lon, "label": label})
    res = find_parcels(listing, lat, lon, road_geom_ll=road_ll, county_layer=job.get("county_layer"), corridor_ll=corridor)
    if not res["candidates"]:
        from .parcel import REGRID_ERRORS
        why = f" (Regrid: {'; '.join(REGRID_ERRORS)[:300]})" if REGRID_ERRORS else ""
        raise RuntimeError("No parcels found near the listing location. Paste the parcel ID (APN) or adjust the address." + why)
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
    # fetch the public layers in parallel under one time budget: a slow service must never leave the job hanging
    import concurrent.futures as _cf
    import time as _t
    from . import topo as topo_mod
    fetch = {"wetlands": context.wetlands, "flood": context.flood, "roads": context.roads, "bld": context.buildings, "pave": context.pavement,
             "topo": lambda e_, ep_: topo_mod.fetch(e_, ep_, k)}
    got = {}
    t_end = _t.time() + 240
    for attempt in range(2):
        todo = [n for n in fetch if n not in got or (isinstance(got[n], dict) and (n != "topo" or "error" in got[n]))]
        if not todo or _t.time() > t_end - 30:
            break
        ex = _cf.ThreadPoolExecutor(max_workers=5)
        futs = {ex.submit(fetch[n], ext, epsg): n for n in todo}
        done, _ = _cf.wait(futs, timeout=max(10, t_end - _t.time()))
        for f, n in futs.items():
            try:
                got[n] = f.result() if f in done else {"error": "timed out"}
            except Exception as e:  # noqa: BLE001
                got[n] = {"error": f"{type(e).__name__}: {e}"[:200]}
        ex.shutdown(wait=False, cancel_futures=True)
    wet, fl, rds = got.get("wetlands", {"error": "timed out"}), got.get("flood", {"error": "timed out"}), got.get("roads", {"error": "timed out"})
    if isinstance(rds, dict) and "error" in rds:
        # without roads there is no front, no setbacks by edge and nothing for the driveway to meet: stop, don't guess
        raise RuntimeError("Road data is unavailable right now (OpenStreetMap and Census both failed). Retry in a few minutes.")
    bld_raw = got.get("bld", {"error": "timed out"})
    dem = got.get("topo", {"error": "timed out"})
    warnings = [f"{n}: {v['error']}" for n, v in (("wetlands", wet), ("flood", fl), ("roads", rds), ("buildings", bld_raw), ("topography", dem)) if isinstance(v, dict) and "error" in v]
    dem = None if (isinstance(dem, dict) and "error" in dem) else topo_mod.analyze(dem)
    if dem:
        dem.pop("z", None)
    from . import existing
    bld = existing.prepare(bld_raw, parcel_sp, k)
    pave = existing.find_pavement(aerial, parcel_sp, k, bld, got.get("pave"))

    supa.update_job(job_id, progress="Drawing")
    out = tempfile.mkdtemp()
    summary = build_ec(out, parcel_sp, epsg, k, aerial, wet, fl, rds, setbacks_ft=job.get("setbacks"), listing=job.get("listing"), bld=bld, topo=dem,
                    steep=topo_mod.steep_polys(dem, parcel_sp, (job.get("zoning") or {}).get("max_slope_pct", topo_mod.DEFAULT_MAX_SLOPE)) if dem else [],
                    thr=(job.get("zoning") or {}).get("max_slope_pct", topo_mod.DEFAULT_MAX_SLOPE) or 0)
    summary["warnings"] = warnings
    summary["topo"] = topo_mod.summary(dem, parcel_sp, (job.get("zoning") or {}).get("max_slope_pct", topo_mod.DEFAULT_MAX_SLOPE)) if dem else {"available": False}
    summary["existing_buildings"] = existing.summary(bld, epsg)
    summary["buildings_checked"] = not isinstance(bld_raw, dict)
    summary["existing_pavement"] = existing.pave_summary(pave, epsg)
    files = {}
    for name in ("existing_conditions.dxf", "existing_conditions.pdf", "existing_conditions.png", "aerial.jpg", "aerial.jgw"):
        files[name] = supa.upload(job_id, os.path.join(out, name))
    # cache the site context so later steps (layout, re-layouts) don't re-fetch it
    ctx = {"parcel_sp": parcel_sp, "epsg": epsg, "k": k, "wet": wet, "fl": fl, "rds": rds, "bld": bld, "pave": pave, "topo": dem, "warnings": warnings,
           "aerial": {"extent": aerial["extent"], "source": aerial.get("source"), "jpg": _jpg(aerial["image"])}}
    with open(os.path.join(out, "context.pkl"), "wb") as f:
        pickle.dump(ctx, f)
    supa.upload(job_id, os.path.join(out, "context.pkl"))
    outputs = {**(job.get("outputs") or {}), "ec": files}
    supa.update_job(job_id, status="ec_ready", step="ec", progress="Existing conditions ready", summary=summary, outputs=outputs)


def step_buildings(job_id):
    """Look up existing buildings for a job whose site context is already cached (jobs from before building
    detection, or a retry after the data sources were down). No re-research, no new drawing until the layout runs."""
    from . import existing
    job = supa.get_job(job_id)
    back = job.get("status") if job.get("status") not in ("error", "building_ec") else ("awaiting_zoning" if job.get("zoning") else "ec_ready")
    supa.update_job(job_id, progress="Looking up existing buildings", error=None)
    ctx = _load_ctx(job_id)
    ext = _extent_around(ctx["parcel_sp"], ctx["k"])
    raw = context.buildings(ext, ctx["epsg"])
    bld = existing.prepare(raw, ctx["parcel_sp"], ctx["k"])
    ctx["bld"] = bld
    ctx["pave"] = existing.find_pavement(ctx["aerial"], ctx["parcel_sp"], ctx["k"], bld, context.pavement(ext, ctx["epsg"]))
    ctx["warnings"] = [w for w in ctx.get("warnings", []) if not w.startswith("buildings:")] + ([f"buildings: {raw['error']}"] if isinstance(raw, dict) else [])
    ctx["aerial"] = {k_: v for k_, v in ctx["aerial"].items() if k_ != "image"}
    path = os.path.join(tempfile.mkdtemp(), "context.pkl")
    with open(path, "wb") as f:
        pickle.dump(ctx, f)
    supa.upload(job_id, path)
    summary = {**(job.get("summary") or {}), "existing_buildings": existing.summary(bld, ctx["epsg"]), "buildings_checked": not isinstance(raw, dict),
               "existing_pavement": existing.pave_summary(ctx["pave"], ctx["epsg"])}
    supa.update_job(job_id, status=back, summary=summary, progress="Confirm zoning, setbacks and program" if back == "awaiting_zoning" else "Existing buildings ready")


def step_topo(job_id):
    """Fetch elevation for a job whose site context is already cached (older jobs, or a retry after 3DEP was down)."""
    from . import topo as topo_mod
    job = supa.get_job(job_id)
    back = job.get("status") if job.get("status") not in ("error", "building_ec") else ("awaiting_zoning" if job.get("zoning") else "ec_ready")
    supa.update_job(job_id, progress="Reading elevation data", error=None)
    ctx = _load_ctx(job_id)
    ext = _extent_around(ctx["parcel_sp"], ctx["k"])
    dem = topo_mod.fetch(ext, ctx["epsg"], ctx["k"])
    ok = "error" not in dem
    if ok:
        dem = topo_mod.analyze(dem)
        dem.pop("z", None)
    ctx["topo"] = dem if ok else None
    ctx["warnings"] = [w for w in ctx.get("warnings", []) if not w.startswith("topography:")] + ([] if ok else [f"topography: {dem['error']}"])
    ctx["aerial"] = {k_: v for k_, v in ctx["aerial"].items() if k_ != "image"}
    path = os.path.join(tempfile.mkdtemp(), "context.pkl")
    with open(path, "wb") as f:
        pickle.dump(ctx, f)
    supa.upload(job_id, path)
    thr = (job.get("zoning") or {}).get("max_slope_pct", topo_mod.DEFAULT_MAX_SLOPE)
    summary = {**(job.get("summary") or {}), "topo": topo_mod.summary(dem, ctx["parcel_sp"], thr) if ok else {"available": False}}
    supa.update_job(job_id, status=back, summary=summary, progress="Confirm zoning, setbacks and program" if back == "awaiting_zoning" else "Elevation ready")


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
    if ctx.get("topo"):
        from . import topo as _tp
        cons += _tp.steep_polys(ctx["topo"], ctx["parcel_sp"], (job.get("zoning") or {}).get("max_slope_pct", _tp.DEFAULT_MAX_SLOPE))
    bld = env.difference(unary_union(cons)) if cons else env
    # the buildable piece that reaches the road (wetlands / streams can cut off the back of a site)
    from .ec_drawing import _polys
    road = unary_union([r["centerline"] for r in (ctx["rds"] if isinstance(ctx["rds"], list) else [])]) if ctx["rds"] else None
    pieces = [g for g in _polys(bld) if g.area * ctx["k"] ** 2 > 4000]
    front = min(pieces, key=lambda g: (round(g.distance(road) * ctx["k"] / 50), -g.area)) if pieces and road is not None and not road.is_empty else (max(pieces, key=lambda g: g.area) if pieces else bld)
    ac = lambda g: round(g.area * ctx["k"] ** 2 / 43560, 3)  # noqa: E731
    from .ec_drawing import front_roads
    summary = {**summary, "setbacks_ft": setbacks, "envelope_acres": ac(env), "buildable_acres": ac(bld),
               "frontage_buildable_acres": ac(front), "front_roads": front_roads(ctx["parcel_sp"], ctx["rds"], ctx["k"], ctx["epsg"])}
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
    from . import existing
    bld = ctx.get("bld") or []          # jobs from before building detection have none: behaves exactly as before
    actions = {b["id"]: (((rules.get("building_actions") or {}).get(b["id"])) or "keep") for b in bld if b.get("onsite")}
    keep, demo = existing.split(bld, actions)
    pave_all = ctx.get("pave") or []
    pactions = {p["id"]: ((rules.get("pavement_actions") or {}).get(p["id"]) or "reuse") for p in pave_all}
    pave_keep, pave_rm = existing.pave_split(pave_all, pactions)
    shared = int(rules.get("shared_stalls") or 0)
    from . import topo as topo_mod
    dem = ctx.get("topo")
    thr = rules.get("max_slope_pct", topo_mod.DEFAULT_MAX_SLOPE)
    steep = topo_mod.steep_polys(dem, ctx["parcel_sp"], thr) if dem else []
    summary = build_ec(out, ctx["parcel_sp"], ctx["epsg"], ctx["k"], ctx["aerial"], ctx["wet"], ctx["fl"], ctx["rds"],
                       setbacks_ft=setbacks, listing=job.get("listing"), bld=bld, actions=actions, topo=dem, steep=steep, thr=thr or 0)
    summary["topo"] = topo_mod.summary(dem, ctx["parcel_sp"], thr) if dem else {"available": False}
    summary["existing_buildings"] = (job.get("summary") or {}).get("existing_buildings", [])
    summary["warnings"] = ctx.get("warnings", [])
    from .ec_drawing import front_roads
    summary["front_roads"] = front_roads(ctx["parcel_sp"], ctx["rds"], ctx["k"], ctx["epsg"])
    psb = rules.get("pavement_setbacks_ft") or zoning_mod.pavement_setbacks(rules, setbacks)
    psb = {k: float(psb[k]) for k in ("front", "side", "rear") if isinstance(psb.get(k), (int, float))}
    try:
        res = generate_layout(ctx["parcel_sp"], ctx["k"], ctx["rds"], ctx["wet"], ctx["fl"], setbacks, prog, rules=rules,
                              parking_setbacks_ft=psb, drive_road=rules.get("drive_road") or None,
                              keep_bldgs=[b["geom"] for b in keep], steep=steep, dem=dem,
                              reuse_pave=[p["geom"] for p in pave_keep], shared_stalls=shared, tie_existing=rules.get("tie_existing", True) is not False)
    except RuntimeError as e:
        dr = rules.get("drive_road")
        fr = {r["key"]: r["frontage_ft"] for r in (summary.get("front_roads") or [])}
        if dr and "does not fit" in str(e) and fr:
            best = max(fr, key=fr.get)
            raise RuntimeError(f"{e} Only {fr.get(dr, 0):,} ft of this parcel fronts {dr}, too little to organize the site around. "
                               f"Pick Automatic or {best} ({fr[best]:,} ft of frontage) for the driveway.") from None
        raise
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
    if dem:
        ew = topo_mod.earthwork(dem, res["sp"]["dev"])
        if ew:
            res["metrics"]["earthwork"] = ew
            if ew["cut_cy"] + ew["fill_cy"] > 0 and ew["area_ac"] > 0 and (ew["cut_cy"] + ew["fill_cy"]) / ew["area_ac"] > 2500:
                res["metrics"]["warnings"].append(f"Grading: about {ew['cut_cy']:,.0f} CY cut / {ew['fill_cy']:,.0f} CY fill to level the development area")
    ex = [{"id": b["id"], "action": "keep", "geom": b["geom"], "sf": b["area_sf"], "label": b.get("kind")} for b in keep] + \
         [{"id": b["id"], "action": "demo", "geom": b["geom"], "sf": b["area_sf"], "label": b.get("kind")} for b in demo]
    ex += [{"id": p["id"], "action": "keep", "geom": p["geom"], "sf": p["area_sf"], "label": ("Parking" if p["kind"] == "parking" else "Drive") +
            (f" (about {p['est_stalls']} stalls)" if p.get("est_stalls") else ""), "kind": "pavement"} for p in pave_keep] + \
          [{"id": p["id"], "action": "demo", "geom": p["geom"], "sf": p["area_sf"], "label": "Parking" if p["kind"] == "parking" else "Drive", "kind": "pavement"} for p in pave_rm]
    res["metrics"]["existing"] = [{"id": e["id"], "action": e["action"], "sf": round(e["sf"], -1), "label": e["label"], **({"kind": "pavement"} if e.get("kind") == "pavement" else {})} for e in ex]
    write_concept_sheet(os.path.join(out, "concept_plan.pdf"), os.path.join(out, "concept_plan.png"), ctx["parcel_sp"], env, wet, fl, rds,
                        ctx["aerial"], ctx["k"], res["sp"], res["labels"], title, res["metrics"], prog, rules, project=project, existing=ex,
                        topo=dem, steep=steep)
    write_concept_dxf(os.path.join(out, "existing_conditions.dxf"), os.path.join(out, "concept_layout.dxf"), res["sp"], res["labels"], ctx["k"], existing=ex, steep=steep)
    with open(os.path.join(out, "layout.pkl"), "wb") as f:   # geometry for the 3D model / render step
        pickle.dump({"sp": res["sp"], "metrics": res["metrics"], "labels": res["labels"], "frame_angle_deg": res["frame_angle_deg"], "existing": ex}, f)
    supa.upload(job_id, os.path.join(out, "layout.pkl"))
    files = {n: supa.upload(job_id, os.path.join(out, n)) for n in ("existing_conditions.dxf", "existing_conditions.pdf", "existing_conditions.png")}
    lay = {n: supa.upload(job_id, os.path.join(out, n)) for n in ("concept_plan.pdf", "concept_plan.png", "concept_layout.dxf")}
    outputs = {**(job.get("outputs") or {}), "ec": {**((job.get("outputs") or {}).get("ec") or {}), **files}, "layout": lay}
    supa.update_job(job_id, status="awaiting_layout", step="layout", progress="Review the concept layout",
                    summary={**(job.get("summary") or {}), **summary}, layout=res["metrics"], outputs=outputs)


def step_redraw(job_id, hide_demo=True, out_dir=None):
    """Re-draw the concept plan images from the stored layout geometry WITHOUT re-running the layout, so the plan, the DXF and the
    3D model all keep exactly the same layout. layout.pkl and the stored layout metrics are never touched."""
    job = supa.get_job(job_id)
    ctx = _load_ctx(job_id)
    lay = pickle.loads(supa.download(job_id, "layout.pkl"))
    rules, prog, setbacks = job.get("zoning") or {}, job.get("program") or {}, job.get("setbacks") or {"front": 25, "side": 10, "rear": 10}
    from . import topo as topo_mod
    dem = ctx.get("topo")
    thr = rules.get("max_slope_pct", topo_mod.DEFAULT_MAX_SLOPE)
    steep = topo_mod.steep_polys(dem, ctx["parcel_sp"], thr) if dem else []
    L = job.get("listing") or {}
    title = [x for x in [L.get("street_address") and f"{L.get('lot') + ', ' if L.get('lot') else ''}{L.get('street_address')}",
                         ", ".join(x for x in [L.get("city"), L.get("state")] if x),
                         L.get("county") and (L["county"] if "county" in L["county"].lower() else f"{L['county']} County")] if x]
    project = [prog.get("use") or "Development"] + title
    rds = ctx["rds"] if isinstance(ctx["rds"], list) else []
    edges = classify_edges(ctx["parcel_sp"], [r["centerline"] for r in rds if r["class"] != "service"], ctx["k"])
    env = envelope(ctx["parcel_sp"], edges, setbacks, ctx["k"])
    wet = ctx["wet"] if isinstance(ctx["wet"], list) else []
    fl = ctx["fl"] if isinstance(ctx["fl"], list) else []
    ex = [e for e in (lay.get("existing") or []) if not (hide_demo and e.get("action") == "demo")]
    out = out_dir or tempfile.mkdtemp()
    write_concept_sheet(os.path.join(out, "concept_plan.pdf"), os.path.join(out, "concept_plan.png"), ctx["parcel_sp"], env, wet, fl, rds,
                        ctx["aerial"], ctx["k"], lay["sp"], lay["labels"], title, lay["metrics"], prog, rules, project=project, existing=ex,
                        topo=dem, steep=steep)
    write_concept_dxf(os.path.join(out, "existing_conditions.dxf"), os.path.join(out, "concept_layout.dxf"), lay["sp"], lay["labels"], ctx["k"], existing=ex, steep=steep)
    if out_dir is None:
        up = {n: supa.upload(job_id, os.path.join(out, n)) for n in ("concept_plan.pdf", "concept_plan.png", "concept_layout.dxf")}
        outputs = {**(job.get("outputs") or {}), "layout": up}
        supa.update_job(job_id, outputs=outputs)
    return out


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
    out = render3d.render(work, width=1800, height=1200, samples=64)
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
    raw = Image.open(io.BytesIO(supa.download(job_id, raw_name))).convert("RGB")
    if not os.environ.get("RENDER_LOT_ONLY"):
        # the whole frame is the photoreal pass (like the Pocahontas render): surroundings included, no seam with the 3D model texture
        path = os.path.join(tempfile.mkdtemp(), "render.jpg")
        raw.save(path, quality=94)
        key = supa.upload(job_id, path, "render.jpg")
        outputs = supa.get_job(job_id).get("outputs") or {}
        outputs["render"] = {**(outputs.get("render") or {}), "render.jpg": key}
        outputs["render"].pop("render.png", None)
        supa.update_job(job_id, outputs=outputs, render_status="completed", render_error=None)
        return
    base = Image.open(io.BytesIO(supa.download(job_id, "model_front.png"))).convert("RGB")
    raw = raw.resize(base.size, Image.LANCZOS)
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


def step_package(job_id):
    """Due diligence package (FORMA style): plan sheet as Exhibit A, the Nano Banana render as Exhibit B1."""
    from . import package as pkg
    job = supa.get_job(job_id)
    files = job.get("outputs") or {}
    rend = files.get("render") or {}
    if "render.jpg" not in rend and "render.png" not in rend:
        raise RuntimeError("Render the project first")
    out = tempfile.mkdtemp()
    plan = os.path.join(out, "concept_plan.png")
    with open(plan, "wb") as fh:
        fh.write(supa.download(job_id, "concept_plan.png"))
    rname = "render.jpg" if "render.jpg" in rend else "render.png"
    render = os.path.join(out, rname)
    with open(render, "wb") as fh:
        fh.write(supa.download(job_id, rname))
    supa.update_job(job_id, package_status="researching", package_error=None)
    F = pkg.facts(job)
    I = None
    try:   # rebuilds reuse the research when the plan has not changed (saves the web-search pass)
        prev = json.loads(supa.download(job_id, "package_inputs.json"))
        if prev.get("plan") == json.loads(json.dumps(F["plan"], default=str)) and prev.get("inputs"):
            I = prev["inputs"]
    except Exception:  # noqa: BLE001
        pass
    I = I or pkg.research_inputs(F)
    with open(os.path.join(out, "package_inputs.json"), "w") as fh:
        json.dump({"plan": F["plan"], "inputs": I}, fh, default=str)
    supa.upload(job_id, os.path.join(out, "package_inputs.json"))
    supa.update_job(job_id, package_status="writing")
    PF = pkg.proforma(F, I)
    T = pkg.write_text(F, I, PF)
    name = pkg.package_name(F)
    pdf = os.path.join(out, "package.pdf")
    supa.update_job(job_id, package_status="typesetting")
    pkg.build(job, plan, render, pdf, inputs=I, text=T)
    key = supa.upload(job_id, pdf, "package.pdf")
    outputs = supa.get_job(job_id).get("outputs") or {}
    outputs["package"] = {"package.pdf": key}
    summary = {**(supa.get_job(job_id).get("summary") or {}), "package_name": name}
    supa.update_job(job_id, outputs=outputs, summary=summary, package_status="completed", package_error=None)


def step_design(job_id):
    """Full design phase (post-pipeline, optional): grading, drainage, utilities and a preliminary plan set from the approved layout."""
    from .fd import planset
    job = supa.get_job(job_id)
    supa.update_job(job_id, design_status="designing", design_error=None)
    ctx = _load_ctx(job_id)
    lay = pickle.loads(supa.download(job_id, "layout.pkl"))
    out = tempfile.mkdtemp()
    res = planset.build(ctx, lay, job, out, prog_cb=lambda m: supa.update_job(job_id, design_status="designing", progress=m))
    files = {"plan_set.pdf": supa.upload(job_id, res["pdf"])}
    for p in res["pngs"]:
        files[os.path.basename(p)] = supa.upload(job_id, p)
    outputs = supa.get_job(job_id).get("outputs") or {}
    outputs["design"] = files
    supa.update_job(job_id, outputs=outputs, design=planset.summary(res["X"]), design_status="completed", design_error=None, progress="Plan set ready")
