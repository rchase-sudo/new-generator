"""Assemble the preliminary plan set (PDF + PNG previews) from the design engine output."""
import datetime as dt
import io
import os

import numpy as np
from PIL import Image
from matplotlib.backends.backend_pdf import PdfPages

from . import run as fdrun
from .sheets import ALL
from .sheetbase import Sheet  # noqa: F401


def _crop(aer, parcel_sp, aspect, pad):
    """Crop the NAIP aerial around the parcel to a given aspect; returns (array, (minx,miny,maxx,maxy))."""
    img = Image.open(io.BytesIO(aer["jpg"])).convert("RGB")
    ex = aer["extent"]
    minx, miny, maxx, maxy = parcel_sp.bounds
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    w, h = (maxx - minx) * pad, (maxy - miny) * pad
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    w, h = min(w, ex[2] - ex[0]), min(h, ex[3] - ex[1])
    if w / h > aspect:
        w = h * aspect
    else:
        h = w / aspect
    bx0 = min(max(cx - w / 2, ex[0]), ex[2] - w)
    by0 = min(max(cy - h / 2, ex[1]), ex[3] - h)
    box = (bx0, by0, bx0 + w, by0 + h)
    W, H = img.size
    px = lambda x: (x - ex[0]) / (ex[2] - ex[0]) * W
    py = lambda y: (ex[3] - y) / (ex[3] - ex[1]) * H
    crop = img.crop((int(px(box[0])), int(py(box[3])), int(px(box[2])), int(py(box[1]))))
    return np.asarray(crop), box


def _facts(X, lay, ctx):
    L, D, ew = X["L"], X["D"], X["earthwork"]
    g = X["grid"]
    pm = g.mask(L["parcel"])
    z = X["ex_z"][pm]
    z = z[~np.isnan(z)]
    ex = lay.get("existing") or []
    bsf = float(sum(b.area for b in L["bldgs"]))
    stalls = len(L["stalls"])
    sp_len = max(X["G"]["spine"].length, 1.0)
    dw = float(L["drive"].area / sp_len) if L.get("drive") is not None else 24.0
    gpd = X["U"]["water_gpd"]
    return {"parcel_ac": L["parcel"].area / 43560, "bldg_sf": bsf, "stalls": stalls, "ada": len(L["ada"]), "imp_sf": float(D["impervious_sf"]),
            "dist_ac": ew["disturbed_ac"], "pond_cf": sum(q_["storage_cf"] for q_ in D.get("ponds") or [D["pond"]]), "pond_ac": sum(q_["top_area_sf"] for q_ in D.get("ponds") or [D["pond"]]) / 43560,
            "ex_bldgs": sum(1 for e in ex if e.get("kind") != "pavement"), "ex_pave_sf": float(sum(e.get("sf") or 0 for e in ex if e.get("kind") == "pavement")),
            "wet": bool(L.get("wet") is not None and not L["wet"].intersection(L["parcel"]).is_empty),
            "flood": bool(L.get("flood") is not None and not L["flood"].intersection(L["parcel"]).is_empty),
            "zmin": float(z.min()) if len(z) else 0.0, "zmax": float(z.max()) if len(z) else 0.0,
            "drive_w": round(min(max(dw, 20.0), 36.0)), "ratio": stalls / (bsf / 1000.0) if bsf else 0.0,
            "reuse_sf": float((lay.get("metrics") or {}).get("reused_pavement_sf") or 0), "green_sf": X["LS"]["landscape_sf"], "w_in": 2 if gpd > 2000 else 1}


def meta_for(job, ctx, X, prog):
    L = job.get("listing") or {}
    street = L.get("street_address") or ""
    title = [x for x in [street and f"{L.get('lot') + ', ' if L.get('lot') else ''}{street}",
                         ", ".join(x for x in [L.get("city"), L.get("state")] if x),
                         L.get("county") and (L["county"] if "county" in L["county"].lower() else f"{L['county']} County")] if x]
    project = [((prog or {}).get("use") or "Site Development")] + title
    z = (job.get("zoning") or {})
    zone = None
    try:
        zone = (job.get("summary") or {}).get("zoning_district") or z.get("district")
    except Exception:  # noqa: BLE001
        pass
    today = dt.date.today()
    aer = ctx["aerial"]
    thumb, tb = _crop(aer, ctx["parcel_sp"], 228 / 218, 6.0)
    cx, cy = ctx["parcel_sp"].centroid.x, ctx["parcel_sp"].centroid.y
    star = ((cx - tb[0]) / (tb[2] - tb[0]), (tb[3] - cy) / (tb[3] - tb[1]))
    img, bb = _crop(aer, ctx["parcel_sp"], 0.40 * 30.0 / (0.52 * 19.0), 1.6)
    ring = list(ctx["parcel_sp"].exterior.coords)
    site_poly = [((x - bb[0]) / (bb[2] - bb[0]), (y - bb[1]) / (bb[3] - bb[1])) for x, y in ring]
    return {"date_short": today.strftime("%m/%d/%y"), "loc_img": thumb, "loc_star": star, "aerial_src": aer.get("source") or "NAIP",
            "project_lines": project, "zone_line": f"Zoning: {zone}" if zone else None,
            "client_lines": ["RJJ Realty" if False else "Apollo Development", "—"], "designer": "FORMA", "job_no": (job.get("id") or "0000")[:8].upper(),
            "year": today.year, "site_img": img, "site_poly": site_poly, "index": [(n, t) for n, t, _ in ALL], "facts_max_slope": 6}


