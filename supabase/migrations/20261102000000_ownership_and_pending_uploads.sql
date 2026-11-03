-- Fixes from the audit of 2026-11-02 (ADR-44). RLS checks who writes a row, not what else it points to: a foreign
-- key ignores RLS, so a detection could name someone else's site and enter that site's statistics and report, and a
-- note could be moved onto someone else's detection. Clips signed for upload but never linked did not count
-- against the recordings quota, so they could fill the project's Storage.

-- 1. A detection can only belong to a site of its own author. Deleting the site still only clears `site_id`.
alter table public.sites
  add constraint sites_id_owner unique (id, owner_id);

alter table public.detections
  drop constraint if exists detections_site_id_fkey;

alter table public.detections
  add constraint detections_site_of_owner
  foreign key (site_id, user_id) references public.sites (id, owner_id) on delete set null (site_id);

-- 2. Free text stays within the limits the API applies (birdnet_api/domain.py: MAX_SPECIES_LENGTH and
-- MAX_MODEL_VERSION_LENGTH); `especie_modelo` already had its own.
alter table public.detections
  add constraint detections_species_length check (char_length(especie) between 1 and 200),
  add constraint detections_model_version_length check (char_length(version_modelo) between 1 and 200);

-- 3. A note stays on a detection of its owner when it is updated, as it already had to when it was inserted.
drop policy if exists "detection_notes_update_own" on public.detection_notes;

create policy "detection_notes_update_own"
  on public.detection_notes for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check (
    (select auth.uid()) = user_id
    and exists (select 1 from public.detections d where d.id = detection_id and d.user_id = (select auth.uid()))
  );

-- 4. Clips the API signed for upload and that were not linked yet. Each counts against the quota as the largest clip
-- the bucket accepts, since its real size is unknown until it is linked; linking or deleting the clip removes it.
create table if not exists public.recording_uploads (
  detection_id uuid primary key references public.detections(id) on delete cascade,
  user_id uuid not null references public.profiles(id) on delete cascade,
  signed_at timestamptz not null default now()
);

alter table public.recording_uploads enable row level security;

create index if not exists idx_recording_uploads_user on public.recording_uploads (user_id);

create policy "recording_uploads_select_own"
  on public.recording_uploads for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "recording_uploads_insert_own"
  on public.recording_uploads for insert
  to authenticated
  with check (
    (select auth.uid()) = user_id
    and exists (select 1 from public.detections d where d.id = detection_id and d.user_id = (select auth.uid()))
  );

create policy "recording_uploads_update_own"
  on public.recording_uploads for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check (
    (select auth.uid()) = user_id
    and exists (select 1 from public.detections d where d.id = detection_id and d.user_id = (select auth.uid()))
  );

create policy "recording_uploads_delete_own"
  on public.recording_uploads for delete
  to authenticated
  using ((select auth.uid()) = user_id);

-- Same second step as every table with data of a person (20261022000000_second_factor.sql).
create policy "recording_uploads_second_factor"
  on public.recording_uploads as restrictive for all
  to authenticated
  using ((select private.session_meets_second_factor()))
  with check ((select private.session_meets_second_factor()));
