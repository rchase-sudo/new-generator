"""After `modal deploy`, store the worker URL in Supabase so the `pipeline` edge function can reach it."""
import sys

import modal

sys.path.insert(0, ".")
from pipeline import supa  # noqa: E402

url = modal.Function.from_name("forma-pipeline", "start").get_web_url()
print("worker url:", url)
supa.set_config("worker_url", url)
