"""Download a job's finished deliverables into test_runs/<job_id>/ (for hand-off).
Usage: fetch.py <job_id> name [name...]
       fetch.py batch:<batch_id> name [name...]   -- every child job of a batch (missing files are skipped), plus test_runs/<batch_id>/index.json"""
import json, os, sys
import requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import supa  # noqa: E402

target = sys.argv[1]
names = sys.argv[2:]


def grab(job_id, strict):
    out = os.path.join("test_runs", job_id)
    os.makedirs(out, exist_ok=True)
    got = []
    for name in names:
        try:
            r = supa.download(job_id, name)
        except Exception as e:  # noqa: BLE001
            if strict:
                raise
            continue
        open(os.path.join(out, name), "wb").write(r)
        got.append(name)
        print(job_id, name, len(r))
    return got


if target.startswith("batch:"):
    bid = target.split(":", 1)[1]
    rows = requests.get(f"{supa.URL}/rest/v1/pipeline_jobs", headers=supa._h(), timeout=60, params={
        "options->>batch_id": f"eq.{bid}", "id": f"neq.{bid}", "order": "created_at",
        "select": "id,status,render_status,package_status,error,listing,program,layout,summary"}).json()
    index = []
    for j in rows:
        L = j.get("listing") or {}
        files = grab(j["id"], False)
        index.append({"id": j["id"], "address": L.get("street_address"), "city": L.get("city"), "state": L.get("state"), "acres": L.get("acres"),
                      "price": L.get("price"), "use": (j.get("program") or {}).get("use"), "status": j["status"], "render": j.get("render_status"),
                      "package": j.get("package_status"), "error": j.get("error"), "rlv": ((j.get("layout") or {}).get("rlv") or {}), "files": files})
    os.makedirs(os.path.join("test_runs", bid), exist_ok=True)
    json.dump(index, open(os.path.join("test_runs", bid, "index.json"), "w"), indent=1, default=str)
else:
    grab(target, True)
