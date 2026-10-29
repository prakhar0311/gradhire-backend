-- =====================================================
-- Fix: infinite recursion in the resumes insert/update policies
-- =====================================================
-- The "parent resume must be yours" check queried public.resumes from inside
-- a policy on public.resumes. Postgres applies resumes' RLS policies to that
-- inner query as well, detects the table referring to itself, and fails every
-- resume insert/update with 42P17 "infinite recursion detected in policy".
--
-- The ownership check now lives in a SECURITY DEFINER function, which reads
-- resumes without going through RLS. It only answers "does the signed-in user
-- own this resume?" (true/false), so it exposes no other data.

create or replace function public.owns_resume(p_resume_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.resumes
    where id = p_resume_id
      and user_id = (select auth.uid())
  );
$$;

-- Policies run as the signed-in user, so authenticated needs EXECUTE.
revoke execute on function public.owns_resume(uuid) from public, anon;
grant execute on function public.owns_resume(uuid) to authenticated, service_role;


drop policy "resumes: insert own" on public.resumes;
drop policy "resumes: update own" on public.resumes;

create policy "resumes: insert own"
  on public.resumes for insert to authenticated
  with check (
    (select auth.uid()) = user_id
    and (parent_resume_id is null or public.owns_resume(parent_resume_id))
  );

create policy "resumes: update own"
  on public.resumes for update to authenticated
  using ((select auth.uid()) = user_id)
  with check (
    (select auth.uid()) = user_id
    and (parent_resume_id is null or public.owns_resume(parent_resume_id))
  );
