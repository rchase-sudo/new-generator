// Forma concept pipeline API (Concept project).
// POST { action: "start" | "approve_parcel" | "approve_zoning" | "approve_layout" | "render" | "rerun" | "status" | "list" | "delete", ... }
//   start           { listing_text, source?: "tool"|"crm", crm_ref? }            -> { job_id }
//   approve_parcel  { job_id, index? | indexes?: number[], geometry?, setbacks? } -> { ok }  (several indexes = assemblage)
//   approve_zoning  { job_id, setbacks?, parking_setbacks?, program?, zoning?, drive_road? } -> { ok }  (runs / re-runs the layout)
//                   zoning.building_actions = { "E1": "keep" | "demo" } sets what happens to each existing building;
//                   zoning.max_slope_pct = steepest ground (%) buildings / parking may use (0 = ignore slope)
//   approve_layout  { job_id }                                                    -> { ok }  (starts the 3D model + render)
//   render          { job_id }                                                    -> { ok }  (re-run the render)
//   package         { job_id }                                                    -> { ok }  (due diligence package, after the render)
//   design          { job_id, sewer?: boolean, water?: boolean, utilities?: "municipal"|"onsite" } -> { ok }  (optional full design phase, after the layout is approved)
//   start_batch     { text, lite? }  -> { job_id }  (many listings in one field: the worker splits them and runs each one unattended to its package)
//   start also takes options { lite?, auto? }: lite = cheap package research (same plan + render), auto = no approval checkpoints
//   rerun           { job_id, step: "parse"|"ec"|"zoning"|"layout"|"buildings"|"topo" }              -> { ok }
//   status          { job_id }                                                    -> job row + signed file URLs
//   list            { limit? }                                                    -> recent jobs (your own; service role: all)
//   delete          { job_id }                                                    -> { ok }
import { createClient } from "npm:@supabase/supabase-js@2.45.4";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json" } });

const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const db = createClient(SB_URL, SERVICE, { auth: { persistSession: false } });

