"""Minimal Supabase REST + Storage client (service role)."""
import datetime as dt
import mimetypes
import os

import requests

URL = os.environ.get("SUPABASE_URL", "https://mmaqwemqbxwngqxtnsug.supabase.co").rstrip("/")
BUCKET = "pipeline"


def _h(extra=None):
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}", **(extra or {})}


def get_job(job_id):
    r = requests.get(f"{URL}/rest/v1/pipeline_jobs", params={"id": f"eq.{job_id}", "select": "*"}, headers=_h(), timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise KeyError(job_id)
    return rows[0]


def update_job(job_id, **fields):
    fields["updated_at"] = dt.datetime.utcnow().isoformat() + "Z"
    r = requests.patch(f"{URL}/rest/v1/pipeline_jobs", params={"id": f"eq.{job_id}"}, json=fields,
                       headers=_h({"Content-Type": "application/json", "Prefer": "return=minimal"}), timeout=30)
    r.raise_for_status()


def log(job_id, msg):
    try:
        requests.post(f"{URL}/rest/v1/rpc/pipeline_log", json={"job": job_id, "msg": msg},
                      headers=_h({"Content-Type": "application/json"}), timeout=15)
    except Exception:  # noqa: BLE001
        pass


def upload(job_id, local_path, name=None):
    name = name or os.path.basename(local_path)
    key = f"{job_id}/{name}"
    ctype = mimetypes.guess_type(name)[0] or ("application/dxf" if name.endswith(".dxf") else "application/octet-stream")
    with open(local_path, "rb") as f:
        r = requests.post(f"{URL}/storage/v1/object/{BUCKET}/{key}", data=f.read(),
                          headers=_h({"Content-Type": ctype, "x-upsert": "true"}), timeout=120)
    r.raise_for_status()
    return key


def set_config(key, value):
    r = requests.post(f"{URL}/rest/v1/pipeline_config", json={"key": key, "value": value},
                      headers=_h({"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}), timeout=30)
    r.raise_for_status()


def download(job_id, name):
    r = requests.get(f"{URL}/storage/v1/object/{BUCKET}/{job_id}/{name}", headers=_h(), timeout=120)
    r.raise_for_status()
    return r.content
