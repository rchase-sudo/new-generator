"""Residual land value check against the program's minimum (program["min_rlv"], dollars).

Uses the same researched rents / costs / cap rate and pro forma as the due diligence package, so the layout card and the
package agree. Residual land value = value at the cap rate (NOI / cap) less all development costs excluding land.
Flag only: a shortfall adds a warning to the layout, it never blocks anything."""
import json

from . import package as pkg, supa


def check(job_id, job, metrics, log=print):
    prog = job.get("program") or {}
    raw = prog.get("min_rlv")
    try:
        mn = float(raw)
    except (TypeError, ValueError):
        return None
    F = pkg.facts({**job, "layout": metrics})
    plan = json.loads(json.dumps(F["plan"], default=str))
    I = None
    try:    # reuse the research when the plan has not changed (same cache the package step uses)
        prev = json.loads(supa.download(job_id, "package_inputs.json"))
        if prev.get("plan") == plan and prev.get("inputs") and "gross_lease" in prev["inputs"] and "soft_rates" in prev["inputs"]:
            I = prev["inputs"]
    except Exception:  # noqa: BLE001
        pass
    if I is None:
        log("residual land value: researching rents, costs and cap rate")
        I = pkg.research_inputs(F)
        import os, tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "package_inputs.json")
        with open(p, "w") as fh:
            json.dump({"plan": plan, "inputs": I}, fh, default=str)
        supa.upload(job_id, p)
    PF = pkg.proforma(F, I)
    resid = float(PF["resid"])
    r = {"min": mn, "residual": round(resid, -3), "ok": resid >= mn, "cap_rate": PF["cap"], "noi": PF["out"]["C"]["noi"],
         "value": PF["out"]["C"]["value"], "cost_ex_land": PF["C"]["tdc"]}
    if not r["ok"]:
        metrics.setdefault("warnings", []).append(
            f"Residual land value about ${resid:,.0f} is below the ${mn:,.0f} minimum (after required developer profit, {PF['profit_basis']}, "
            f"and ${r['cost_ex_land']:,.0f} development cost, land excluded)")
    metrics["rlv"] = r
    return r


# ---------------------------------------------------------------------------------------------- sizing
def _sf(F):
    return {b["name"]: float(pkg._num(b.get("footprint_sf"))) * max(1, int(pkg._num(b.get("stories"), 1))) for b in F["buildings"]}


def _scaled_inputs(I0, F0, F1):
    """The researched inputs of the base plan re-expressed for a resized plan: vertical cost per SF, site cost with impervious area."""
    sf0, sf1 = _sf(F0), _sf(F1)
    tot0 = sum(sf0.values()) or 1.0
    v_psf = sum(pkg._num(r.get("subtotal")) for r in I0.get("vertical") or []) / tot0
    imp0 = float(pkg._num((F0["plan"] or {}).get("impervious_sf"))) or 1.0
    imp1 = float(pkg._num((F1["plan"] or {}).get("impervious_sf"))) or imp0
    rents = {r.get("building"): pkg._num(r.get("rent_psf")) for r in I0.get("rents") or []}
    _pos = [v for v in rents.values() if v > 0]
    avg_rent = (sum(_pos) / len(_pos)) if _pos else 24.0
    I = dict(I0)
    I["site"] = [{**r, "subtotal": pkg._num(r.get("subtotal")) * imp1 / imp0} for r in I0.get("site") or []]
    I["vertical"] = [{"item": "Vertical construction", "qty": f"{sf:,.0f} SF", "unit": "$/SF", "subtotal": v_psf * sf, "building": n}
                     for n, sf in sf1.items()]
    I["rents"] = [{"building": n, "rent_psf": rents.get(n, avg_rent), "basis": "scaled from researched rent"} for n in sf1]
    I["phase1"] = list(sf1)
    if I0.get("for_sale"):
        pr = {r.get("building"): pkg._num(r.get("price_psf")) for r in I0.get("sale_psf") or []}
        avg_p = (sum(pr.values()) / len(pr)) if pr else 300.0
        I["sale_psf"] = [{"building": n, "price_psf": pr.get(n, avg_p), "basis": "scaled from researched sale price"} for n in sf1]
    return I


def _resid(F, I):
    PF = pkg.proforma(F, I)
    return PF, float(PF["resid"])


def _grown(F, g):
    """Copy of the facts with every building's footprint and the impervious area scaled by g (the search model)."""
    F2 = json.loads(json.dumps(F, default=str))
    for b in F2["buildings"]:
        b["footprint_sf"] = pkg._num(b.get("footprint_sf")) * g
    F2["plan"]["impervious_sf"] = pkg._num(F["plan"].get("impervious_sf")) * g ** 0.85
    return F2


