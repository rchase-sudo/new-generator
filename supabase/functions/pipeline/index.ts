// Forma concept pipeline API (Concept project).
// POST { action: "start" | "approve_parcel" | "approve_zoning" | "approve_layout" | "render" | "rerun" | "status" | "list" | "delete", ... }
//   start           { listing_text, source?: "tool"|"crm", crm_ref? }            -> { job_id }
//   approve_parcel  { job_id, index? | indexes?: number[], geometry?, setbacks? } -> { ok }  (several indexes = assemblage)
//   approve_zoning  { job_id, setbacks?, parking_setbacks?, program?, zoning? }   -> { ok }  (runs / re-runs the layout)
//   approve_layout  { job_id }                                                    -> { ok }  (starts the 3D model + render)
//   render          { job_id }                                                    -> { ok }  (re-run the render)
//   rerun           { job_id, step: "parse"|"ec"|"zoning"|"layout" }              -> { ok }
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

// Who is calling: the service role (CI / CRM backend) sees everything; a signed-in user only their own jobs.
async function caller(req: Request): Promise<{ admin: boolean; uid: string | null }> {
  const token = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (token && token === SERVICE) return { admin: true, uid: null };
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
    if (action !== "start" && action !== "list" && !(await ownJob(who, body.job_id))) return json({ error: "Job not found" }, 404);

    if (action === "start") {
      const text = String(body.listing_text ?? "").trim();
      if (text.length < 20) return json({ error: "Paste the full listing text" }, 400);
      const { data, error } = await db.from("pipeline_jobs")
        .insert({ listing_text: text, source: body.source ?? "tool", crm_ref: body.crm_ref ?? null, status: "queued", step: "parse",
                  created_by: who.uid })
        .select("id").single();
      if (error) throw error;
      await kickWorker(data.id, "parse");
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

    if (action === "rerun") {
      if (!["parse", "ec", "zoning", "layout"].includes(body.step)) return json({ error: "Unknown step" }, 400);
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
        .select("id,created_at,updated_at,source,crm_ref,status,step,progress,error,listing->street_address,listing->lot,listing->city,listing->state,program->use")
        .order("created_at", { ascending: false }).limit(Math.min(Number(body.limit ?? 30), 100));
      if (!who.admin) q = q.eq("created_by", who.uid);
      const { data, error } = await q;
      if (error) throw error;
      return json({ jobs: data });
    }

    return json({ error: "Unknown action" }, 400);
  } catch (e) {
    return json({ error: e instanceof Error ? e.message : String(e) }, 500);
  }
});
