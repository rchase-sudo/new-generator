"""Modal worker for the Forma concept pipeline.

Supabase (edge function `pipeline`) calls the `start` web endpoint with {job_id, step}; the endpoint
spawns `run_step` in the background and returns immediately.
"""
import os

import modal

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libpango-1.0-0", "libpangoft2-1.0-0", "libharfbuzz0b", "libharfbuzz-subset0", "fontconfig", "fonts-inter")
    .pip_install(
        "requests==2.32.3", "shapely==2.0.6", "pyproj==3.7.0", "numpy==1.26.4", "pillow==10.4.0",
        "matplotlib==3.9.2", "scipy==1.13.1", "ezdxf==1.3.4", "fastapi[standard]==0.115.0", "weasyprint==70.0", "pydyf==0.13.0",
    )
    .env({"FORMA_ASSETS": "/assets"})
    .add_local_python_source("pipeline")
    .add_local_dir("pipeline/assets", remote_path="/assets")
)

# Blender (bpy) for the 3D model render -- same Cycles scene as the Pocahontas Trail model
render_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("libxrender1", "libxxf86vm1", "libxfixes3", "libxi6", "libxkbcommon0", "libsm6", "libgl1", "libegl1",
                 "libx11-6", "libxext6", "libgomp1")
    .pip_install(
        "bpy==5.0.1", "requests==2.32.3", "shapely==2.0.6", "pyproj==3.7.0", "numpy==1.26.4", "pillow==10.4.0",
        "matplotlib==3.9.2", "ezdxf==1.3.4",
    )
    .env({"FORMA_ASSETS": "/assets"})
    .add_local_python_source("pipeline")
    .add_local_dir("pipeline/assets", remote_path="/assets")
)

app = modal.App("forma-pipeline", image=image)
secrets = [modal.Secret.from_name("forma-pipeline")]


@app.function(secrets=secrets, timeout=1800, cpu=2.0, memory=4096, retries=0)
def run_step(job_id: str, step: str):
    from pipeline.jobs import run

    run(job_id, step)


@app.function(secrets=secrets, image=render_image, timeout=2400, cpu=8.0, memory=12288, retries=0)
def run_render(job_id: str):
    from pipeline.jobs import run

    run(job_id, "render")


@app.function(secrets=secrets, timeout=300, schedule=modal.Period(minutes=3))
def poll_outreach():
    """CRM send button -> Instantly: picks up contacts marked send_requested (see pipeline/outreach.py)."""
    from pipeline import outreach

    outreach.poll_requests()
    from pipeline import auto
    try:
        auto.refresh_batches()
    except Exception as e:  # noqa: BLE001
        print("refresh_batches failed:", repr(e))


@app.function(secrets=secrets, timeout=60)
@modal.asgi_app()
def start():
    """POST / {job_id, step} with `Authorization: Bearer <worker_token>` (sha256 of the service key, stored in pipeline_config)."""
    from fastapi import FastAPI, Header, HTTPException

    web = FastAPI()

    @web.post("/")
    def kick(body: dict, authorization: str = Header(default="")):
        import hashlib
        token = hashlib.sha256(os.environ["SUPABASE_SERVICE_ROLE_KEY"].encode()).hexdigest()
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="unauthorized")
        job_id, step = body.get("job_id"), body.get("step", "parse")
        if not job_id:
            raise HTTPException(status_code=400, detail="job_id required")
        call = run_render.spawn(job_id) if step == "render" else run_step.spawn(job_id, step)
        return {"ok": True, "call_id": call.object_id}

    @web.get("/health")
    def health():
        return {"ok": True}

    return web


@app.local_entrypoint()
def smoke(job_id: str, step: str = "parse"):
    """modal run modal_app.py --job-id <uuid> --step parse   (runs a step synchronously, for debugging)"""
    run_step.remote(job_id, step)
