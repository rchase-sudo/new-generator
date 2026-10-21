"""Job steps. Each step reads the job row, does its work, writes results back and sets the next status.

Status flow (step 1 of the build):
  queued -> parsing -> awaiting_parcel  (user picks / confirms the parcel)
  parcel_approved -> building_ec -> ec_ready
Any failure -> error (message in `error`, step in `step`).
"""
import math
import os
import tempfile
import traceback

from shapely.geometry import box, mapping, shape

from . import context, supa
from .ec_drawing import build as build_ec
from .geo import geocode, state_plane_epsg, to_crs, units_to_ft
from .listing import parse_listing
from .parcel import find_parcels


def run(job_id, step):
    try:
        if step == "parse":
            step_parse(job_id)
        elif step == "ec":
            step_ec(job_id)
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
    c = to_crs(shape({"type": "Point", "coordinates": [lon, lat]}), 4326, epsg)
    half = 1500 / k
    rds = context.roads((c.x - half, c.y - half, c.x + half, c.y + half), epsg)
    road_sp = context.named_road(rds, listing.get("road_name") or listing.get("street_address"))
    road_ll = to_crs(road_sp, epsg, 4326) if road_sp is not None else None

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
                    candidates=res["candidates"], parcel_source=res["source"], county_layer=res["county_layer"])


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
    outputs = {**(job.get("outputs") or {}), "ec": files}
    supa.update_job(job_id, status="ec_ready", step="ec", progress="Existing conditions ready", summary=summary, outputs=outputs)
