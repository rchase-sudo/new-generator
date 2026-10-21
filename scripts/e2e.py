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

api(action="approve_parcel", job_id=job_id, index=idx, **({"setbacks": sb} if sb else {}))
j = wait(job_id, {"ec_ready"})
json.dump({k: j.get(k) for k in ("status", "summary", "error", "progress", "outputs")}, open(f"{out}/job.json", "w"), indent=1)
if j["status"] == "error":
    print("ERROR:", j["error"]); print(j.get("progress")); sys.exit(1)
for step, files in j["files"].items():
    for name, url in files.items():
        r = requests.get(url, timeout=120)
        open(f"{out}/{name}", "wb").write(r.content)
print(json.dumps(j["summary"], indent=1))
