"""Delete preview pages (bucket objects) for the slugs listed in a file: python scripts/purge_previews.py tests/homes/old_slugs.txt"""
import os
import sys

import requests

BASE = "https://mmaqwemqbxwngqxtnsug.supabase.co/storage/v1"
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def ls(prefix):
    r = requests.post(f"{BASE}/object/list/previews", json={"prefix": prefix, "limit": 1000}, headers=H, timeout=60)
    r.raise_for_status()
    return r.json()


total = 0
for slug in [x.strip() for x in open(sys.argv[1]) if x.strip()]:
    paths = [f"{slug}/{e['name']}" for e in ls(slug) if e.get("id")] + [f"{slug}/img/{e['name']}" for e in ls(f"{slug}/img") if e.get("id")]
    if not paths:
        continue
    r = requests.delete(f"{BASE}/object/previews", json={"prefixes": paths}, headers=H, timeout=60)
    print(slug, len(paths), r.status_code, flush=True)
    total += len(paths)
print("deleted", total)
