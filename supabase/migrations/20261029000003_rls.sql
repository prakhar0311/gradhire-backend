-- =====================================================
-- GradHire row level security
-- =====================================================
-- RLS is turned on automatically in this project, but it is enabled here
-- explicitly so the schema is correct in any project. With RLS on and no
-- matching policy, a role sees no rows and can't write any.
--
-- (select auth.uid()) is wrapped in a subquery so Postgres evaluates it once
-- per query instead of once per row.
--
-- Inside EXISTS subqueries the outer row is always qualified by table name
-- (e.g. resumes.parent_resume_id): an unqualified column that also exists on
-- the subquery's table would silently refer to the inner row instead.

alter table public.profiles           enable row level security;
alter table public.jobs               enable row level security;
alter table public.resumes            enable row level security;
alter table public.job_matches        enable row level security;
alter table public.applications       enable row level security;
alter table public.application_events enable row level security;
alter table public.device_tokens      enable row level security;


-- -----------------------------------------------------
-- profiles: read and update your own row
-- (created by the sign-up trigger, deleted with the auth user)
-- -----------------------------------------------------

create policy "profiles: select own"
  on public.profiles for select to authenticated
  using ((select auth.uid()) = id);

create policy "profiles: update own"
  on public.profiles for update to authenticated
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);


-- -----------------------------------------------------
-- jobs: readable by everyone, including guests
-- (no write policies: only the backend's service_role can write)
-- -----------------------------------------------------

create policy "jobs: select all"
  on public.jobs for select to anon, authenticated
  using (true);


-- -----------------------------------------------------
-- resumes: full access to your own rows
-- A parent resume, if set, must also be yours.
-- -----------------------------------------------------

create policy "resumes: select own"
  on public.resumes for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "resumes: insert own"
  on public.resumes for insert to authenticated
  with check (
    (select auth.uid()) = user_id
    and (
      parent_resume_id is null
      or exists (
        select 1 from public.resumes parent
        where parent.id = resumes.parent_resume_id
          and parent.user_id = (select auth.uid())
      )
    )
  );

create policy "resumes: update own"
  on public.resumes for update to authenticated
  using ((select auth.uid()) = user_id)
  with check (
    (select auth.uid()) = user_id
    and (
      parent_resume_id is null
      or exists (
        select 1 from public.resumes parent
        where parent.id = resumes.parent_resume_id
          and parent.user_id = (select auth.uid())
      )
    )
  );

create policy "resumes: delete own"
  on public.resumes for delete to authenticated
  using ((select auth.uid()) = user_id);


-- -----------------------------------------------------
-- job_matches: read matches for your own resumes
-- (written only by the backend)
-- -----------------------------------------------------

create policy "job_matches: select own via resume"
  on public.job_matches for select to authenticated
  using (
    exists (
      select 1 from public.resumes r
      where r.id = job_matches.resume_id
        and r.user_id = (select auth.uid())
    )
  );


-- -----------------------------------------------------
-- applications: full access to your own rows
-- A linked resume, if set, must also be yours.
-- -----------------------------------------------------

create policy "applications: select own"
  on public.applications for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "applications: insert own"
  on public.applications for insert to authenticated
  with check (
    (select auth.uid()) = user_id
    and (
      resume_id is null
      or exists (
        select 1 from public.resumes r
        where r.id = applications.resume_id
          and r.user_id = (select auth.uid())
      )
    )
  );

create policy "applications: update own"
  on public.applications for update to authenticated
  using ((select auth.uid()) = user_id)
  with check (
    (select auth.uid()) = user_id
    and (
      resume_id is null
      or exists (
        select 1 from public.resumes r
        where r.id = applications.resume_id
          and r.user_id = (select auth.uid())
      )
    )
  );

create policy "applications: delete own"
  on public.applications for delete to authenticated
  using ((select auth.uid()) = user_id);


-- -----------------------------------------------------
-- application_events: read history for your own applications
-- (written only by the status-change trigger)
-- -----------------------------------------------------

create policy "application_events: select own via application"
  on public.application_events for select to authenticated
  using (
    exists (
      select 1 from public.applications a
      where a.id = application_events.application_id
        and a.user_id = (select auth.uid())
    )
  );


-- -----------------------------------------------------
-- device_tokens: full access to your own rows
-- -----------------------------------------------------

create policy "device_tokens: select own"
  on public.device_tokens for select to authenticated
  using ((select auth.uid()) = user_id);

create policy "device_tokens: insert own"
  on public.device_tokens for insert to authenticated
  with check ((select auth.uid()) = user_id);

create policy "device_tokens: update own"
  on public.device_tokens for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "device_tokens: delete own"
  on public.device_tokens for delete to authenticated
  using ((select auth.uid()) = user_id);
