-- =====================================================
-- Bookmarks, separate from application status
-- =====================================================
-- applications.is_bookmarked: the user saved (bookmarked) this job. It's
-- independent of status, so a job can be Applied and still bookmarked, or
-- Applied without a bookmark (added through "Did you apply? Yes").
--
-- Existing rows: bookmarked if they started as Saved (the user tapped the
-- bookmark), not bookmarked if they started as Applied. Rows without
-- history count as bookmarked (the default before this column existed).
--
-- Grants and RLS are unchanged: users can already read and write their own
-- applications rows, and table-level grants cover the new column. Older app
-- versions don't send it, so their saves default to bookmarked.

alter table public.applications
  add column is_bookmarked boolean not null default true;

-- Backfill without touching updated_at (the tracker sorts by it)
alter table public.applications disable trigger applications_set_updated_at;

update public.applications a
set is_bookmarked = coalesce(
  (
    select e.to_status = 'saved'
    from public.application_events e
    where e.application_id = a.id
    order by e.changed_at asc
    limit 1
  ),
  true
);

alter table public.applications enable trigger applications_set_updated_at;
