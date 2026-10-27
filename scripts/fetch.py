"""Download a job's finished deliverables into test_runs/<job_id>/ (for hand-off). Usage: fetch.py <job_id> name [name...]"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import supa  # noqa: E402

job_id = sys.argv[1]
out = os.path.join("test_runs", job_id)
os.makedirs(out, exist_ok=True)
for name in sys.argv[2:]:
    r = supa.download(job_id, name)
    open(os.path.join(out, name), "wb").write(r)
    print(name, len(r))
