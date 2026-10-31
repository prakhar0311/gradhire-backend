-- =====================================================
-- Stored resume analyses (Optimize screen)
-- =====================================================
-- One row per (resume, job): the keywords, scores and rewritten bullets the
-- Optimize screen shows, so reopening it is instant, consistent, and costs
-- no AI call. resume_hash / job_hash record the text it was made from; the
-- backend ignores a row whose text has changed.
--
-- Only the backend (service_role) writes rows. Users can read and delete
-- their own. Rows go with the resume, the job or the user.

create table public.resume_analyses (
  user_id     uuid not null references auth.users (id) on delete cascade,
  resume_id   uuid not null references public.resumes (id) on delete cascade,
  job_id      uuid not null references public.jobs (id) on delete cascade,
  resume_hash text not null check (char_length(resume_hash) = 64),
  job_hash    text not null check (char_length(job_hash) = 64),
  version     text not null check (char_length(version) <= 16),
  payload     jsonb not null
                check (jsonb_typeof(payload) = 'object' and pg_column_size(payload) <= 65536),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),

  primary key (resume_id, job_id)
);

create index resume_analyses_user_id_idx on public.resume_analyses (user_id);
create index resume_analyses_job_id_idx on public.resume_analyses (job_id);

create trigger resume_analyses_set_updated_at
  before update on public.resume_analyses
  for each row execute function public.set_updated_at();

alter table public.resume_analyses enable row level security;

revoke all on table public.resume_analyses from anon, authenticated;
grant select, delete on table public.resume_analyses to authenticated;
grant all on table public.resume_analyses to service_role;

create policy "resume_analyses: select own"
  on public.resume_analyses for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "resume_analyses: delete own"
  on public.resume_analyses for delete to authenticated
  using ((select auth.uid()) = user_id);
