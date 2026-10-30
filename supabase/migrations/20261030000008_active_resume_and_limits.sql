-- =====================================================
-- Active resume and saved-resume limits
-- =====================================================
-- profiles.active_resume_id: the resume used for job matching. Stored on the
-- profile so it follows the user across sign-outs, reinstalls and devices.
-- Deleting that resume clears it (the app then picks the next original).
--
-- resume_limits: how many resumes a user may save. The one place to change
-- the limits (or add a plan with higher ones later). A trigger enforces them
-- on every insert, so the app's own check is only for a friendly message.
--
-- The storage bucket is unchanged: already private, PDF only, 5 MB
-- (20261029000004_storage.sql).


-- -----------------------------------------------------
-- Limits (config)
-- -----------------------------------------------------

create table public.resume_limits (
  plan          text primary key,
  max_originals smallint not null check (max_originals >= 0),
  -- optimized + tailored versions together
  max_versions  smallint not null check (max_versions >= 0),
  updated_at    timestamptz not null default now()
);

insert into public.resume_limits (plan, max_originals, max_versions)
values ('free', 3, 10);

create trigger resume_limits_set_updated_at
  before update on public.resume_limits
  for each row execute function public.set_updated_at();

alter table public.resume_limits enable row level security;

-- The app reads the limits to show "2 of 3 resumes saved"; only the backend
-- (or the dashboard) changes them.
grant select on table public.resume_limits to authenticated;
grant all on table public.resume_limits to service_role;

create policy "resume_limits: select all"
  on public.resume_limits for select to authenticated
  using (true);


-- -----------------------------------------------------
-- Limit enforcement
-- -----------------------------------------------------
-- Rejects a resume beyond the user's limit with SQLSTATE P0001 and the
-- message RESUME_LIMIT_ORIGINAL or RESUME_LIMIT_VERSION, which the app maps
-- to its "delete one to save a new one" message. Also runs when a row's kind
-- or owner changes, so an update can't get around the limit.

create or replace function public.enforce_resume_limit()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_limits public.resume_limits%rowtype;
  v_count  integer;
begin
  if tg_op = 'UPDATE'
     and new.kind = old.kind
     and new.user_id = old.user_id then
    return new;
  end if;

  -- Two saves at the same moment are counted one after the other
  perform pg_advisory_xact_lock(hashtextextended('resume_limit:' || new.user_id::text, 0));

  select * into v_limits from public.resume_limits where plan = 'free';
  if not found then
    return new;
  end if;

  if new.kind = 'original' then
    select count(*) into v_count
    from public.resumes
    where user_id = new.user_id
      and kind = 'original'
      and id <> new.id;

    if v_count >= v_limits.max_originals then
      raise exception using
        errcode = 'P0001',
        message = 'RESUME_LIMIT_ORIGINAL',
        detail  = format('You can save up to %s resumes.', v_limits.max_originals);
    end if;
  else
    select count(*) into v_count
    from public.resumes
    where user_id = new.user_id
      and kind in ('optimized', 'tailored')
      and id <> new.id;

    if v_count >= v_limits.max_versions then
      raise exception using
        errcode = 'P0001',
        message = 'RESUME_LIMIT_VERSION',
        detail  = format('You can save up to %s tailored versions.', v_limits.max_versions);
    end if;
  end if;

  return new;
end;
$$;

create trigger resumes_enforce_limit
  before insert or update of kind, user_id on public.resumes
  for each row execute function public.enforce_resume_limit();


-- -----------------------------------------------------
-- profiles.active_resume_id
-- -----------------------------------------------------

alter table public.profiles
  add column active_resume_id uuid
    references public.resumes (id) on delete set null;

create index profiles_active_resume_id_idx on public.profiles (active_resume_id);

-- Only one of the user's own resumes can be active
create or replace function public.check_active_resume_owner()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.active_resume_id is not null
     and not exists (
       select 1
       from public.resumes
       where id = new.active_resume_id
         and user_id = new.id
     ) then
    raise exception using
      errcode = 'P0001',
      message = 'ACTIVE_RESUME_NOT_OWNED';
  end if;

  return new;
end;
$$;

create trigger profiles_check_active_resume
  before insert or update of active_resume_id on public.profiles
  for each row execute function public.check_active_resume_owner();


-- Trigger functions are never meant to be called directly
revoke execute on function
  public.enforce_resume_limit(),
  public.check_active_resume_owner()
from public, anon, authenticated;
