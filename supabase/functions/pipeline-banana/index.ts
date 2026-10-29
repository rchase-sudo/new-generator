// pipeline-banana: Nano Banana (Gemini image) photoreal pass over the Blender model render of an approved concept.
// POST { job_id }  (service role only — called by the Modal worker once model_front.png is uploaded)
// Reads pipeline/{job}/model_front.png, sends it to Gemini with the render prompt, writes pipeline/{job}/render_raw.*,
// then hands off to the worker (render_composite) which writes render.jpg.
// A job can add its own instructions in zoning.render_notes (free text appended to the prompt).
import { createClient } from "npm:@supabase/supabase-js@2.45.4";

declare const EdgeRuntime: { waitUntil(p: Promise<unknown>): void };

const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const GEMINI_API_KEY = Deno.env.get("GEMINI_API_KEY")!;
const MODEL = Deno.env.get("GEMINI_IMAGE_MODEL") ?? "gemini-3-pro-image-preview";
const db = createClient(SB_URL, SERVICE, { auth: { persistSession: false } });

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

const json = (b: unknown, s = 200) => new Response(JSON.stringify(b), { status: s, headers: { "Content-Type": "application/json" } });

function prompt(program: Record<string, unknown> | null, notes: string | null) {
  const use = (program?.use as string) || "commercial development";
  const bl = (program?.buildings as Record<string, unknown>[]) || [];
  const resi = /town|multifamily|apartment|residential|single.family|housing/i.test(use + " " + bl.map((b) => String(b.use || "")).join(" "));
  const blds = bl
    .map((b) => `${b.label || b.use || b.name} (${Math.round(Number(b.footprint_sf) || 0).toLocaleString()} SF${b.drive_thru ? ", drive-thru" : ""})`)
    .join(", ");
  const look = resi
    ? `residential townhome buildings (brick, siding and glazing, pitched or flat roofs as the model shows them)`
    : `modern commercial buildings with real glazing, dark metal panel, brick and wood accents and believable roof equipment, as the model shows them`;
  return (
    `This image is a 3D model render of a proposed ${use}${blds && !resi ? ` — ${blds}` : ""}, shown from a drone at a 3/4 aerial angle ` +
    `over its real surroundings. Turn it into a stunning, photorealistic aerial drone photograph of the SAME scene from the EXACT same ` +
    `camera position, angle and framing — the quality of a professional architectural marketing photo shot by a drone at golden hour.\n\n` +
    `Keep everything exactly where it is: every building's footprint, size, height, roof line and position; the parking lot ` +
    `layout, stall rows and stall count; the landscape islands and medians; the drive aisles and the single driveway and where ` +
    `it meets the road; the road itself; any pond; the lawn areas and the surrounding tree line and neighboring properties. ` +
    `Do not add, remove, move, enlarge or re-orient any building, road, driveway or parking area. Do not change the camera.\n\n` +
    `Make the whole frame photoreal, not just the project: real textured tree canopy with individual crowns and natural color variation, ` +
    `real roads with lane markings, real neighboring roofs and lots, natural ground with grass, soil and leaf litter. Warm low golden-hour ` +
    `sunlight from the side with long soft shadows, rich natural color, subtle atmospheric depth, crisp detail everywhere. ` +
    `The project itself: fresh dark asphalt with crisp white striping, light concrete curbs and sidewalks, ${look}, a few parked cars ` +
    `in the stalls and a couple on the road, healthy mowed lawn and realistic ornamental trees and shrubs in the islands, and any detention ` +
    `pond as a natural looking water body with vegetated banks.\n\n` +
    `IMPORTANT: the ONLY buildings, parking lots and paved areas that exist on the proposed site are the ones already drawn in the ` +
    `model. Count them and keep exactly that number. Never add extra buildings, extra parking lots, extra driveways or extra ` +
    `paving, and never turn woods, fields or open ground into developed land: undeveloped land stays natural woods or field. ` +
    `Neighboring buildings, lots, roads, fields and woods must keep the same layout, buildings and positions as in the input; only make them photoreal. ` +
    `Do not redesign, rebuild, add to or remove anything.\n\n` +
    `PARKING: the number of parking stalls and parking rows in your photograph must not exceed what the model image shows. Count the stall rows in the ` +
    `model and draw exactly those. The open lawn, grass and woods in front of, behind and beside the buildings in the model must stay lawn, grass and ` +
    `woods: never convert them into additional parking, a second lot, parking islands or more paving, even where it looks like a natural place for it. ` +
    `If the model shows a single row of stalls along a building, the photograph shows a single row.\n\n` +
    `OUTSIDE THE PROPERTY NOTHING IS PROPOSED: do not create any new road, street, intersection, driveway, sidewalk, median, parking area or ` +
    `building outside the developed area. Existing roads stay exactly the width, type and surface the input shows (a dirt or gravel farm track ` +
    `stays a dirt or gravel track; a two-lane road stays two lanes). A RAILWAY stays a railway: a narrow pair of steel rails on ties and gravel ballast, ` +
    `never a paved road, never with lane markings, sidewalks or cars, and the project does not touch it or cross it. Farmland, orchards and fields ` +
    `around the site stay farmland, orchards and fields of the same kind and pattern as the input.\n\n` +
    (notes ? `Site-specific facts you must follow: ${notes}\n\n` : "") +
    `No text, no labels, no signs with words, no logos, no watermarks, no borders.`
  );
}

