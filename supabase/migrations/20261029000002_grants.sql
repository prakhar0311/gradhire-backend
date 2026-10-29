-- =====================================================
-- GradHire grants
-- =====================================================
-- "Automatically expose new tables" is OFF for this project, so no role can
-- reach these tables until it is granted access here. Grants decide WHICH
-- ROLES can reach a table; RLS policies (next migration) decide WHICH ROWS.
--
--   anon          = signed-out app users (publishable key, no session)
--   authenticated = signed-in app users
--   service_role  = the backend (secret key); bypasses RLS but still needs grants


-- Start from nothing for the app-facing roles
revoke all on table
  public.profiles,
  public.jobs,
  public.resumes,
  public.job_matches,
  public.applications,
  public.application_events,
  public.device_tokens
from anon, authenticated;

grant usage on schema public to anon, authenticated, service_role;
grant usage on type public.resume_kind, public.application_status
  to anon, authenticated, service_role;


-- jobs: everyone may read; nobody but the backend may write
grant select on table public.jobs to anon, authenticated;

-- User-owned tables: signed-in users only (RLS limits them to their own rows)
grant select, insert, update, delete on table
  public.profiles,
  public.resumes,
  public.applications,
  public.device_tokens
to authenticated;

-- Read-only for users: scores come from the backend, history from a trigger
grant select on table
  public.job_matches,
  public.application_events
to authenticated;

-- Backend: full access to every table
grant all on table
  public.profiles,
  public.jobs,
  public.resumes,
  public.job_matches,
  public.applications,
  public.application_events,
  public.device_tokens
to service_role;


-- Trigger functions are never meant to be called directly
revoke execute on function
  public.set_updated_at(),
  public.handle_new_user(),
  public.log_application_status_change()
from public, anon, authenticated;
