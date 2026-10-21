// Forma concept pipeline API (Concept project).
// POST { action: "start" | "approve_parcel" | "status" | "list", ... }
//   start           { listing_text, source?: "tool"|"crm", crm_ref? }            -> { job_id }
//   approve_parcel  { job_id, index?: number, geometry?: GeoJSON, setbacks? }   -> { ok }
//   status          { job_id }                                                    -> job row + signed file URLs
//   list            { limit? }                                                    -> recent jobs
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
  const { data } = await db.from("pipeline_config").select("value").eq("key", "worker_url").maybeSingle();
  if (!data?.value) throw new Error("Worker URL not registered yet (deploy the Modal worker first)");
  const r = await fetch(data.value.replace(/\/$/, "") + "/", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${SERVICE}` },
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

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const body = await req.json();
    const action = body.action;

    if (action === "start") {
      const text = String(body.listing_text ?? "").trim();
      if (text.length < 20) return json({ error: "Paste the full listing text" }, 400);
      const { data, error } = await db.from("pipeline_jobs")
        .insert({ listing_text: text, source: body.source ?? "tool", crm_ref: body.crm_ref ?? null, status: "queued", step: "parse" })
        .select("id").single();
      if (error) throw error;
      await kickWorker(data.id, "parse");
      return json({ job_id: data.id });
    }

    if (action === "approve_parcel") {
      const { data: job, error } = await db.from("pipeline_jobs").select("candidates,status").eq("id", body.job_id).single();
      if (error) throw error;
      let parcel = null;
      if (body.geometry) parcel = { source: "manual", geometry: body.geometry };
      else if (Number.isInteger(body.index) && job.candidates?.[body.index]) parcel = job.candidates[body.index];
      if (!parcel) return json({ error: "Pick a candidate index or send a geometry" }, 400);
      const patch: Record<string, unknown> = { parcel, status: "parcel_approved", step: "ec", error: null };
      if (body.setbacks) patch.setbacks = body.setbacks;
      const { error: e2 } = await db.from("pipeline_jobs").update(patch).eq("id", body.job_id);
      if (e2) throw e2;
      await kickWorker(body.job_id, "ec");
      return json({ ok: true });
    }

    if (action === "status") {
      const { data: job, error } = await db.from("pipeline_jobs").select("*").eq("id", body.job_id).single();
      if (error) throw error;
      return json({ ...job, files: await signOutputs(job.outputs) });
    }

    if (action === "list") {
      const { data, error } = await db.from("pipeline_jobs")
        .select("id,created_at,updated_at,source,crm_ref,status,step,progress,error,listing->street_address,listing->city,listing->state")
        .order("created_at", { ascending: false }).limit(Math.min(Number(body.limit ?? 20), 100));
      if (error) throw error;
      return json({ jobs: data });
    }

    return json({ error: "Unknown action" }, 400);
  } catch (e) {
    return json({ error: e instanceof Error ? e.message : String(e) }, 500);
  }
});
