-- Forma concept pipeline: job queue + config + private storage bucket.
-- Only the service role (edge functions, Modal worker) touches these; RLS on with no policies.

create table if not exists public.pipeline_jobs (
  id            uuid primary key default gen_random_uuid(),
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  created_by    uuid,                 -- auth user who started it (tool) or null (CRM)
  source        text not null default 'tool',   -- 'tool' | 'crm'
  crm_ref       text,                 -- CRM lead / project id when started from the CRM
  listing_text  text not null,
  listing       jsonb,                -- parsed listing fields
  status        text not null default 'queued',
  step          text,
  progress      text,
  error         text,
  geocode       jsonb,
  epsg          integer,
  candidates    jsonb,                -- parcel candidates for approval
  parcel_source text,
  county_layer  text,
  parcel        jsonb,                -- approved parcel (geojson + attrs)
  setbacks      jsonb,                -- {"front": ft, "side": ft, "rear": ft}
  summary       jsonb,
  outputs       jsonb not null default '{}'::jsonb,   -- storage keys by step
  log           jsonb not null default '[]'::jsonb
);

create index if not exists pipeline_jobs_status_idx on public.pipeline_jobs (status);
create index if not exists pipeline_jobs_crm_ref_idx on public.pipeline_jobs (crm_ref);
alter table public.pipeline_jobs enable row level security;

create table if not exists public.pipeline_config (
  key   text primary key,
  value text not null,
  updated_at timestamptz not null default now()
);
alter table public.pipeline_config enable row level security;

create or replace function public.pipeline_log(job uuid, msg text)
returns void language sql security definer set search_path = public as $$
  update public.pipeline_jobs
     set log = log || jsonb_build_array(jsonb_build_object('t', now(), 'msg', msg)),
         updated_at = now()
   where id = job;
$$;
revoke all on function public.pipeline_log(uuid, text) from public, anon, authenticated;

insert into storage.buckets (id, name, public)
values ('pipeline', 'pipeline', false)
on conflict (id) do nothing;
