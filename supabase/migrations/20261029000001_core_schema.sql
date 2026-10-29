-- =====================================================
-- GradHire core schema: types, tables, indexes, triggers
-- =====================================================
-- Every user-owned row references auth.users (directly or through a parent
-- row) with ON DELETE CASCADE, so deleting the auth user deletes all of
-- their data. Shared jobs rows are never deleted, only deactivated.


-- -----------------------------------------------------
-- Types
-- -----------------------------------------------------

create type public.resume_kind as enum ('original', 'optimized', 'tailored');

create type public.application_status as enum (
  'saved',
  'applied',
  'interviewing',
  'offer',
  'rejected'
);


-- -----------------------------------------------------
-- updated_at helper
-- -----------------------------------------------------

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;


-- -----------------------------------------------------
-- profiles (one row per auth user, created by trigger)
-- -----------------------------------------------------

create table public.profiles (
  id               uuid primary key references auth.users (id) on delete cascade,
  name             text,
  email            text,
  notify_new_jobs  boolean not null default true,
  notify_followups boolean not null default true,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create trigger profiles_set_updated_at
  before update on public.profiles
  for each row execute function public.set_updated_at();


-- -----------------------------------------------------
-- jobs (shared; written only by the backend)
-- -----------------------------------------------------
-- id is supplied by the backend as the same stable UUID the app already
-- uses for a job, so IDs match between the app and the database.

create table public.jobs (
  id            uuid primary key default gen_random_uuid(),
  source        text not null check (source in ('adzuna')),
  external_id   text not null,
  title         text not null,
  company       text,
  location      text,
  salary_min    numeric(12, 2) check (salary_min >= 0),
  salary_max    numeric(12, 2) check (salary_max >= 0),
  description   text,
  apply_url     text,
  posted_at     timestamptz,
  is_new_grad   boolean not null default false,
  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),
  is_active     boolean not null default true,

  constraint jobs_source_external_id_key unique (source, external_id),
  constraint jobs_salary_range_check
    check (salary_min is null or salary_max is null or salary_min <= salary_max)
);

create index jobs_posted_at_idx on public.jobs (posted_at desc);
create index jobs_is_active_idx on public.jobs (is_active);
create index jobs_new_grad_posted_at_idx on public.jobs (is_new_grad, posted_at desc);


-- -----------------------------------------------------
-- resumes
-- -----------------------------------------------------

create table public.resumes (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null default auth.uid()
                     references auth.users (id) on delete cascade,
  kind             public.resume_kind not null,
  parent_resume_id uuid references public.resumes (id) on delete set null,
  target_job_id    uuid references public.jobs (id) on delete set null,
  title            text,
  parsed_data      jsonb,
  file_path        text,
  readiness_score  smallint check (readiness_score between 0 and 100),
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create index resumes_user_id_idx on public.resumes (user_id);
create index resumes_parent_resume_id_idx on public.resumes (parent_resume_id);
create index resumes_target_job_id_idx on public.resumes (target_job_id);

create trigger resumes_set_updated_at
  before update on public.resumes
  for each row execute function public.set_updated_at();


-- -----------------------------------------------------
-- job_matches (written by the backend; deleted with the resume)
-- -----------------------------------------------------

create table public.job_matches (
  resume_id      uuid not null references public.resumes (id) on delete cascade,
  job_id         uuid not null references public.jobs (id) on delete cascade,
  match_score    smallint not null check (match_score between 0 and 100),
  matched_skills jsonb not null default '[]'::jsonb,
  missing_skills jsonb not null default '[]'::jsonb,
  created_at     timestamptz not null default now(),

  primary key (resume_id, job_id)
);

-- (resume_id lookups use the primary key)
create index job_matches_job_id_idx on public.job_matches (job_id);


-- -----------------------------------------------------
-- applications (the tracker)
-- -----------------------------------------------------
-- job_id is RESTRICT: jobs are deactivated, never deleted, so a tracked job
-- can't disappear from under an application.

create table public.applications (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null default auth.uid()
                 references auth.users (id) on delete cascade,
  job_id       uuid not null references public.jobs (id) on delete restrict,
  status       public.application_status not null default 'saved',
  resume_id    uuid references public.resumes (id) on delete set null,
  notes        text,
  saved_at     timestamptz not null default now(),
  applied_at   timestamptz,
  updated_at   timestamptz not null default now(),
  follow_up_at timestamptz,

  constraint applications_user_id_job_id_key unique (user_id, job_id)
);

-- (user_id lookups use the unique index above, whose first column is user_id)
create index applications_job_id_idx on public.applications (job_id);
create index applications_status_idx on public.applications (status);
create index applications_user_id_status_idx on public.applications (user_id, status);
create index applications_resume_id_idx on public.applications (resume_id);

create trigger applications_set_updated_at
  before update on public.applications
  for each row execute function public.set_updated_at();


-- -----------------------------------------------------
-- application_events (status history; written only by trigger)
-- -----------------------------------------------------

create table public.application_events (
  id             uuid primary key default gen_random_uuid(),
  application_id uuid not null references public.applications (id) on delete cascade,
  from_status    public.application_status,
  to_status      public.application_status not null,
  changed_at     timestamptz not null default now()
);

create index application_events_application_id_idx
  on public.application_events (application_id, changed_at desc);


-- -----------------------------------------------------
-- device_tokens (APNs push tokens)
-- -----------------------------------------------------
-- environment says which APNs server the token belongs to: Xcode builds use
-- the sandbox, TestFlight/App Store builds use production.

create table public.device_tokens (
  user_id     uuid not null default auth.uid()
                references auth.users (id) on delete cascade,
  apns_token  text not null,
  environment text not null default 'production'
                check (environment in ('sandbox', 'production')),
  updated_at  timestamptz not null default now(),

  primary key (user_id, apns_token)
);

-- (user_id lookups use the primary key; this finds a token across users)
create index device_tokens_apns_token_idx on public.device_tokens (apns_token);

create trigger device_tokens_set_updated_at
  before update on public.device_tokens
  for each row execute function public.set_updated_at();


-- -----------------------------------------------------
-- Trigger: create a profile when a new auth user signs up
-- -----------------------------------------------------
-- SECURITY DEFINER so it can insert even though users have no INSERT policy
-- on profiles. search_path is empty so every name is schema-qualified and
-- the function can't be hijacked through a user-controlled schema.

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, name, email)
  values (
    new.id,
    coalesce(new.raw_user_meta_data ->> 'full_name', new.raw_user_meta_data ->> 'name'),
    new.email
  )
  on conflict (id) do nothing;

  return new;
end;
$$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();


-- -----------------------------------------------------
-- Trigger: record application status history
-- -----------------------------------------------------
-- SECURITY DEFINER because users can only read application_events; this is
-- the only way events get written, so the history can't be forged.

create or replace function public.log_application_status_change()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    insert into public.application_events (application_id, from_status, to_status)
    values (new.id, null, new.status);
  elsif new.status is distinct from old.status then
    insert into public.application_events (application_id, from_status, to_status)
    values (new.id, old.status, new.status);
  end if;

  return new;
end;
$$;

create trigger applications_log_status_change
  after insert or update of status on public.applications
  for each row execute function public.log_application_status_change();
