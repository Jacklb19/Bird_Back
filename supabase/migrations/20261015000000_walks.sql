-- Walks saved in the account (Sprint 10, owner decision of 2026-10-15; ADR-35 modifies ADR-25).
-- A walk is stored only when its person chose to save walks in their account. It is private: unlike detections,
-- no one else ever reads it, and it never reaches the collective map. The limits mirror birdnet_api/domain.py:
-- MAX_WALK_NAME_LENGTH and MAX_WALK_POINTS.

-- 1. The walk: name, period and path. Its songs are not copied here: they are the owner's detections between
-- `inicio` and `fin`, read when the walk is, so a later review of a song also changes the walk.
create table if not exists public.walks (
  -- Generated on the phone, so a repeated upload of the same walk is recognized (idempotent synchronization).
  id uuid primary key,
  user_id uuid not null references public.profiles(id) on delete cascade,
  nombre text not null,
  inicio timestamptz not null,
  fin timestamptz not null,
  -- Cells of the ~10 m location grid in walking order, as [latitude, longitude] or [latitude, longitude, seconds
  -- since the start]; never the raw reading of the device. The API validates every point before storing it.
  recorrido jsonb not null default '[]'::jsonb,
  -- Length of the path in metres, computed by the API with the same formula the phone uses.
  distancia_m double precision not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint walks_name_length check (char_length(nombre) between 1 and 80),
  constraint walks_period check (fin >= inicio),
  constraint walks_path_shape check (jsonb_typeof(recorrido) = 'array' and jsonb_array_length(recorrido) <= 2000),
  constraint walks_distance check (distancia_m >= 0)
);

alter table public.walks enable row level security;

create trigger trg_walks_updated_at
  before update on public.walks
  for each row
  execute function public.set_current_timestamp_updated_at();

-- The list goes newest first, per owner; the quota counts per owner.
create index if not exists idx_walks_user_inicio on public.walks (user_id, inicio desc);

-- A walk's songs are read by owner and period.
create index if not exists idx_detections_user_momento on public.detections (user_id, momento);

-- 2. Private to their owner for every operation.
create policy "walks_select_own"
  on public.walks for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "walks_insert_own"
  on public.walks for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

create policy "walks_update_own"
  on public.walks for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "walks_delete_own"
  on public.walks for delete
  to authenticated
  using ((select auth.uid()) = user_id);
