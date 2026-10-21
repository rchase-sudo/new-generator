// pipeline-banana: Nano Banana (Gemini image) photoreal pass over the Blender model render of an approved concept.
// POST { job_id }  (service role only — called by the Modal worker once model_front.png is uploaded)
// Reads pipeline/{job}/model_front.png, sends it to Gemini with the render prompt, writes pipeline/{job}/render.png,
// records outputs.render["render.png"] and render_status = completed | failed.
import { createClient } from "npm:@supabase/supabase-js@2.45.4";

declare const EdgeRuntime: { waitUntil(p: Promise<unknown>): void };

const SB_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const GEMINI_API_KEY = Deno.env.get("GEMINI_API_KEY")!;
const MODEL = Deno.env.get("GEMINI_IMAGE_MODEL") ?? "gemini-3-pro-image-preview";
const db = createClient(SB_URL, SERVICE, { auth: { persistSession: false } });

const json = (b: unknown, s = 200) => new Response(JSON.stringify(b), { status: s, headers: { "Content-Type": "application/json" } });

function prompt(program: Record<string, unknown> | null) {
  const use = (program?.use as string) || "commercial development";
  const blds = ((program?.buildings as Record<string, unknown>[]) || [])
    .map((b) => `${b.label || b.use || b.name} (${Math.round(Number(b.footprint_sf) || 0).toLocaleString()} SF${b.drive_thru ? ", drive-thru" : ""})`)
    .join(", ");
  return (
    `This image is a 3D model render of a proposed ${use}${blds ? ` — ${blds}` : ""}, shown from a drone at a 3/4 aerial angle ` +
    `over its real surroundings. Turn it into a photorealistic aerial drone photograph of the SAME scene from the EXACT same ` +
    `camera position, angle and framing.\n\n` +
    `Keep everything exactly where it is: every building's footprint, size, height, roof line and position; the parking lot ` +
    `layout, stall rows and stall count; the landscape islands and medians; the drive aisles and the single driveway and where ` +
    `it meets the road; the road itself; any pond; the lawn areas and the surrounding tree line and neighboring properties. ` +
    `Do not add, remove, move, enlarge or re-orient any building, road, driveway or parking area. Do not change the camera.\n\n` +
    `Only make it real: fresh dark asphalt with crisp white striping, light concrete curbs and sidewalks, modern but not ` +
    `over-the-top commercial buildings with real glazing, metal panel and wood accents where the model shows them, a few ` +
    `parked cars in the stalls and a couple on the road, healthy mowed lawn and realistic ornamental trees in the islands, ` +
    `natural mixed woodland matching the tree line, summer midday sun with soft natural shadows and light atmospheric haze ` +
    `toward the horizon. Architectural marketing photography quality.\n\n` +
    `No text, no labels, no signs with words, no logos, no watermarks, no borders.`
  );
}

function b64(bytes: Uint8Array) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

async function run(jobId: string) {
  const { data: job } = await db.from("pipeline_jobs").select("program,outputs").eq("id", jobId).single();
  const { data: blob, error } = await db.storage.from("pipeline").download(`${jobId}/model_front.png`);
  if (error || !blob) throw new Error(`model render missing: ${error?.message}`);
  const img = b64(new Uint8Array(await blob.arrayBuffer()));
  let lastErr = "";
  for (let attempt = 0; attempt < 2; attempt++) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 150_000);
    try {
      const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=${GEMINI_API_KEY}`, {
        method: "POST", headers: { "content-type": "application/json" }, signal: ctl.signal,
        body: JSON.stringify({
          contents: [{ role: "user", parts: [{ text: prompt(job?.program ?? null) }, { inline_data: { mime_type: "image/png", data: img } }] }],
          generationConfig: { responseModalities: ["TEXT", "IMAGE"] },
        }),
      });
      if (!r.ok) { lastErr = `Gemini ${r.status}: ${(await r.text()).slice(0, 400)}`; continue; }
      const data = await r.json();
      const parts = data?.candidates?.[0]?.content?.parts ?? [];
      const part = parts.find((p: Record<string, any>) => (p.inlineData?.mimeType || p.inline_data?.mime_type || "").startsWith("image/"));
      const b = part?.inlineData?.data || part?.inline_data?.data;
      if (!b) { lastErr = `Gemini returned no image (${data?.candidates?.[0]?.finishReason ?? "unknown"})`; continue; }
      const mime = part.inlineData?.mimeType || part.inline_data?.mime_type || "image/png";
      const ext = mime.includes("jpeg") ? "jpg" : "png";
      const bytes = Uint8Array.from(atob(b), (c) => c.charCodeAt(0));
      const key = `${jobId}/render.${ext}`;
      const up = await db.storage.from("pipeline").upload(key, bytes, { contentType: mime, upsert: true });
      if (up.error) throw new Error(`upload failed: ${up.error.message}`);
      const { data: cur } = await db.from("pipeline_jobs").select("outputs").eq("id", jobId).single();
      const outputs = { ...(cur?.outputs ?? {}) };
      outputs.render = { ...(outputs.render ?? {}), [`render.${ext}`]: key };
      await db.from("pipeline_jobs").update({ outputs, render_status: "completed", render_error: null }).eq("id", jobId);
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
  if (token !== SERVICE) return json({ error: "service role only" }, 401);
  const { job_id } = await req.json().catch(() => ({}));
  if (!job_id) return json({ error: "job_id required" }, 400);
  await db.from("pipeline_jobs").update({ render_status: "nanobanana", render_error: null }).eq("id", job_id);
  EdgeRuntime.waitUntil(run(job_id).catch(async (e) => {
    await db.from("pipeline_jobs").update({ render_status: "failed", render_error: String(e?.message ?? e).slice(0, 1000) }).eq("id", job_id);
  }));
  return json({ ok: true, accepted: true }, 202);
});