// Gemini 3 Pro Image list prices (ai.google.dev/gemini-api/docs/pricing, Oct 2026): $2/M input,
// $12/M text+thinking output, $120/M image output (~$0.134 per 1K/2K image).
function geminiCost(u: Record<string, any> | undefined) {
  if (!u) return { usd: 0.134, input_tokens: 0, output_tokens: 0 };
  const inp = Number(u.promptTokenCount || 0);
  const det: { modality?: string; tokenCount?: number }[] = u.candidatesTokensDetails || [];
  const img = det.filter((d) => d.modality === "IMAGE").reduce((a, d) => a + Number(d.tokenCount || 0), 0);
  const cand = Number(u.candidatesTokenCount || 0);
  const text = Math.max(0, cand - img) + Number(u.thoughtsTokenCount || 0);
  const imgTok = img || (det.length ? 0 : cand);
  return { usd: inp * 2e-6 + text * 12e-6 + imgTok * 120e-6, input_tokens: inp, output_tokens: cand + Number(u.thoughtsTokenCount || 0) };
}

async function logUsage(jobId: string, entry: Record<string, unknown>) {
  try {
    const { data } = await db.from("pipeline_jobs").select("usage").eq("id", jobId).single();
    const usage = Array.isArray(data?.usage) ? data.usage : [];
    usage.push({ ...entry, step: "render", at: new Date().toISOString().slice(0, 19) + "+00:00" });
    await db.from("pipeline_jobs").update({ usage }).eq("id", jobId);
  } catch (_) { /* cost logging never breaks the render */ }
}

function b64(bytes: Uint8Array) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

