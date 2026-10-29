"""Build (or rebuild) the warm-lead preview page for a finished lite job. Usage: preview.py <job_id>[,<job_id>...] [--force]"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import preview, supa  # noqa: E402

for jid in [j for j in sys.argv[1].split(",") if j]:
    preview.step_preview(jid, force="--force" in sys.argv)
    print(jid, (supa.get_job(jid).get("summary") or {}).get("preview"))
