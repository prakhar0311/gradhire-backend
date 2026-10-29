-- =====================================================
-- Stage 2B: dashboard
-- =====================================================
-- profiles.weekly_goal: optional number of applications per week the user
-- aims for (null = goal off). Stored on the profile so it follows the user
-- across devices.
--
-- Grants and RLS are unchanged: users can already read and update their own
-- profile row, and table-level grants cover the new column.

alter table public.profiles
  add column weekly_goal smallint;

alter table public.profiles
  add constraint profiles_weekly_goal_range_check
    check (weekly_goal is null or weekly_goal between 1 and 50);
