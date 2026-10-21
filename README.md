# Forma concept pipeline

Pasted listing text → parcel → existing-conditions drawing → (next) layout → sheet → render → package → website.

## Pieces
| Part | Where | What |
|---|---|---|
| `supabase/functions/pipeline` | Supabase **Concept** project | API for the CRM and the tool: `start`, `approve_parcel`, `status`, `list` |
| `supabase/migrations` | Concept DB | `pipeline_jobs`, `pipeline_config`, private `pipeline` storage bucket |
| `modal_app.py` + `pipeline/` | Modal | Python worker. Supabase calls its `start` endpoint; steps run in the background |
| `.github/workflows/deploy.yml` | GitHub Actions | On push to `main`: syncs secrets to Modal, deploys, registers the worker URL in Supabase |

## Job flow (build step 1)
```
start ──► parsing ──► awaiting_parcel ──(approve_parcel)──► building_ec ──► ec_ready
                                                       any failure ──► error
```
`ec_ready` outputs (in the `pipeline` bucket under the job id): `existing_conditions.dxf` (state-plane feet,
Forma layers, aerial referenced as `aerial.jpg`), `existing_conditions.pdf`, `.png`, `aerial.jpg`, `aerial.jgw`.

## Data sources
- Parcels: Regrid API (when the token covers the county), else statewide/county ArcGIS layers (auto-discovered)
- Aerial: USDA NAIP via USGS National Map (free)
- Wetlands: USFWS NWI · Flood: FEMA NFHL · Roads: OpenStreetMap (Overpass)

## Repo secrets (Settings → Secrets and variables → Actions)
`MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `SUPABASE_SERVICE_ROLE_KEY`, `REGRID_TOKEN`, `ANTHROPIC_API_KEY`

## Calling it
```js
await supabase.functions.invoke("pipeline", { body: { action: "start", listing_text } })          // -> { job_id }
await supabase.functions.invoke("pipeline", { body: { action: "status", job_id } })               // poll
await supabase.functions.invoke("pipeline", { body: { action: "approve_parcel", job_id, index: 0,
                                                       setbacks: { front: 65, side: 20, rear: 20 } } })
```
