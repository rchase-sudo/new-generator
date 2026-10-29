"""End-to-end test through the real API: edge function `pipeline` -> Modal worker -> Supabase.

python scripts/e2e.py tests/lot6_loopnet.txt [candidate_index] [front side rear]
Writes everything to test_runs/<job_id>/ (candidates.json, job.json, output files).
"""
import json
import os
import sys
import time

import requests

URL = os.environ.get("SUPABASE_URL", "https://mmaqwemqbxwngqxtnsug.supabase.co")
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
H = {"Authorization": f"Bearer {KEY}", "apikey": KEY, "Content-Type": "application/json"}


def api(**body):
    r = requests.post(f"{URL}/functions/v1/pipeline", json=body, headers=H, timeout=120)
    js = r.json()
    if r.status_code >= 300:
        raise SystemExit(f"API error {r.status_code}: {js}")
    return js


def wait(job_id, until, timeout=1500):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        j = api(action="status", job_id=job_id)
        if (j["status"], j.get("progress")) != last:
            last = (j["status"], j.get("progress"))
            print(f"[{int(time.time()-t0):4d}s] {j['status']} · {j.get('progress')}", flush=True)
        if j["status"] in until or j["status"] == "error":
            return j
        time.sleep(5)
    raise SystemExit("timed out")


if sys.argv[1].startswith("batch:"):   # batch:<listings file>[:lite] -- start an unattended batch and report on it until every child has a package (or failed)
    _, path, *flags = sys.argv[1].split(":")
    bid = api(action="start_batch", text=open(path).read(), lite="lite" in flags)["job_id"]
    print("batch", bid, flush=True)
    t0 = time.time()
    while time.time() - t0 < 3000:
        time.sleep(20)
        kids = api(action="list", batch=bid, limit=100)["jobs"]
        kids = [k for k in kids if k["id"] != bid]
        print(f"[{int(time.time()-t0):4d}s]", [(k["status"], k.get("render_status"), k.get("package_status")) for k in kids], flush=True)
        if kids and all(k["status"] == "error" or k.get("package_status") in ("completed", "failed") for k in kids):
            break
    sys.exit(0)

if sys.argv[1].startswith("rerun:"):   # rerun:<job_id>:<step> -- re-run one step of an existing job and report
    _, job_id, step = sys.argv[1].split(":", 2)
    api(action="rerun", job_id=job_id, step=step)
    for _ in range(30):   # the job keeps its old status until the worker picks the step up
        if api(action="status", job_id=job_id)["status"] != "error":
            break
        time.sleep(2)
    j = wait(job_id, {"awaiting_parcel", "awaiting_zoning", "awaiting_layout", "ec_ready"})
    print(json.dumps({k: j.get(k) for k in ("status", "error", "parcel_source", "county_layer", "geocode")}, indent=1, default=str))
    print(json.dumps([{k: c.get(k) for k in ("source", "acres", "apn", "address", "zoning", "score")} for c in (j.get("candidates") or [])][:6], indent=1))
    sys.exit(0 if j["status"] != "error" else 1)

if sys.argv[1].startswith("design:"):   # build the preliminary plan set for an approved job and save it under test_runs
    job_id = sys.argv[1].split(":", 1)[1]
    api(action="design", job_id=job_id)
    for _ in range(120):
        time.sleep(5)
        j = api(action="status", job_id=job_id)
        if j.get("design_status") in ("completed", "failed"):
            break
    out = os.path.join("test_runs", job_id)
    os.makedirs(out, exist_ok=True)
    import requests as _rq
    for name, url in ((j.get("files") or {}).get("design") or {}).items():
        open(os.path.join(out, "design_" + name), "wb").write(_rq.get(url, timeout=120).content)
    print(json.dumps({k: j.get(k) for k in ("design_status", "design_error", "design")}, indent=1, default=str))
    sys.exit(0 if j.get("design_status") == "completed" else 1)

if sys.argv[1].startswith("render:"):   # re-render an approved job (3D model -> Nano Banana) and save the images under test_runs
    job_id = sys.argv[1].split(":", 1)[1]
    print(api(action="render", job_id=job_id))
    t0 = time.time(); last = None
    time.sleep(6)
    while time.time() - t0 < 1500:
        j = api(action="status", job_id=job_id)
        if j.get("render_status") != last:
            last = j.get("render_status"); print(f"[{int(time.time()-t0):4d}s] render {last} {j.get('render_error') or ''}", flush=True)
        if last in ("completed", "failed"):
            break
        time.sleep(10)
    out = os.path.join("test_runs", job_id)
    os.makedirs(out, exist_ok=True)
    import requests as _rq
    for name, url in ((j.get("files") or {}).get("render") or {}).items():
        open(os.path.join(out, name), "wb").write(_rq.get(url, timeout=120).content)
    sys.exit(0 if last == "completed" else 1)