def build(ctx, lay, job, out_dir, prog_cb=None, log=print):
    rules, prog = job.get("zoning") or {}, job.get("program") or {}
    def pr(s):
        log(s)
        if prog_cb:
            prog_cb(s)
    pr("Designing grading, drainage and utilities")
    X = fdrun.build(ctx, lay, job, rules, prog, log=log)
    X["parcel_sp"] = ctx["parcel_sp"]
    X["facts"] = _facts(X, lay, ctx)
    meta = meta_for(job, ctx, X, prog)
    os.makedirs(out_dir, exist_ok=True)
    pdf_path = os.path.join(out_dir, "plan_set.pdf")
    pngs = []
    with PdfPages(pdf_path) as pdf:
        from . import sheets as _sh
        wins = _sh.windows(X)
        X["_wins"] = wins
        X["sheet_list"] = []
        for n, title, fn in ALL:
            pr(f"Drawing {n} {title.title()}")
            for w in (wins if (wins and n in _sh.SPLIT) else [None]):
                X["_win"] = w
                S = fn(meta, X)
                name = f"{n}.{w['k']}" if w else n
                png = os.path.join(out_dir, f"sheet_{name}.png")
                S.save(pdf=pdf, png=png, dpi=60)
                pngs.append(png)
                X["sheet_list"].append(name)
        X.pop("_win", None)
        X.pop("_wins", None)
    files = {}
    try:
        from . import export
        pr("Writing DXF and LandXML")
        files["design.dxf"] = export.dxf(X, os.path.join(out_dir, "design.dxf"))
        files["surfaces_pipes.xml"] = export.landxml(X, os.path.join(out_dir, "surfaces_pipes.xml"), name=" ".join(meta["project_lines"][:2]))
        try:
            from . import c3d
            pr("Writing the Civil 3D package")
            files["drafted_c3d.zip"] = c3d.package(X, meta, job, out_dir, files["design.dxf"], files["surfaces_pipes.xml"], template=job.get("_cad_template"))
        except Exception as e:  # noqa: BLE001
            log(f"civil 3d package failed: {type(e).__name__}: {e}")
            X.setdefault("export_error", f"civil 3d package: {type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001  (the plan set itself never fails because of an export)
        log(f"export failed: {type(e).__name__}: {e}")
        X.setdefault("export_error", f"{type(e).__name__}: {e}")
    return {"pdf": pdf_path, "pngs": pngs, "X": X, "meta": meta, "files": files}


def summary(X):
    f, D, ew = X["facts"], X["D"], X["earthwork"]
    return {"parcel_ac": round(f["parcel_ac"], 2), "pond_cf": round(f["pond_cf"]), "pond_top_sf": round(sum(q_["top_area_sf"] for q_ in D.get("ponds") or [D["pond"]])), "ponds": len(D.get("ponds") or [1]), "orifice_in": D["pond"]["orifice_in"],
            "cut_cy": ew["cut_cy"], "fill_cy": ew["fill_cy"], "disturbed_ac": ew["disturbed_ac"], "npdes": bool(X["ER"]["npdes"]),
            "hsg": D["hsg"], "rain_source": X["rain"]["source"], "soils_source": X["soils"]["source"],
            "warnings": list(D["warnings"]) + list(X["net"]["warnings"]) + list(X["U"]["warnings"]) + list(X["FIRE"]["warnings"]) + list(X["G"]["warnings"]),
            "sheets": X.get("sheet_list") or [n for n, _, _ in ALL], "notes": list(X.get("fire_notes") or []),
            "grade_checks": [list(r) for r in X.get("grade_checks") or []]}
