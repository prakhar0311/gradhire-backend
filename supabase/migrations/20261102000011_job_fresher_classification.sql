-- =====================================================
-- Fresher classification on shared jobs
-- =====================================================
-- Jobs whose posting doesn't say whether a fresher (0-1 years) can apply
-- are classified once by the backend's AI check; the result is kept here so
-- the same job is never classified again. classification_version lets a
-- future prompt change re-check old rows.
--
--   fresher_friendly    the AI's answer (null: not classified)
--   min_years           minimum experience it found or inferred
--   grad_batches        graduation years the posting accepts, e.g. {2025,2026}
--   fresher_confidence  0-1; low-confidence "no" answers don't hide a job
--
-- Only the backend writes jobs. Users can already read jobs (table-level
-- select grant), which covers these columns; nothing here is personal.

alter table public.jobs
  add column min_years numeric(3, 1) check (min_years >= 0),
  add column grad_batches smallint[] not null default '{}',
  add column fresher_friendly boolean,
  add column fresher_confidence real check (fresher_confidence between 0 and 1),
  add column classification_version smallint,
  add column classified_at timestamptz;
