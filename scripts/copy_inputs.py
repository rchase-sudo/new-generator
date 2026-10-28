"""Copy the researched package inputs of one job onto another (same plan) so both packages use identical numbers.
Usage: copy_inputs.py <src_job> <dst_job>"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import supa  # noqa: E402

src, dst = sys.argv[1], sys.argv[2]
s = json.loads(supa.download(src, "package_inputs.json"))
d = json.loads(supa.download(dst, "package_inputs.json"))
d["inputs"] = s["inputs"]
p = os.path.join(tempfile.mkdtemp(), "package_inputs.json")
json.dump(d, open(p, "w"), default=str)
supa.upload(dst, p)
print("copied inputs", src, "->", dst, "plan equal:", s.get("plan") == d.get("plan"))