async function kickWorker(job_id: string, step: string) {
  const { data } = await db.from("pipeline_config").select("key,value").in("key", ["worker_url", "worker_token"]);
  const cfg = Object.fromEntries((data ?? []).map((r: { key: string; value: string }) => [r.key, r.value]));
  if (!cfg.worker_url || !cfg.worker_token) throw new Error("Worker not registered yet (deploy the Modal worker first)");
  const r = await fetch(cfg.worker_url.replace(/\/$/, "") + "/", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${cfg.worker_token}` },
    body: JSON.stringify({ job_id, step }),
  });
  if (!r.ok) throw new Error(`Worker returned ${r.status}: ${await r.text()}`);
}

async function signOutputs(outputs: Record<string, Record<string, string>>) {
  const signed: Record<string, Record<string, string>> = {};
  for (const [step, files] of Object.entries(outputs ?? {})) {
    signed[step] = {};
    for (const [name, key] of Object.entries(files)) {
      const { data } = await db.storage.from("pipeline").createSignedUrl(key, 3600);
      if (data?.signedUrl) signed[step][name] = data.signedUrl;
    }
  }
  return signed;
}

// The gateway (verify_jwt) has already checked the signature, so the role claim can be trusted.
function isServiceRole(token: string): boolean {
  if (!token) return false;
  if (token === SERVICE) return true;
  try {
    const part = token.split(".")[1];
    const payload = JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((part.length + 3) % 4)));
    return payload?.role === "service_role";
  } catch (_) { return false; }
}

// Who is calling: the service role (CI / CRM backend) sees everything; a signed-in user only their own jobs.
async function caller(req: Request): Promise<{ admin: boolean; uid: string | null }> {
  const token = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (isServiceRole(token)) return { admin: true, uid: null };
  if (!token) return { admin: false, uid: null };
  const { data } = await db.auth.getUser(token);
  return { admin: false, uid: data?.user?.id ?? null };
}

async function ownJob(who: { admin: boolean; uid: string | null }, id: string) {
  if (!id) return false;
  if (who.admin) return true;
  const { data } = await db.from("pipeline_jobs").select("created_by").eq("id", id).maybeSingle();
  return !!data && data.created_by === who.uid;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const body = await req.json();
    const action = body.action;
    const who = await caller(req);
    if (!who.admin && !who.uid) return json({ error: "Sign in required" }, 401);
    if (action !== "start" && action !== "start_batch" && action !== "list" && !(await ownJob(who, body.job_id))) return json({ error: "Job not found" }, 404);

    if (action === "start") {
      const text = String(body.listing_text ?? "").trim();
      if (text.length < 20) return json({ error: "Paste the full listing text" }, 400);
      const { data, error } = await db.from("pipeline_jobs")
        .insert({ listing_text: text, source: body.source ?? "tool", crm_ref: body.crm_ref ?? null, status: "queued", step: "parse",
                  created_by: who.uid, options: { lite: !!body.options?.lite, auto: !!body.options?.auto } })
        .select("id").single();
      if (error) throw error;
      await kickWorker(data.id, "parse");
      return json({ job_id: data.id });
    }

    if (action === "start_batch") {   // many listing descriptions in one field -> the worker splits them and starts one auto job per listing
      const text = String(body.text ?? "").trim();
      if (text.length < 20) return json({ error: "Paste the listings" }, 400);
      const { data, error } = await db.from("pipeline_jobs")
        .insert({ listing_text: text, source: "batch", status: "queued", step: "batch", progress: "Splitting the listings",
                  created_by: who.uid, options: { batch_parent: true, lite: !!body.lite, auto: true } })
        .select("id").single();
      if (error) throw error;
      await db.from("pipeline_jobs").update({ options: { batch_parent: true, batch_id: data.id, lite: !!body.lite, auto: true } }).eq("id", data.id);
      await kickWorker(data.id, "batch");
      return json({ job_id: data.id });
    }

    if (action === "approve_parcel") {
      const { data: job, error } = await db.from("pipeline_jobs").select("candidates,status").eq("id", body.job_id).single();
      if (error) throw error;
      let parcel = null;
      const idx: number[] = Array.isArray(body.indexes) ? body.indexes : Number.isInteger(body.index) ? [body.index] : [];
      const picked = idx.map((i) => job.candidates?.[i]).filter(Boolean);
      if (body.geometry) parcel = { source: "manual", geometry: body.geometry };
      else if (picked.length === 1) parcel = picked[0];
      else if (picked.length > 1) parcel = { source: picked[0].source, parts: picked, combine: true };  // worker unions them
      if (!parcel) return json({ error: "Pick a candidate index or send a geometry" }, 400);
      const patch: Record<string, unknown> = { parcel, status: "parcel_approved", step: "ec", error: null };
      if (body.setbacks) patch.setbacks = body.setbacks;
      const { error: e2 } = await db.from("pipeline_jobs").update(patch).eq("id", body.job_id);
      if (e2) throw e2;
      await kickWorker(body.job_id, "ec");
      return json({ ok: true });
    }

    if (action === "approve_zoning") {
      const { data: job, error } = await db.from("pipeline_jobs").select("status,zoning,program").eq("id", body.job_id).single();
      if (error) throw error;
      if (!["awaiting_zoning", "awaiting_layout", "layout_approved", "error"].includes(job.status))
        return json({ error: `Job is ${job.status}; zoning can be confirmed once research finishes` }, 409);
      const patch: Record<string, unknown> = { status: "zoning_approved", step: "layout", error: null };
      const zoning = { ...(job.zoning ?? {}), ...(body.zoning ?? {}) };
      // parking_setbacks = final property-line-to-pavement distances {front, side, rear} chosen at the checkpoint
      // drive_road = key of the road the driveway connects to (summary.front_roads[].key); null / "" = automatic
      if ("drive_road" in body) zoning.drive_road = body.drive_road || null;
      if (body.parking_setbacks) zoning.pavement_setbacks_ft = { ...body.parking_setbacks, source: { front: "user", side: "user", rear: "user" } };
      patch.zoning = zoning;
      if (body.setbacks) patch.setbacks = body.setbacks;
      if (body.program) patch.program = { ...body.program, locked: true };
      const { error: e2 } = await db.from("pipeline_jobs").update(patch).eq("id", body.job_id);
      if (e2) throw e2;
      await kickWorker(body.job_id, "layout");
      return json({ ok: true });
    }

    if (action === "approve_layout") {
      const { data: job, error } = await db.from("pipeline_jobs").select("status").eq("id", body.job_id).single();
      if (error) throw error;
      if (job.status !== "awaiting_layout") return json({ error: `Job is ${job.status}` }, 409);
      const { error: e2 } = await db.from("pipeline_jobs")
        .update({ status: "layout_approved", step: "deliverables", progress: "Layout approved", render_status: "queued", render_error: null })
        .eq("id", body.job_id);
      if (e2) throw e2;
      await kickWorker(body.job_id, "render");   // 3D model -> Nano Banana render
      return json({ ok: true });
    }

    if (action === "render") {   // re-run the 3D model + Nano Banana render for an approved layout
      const { data: job, error } = await db.from("pipeline_jobs").select("status").eq("id", body.job_id).single();
      if (error) throw error;
      if (job.status !== "layout_approved") return json({ error: "Approve the layout first" }, 409);
      await db.from("pipeline_jobs").update({ render_status: "queued", render_error: null }).eq("id", body.job_id);
      await kickWorker(body.job_id, "render");
      return json({ ok: true });
    }

    if (action === "package") {   // due diligence package: plan sheet + Nano Banana render + researched proforma
      const { data: job, error } = await db.from("pipeline_jobs").select("status,render_status,package_status").eq("id", body.job_id).single();
      if (error) throw error;
      if (job.status !== "layout_approved" || job.render_status !== "completed") return json({ error: "Finish the render first" }, 409);
      if (["queued", "researching", "writing", "typesetting"].includes(job.package_status)) return json({ error: "The package is already being built" }, 409);
      await db.from("pipeline_jobs").update({ package_status: "queued", package_error: null }).eq("id", body.job_id);
      await kickWorker(body.job_id, "package");
      return json({ ok: true });
    }

    if (action === "design") {   // optional full design phase: preliminary civil plan set from the approved layout (independent of render / package)
      const { data: job, error } = await db.from("pipeline_jobs").select("status,design_status,zoning").eq("id", body.job_id).single();
      if (error) throw error;
      if (job.status !== "layout_approved") return json({ error: "Approve the layout first" }, 409);
      if (job.design_status === "queued" || job.design_status === "designing") return json({ error: "The plan set is already being built" }, 409);
      const patch: Record<string, unknown> = { design_status: "queued", design_error: null };
      const z: Record<string, unknown> = { ...(job.zoning ?? {}) };
      let touched = false;
      if (typeof body.sewer === "boolean" || typeof body.water === "boolean") {
        // true = municipal service, false = private (septic / well). design_utilities keeps the old both-or-neither meaning for older readers.
        const sewer = typeof body.sewer === "boolean" ? body.sewer : z.design_sewer !== false;
        const water = typeof body.water === "boolean" ? body.water : z.design_water !== false;
        z.design_sewer = sewer; z.design_water = water;
        z.design_utilities = sewer && water ? "municipal" : (!sewer && !water ? "onsite" : "mixed");
        touched = true;
      } else if (body.utilities === "municipal" || body.utilities === "onsite") {
        z.design_utilities = body.utilities; z.design_sewer = body.utilities === "municipal"; z.design_water = body.utilities === "municipal"; touched = true;
      }
      if (touched) patch.zoning = z;
      await db.from("pipeline_jobs").update(patch).eq("id", body.job_id);
      await kickWorker(body.job_id, "design");
      return json({ ok: true });
    }

    if (action === "rerun") {
      if (!["parse", "ec", "zoning", "layout", "buildings", "topo"].includes(body.step)) return json({ error: "Unknown step" }, 400);
      await db.from("pipeline_jobs").update({ error: null, progress: `Re-running ${body.step}` }).eq("id", body.job_id);
      await kickWorker(body.job_id, body.step);
      return json({ ok: true });
    }

    if (action === "delete") {
      const { error } = await db.from("pipeline_jobs").delete().eq("id", body.job_id);
      if (error) throw error;
      const { data: objs } = await db.storage.from("pipeline").list(body.job_id, { limit: 100 });
      if (objs?.length) await db.storage.from("pipeline").remove(objs.map((o: { name: string }) => `${body.job_id}/${o.name}`));
      return json({ ok: true });
    }

    if (action === "status") {
      const { data: job, error } = await db.from("pipeline_jobs").select("*").eq("id", body.job_id).single();
      if (error) throw error;
      return json({ ...job, files: await signOutputs(job.outputs) });
    }

    if (action === "list") {
      let q = db.from("pipeline_jobs")
        .select("id,created_at,updated_at,source,crm_ref,status,step,progress,error,render_status,package_status,options,listing->street_address,listing->lot,listing->city,listing->state,program->use")
        .order("created_at", { ascending: false }).limit(Math.min(Number(body.limit ?? 30), 100));
      if (!who.admin) q = q.eq("created_by", who.uid);
      if (body.batch) q = q.eq("options->>batch_id", String(body.batch));
      const { data, error } = await q;
      if (error) throw error;
      return json({ jobs: data });
    }

    return json({ error: "Unknown action" }, 400);
  } catch (e) {
    return json({ error: e instanceof Error ? e.message : String(e) }, 500);
  }
});
