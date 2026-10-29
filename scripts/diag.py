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
for nm in ("concept_plan.png", "existing_conditions.png", "render.jpg", "model_front.png"):
    try:
        open(f"{out}/{nm}", "wb").write(supa.download(job_id, nm))
    except Exception as e:  # noqa: BLE001
        print("no", nm, e)
ctx = pickle.loads(raw)
from pipeline.jobs import _extent_around  # noqa: E402
ext = _extent_around(ctx["parcel_sp"], ctx["k"])
bld = context.buildings(ext, ctx["epsg"])
print("buildings:", bld if isinstance(bld, dict) else len(bld))
pv = context.pavement(ext, ctx["epsg"])
print("osm pavement:", pv if isinstance(pv, dict) else [(p["kind"], round(p["geom"].area * ctx["k"] ** 2)) for p in pv][:30])
from pipeline import pavement as _pv  # noqa: E402
from PIL import Image as _I  # noqa: E402
import io as _io  # noqa: E402
_a = dict(ctx["aerial"]); _a["image"] = _I.open(_io.BytesIO(_a["jpg"])).convert("RGB")
_det = _pv.detect(_a, ctx["parcel_sp"], ctx["k"], ctx.get("bld"), osm=pv if isinstance(pv, list) else [])
print("detected pavement:", [(d["id"], round(d["area_sf"]), d["kind"], d["est_stalls"]) for d in _det])
if isinstance(bld, list):
    items = existing.prepare(bld, ctx["parcel_sp"], ctx["k"])
    for b in items:
        print(b["id"], b["onsite"], round(b["area_sf"]), round(b["pct_on_site"], 2), b["src"], b["kind"])

from pipeline import topo  # noqa: E402
dem = topo.fetch(ext, ctx["epsg"], ctx["k"])
print("topo:", dem if "error" in dem else {k: (v.shape if hasattr(v, "shape") else v) for k, v in dem.items()})
if "error" not in dem:
    dem = topo.analyze(dem)
    print("summary:", topo.summary(dem, ctx["parcel_sp"]))
    st = topo.steep_polys(dem, ctx["parcel_sp"], 15)
    print("steep polys:", len(st), [round(g.area * ctx["k"] ** 2 / 43560, 2) for g in st])
    cl, iv = topo.contours(dem)
    print("contours:", len(cl), "interval", iv)
    import pickle as _p
    dem.pop("z", None)
    _p.dump(dem, open(f"{out}/dem.pkl", "wb"))
