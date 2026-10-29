"""Warm-lead preview page for a finished lite job (the layout, copy rules and template live in rchase-sudo/model: tools/PREVIEW_PAGES.md).

The worker fetches the current template + generator from the model repo (so editing the template there changes every future page), builds
p/<slug>/ with the watermark baked into the images, and uploads the folder to the public `previews` bucket. A cron workflow in the model repo
copies new folders into p/ and GitHub Pages serves them. The job summary keeps the link and the email snippet (visible text formatted as the
formadesigns.io/p/<slug> address, real href on GitHub Pages)."""
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import requests

from . import supa
from .claude import ask_tool

RAW = "https://raw.githubusercontent.com/rchase-sudo/model/main/tools/"
PAGES = "https://rchase-sudo.github.io/model/p/"
BUCKET = "previews"
SHOW = "formadesigns.io/p/"


def slugify(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def _put(key, data, ctype):
    last = None
    for a in range(5):
        try:
            r = requests.post(f"{supa.URL}/storage/v1/object/{BUCKET}/{key}", data=data, headers=supa._h({"Content-Type": ctype, "x-upsert": "true"}), timeout=120)
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return
            last = r.status_code
        except (requests.ConnectionError, requests.Timeout) as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(2 * (a + 1))
    raise RuntimeError(f"preview upload failed ({last})")


COPY_SYS = """You write the copy for a one-page preview that a one-person design studio (Forma Designs, Ryan Chase) emails to a listing agent for their vacant-land listing. The page shows a rendering and concept plan of what a buyer could build.
Rules: first person singular ("I"), never "we". Plain, direct, no hype. The page only promotes the listing: never say the price is too high, never mention shortfalls, risks, gaps or what the land is not good for. Use ONLY the facts provided, never invent numbers. State zoning as the listing gives it; never write hedges such as "to be confirmed", "listing says" or any discrepancy.
Return:
- headline: "{Address}, *built out.*" (asterisks mark the gold word; keep that pattern unless the property has a clearly better short line)
- facts: 4 to 6 short chips (town/state/county, acres, zoning, frontage, a location hook) from the facts only
- note: 1 to 2 sentences, what I planned and why it fits this lot, ending exactly with "Here's a preview of the plan and rendering."
- use: the use in a few lowercase words (e.g. "self-storage")
- numbers: EXACTLY 3 {value,label,gold}. First is about the land (buildable area / acreage), second is about the program (gold true; building SF, parking, access), third is about upside (room left, extra pads, units). label starts with <b>Short bold phrase</b> then one sentence. Figures only from the facts.
- plan_heading: short, with one *emphasized* word.
- plan_text: 2 sentences describing the layout in plain words (access, circulation, why it works)."""

COPY_PROPS = {
    "headline": {"type": "string"}, "facts": {"type": "array", "items": {"type": "string"}}, "note": {"type": "string"}, "use": {"type": "string"},
    "numbers": {"type": "array", "items": {"type": "object", "properties": {"value": {"type": "string"}, "label": {"type": "string"}, "gold": {"type": "boolean"}},
                                           "required": ["value", "label"]}},
    "plan_heading": {"type": "string"}, "plan_text": {"type": "string"},
}


def _tools():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "tools"))
    for f in ("make_preview.py", "preview_template.html", "watermark.py"):
        r = requests.get(RAW + f, timeout=60)
        r.raise_for_status()
        with open(os.path.join(d, "tools", f), "wb") as fh:
            fh.write(r.content)
    return d


