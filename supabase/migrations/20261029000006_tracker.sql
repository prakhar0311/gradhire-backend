-- =====================================================
-- Stage 2: application tracker
-- =====================================================
-- 1. applications.job_snapshot: the key job fields saved with the
--    application, so a tracked job still displays after it closes or
--    disappears from Adzuna.
-- 2. applied_at is filled in automatically when an application first
--    reaches applied (or later), whichever way the status was changed.
-- 3. A length limit on notes.
--
-- Grants and RLS are unchanged: table-level grants already cover the new
-- column, and the existing "own rows" policies apply to it.


-- -----------------------------------------------------
-- Job snapshot and notes limits
-- -----------------------------------------------------

alter table public.applications
  add column job_snapshot jsonb not null default '{}'::jsonb;

alter table public.applications
  add constraint applications_job_snapshot_object_check
    check (jsonb_typeof(job_snapshot) = 'object'),
  add constraint applications_job_snapshot_size_check
    check (pg_column_size(job_snapshot) <= 16384),
  add constraint applications_notes_length_check
    check (notes is null or char_length(notes) <= 10000);


-- -----------------------------------------------------
-- Trigger: fill in applied_at
-- -----------------------------------------------------
-- Runs before the row is written, so the value is part of the same insert
-- or update. An applied_at the app already set (or an earlier one) is kept.

create or replace function public.set_application_applied_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.applied_at is null
     and new.status in ('applied', 'interviewing', 'offer') then
    new.applied_at := now();
  end if;

  return new;
end;
$$;

create trigger applications_set_applied_at
  before insert or update of status, applied_at on public.applications
  for each row execute function public.set_application_applied_at();

-- Trigger functions are never meant to be called directly
revoke execute on function public.set_application_applied_at()
from public, anon, authenticated;