if sys.argv[1].startswith("package:"):   # build the due diligence package for an existing rendered job
    job_id = sys.argv[1].split(":", 1)[1]
    out = os.path.join("test_runs", job_id)
    os.makedirs(out, exist_ok=True)
    api(action="package", job_id=job_id)
    t0, last = time.time(), None
    while time.time() - t0 < 1500:
        j = api(action="status", job_id=job_id)
        if j.get("package_status") != last:
            last = j.get("package_status")
            print(f"[{int(time.time()-t0):4d}s] package {last} {j.get('package_error') or ''}", flush=True)
        if last in ("completed", "failed"):
            break
        time.sleep(8)
    for step in ("package",):
        for name, url in ((j.get("files") or {}).get(step) or {}).items():
            open(f"{out}/{name}", "wb").write(requests.get(url, timeout=120).content)
    sys.exit(0 if last == "completed" else 1)

text = open(sys.argv[1]).read()
idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
sb = dict(zip(("front", "side", "rear"), map(float, sys.argv[3:6]))) if len(sys.argv) > 5 else None

job_id = api(action="start", listing_text=text, source="test")["job_id"]
print("job", job_id)
out = os.path.join("test_runs", job_id)
os.makedirs(out, exist_ok=True)
j = wait(job_id, {"awaiting_parcel"})
json.dump({k: j.get(k) for k in ("listing", "geocode", "epsg", "parcel_source", "county_layer", "error", "progress")}, open(f"{out}/parse.json", "w"), indent=1)
json.dump([{k: c.get(k) for k in ("source", "acres", "apn", "owner", "score", "address", "zoning")} for c in (j.get("candidates") or [])],
          open(f"{out}/candidates.json", "w"), indent=1)
json.dump(j.get("candidates"), open(f"{out}/candidates_full.json", "w"))
if j["status"] == "error":
    print("ERROR:", j["error"]); print(j.get("progress")); sys.exit(1)
print(json.dumps(json.load(open(f"{out}/candidates.json")), indent=1))

api(action="approve_parcel", job_id=job_id, index=idx)
j = wait(job_id, {"awaiting_zoning"})
json.dump({k: j.get(k) for k in ("status", "summary", "zoning", "program", "setbacks", "error", "progress")}, open(f"{out}/zoning.json", "w"), indent=1)
if j["status"] == "error":
    print("ERROR:", j["error"]); print(j.get("progress")); sys.exit(1)
print("ZONING:", json.dumps({k: (j.get("zoning") or {}).get(k) for k in ("jurisdiction", "district", "district_name", "setbacks_ft", "parking_setbacks_ft", "confidence")}, indent=1))
print("PROGRAM:", json.dumps(j.get("program"), indent=1)[:2500])

api(action="approve_zoning", job_id=job_id, **({"setbacks": sb} if sb else {}))
j = wait(job_id, {"awaiting_layout"})
json.dump({k: j.get(k) for k in ("status", "summary", "layout", "zoning", "program", "setbacks", "error", "progress", "outputs")}, open(f"{out}/job.json", "w"), indent=1)
if j["status"] == "error":
    print("ERROR:", j["error"]); print(j.get("progress")); sys.exit(1)
for step, files in j["files"].items():
    for name, url in files.items():
        if name.endswith((".pkl",)):
            continue
        r = requests.get(url, timeout=120)
        open(f"{out}/{name}", "wb").write(r.content)
print("LAYOUT:", json.dumps(j["layout"], indent=1))
print(api(action="approve_layout", job_id=job_id))
# 3D model (Blender) -> Nano Banana render
t0 = time.time(); last = None
while time.time() - t0 < 1500:
    j = api(action="status", job_id=job_id)
    if j.get("render_status") != last:
        last = j.get("render_status"); print(f"[{int(time.time()-t0):4d}s] render {last} {j.get('render_error') or ''}", flush=True)
    if last in ("completed", "failed"):
        break
    time.sleep(10)
for name, url in (j.get("files", {}).get("render") or {}).items():
    open(f"{out}/{name}", "wb").write(requests.get(url, timeout=120).content)
if last != "completed":
    print("RENDER ERROR:", j.get("render_error")); sys.exit(1)
