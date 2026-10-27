"""Copy a finished job (row + stored files) to a NEW job id so a variant can be tried without touching the original.
Usage: clone_job.py <job_id>   -> prints the new job id"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import requests  # noqa: E402
from pipeline import supa  # noqa: E402

src = sys.argv[1]
job = supa.get_job(src)
new = {k: v for k, v in job.items() if k not in ("id", "created_at", "updated_at", "usage", "design_status", "design_error", "render_status",
                                                  "render_error", "package_status", "package_error")}
r = requests.post(f"{supa.URL}/rest/v1/pipeline_jobs", json=new, headers=supa._h({"Content-Type": "application/json", "Prefer": "return=representation"}), timeout=60)
r.raise_for_status()
nid = r.json()[0]["id"]
names = {"context.pkl", "layout.pkl", "model_front_mask.json", "existing_conditions.dxf"}
for files in (job.get("outputs") or {}).values():
    for key in files.values():
        names.add(key.split("/", 1)[1])
outs = {}
for step, files in (job.get("outputs") or {}).items():
    outs[step] = {n: f"{nid}/{k.split('/', 1)[1]}" for n, k in files.items()}
import tempfile  # noqa: E402
for n in sorted(names):
    try:
        data = supa.download(src, n)
    except Exception as e:  # noqa: BLE001
        print("skip", n, e)
        continue
    p = os.path.join(tempfile.mkdtemp(), n)
    open(p, "wb").write(data)
    supa.upload(nid, p, n)
    print("copied", n, len(data))
supa.update_job(nid, outputs=outs)
print("NEW_JOB_ID", nid)
