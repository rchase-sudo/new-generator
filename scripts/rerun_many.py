"""Re-run one step for many jobs: python scripts/rerun_many.py <step> <hours> [only_status ...]
Jobs = batch children created in the last <hours> hours (optionally only those whose status is in the list). Staggered so the data services are not hit at once."""
import os
import sys
import time

import requests

URL = "https://mmaqwemqbxwngqxtnsug.supabase.co"
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
H = {"Authorization": f"Bearer {KEY}", "apikey": KEY, "Content-Type": "application/json"}
step, hours, only = sys.argv[1], float(sys.argv[2]), set(sys.argv[3:])
since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))
r = requests.get(f"{URL}/rest/v1/pipeline_jobs", params={"created_at": f"gt.{since}", "options->>batch_id": "not.is.null", "options->>batch_parent": "is.null",
                 "select": "id,status,listing", "limit": 300}, headers=H, timeout=60)
r.raise_for_status()
jobs = [j for j in r.json() if not only or j["status"] in only]
print(len(jobs), "jobs", flush=True)
for j in jobs:
    x = requests.post(f"{URL}/functions/v1/pipeline", json={"action": "rerun", "job_id": j["id"], "step": step}, headers=H, timeout=120)
    print(j["id"], (j.get("listing") or {}).get("street_address"), x.status_code, x.text[:80], flush=True)
    time.sleep(4)
