"""Unattended runs: batch intake (many listings in one field) and auto-advance through the approval checkpoints.

A job with options.auto = true never waits for a person: after each step the worker picks the best parcel candidate, accepts the researched
zoning / program, accepts the layout, renders, and builds the due diligence package. options.lite = cheap package research (same plan + render).
Every child job of a batch carries options.batch_id (the id of the batch parent row) so the whole batch can be listed and pulled together."""
import json
import re
import time

import requests

from . import supa
from .claude import ask_tool

MAX_BATCH = 40
SPLIT_MODEL = "claude-haiku-5-5"


def kick(job_id, step):
    """Start a worker step for a job (same call the edge function makes)."""
    r = requests.get(f"{supa.URL}/rest/v1/pipeline_config", params={"key": "in.(worker_url,worker_token)", "select": "key,value"}, headers=supa._h(), timeout=30)
    r.raise_for_status()
    cfg = {x["key"]: x["value"] for x in r.json()}
    resp = requests.post(cfg["worker_url"].rstrip("/") + "/", json={"job_id": job_id, "step": step},
                         headers={"Authorization": f"Bearer {cfg['worker_token']}", "Content-Type": "application/json"}, timeout=60)
    resp.raise_for_status()


def _insert_job(row):
    r = requests.post(f"{supa.URL}/rest/v1/pipeline_jobs", json=row, headers=supa._h({"Content-Type": "application/json", "Prefer": "return=representation"}), timeout=30)
    r.raise_for_status()
    return r.json()[0]["id"]


def split_listings(text):
    """Split a blob of many listing descriptions into one text per listing. The model only returns line ranges, so the text stays verbatim."""
    lines = text.replace("\r", "").split("\n")
    numbered = "\n".join(f"L{i}: {ln}" for i, ln in enumerate(lines))
    out = ask_tool(
        "You split a pasted block of real-estate listing descriptions into the separate listings. Each listing describes ONE property (address, "
        "acreage, price, zoning, remarks). Return the inclusive line range of each listing, in order. Ranges must not overlap; leave out "
        "separators, headings and text that belongs to no listing. Never merge two properties and never split one property.",
        f"Lines are numbered L0..L{len(lines) - 1}.\n\n{numbered}", "submit_ranges", "Submit the line range of each listing.",
        max_tokens=4000, label="Batch: split listings", model=SPLIT_MODEL,
        properties={"listings": {"type": "array", "items": {"type": "object", "properties": {
            "start": {"type": "integer"}, "end": {"type": "integer"}}, "required": ["start", "end"]}}})
    res = []
    for r in out.get("listings") or []:
        try:
            a, b = max(0, int(r["start"])), min(len(lines) - 1, int(r["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        t = "\n".join(lines[a:b + 1]).strip()
        if len(t) >= 20:
            res.append(t)
    if not res and len(text.strip()) >= 20:
        res = [p.strip() for p in re.split(r"\n\s*\n\s*\n+", text) if len(p.strip()) >= 20]
    return res


def step_batch(job_id):
    parent = supa.get_job(job_id)
    opts = parent.get("options") or {}
    supa.update_job(job_id, status="batch_running", step="batch", progress="Splitting the listings", error=None)
    listings = split_listings(parent["listing_text"])
    if not listings:
        raise RuntimeError("No listings found in the pasted text")
    skipped = max(0, len(listings) - MAX_BATCH)
    listings = listings[:MAX_BATCH]
    child_opts = {"lite": bool(opts.get("lite")), "auto": True, "batch_id": job_id}
    ids = []
    for i, t in enumerate(listings):
        cid = _insert_job({"listing_text": t, "source": "batch", "status": "queued", "step": "parse", "created_by": parent.get("created_by"), "options": child_opts})
        ids.append(cid)
        supa.update_job(job_id, progress=f"Started {len(ids)} of {len(listings)} listings", summary={"children": ids, "count": len(listings), "skipped": skipped})
        try:
            kick(cid, "parse")
        except Exception as e:  # noqa: BLE001
            supa.update_job(cid, status="error", error=f"Could not start: {e}"[:300])
        time.sleep(3)    # stagger so the public data services are not hit by every job in the same second
    supa.update_job(job_id, status="batch_running", progress=f"{len(ids)} listings running" + (f" ({skipped} over the {MAX_BATCH} limit were not started)" if skipped else ""),
                    summary={"children": ids, "count": len(ids), "skipped": skipped})


def advance(job_id, step):
    """Called after a step finishes: when the job is auto, take the approval a person would have taken and start the next step."""
    job = supa.get_job(job_id)
    opts = job.get("options") or {}
    if not opts.get("auto") or opts.get("batch_parent"):
        return
    st = job.get("status")
    if st == "awaiting_parcel":
        cands = job.get("candidates") or []
        if not cands:
            return
        best = max(range(len(cands)), key=lambda i: (cands[i].get("score") or 0, -i))
        supa.update_job(job_id, parcel=cands[best], status="parcel_approved", step="ec", error=None, progress="Parcel confirmed")
        kick(job_id, "ec")
    elif st == "awaiting_zoning":
        supa.update_job(job_id, status="zoning_approved", step="layout", error=None, progress="Starting the layout")
        kick(job_id, "layout")
    elif st == "awaiting_layout":
        supa.update_job(job_id, status="layout_approved", step="deliverables", progress="Layout approved", render_status="queued", render_error=None)
        kick(job_id, "render")
    elif st == "layout_approved" and step == "render_composite" and job.get("render_status") == "completed" and not job.get("package_status"):
        supa.update_job(job_id, package_status="queued", package_error=None)
        kick(job_id, "package")


def retry_layout(job_id, err=None):
    """An unattended job whose layout fails because the researched program does not fit: shrink the program (60%, then 35% and two buildings)
    and lay it out again, instead of stopping at an error nobody is there to fix. Returns True once a layout exists."""
    job = supa.get_job(job_id)
    opts = job.get("options") or {}
    n = int(opts.get("layout_retries") or 0)
    if not opts.get("auto") or n >= 2:
        return False
    prog = job.get("program") or {}
    blds = prog.get("buildings") or []
    if not blds:
        return False
    f = 0.6 if n == 0 else 0.35
    new_b = [{**b, "footprint_sf": max(1500.0, round(float(b.get("footprint_sf") or 5000) * f, -2))} for b in (blds if n == 0 else blds[:2])]
    par = dict(prog.get("parking") or {})
    if par.get("required"):
        par["required"] = max(4, round(float(par["required"]) * f))
    note = f"Program reduced to {int(f * 100)}% so it fits the site" if n == 0 else "Program reduced to the two main buildings so it fits the site"
    supa.update_job(job_id, program={**prog, "buildings": new_b, "parking": par, "auto_note": note}, options={**opts, "layout_retries": n + 1},
                    status="zoning_approved", error=None, progress=note)
    from . import jobs
    try:
        jobs.step_layout(job_id)
        return True
    except Exception as e:  # noqa: BLE001
        supa.update_job(job_id, status="error", step="layout", error=f"{type(e).__name__}: {e}"[:500])
        return retry_layout(job_id, e)