def size_program(job_id, job, prog, res, regenerate, log=print):
    """Grow the program until the residual land value meets program['min_rlv']. Returns (prog, res). The site decides the
    ceiling: when the layout cannot hold the building area needed the result keeps the best layout and says so."""
    try:
        mn_user = float(prog.get("min_rlv"))
    except (TypeError, ValueError):
        mn_user = 0.0
    mn = mn_user           # replaced below by max(user minimum, cost of the land): the land cost is always in
    job0 = {**job, "program": prog}
    F0 = pkg.facts({**job0, "layout": res["metrics"]})
    plan0 = json.loads(json.dumps(F0["plan"], default=str))
    I0 = None
    try:
        prev = json.loads(supa.download(job_id, "package_inputs.json"))
        if prev.get("plan") == plan0 and prev.get("inputs") and "gross_lease" in prev["inputs"] and "soft_rates" in prev["inputs"]:
            I0 = prev["inputs"]
    except Exception:  # noqa: BLE001
        pass
    if I0 is None:
        log("residual land value: researching rents, costs and cap rate")
        I0 = pkg.research_inputs(F0)
        try:    # keep the research so a rerun and the due diligence package reuse it
            import os, tempfile
            d_ = tempfile.mkdtemp()
            p_ = os.path.join(d_, "package_inputs.json")
            with open(p_, "w") as fh:
                json.dump({"plan": plan0, "inputs": I0}, fh, default=str)
            supa.upload(job_id, p_)
        except Exception:  # noqa: BLE001
            pass
    base_gfa = float(res["metrics"].get("gfa_sf") or 0)
    PF0, r0 = _resid(F0, I0)
    # The asking price is NEVER part of the residual land value calculation or its minimum.
    mn = mn_user
    note = {"min": mn, "min_user": mn_user, "cap_rate": PF0["cap"], "base_residual": round(r0, -3), "base_gfa_sf": round(base_gfa)}
    best_prog, best_res, best_r = prog, res, r0
    cur_prog, cur_res, cur_F, cur_r = prog, res, F0, r0
    max_st = int(pkg._num((job.get("zoning") or {}).get("max_stories"))) or None
    if not max_st and pkg._num((job.get("zoning") or {}).get("max_height_ft")):
        max_st = max(1, int(pkg._num(job["zoning"]["max_height_ft"]) // 12))
    for it in range(4):
        if cur_r >= mn:
            break
        # smallest uniform growth that reaches the minimum, on the cost model scaled from the researched inputs
        g = next((x / 100 for x in range(105, 801, 5) if _resid(_grown(cur_F, x / 100), _scaled_inputs(I0, F0, _grown(cur_F, x / 100)))[1] >= mn), None)
        if g is None:
            note["unreachable"] = True
            break
        bl = [dict(b) for b in cur_prog["buildings"]]
        stories = {int(b.get("stories") or 1) for b in bl}
        if max_st and len(stories) == 1 and next(iter(stories)) < max_st:      # add floors first where the height limit allows
            s0 = next(iter(stories))
            s1 = min(max_st, max(s0 + 1, int(round(s0 * g))))
            for b in bl:
                b["stories"] = s1
            g = g * s0 / s1
        if g > 1.01:
            for b in bl:
                b["footprint_sf"] = round(float(b["footprint_sf"]) * g, -1)
        newp = {**cur_prog, "buildings": bl}
        req = (cur_prog.get("parking") or {}).get("required")
        if isinstance(req, (int, float)) and req > 0:
            newp["parking"] = {**cur_prog["parking"], "required": round(req * sum(float(b["footprint_sf"]) * int(b.get("stories") or 1) for b in bl)
                                                                         / sum(float(b["footprint_sf"]) * int(b.get("stories") or 1) for b in cur_prog["buildings"]))}
        log(f"residual land value ${cur_r:,.0f} < ${mn:,.0f}: growing the program (pass {it + 1})")
        def _try(p_):
            try:
                return regenerate(p_)
            except RuntimeError:
                return None
        r2 = _try(newp)
        held = lambda r_: r_ is not None and float(r_["metrics"].get("gfa_sf") or 0) > float(cur_res["metrics"].get("gfa_sf") or 0) * 1.01  # noqa: E731
        if not held(r2) and float(newp.get("gap_ft") or 0) > 40:
            # open space gives way to value: pull the buildings back to the standard spacing and try again
            newp = {k_: v_ for k_, v_ in newp.items() if k_ != "gap_ft"}
            r2 = _try(newp)
            note["open_space_reduced"] = True
        if not held(r2):
            note["site_limit"] = True        # the layout could not hold more building area
            break
        F2 = pkg.facts({**job, "program": newp, "layout": r2["metrics"]})
        _, ra = _resid(F2, _scaled_inputs(I0, F0, F2))
        cur_prog, cur_res, cur_F, cur_r = newp, r2, F2, ra
        if ra > best_r:
            best_prog, best_res, best_r = newp, r2, ra
    prog, res = best_prog, best_res
    note.update({"residual": round(best_r, -3), "ok": best_r >= mn, "gfa_sf": round(float(res["metrics"].get("gfa_sf") or 0)),
                 "value": None, "sized": res is not None and note.get("base_gfa_sf") != round(float(res["metrics"].get("gfa_sf") or 0))})
    res["metrics"]["rlv"] = note
    if not note["ok"]:
        why = ("the site cannot hold more building area" if note.get("site_limit") else
               "the researched rents and costs do not reach it at any building size" if note.get("unreachable") else "the program could not be grown enough")
        res["metrics"].setdefault("warnings", []).append(
            f"Residual land value about ${best_r:,.0f} is below the ${mn:,.0f} minimum: {why}.")
    return prog, res
