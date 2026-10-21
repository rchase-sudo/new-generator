-- Step 2: zoning research, development program, layout
alter table public.pipeline_jobs
  add column if not exists zoning jsonb,
  add column if not exists program jsonb,
  add column if not exists layout jsonb;

create table if not exists public.zoning_cache (
  key text primary key,
  rules jsonb not null,
  updated_at timestamptz not null default now()
);
alter table public.zoning_cache enable row level security;
-- service role only (worker); no public policies
