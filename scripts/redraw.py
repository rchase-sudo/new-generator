"""Re-draw a job's concept plan images from its stored layout (never re-runs the layout). Usage: redraw.py <job_id> [--keep-demo]"""
import os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import jobs  # noqa: E402

job_id = sys.argv[1]
out = os.path.join("test_runs", job_id)
os.makedirs(out, exist_ok=True)
jobs.step_redraw(job_id, hide_demo="--keep-demo" not in sys.argv, out_dir=out)      # preview into test_runs first
jobs.step_redraw(job_id, hide_demo="--keep-demo" not in sys.argv)                 # then publish to the job
print("redrawn", job_id)
