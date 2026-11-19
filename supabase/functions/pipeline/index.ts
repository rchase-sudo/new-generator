// Forma concept pipeline API (Concept project).
// POST { action: "start" | "approve_parcel" | "approve_zoning" | "approve_layout" | "render" | "rerun" | "status" | "list" | "delete", ... }
//   start           { listing_text, source?: "tool"|"crm", crm_ref? }            -> { job_id }
//   approve_parcel  { job_id, index? | indexes?: number[], geometry?, setbacks? } -> { ok }  (several indexes = assemblage)
//   approve_zoning  { job_id, setbacks?, parking_setbacks?, program?, zoning?, drive_road? } -> { ok }  (runs / re-runs the layout)
//                   zoning.building_actions = { "E1": "keep" | "demo" } sets what happens to each existing building;
//                   zoning.max_slope_pct = steepest ground (%) buildings / parking may use (0 = ignore slope)
//                   manual: true -> no automatic layout; the job waits for a test fit drawn in Drafted (testfit_* actions below)
//   approve_layout  { job_id }                                                    -> { ok }  (starts the 3D model + render)
//   render          { job_id }                                                    -> { ok }  (re-run the render)
//   package         { job_id }                                                    -> { ok }  (due diligence package, after the render)
//   cad_template_*  (account) the firm's Civil 3D template; plugin_url -> the Civil 3D plugin download
//   c3d_export      { job_id } -> { ok }  (rebuild only the Civil 3D package of a finished plan set with the current template)
//   design          { job_id, sewer?: boolean, water?: boolean, utilities?: "municipal"|"onsite" } -> { ok }  (optional full design phase, after the layout is approved)
//   start_batch     { text, lite? }  -> { job_id }  (many listings in one field: the worker splits them and runs each one unattended to its package)
//   start also takes options { lite?, auto? }: lite = cheap package research (same plan + render), auto = no approval checkpoints
//   preview         { job_id, push?: boolean, force?: boolean } -> { ok }  (preview page for a finished manual job; push = also add the agent to Instantly + CRM)
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
    const ACCOUNT = ["start", "start_batch", "list", "cad_template_get", "cad_template_upload", "cad_template_save", "cad_template_delete", "plugin_url"];
    if (!ACCOUNT.includes(action) && !(await ownJob(who, body.job_id))) return json({ error: "Job not found" }, 404);

    // ---- the firm's Civil 3D template (account level): layers + layer mapping + styles used by the CAD exports --------------
    //   cad_template_get                     -> { template, file_url }
    //   cad_template_upload { file_name }    -> { path, signed_url, token }   (browser uploads the .dwt / .dwg / .dxf / standards .json there)
    //   cad_template_save { file_name?, file_path?, file_kind?, layers?, mapping?, styles? } -> { ok }
    //   cad_template_delete                  -> { ok }
    //   plugin_url                           -> { url }  (the Civil 3D plugin bundle, built by CI)
    if (action.startsWith("cad_template") || action === "plugin_url") {
      const uid = who.uid ?? body.user_id;
      if (action === "plugin_url") {
        const { data } = await db.storage.from("pipeline").createSignedUrl("plugins/Drafted.bundle.zip", 3600, { download: "Drafted.bundle.zip" });
        return data?.signedUrl ? json({ url: data.signedUrl }) : json({ error: "The plugin has not been built yet" }, 404);
      }
      if (!uid) return json({ error: "Sign in required" }, 401);
      if (action === "cad_template_get") {
        const { data } = await db.from("cad_templates").select("*").eq("user_id", uid).maybeSingle();
        let file_url = null;
        if (data?.file_path) file_url = (await db.storage.from("pipeline").createSignedUrl(data.file_path, 3600, { download: data.file_name ?? true })).data?.signedUrl ?? null;
        return json({ template: data ?? null, file_url });
      }
      if (action === "cad_template_upload") {
        const name = String(body.file_name ?? "template").replace(/[^A-Za-z0-9._ -]/g, "_").slice(0, 120);
        if (!/\.(dwt|dwg|dxf|json)$/i.test(name)) return json({ error: "Upload a .dwt, .dwg, .dxf or Drafted standards .json" }, 400);
        const path = `cad_templates/${uid}/${Date.now()}_${name}`;
        const { data, error } = await db.storage.from("pipeline").createSignedUploadUrl(path);
        if (error) throw error;
        return json({ path, signed_url: data.signedUrl, token: data.token });
      }
      if (action === "cad_template_save") {
        const row: Record<string, unknown> = { user_id: uid, updated_at: new Date().toISOString() };
        for (const k of ["file_name", "file_path", "file_kind"]) if (k in body) row[k] = body[k] == null ? null : String(body[k]).slice(0, 300);
        if (body.file_path && !String(body.file_path).startsWith(`cad_templates/${uid}/`)) return json({ error: "Bad file path" }, 400);
        if (Array.isArray(body.layers)) row.layers = body.layers.slice(0, 5000).map((l: Record<string, unknown>) => ({ name: String(l.name ?? "").slice(0, 255), color: Number(l.color) || 7, linetype: String(l.linetype ?? "").slice(0, 64) })).filter((l: { name: string }) => l.name);
        if (body.mapping && typeof body.mapping === "object") row.mapping = Object.fromEntries(Object.entries(body.mapping).filter(([k, v]) => typeof v === "string" && v).map(([k, v]) => [k.slice(0, 40), String(v).slice(0, 255)]));
        if (body.styles && typeof body.styles === "object") row.styles = body.styles;
        if (JSON.stringify(row).length > 2_000_000) return json({ error: "Template data too large" }, 413);
        const { error } = await db.from("cad_templates").upsert(row, { onConflict: "user_id" });
        if (error) throw error;
        return json({ ok: true });
      }
      if (action === "cad_template_delete") {
        const { data } = await db.from("cad_templates").select("file_path").eq("user_id", uid).maybeSingle();
        if (data?.file_path) await db.storage.from("pipeline").remove([data.file_path]);
        await db.from("cad_templates").delete().eq("user_id", uid);
        return json({ ok: true });
      }
    }

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
      zoning.layout_mode = body.manual ? "manual" : "auto";    // manual = stop after the existing conditions for the test-fit editor
      if (body.parking_setbacks) zoning.pavement_setbacks_ft = { ...body.parking_setbacks, source: { front: "user", side: "user", rear: "user" } };
      patch.zoning = zoning;
      if (body.setbacks) patch.setbacks = body.setbacks;
      if (body.program) patch.program = { ...body.program, locked: true };
      const { error: e2 } = await db.from("pipeline_jobs").update(patch).eq("id", body.job_id);
      if (e2) throw e2;
      await kickWorker(body.job_id, "layout");
      return json({ ok: true });
    }

    // ---- manual test fit (Drafted's site editor) ----------------------------------------------------------------
    //   testfit_base   { job_id }          -> { ok }  (prepares the editor's site data for a job laid out before the editor existed)
    //   testfit_save   { job_id, design }  -> { ok }  (keeps the draft; nothing runs)
    //   testfit_submit { job_id, design }  -> { ok }  (draws the concept plan from the test fit, then the job continues as usual)
    if (action === "testfit_base" || action === "testfit_save" || action === "testfit_submit") {
      const { data: job, error } = await db.from("pipeline_jobs").select("status,summary,zoning").eq("id", body.job_id).single();
      if (error) throw error;
      const okStatus = ["awaiting_layout", "layout_approved", "error"].includes(job.status) && !!job.zoning;
      if (!okStatus) return json({ error: `Job is ${job.status}; the test fit opens once the existing conditions and zoning are confirmed` }, 409);
      const summary = { ...(job.summary ?? {}) };
      if (action === "testfit_base") {
        summary.testfit_status = "building";
        await db.from("pipeline_jobs").update({ summary }).eq("id", body.job_id);
        await kickWorker(body.job_id, "testfit_base");
        return json({ ok: true });
      }
      if (!body.design || !Array.isArray(body.design.buildings)) return json({ error: "No test fit design" }, 400);
      if (JSON.stringify(body.design).length > 4_000_000) return json({ error: "Test fit too large" }, 413);
      summary.testfit_design = { ...body.design, saved_at: new Date().toISOString() };
      if (action === "testfit_save") {
        const { error: e2 } = await db.from("pipeline_jobs").update({ summary }).eq("id", body.job_id);
        if (e2) throw e2;
        return json({ ok: true });
      }
      if (!body.design.buildings.length) return json({ error: "Place at least one building" }, 400);
      const zoning = { ...(job.zoning ?? {}), layout_mode: "manual" };
      const { error: e3 } = await db.from("pipeline_jobs")
        .update({ summary, zoning, status: "building_layout", step: "layout", progress: "Drawing your test fit", error: null }).eq("id", body.job_id);
      if (e3) throw e3;
      await kickWorker(body.job_id, "manual_layout");
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

    if (action === "c3d_export") {   // rebuild only the Civil 3D package of a finished plan set with the firm's current template
      const { data: job, error } = await db.from("pipeline_jobs").select("design_status,design,outputs").eq("id", body.job_id).single();
      if (error) throw error;
      if (job.design_status !== "completed") return json({ error: "Build the plan set first" }, 409);
      if (!job.outputs?.design?.["design_x.pkl"]) return json({ error: "This plan set was built before Civil 3D exports; build the plan set again" }, 409);
      if (job.design?.c3d_status === "building") return json({ error: "The Civil 3D package is already being built" }, 409);
      await db.from("pipeline_jobs").update({ design: { ...(job.design ?? {}), c3d_status: "building", c3d_error: null } }).eq("id", body.job_id);
      await kickWorker(body.job_id, "c3d_export");
      return json({ ok: true });
    }

    if (action === "preview") {   // warm-lead preview page for a finished (manual) job: same page the lite pipeline builds, from the approved layout, render and package
      const { data: job, error } = await db.from("pipeline_jobs").select("status,render_status,package_status,options,summary").eq("id", body.job_id).single();
      if (error) throw error;
      if (job.status !== "layout_approved" || job.render_status !== "completed") return json({ error: "Finish the render first" }, 409);
      if (!job.options?.lite && job.package_status !== "completed") return json({ error: "Build the due diligence package first" }, 409);
      if (["queued", "building"].includes(job.summary?.preview?.status)) return json({ error: "The preview page is already being built" }, 409);
      const options = { ...(job.options ?? {}), preview_push: !!body.push };
      await db.from("pipeline_jobs").update({ options, summary: { ...(job.summary ?? {}), preview: { ...(job.summary?.preview ?? {}), status: "queued", reason: null, outreach: null } } }).eq("id", body.job_id);
      await kickWorker(body.job_id, body.force ? "preview_force" : "preview");
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
        .select("id,created_at,updated_at,source,crm_ref,status,step,progress,error,render_status,package_status,options,listing->street_address,listing->lot,listing->city,listing->state,program->use,preview_status:summary->preview->>status")
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