async function run(jobId: string) {
  const { data: job } = await db.from("pipeline_jobs").select("program,outputs,zoning").eq("id", jobId).single();
  const notes = typeof job?.zoning?.render_notes === "string" && job.zoning.render_notes.trim() ? job.zoning.render_notes.trim().slice(0, 1500) : null;
  const { data: blob, error } = await db.storage.from("pipeline").download(`${jobId}/model_front.png`);
  if (error || !blob) throw new Error(`model render missing: ${error?.message}`);
  const img = b64(new Uint8Array(await blob.arrayBuffer()));
  let lastErr = "";
  for (let attempt = 0; attempt < 2; attempt++) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 170_000);
    try {
      // first try 2K output at 3:2 (the model render is 3:2); if the model rejects the image config, retry plain
      const cfg = attempt === 0 ? { responseModalities: ["TEXT", "IMAGE"], imageConfig: { aspectRatio: "3:2", imageSize: "2K" } } : { responseModalities: ["TEXT", "IMAGE"] };
      const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=${GEMINI_API_KEY}`, {
        method: "POST", headers: { "content-type": "application/json" }, signal: ctl.signal,
        body: JSON.stringify({
          contents: [{ role: "user", parts: [{ text: prompt(job?.program ?? null, notes) }, { inline_data: { mime_type: "image/png", data: img } }] }],
          generationConfig: cfg,
        }),
      });
      if (!r.ok) { lastErr = `Gemini ${r.status}: ${(await r.text()).slice(0, 400)}`; continue; }
      const data = await r.json();
      const gc = geminiCost(data?.usageMetadata);
      await logUsage(jobId, { call: "Nano Banana render", provider: "Google", model: MODEL, input_tokens: gc.input_tokens,
                              output_tokens: gc.output_tokens, searches: 0, cost_usd: Math.round(gc.usd * 10000) / 10000 });
      const parts = data?.candidates?.[0]?.content?.parts ?? [];
      const part = parts.find((p: Record<string, any>) => (p.inlineData?.mimeType || p.inline_data?.mime_type || "").startsWith("image/"));
      const b = part?.inlineData?.data || part?.inline_data?.data;
      if (!b) { lastErr = `Gemini returned no image (${data?.candidates?.[0]?.finishReason ?? "unknown"})`; continue; }
      const mime = part.inlineData?.mimeType || part.inline_data?.mime_type || "image/png";
      const ext = mime.includes("jpeg") ? "jpg" : "png";
      const bytes = Uint8Array.from(atob(b), (c) => c.charCodeAt(0));
      const key = `${jobId}/render_raw.${ext}`;
      const up = await db.storage.from("pipeline").upload(key, bytes, { contentType: mime, upsert: true });
      if (up.error) throw new Error(`upload failed: ${up.error.message}`);
      const { data: cur } = await db.from("pipeline_jobs").select("outputs").eq("id", jobId).single();
      const outputs = { ...(cur?.outputs ?? {}) };
      outputs.render = { ...(outputs.render ?? {}), [`render_raw.${ext}`]: key };
      await db.from("pipeline_jobs").update({ outputs, render_status: "compositing", render_error: null }).eq("id", jobId);
      const { data: cfgRows } = await db.from("pipeline_config").select("key,value").in("key", ["worker_url", "worker_token"]);
      const c2 = Object.fromEntries((cfgRows ?? []).map((r: { key: string; value: string }) => [r.key, r.value]));
      const k = await fetch(String(c2.worker_url).replace(/\/$/, "") + "/", {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${c2.worker_token}` },
        body: JSON.stringify({ job_id: jobId, step: "render_composite" }),
      });
      if (!k.ok) throw new Error(`composite hand-off failed: ${k.status}`);
      return;
    } catch (e) {
      lastErr = e instanceof Error ? e.message : String(e);
    } finally {
      clearTimeout(t);
    }
  }
  throw new Error(lastErr || "Nano Banana failed");
}

Deno.serve(async (req) => {
  const token = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (!isServiceRole(token)) return json({ error: "service role only" }, 401);
  const { job_id } = await req.json().catch(() => ({}));
  if (!job_id) return json({ error: "job_id required" }, 400);
  await db.from("pipeline_jobs").update({ render_status: "nanobanana", render_error: null }).eq("id", job_id);
  EdgeRuntime.waitUntil(run(job_id).catch(async (e) => {
    await db.from("pipeline_jobs").update({ render_status: "failed", render_error: String(e?.message ?? e).slice(0, 1000) }).eq("id", job_id);
  }));
  return json({ ok: true, accepted: true }, 202);
});
