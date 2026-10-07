-- Migración inicial de BirdNet Local
-- Esquema relacional con soporte geoespacial (PostGIS) y Row Level Security (RLS) estricto

-- 1. Extensiones
create extension if not exists "postgis";

-- 2. Función auxiliar para redondear coordenadas geográficas a cuadrícula de ~100 metros
-- 0.001 grados en latitud equivale a aprox. 111 metros; en longitud varía entre 80 y 111 m en latitudes medias.
create or replace function public.round_to_100m_grid(p_geog geography)
returns geography
language plpgsql
immutable
as $$
declare
  geom geometry;
  lon double precision;
  lat double precision;
begin
  if p_geog is null then
    return null;
  end if;
  geom := p_geog::geometry;
  lon := round(st_x(geom)::numeric, 3)::double precision;
  lat := round(st_y(geom)::numeric, 3)::double precision;
  return st_setsrid(st_makepoint(lon, lat), 4326)::geography;
end;
$$;

-- 3. Tabla: profiles (perfil de usuario vinculado a auth.users)
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  alias text,
  preferencias jsonb not null default '{}'::jsonb,
  region text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.profiles enable row level security;

-- 4. Tabla: sites (sitios de monitoreo creados por usuarios)
create table if not exists public.sites (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null references public.profiles(id) on delete cascade,
  nombre text not null,
  centro geography(Point, 4326) not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.sites enable row level security;

-- 5. Tabla: detections (detecciones acústicas, clave generada en cliente para idempotencia)
create table if not exists public.detections (
  id uuid primary key,
  user_id uuid not null references public.profiles(id) on delete cascade,
  site_id uuid references public.sites(id) on delete set null,
  especie text not null,
  confianza real not null check (confianza >= 0.0 and confianza <= 1.0),
  estado text not null check (estado in ('confirmada', 'provisional', 'verificada', 'corregida', 'descartada')),
  momento timestamptz not null default now(),
  ubicacion geography(Point, 4326) not null,
  version_modelo text not null,
  ruta_audio text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.detections enable row level security;

-- 6. Tabla: verification_jobs (cola de verificación diferida de detecciones dudosas)
create table if not exists public.verification_jobs (
  id uuid primary key default gen_random_uuid(),
  detection_id uuid not null references public.detections(id) on delete cascade,
  estado text not null default 'pendiente' check (estado in ('pendiente', 'procesando', 'completado', 'fallido', 'revision')),
  intentos integer not null default 0 check (intentos >= 0),
  ultimo_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.verification_jobs enable row level security;

-- 7. Tabla: model_versions (versiones versionadas del modelo acústico)
create table if not exists public.model_versions (
  version text primary key,
  ruta_storage text not null,
  tamano_bytes bigint not null check (tamano_bytes > 0),
  umbrales jsonb not null default '{"alta": 0.80, "intermedia": 0.45}'::jsonb,
  metricas jsonb not null default '{}'::jsonb,
  activo boolean not null default true,
  created_at timestamptz not null default now()
);

alter table public.model_versions enable row level security;

-- 8. Triggers para ofuscación y redondeo automático de ubicación (~100 m)
create or replace function public.trigger_round_detections_location()
returns trigger
language plpgsql
as $$
begin
  if new.ubicacion is not null then
    new.ubicacion := public.round_to_100m_grid(new.ubicacion);
  end if;
  return new;
end;
$$;

create trigger trg_round_detections_location
  before insert or update on public.detections
  for each row
  execute function public.trigger_round_detections_location();

create or replace function public.trigger_round_sites_center()
returns trigger
language plpgsql
as $$
begin
  if new.centro is not null then
    new.centro := public.round_to_100m_grid(new.centro);
  end if;
  return new;
end;
$$;

create trigger trg_round_sites_center
  before insert or update on public.sites
  for each row
  execute function public.trigger_round_sites_center();

-- 9. Triggers para updated_at automático
create or replace function public.set_current_timestamp_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger trg_profiles_updated_at
  before update on public.profiles
  for each row
  execute function public.set_current_timestamp_updated_at();

create trigger trg_sites_updated_at
  before update on public.sites
  for each row
  execute function public.set_current_timestamp_updated_at();

create trigger trg_detections_updated_at
  before update on public.detections
  for each row
  execute function public.set_current_timestamp_updated_at();

create trigger trg_verification_jobs_updated_at
  before update on public.verification_jobs
  for each row
  execute function public.set_current_timestamp_updated_at();

-- 10. Trigger para sincronización de perfil con auth.users
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, alias, preferencias)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'alias', split_part(new.email, '@', 1)),
    '{}'::jsonb
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row
  execute function public.handle_new_user();

-- 11. Índices
create index if not exists idx_sites_owner_id on public.sites (owner_id);
create index if not exists idx_sites_centro on public.sites using gist (centro);

create index if not exists idx_detections_user_id on public.detections (user_id);
create index if not exists idx_detections_site_id on public.detections (site_id);
create index if not exists idx_detections_ubicacion on public.detections using gist (ubicacion);
create index if not exists idx_detections_especie_momento on public.detections (especie, momento desc);
create index if not exists idx_detections_estado on public.detections (estado);

create index if not exists idx_verification_jobs_detection_id on public.verification_jobs (detection_id);
create index if not exists idx_verification_jobs_estado_intentos on public.verification_jobs (estado, intentos);

-- 12. Políticas de Row Level Security (RLS)

-- Profiles: privada (cada usuario lee y edita únicamente su propio perfil)
create policy "profiles_select_own"
  on public.profiles for select
  to authenticated
  using ((select auth.uid()) = id);

create policy "profiles_insert_own"
  on public.profiles for insert
  to authenticated
  with check ((select auth.uid()) = id);

create policy "profiles_update_own"
  on public.profiles for update
  to authenticated
  using ((select auth.uid()) = id)
  with check ((select auth.uid()) = id);

create policy "profiles_delete_own"
  on public.profiles for delete
  to authenticated
  using ((select auth.uid()) = id);

-- Sites: privada por propietario (cada usuario gestiona sus propios sitios)
create policy "sites_select_own"
  on public.sites for select
  to authenticated
  using ((select auth.uid()) = owner_id);

create policy "sites_insert_own"
  on public.sites for insert
  to authenticated
  with check ((select auth.uid()) = owner_id);

create policy "sites_update_own"
  on public.sites for update
  to authenticated
  using ((select auth.uid()) = owner_id)
  with check ((select auth.uid()) = owner_id);

create policy "sites_delete_own"
  on public.sites for delete
  to authenticated
  using ((select auth.uid()) = owner_id);

-- Detections: lectura colectiva para mapa; inserción y modificación estrictamente del autor
create policy "detections_select_authenticated"
  on public.detections for select
  to authenticated
  using (true);

create policy "detections_insert_own"
  on public.detections for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

create policy "detections_update_own"
  on public.detections for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "detections_delete_own"
  on public.detections for delete
  to authenticated
  using ((select auth.uid()) = user_id);

-- Verification Jobs: propiedad verificada a través de la tabla padre (detections)
create policy "verification_jobs_select_own"
  on public.verification_jobs for select
  to authenticated
  using (
    exists (
      select 1 from public.detections d
      where d.id = verification_jobs.detection_id
        and d.user_id = (select auth.uid())
    )
  );

create policy "verification_jobs_insert_own"
  on public.verification_jobs for insert
  to authenticated
  with check (
    exists (
      select 1 from public.detections d
      where d.id = verification_jobs.detection_id
        and d.user_id = (select auth.uid())
    )
  );

create policy "verification_jobs_update_own"
  on public.verification_jobs for update
  to authenticated
  using (
    exists (
      select 1 from public.detections d
      where d.id = verification_jobs.detection_id
        and d.user_id = (select auth.uid())
    )
  );

create policy "verification_jobs_delete_own"
  on public.verification_jobs for delete
  to authenticated
  using (
    exists (
      select 1 from public.detections d
      where d.id = verification_jobs.detection_id
        and d.user_id = (select auth.uid())
    )
  );

-- Model Versions: catálogo de modelos público/autenticado para consulta; escritura solo por service_role
create policy "model_versions_select_all"
  on public.model_versions for select
  to authenticated, anon
  using (true);
