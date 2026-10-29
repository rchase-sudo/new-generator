"""After a lite job's preview page is built: add the listing agent to the Instantly campaign (with the page link) and to the CRM.

Needs INSTANTLY_API_KEY and CRM_SERVICE_ROLE_KEY in the worker environment (repo secrets, synced to Modal on deploy); each part is skipped when its key is
missing. Leads land in a campaign that is still a draft, so nothing is emailed until the campaign is started in Instantly."""
import os

import requests

from . import supa

CAMPAIGN_ID = os.environ.get("INSTANTLY_CAMPAIGN_ID", "b8d2dc10-ce22-4d46-b22f-37c87fec1686")   # Forma — Free Preview (3-step)
CRM_URL = os.environ.get("CRM_URL", "https://qrmqmtcxrkexazovtlev.supabase.co").rstrip("/")


def _names(full):
    parts = (full or "").replace(",", " ").split()
    return (parts[0] if parts else ""), (" ".join(parts[1:]) if len(parts) > 1 else "")


def to_instantly(job, pv, agent, F):
    key = os.environ.get("INSTANTLY_API_KEY")
    if not key:
        return "skipped: no INSTANTLY_API_KEY"
    first, last = _names(agent.get("name"))
    if not first:
        return "held: no agent name (emails open with the first name)"
    town = F.get("city") or ""
    body = {
        "campaign": CAMPAIGN_ID, "email": agent["email"], "first_name": first, "last_name": last,
        "company_name": (agent.get("company") or ""), "skip_if_in_campaign": True,
        "custom_variables": {
            "town": town, "parcel": f"{F.get('acres')}-acre" + (f" {town}" if town else ""), "listing_phrase": "vacant land listing",
            "listing_short": F.get("address") or "", "preview_link": pv["email_html"],
        },
    }
    r = requests.post("https://api.instantly.ai/api/v2/leads", json=body, headers={"Authorization": f"Bearer {key}"}, timeout=60)
    return "added" if r.status_code < 300 else f"error {r.status_code}: {r.text[:200]}"


def to_crm(job, pv, agent, F):
    key = os.environ.get("CRM_SERVICE_ROLE_KEY")
    if not key:
        return "skipped: no CRM_SERVICE_ROLE_KEY"
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "return=minimal"}
    addr = ", ".join(x for x in [F.get("address"), F.get("city"), F.get("state")] if x)
    r = requests.get(f"{CRM_URL}/rest/v1/contacts", params={"email": f"eq.{agent['email']}", "select": "id,notes"}, headers=h, timeout=30)
    r.raise_for_status()
    rows = r.json()
    note = f"Drafted lite preview built for {addr}: {pv['url']}"
    if rows:
        upd = {"listing_address": addr, "website_url": pv["url"], "notes": ((rows[0].get("notes") or "") + "\n" + note).strip()}
        r = requests.patch(f"{CRM_URL}/rest/v1/contacts", params={"id": f"eq.{rows[0]['id']}"}, json=upd, headers=h, timeout=30)
        r.raise_for_status()
        return "updated"
    row = {"name": agent.get("name") or agent["email"], "company": agent.get("company"), "email": agent["email"], "phone": agent.get("phone"),
           "status": "prospect", "source": "drafted-lite", "company_type": "real_estate", "tags": ["listing-agent", "preview-page"],
           "listing_address": addr, "website_url": pv["url"], "notes": note}
    r = requests.post(f"{CRM_URL}/rest/v1/contacts", json=row, headers=h, timeout=30)
    r.raise_for_status()
    return "added"


def find_name(agent, F):
    """The listing gave only an email: look the agent up on the web. Accept a name only when a page ties it to this email or this listing."""
    from .claude import ask_json
    try:
        out, _ = ask_json(
            "Find the full name of a commercial real estate listing agent. Reply ONLY with JSON {\"name\": string or null, \"confidence\": \"high\"|\"low\"}. "
            "Say high only when a page ties the name to this exact email address or to this exact listing; otherwise name null.",
            f"Email: {agent.get('email')}\nBrokerage: {agent.get('company') or 'unknown'}\nListing: {F.get('address')}, {F.get('city')}, {F.get('state')}",
            max_tokens=1500, web_search=3, label="Find agent name", model="claude-haiku-5-5")
    except Exception:  # noqa: BLE001
        return None
    return out.get("name") if out.get("confidence") == "high" and out.get("name") else None


