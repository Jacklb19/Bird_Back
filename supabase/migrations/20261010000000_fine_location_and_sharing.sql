-- Finer location grid and a per-person choice to share detections (Sprint 8, owner decisions of 2026-10-10).

-- 1. Location grid of ~10 m instead of ~100 m.
-- 0.0001 degrees of latitude is about 11 m. The 4 decimals mirror LOCATION_GRID_DECIMALS in birdnet_api/domain.py.
-- Rows stored with 3 decimals already lie on this grid, so they are left as they are.
create or replace function public.round_to_location_grid(p_geog geography)
returns geography
language plpgsql
immutable
as $$
declare
  geom geometry;
begin
  if p_geog is null then
    return null;
  end if;
  geom := p_geog::geometry;
  return st_setsrid(
    st_makepoint(round(st_x(geom)::numeric, 4)::double precision, round(st_y(geom)::numeric, 4)::double precision),
    4326
  )::geography;
end;
$$;

-- The triggers keep their names; only the functions they run change.
create or replace function public.trigger_round_detections_location()
returns trigger
language plpgsql
as $$
begin
  if new.ubicacion is not null then
    new.ubicacion := public.round_to_location_grid(new.ubicacion);
  end if;
  return new;
end;
$$;

create or replace function public.trigger_round_sites_center()
returns trigger
language plpgsql
as $$
begin
  if new.centro is not null then
    new.centro := public.round_to_location_grid(new.centro);
  end if;
  return new;
end;
$$;

-- Only those two trigger functions called the coarser rounding.
drop function if exists public.round_to_100m_grid(geography);

-- 2. Each person decides whether their detections appear on everyone's map.
-- Sharing is the default, which is also what every row uploaded before this choice existed already did.
alter table public.detections add column if not exists compartida boolean not null default true;

-- Reading used to be open to every signed-in user; now a row is visible to others only while it is shared.
-- The author always reads their own rows, so their record, site statistics and export are unaffected.
drop policy if exists "detections_select_authenticated" on public.detections;

create policy "detections_select_shared_or_own"
  on public.detections for select
  to authenticated
  using (compartida or (select auth.uid()) = user_id);
