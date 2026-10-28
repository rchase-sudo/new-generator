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
        if prev.get("plan") == plan and prev.get("inputs"):
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
            f"Residual land value about ${resid:,.0f} is below the ${mn:,.0f} minimum (value ${r['value']:,.0f} at a {PF['cap'] * 100:.1f}% cap rate "
            f"less ${r['cost_ex_land']:,.0f} development cost, land excluded)")
    metrics["rlv"] = r
    return r