def find_agent(job, F):
    """The pasted listing has no agent email: search the web for the listing agent. A result is accepted only when the email address itself
    appears on a page (brokerage staff page, listing page, profile), never a guessed pattern."""
    from .claude import ask_json
    L = job.get("listing") or {}
    try:
        out, _ = ask_json(
            "You find the listing agent for a commercial real estate listing and their business email. Search the web (LoopNet / CREXi / brokerage "
            "websites / the broker's staff page). Reply ONLY with JSON {\"name\": string or null, \"email\": string or null, \"company\": string or null, "
            "\"phone\": string or null, \"source_url\": string or null, \"confidence\": \"high\"|\"low\"}. Say high ONLY when the email address is written "
            "on a page you saw and that page ties this person to this listing or to this brokerage. Never guess an email from a name pattern; if you "
            "cannot see the email on a page, return email null.",
            f"Listing: {F.get('address')}, {F.get('city')}, {F.get('state')} {F.get('zip') or ''}\nAcres: {F.get('acres')}  Asking: {F.get('asking_price')}\n"
            f"Listing brokerage: {L.get('broker') or 'unknown'}\nLoopNet listing ID: {L.get('listing_id') or 'unknown'}\nListing summary: {F.get('listing_summary')}",
            max_tokens=2500, web_search=5, label="Find listing agent", model="claude-sonnet-5-5")
    except Exception:  # noqa: BLE001
        return {}
    if out.get("confidence") == "high" and out.get("email") and "@" in str(out["email"]):
        return {k: out[k] for k in ("name", "email", "company", "phone") if out.get(k)} | {"source_url": out.get("source_url")}
    return {}


def _crm_headers():
    key = os.environ.get("CRM_SERVICE_ROLE_KEY")
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "return=minimal"} if key else None


def crm_hold(job, pv, agent, F, reason):
    """No agent email could be found: the contact still goes into the CRM, flagged needs_contact_info with the page link, so the email can be typed
    in there and the CRM's send button then adds the agent to the Instantly campaign (see poll_requests)."""
    h = _crm_headers()
    if not h:
        return "skipped: no CRM_SERVICE_ROLE_KEY"
    addr = ", ".join(x for x in [F.get("address"), F.get("city"), F.get("state")] if x)
    L = job.get("listing") or {}
    note = (f"Drafted lite preview built for {addr}: {pv['url']}\nNo agent email found ({reason}). Type the agent's email into this contact and press send: "
            "it is then added to the Instantly Free Preview campaign.")
    r = requests.get(f"{CRM_URL}/rest/v1/contacts", params={"listing_address": f"eq.{addr}", "source": "eq.drafted-lite", "email": "is.null", "select": "id"}, headers=h, timeout=30)
    r.raise_for_status()
    if r.json():
        return "held: already in the CRM"
    row = {"name": agent.get("name") or f"Listing agent, {F.get('address')}", "company": agent.get("company") or L.get("broker"), "phone": agent.get("phone"),
           "status": "prospect", "source": "drafted-lite", "company_type": "real_estate", "tags": ["listing-agent", "preview-page", "needs-email"],
           "listing_address": addr, "website_url": pv["url"], "outreach_status": "needs_contact_info", "notes": note}
    requests.post(f"{CRM_URL}/rest/v1/contacts", json=row, headers=h, timeout=30).raise_for_status()
    return "held: added to the CRM flagged needs_contact_info"


