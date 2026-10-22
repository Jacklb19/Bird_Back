-- Two-step verification with an authenticator app (Sprint 11, ADR-39; completes ADR-21).
-- Supabase Auth keeps the TOTP factors in auth.mfa_factors and says in every access token how the session signed in:
-- `aal1` with the password or Google only, `aal2` after a code from the authenticator. A person who turned the
-- second step on must give a code before reading or changing anything of their account, and that must hold also
-- for someone who reaches PostgREST directly with the public key and a session that stopped at the password.
-- The API checks the same rule itself (birdnet_api/auth.py) and passes the session's level in the claims it sets.

create schema if not exists private;

-- 1. Whether a person has a verified second factor. Security definer because the factors live in the auth schema,
-- which the roles of the API and of the clients do not read.
create or replace function private.has_second_factor(person uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from auth.mfa_factors
    where user_id = person and status = 'verified'
  );
$$;

-- 2. Whether the current session satisfies the second step: always for people without a factor; with one, only a
-- session that gave the code. A token without `aal` counts as `aal1`, as Supabase documents.
create or replace function private.session_meets_second_factor()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(auth.jwt() ->> 'aal', 'aal1') = 'aal2'
    or not private.has_second_factor((select auth.uid()));
$$;

revoke all on function private.has_second_factor(uuid) from public;
revoke all on function private.session_meets_second_factor() from public;
grant usage on schema private to authenticated;
grant execute on function private.session_meets_second_factor() to authenticated;

-- 3. Restrictive policies: they add the condition to every permissive policy of each table that holds data of a
-- person. Public tables (species cards, model versions) stay readable without it.
create policy "profiles_second_factor"
  on public.profiles as restrictive for all
  to authenticated
  using ((select private.session_meets_second_factor()))
  with check ((select private.session_meets_second_factor()));

create policy "sites_second_factor"
  on public.sites as restrictive for all
  to authenticated
  using ((select private.session_meets_second_factor()))
  with check ((select private.session_meets_second_factor()));

create policy "detections_second_factor"
  on public.detections as restrictive for all
  to authenticated
  using ((select private.session_meets_second_factor()))
  with check ((select private.session_meets_second_factor()));

create policy "walks_second_factor"
  on public.walks as restrictive for all
  to authenticated
  using ((select private.session_meets_second_factor()))
  with check ((select private.session_meets_second_factor()));

create policy "site_reports_second_factor"
  on public.site_reports as restrictive for all
  to authenticated
  using ((select private.session_meets_second_factor()))
  with check ((select private.session_meets_second_factor()));

create policy "llm_usage_second_factor"
  on public.llm_usage as restrictive for all
  to authenticated
  using ((select private.session_meets_second_factor()))
  with check ((select private.session_meets_second_factor()));
