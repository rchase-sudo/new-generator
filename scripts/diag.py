"""CI diagnostics: python scripts/diag.py <job_id>
Saves the job's cached site context (context.pkl) and tests the existing-building fetch around its parcel."""
import json
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import context, existing, supa  # noqa: E402

job_id = sys.argv[1]
out = os.path.join("test_runs", "diag_" + job_id[:8])
os.makedirs(out, exist_ok=True)
raw = supa.download(job_id, "context.pkl")
open(f"{out}/context.pkl", "wb").write(raw)
j = supa.get_job(job_id)
json.dump({k: j.get(k) for k in ("program", "zoning", "setbacks", "summary", "layout", "error", "listing")}, open(f"{out}/job.json", "w"), default=str)
ctx = pickle.loads(raw)
from pipeline.jobs import _extent_around  # noqa: E402
ext = _extent_around(ctx["parcel_sp"], ctx["k"])
bld = context.buildings(ext, ctx["epsg"])
print("buildings:", bld if isinstance(bld, dict) else len(bld))
if isinstance(bld, list):
    items = existing.prepare(bld, ctx["parcel_sp"], ctx["k"])
    for b in items:
        print(b["id"], b["onsite"], round(b["area_sf"]), round(b["pct_on_site"], 2), b["src"], b["kind"])