def poll_requests():
    """Runs every few minutes (Modal schedule). Contacts the CRM's send button marked send_requested (preview-page contacts that now have an email)
    are added to the Instantly campaign with their page link, then marked sent."""
    h = _crm_headers()
    if not h:
        return
    import datetime as dt
    r = requests.get(f"{CRM_URL}/rest/v1/contacts", params={"outreach_status": "eq.send_requested", "website_url": "like.*/model/p/*",
                                                          "select": "id,name,company,email,phone,website_url,notes,touch_count"}, headers=h, timeout=30)
    r.raise_for_status()
    for c in r.json():
        res, extra = "error", ""
        try:
            slug = c["website_url"].rstrip("/").split("/")[-1]
            jr = requests.get(f"{supa.URL}/rest/v1/pipeline_jobs", params={"summary->preview->>slug": f"eq.{slug}", "select": "id"}, headers=supa._h(), timeout=30)
            jr.raise_for_status()
            if not jr.json():
                raise RuntimeError("no Drafted job for this page")
            from . import package as pkg
            job = supa.get_job(jr.json()[0]["id"])
            pv = (job.get("summary") or {}).get("preview") or {}
            F = pkg.facts(job)
            nm = (c.get("name") or "").strip()
            agent = {"email": c["email"], "company": c.get("company"), "phone": c.get("phone"),
                     **({"name": nm} if nm and "@" not in nm and not nm.lower().startswith("listing agent") else {})}
            if not agent.get("name"):
                agent["name"] = find_name(agent, F)
            if not agent.get("name"):
                res, extra = "needs_name", "Needs the agent's name: put it in the Name field and press send again."
            else:
                out = to_instantly(job, pv, agent, F)
                res = "sent" if out == "added" else "send_failed"
                extra = f"Instantly: {out}"
                s = job.get("summary") or {}
                supa.update_job(job["id"], summary={**s, "preview": {**pv, "outreach": {**(pv.get("outreach") or {}), "instantly": out + " (CRM send)"}}})
        except Exception as e:  # noqa: BLE001
            res, extra = "send_failed", f"{type(e).__name__}: {e}"[:200]
        today = dt.date.today().isoformat()
        upd = {"outreach_status": {"sent": "sent", "needs_name": "needs_contact_info"}.get(res, "send_failed"),
               "notes": ((c.get("notes") or "") + f"\n[{today}] CRM send -> {extra}").strip()}
        if res == "sent":
            upd.update({"last_contact": today, "touch_count": (c.get("touch_count") or 0) + 1, "campaign_active": True})
            requests.post(f"{CRM_URL}/rest/v1/touches", json={"contact_id": c["id"], "date": today, "type": "email",
                                                              "note": f"Added to Instantly campaign (Free Preview): {c['website_url']}"}, headers=h, timeout=30)
        requests.patch(f"{CRM_URL}/rest/v1/contacts", params={"id": f"eq.{c['id']}"}, json=upd, headers=h, timeout=30)


def push(job_id, pv, agent, F):
    """Returns {"instantly": status, "crm": status}; never raises."""
    job = supa.get_job(job_id)
    out = {}
    if not agent.get("email"):
        found = find_agent(job, F)
        if not found.get("email"):
            try:
                crm = crm_hold(job, pv, agent, F, "web search found none")
            except Exception as e:  # noqa: BLE001
                crm = f"error: {type(e).__name__}: {e}"[:200]
            return {"instantly": "held: no agent email found", "crm": crm}
        out["agent_found"] = {k: found[k] for k in ("name", "email", "source_url") if found.get(k)}
        agent = {**agent, **{k: v for k, v in found.items() if k != "source_url" and v}}
    if not agent.get("name"):
        agent = {**agent, "name": find_name(agent, F)}
        if not agent["name"]:
            agent.pop("name")
        else:
            out["name_found"] = agent["name"]
    for name, fn in (("instantly", to_instantly), ("crm", to_crm)):
        try:
            out[name] = fn(job, pv, agent, F)
        except Exception as e:  # noqa: BLE001
            out[name] = f"error: {type(e).__name__}: {e}"[:250]
    return out