def step_preview(job_id, force=False):
    """Build and publish the preview page for a job. Never raises: the outcome is stored in summary.preview."""
    def save(**kw):
        job = supa.get_job(job_id)
        s = job.get("summary") or {}
        supa.update_job(job_id, summary={**s, "preview": {**(s.get("preview") or {}), **kw}})
    try:
        from . import package as pkg
        job = supa.get_job(job_id)
        F = pkg.facts(job)
        asking = F.get("asking_price") or 0
        lite = bool((job.get("options") or {}).get("lite"))
        if lite:
            # Lite pages have no package behind them: the investor math is an illustrative figure set, pitched slightly above the asking price,
            # and the page carries the "Illustrative figures" footnote (PREVIEW_PAGES.md: made-up figures keep that footnote).
            sf = float((F.get("plan") or {}).get("gfa_sf") or (F.get("plan") or {}).get("building_sf") or 0)
            resid = round(asking * 1.08, -3) if asking else 0
            hard = round(sf * 165, -3)
            soft = round(hard * 0.22, -3)
            profit = round(0.15 * (hard + soft), -3)
            value = resid + hard + soft + profit
            show_rlv = bool(asking and sf)
            note = "Illustrative figures for this preview. The full package shows every input behind them."
        else:
            if (job.get("package_status") or "") != "completed":
                raise RuntimeError("Build the package first")
            prev = json.loads(supa.download(job_id, "package_inputs.json"))
            PF = pkg.proforma(F, prev["inputs"])
            value, hard, profit, resid = PF["out"]["C"]["value"], PF["C"]["hard"], PF["profit"], PF["resid"]
            show_rlv = resid > 0 and (not asking or resid >= asking or force)
            soft = value - hard - profit - resid
            note = "Preliminary estimate. The full package shows every input behind it."
        info = {"resid": round(resid), "asking": round(asking) if asking else None, "rlv_shown": bool(show_rlv), "illustrative": lite}
        L = job.get("listing") or {}
        slug = slugify(" ".join(x for x in [L.get("lot"), L.get("street_address") or F["address"], F.get("city")] if x)) or job_id[:8]
        copy = ask_tool(COPY_SYS, "FACTS:\n" + json.dumps({k: F.get(k) for k in (
            "address", "city", "state", "county", "acres", "listing_zoning", "district", "frontage_ft", "fronting_roads", "buildable_acres", "wetland_acres",
            "use", "rationale", "buildings", "plan", "utilities", "listing_summary")}, default=str), "submit_copy", "Submit the page copy.",
            max_tokens=2500, label="Preview page copy", properties=COPY_PROPS, model="claude-haiku-5-5")
        if len(copy.get("numbers") or []) != 3:
            raise RuntimeError("copy did not return exactly 3 numbers")
        tmp = tempfile.mkdtemp()
        rend = (job.get("outputs") or {}).get("render") or {}
        rname = "render.jpg" if "render.jpg" in rend else "render.png"
        for name, fn in ((rname, "render_src"), ("concept_plan.png", "plan_src")):
            with open(os.path.join(tmp, fn), "wb") as fh:
                fh.write(supa.download(job_id, name))
        from PIL import Image    # the plan sheet's title block stays out of the preview: keep only the drawing frame
        pp = os.path.join(tmp, "plan_src")
        im = Image.open(pp)
        W, H = im.size
        im.crop((int(W * 0.040), int(H * 0.045), int(W * 0.876), int(H * 0.955))).convert("RGB").save(pp, "JPEG", quality=93)
        agent = {k: L.get("agent_" + k) for k in ("name", "email", "phone")}
        agent["company"] = L.get("broker")
        cfg = {
            "slug": slug, "title": F["address"], "address_short": F["address"],
            "address_full": ", ".join(x for x in [F["address"], F.get("city"), F.get("state")] if x),
            "agent": {k: v for k, v in agent.items() if v},
            "headline": copy["headline"], "facts": copy["facts"][:6], "note": copy["note"], "use": copy["use"], "numbers": copy["numbers"],
            "plan_heading": copy["plan_heading"], "plan_text": copy["plan_text"],
            **({"rlv": {"value": round(value), "hard": round(hard), "soft": round(soft), "profit": round(profit),
                        **({"asking": round(asking)} if asking and resid >= asking else {}),
                        "note": note}} if show_rlv else {}),
            "images": {"render": os.path.join(tmp, "render_src"), "plan": os.path.join(tmp, "plan_src"), "plan_dark": False},
        }
        root = _tools()
        cp = os.path.join(root, "cfg.json")
        with open(cp, "w") as fh:
            json.dump(cfg, fh)
        res = subprocess.run([sys.executable, os.path.join(root, "tools", "make_preview.py"), cp], capture_output=True, text=True, timeout=300)
        if res.returncode:
            raise RuntimeError("generator failed: " + (res.stderr or res.stdout)[-600:])
        out = os.path.join(root, "p", slug)
        for dp, _, fs in os.walk(out):
            for f in fs:
                p = os.path.join(dp, f)
                key = slug + "/" + os.path.relpath(p, out).replace(os.sep, "/")
                with open(p, "rb") as fh:
                    _put(key, fh.read(), "text/html" if f.endswith(".html") else "image/jpeg")
        url = PAGES + slug + "/"
        show = SHOW + slug
        save(status="built", slug=slug, url=url, display=show, email_html=f'<a href="{html.escape(url)}">{show}</a>', reason=None,
             agent={k: v for k, v in agent.items() if v}, **info)
    except Exception as e:  # noqa: BLE001
        try:
            save(status="error", reason=f"{type(e).__name__}: {e}"[:500])
        except Exception:  # noqa: BLE001
            pass
