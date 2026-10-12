-- Saved recordings and review by a person (Sprint 9, owner decisions of 2026-10-11).
-- The limits mirror birdnet_api/domain.py: RECORDING_OBJECT_PATH_TEMPLATE, MAX_RECORDING_BYTES,
-- RECORDING_CONTENT_TYPE and the DbReviewer values. Row level security on public.detections already limits every
-- write to the owner's rows (initial migration), so no policy is added here.

-- 1. Recording of a detection: a compressed clip the person chose to keep in their account.
alter table public.detections add column if not exists ruta_grabacion text;
alter table public.detections add column if not exists bytes_grabacion integer;

-- The clip can only live under the owner's own folder and is always stored together with its size, which is what
-- the per-account quota adds up.
alter table public.detections
  add constraint detections_recording_owned check (
    (ruta_grabacion is null and bytes_grabacion is null)
    or (ruta_grabacion = user_id::text || '/' || id::text || '.webm' and bytes_grabacion between 1 and 100000)
  );

-- The quota is computed per owner on every upload.
create index if not exists idx_detections_recordings on public.detections (user_id) where ruta_grabacion is not null;

-- 2. Review: who confirmed, corrected or discarded a detection, and what the model had said.
-- `especie_modelo` keeps the model's species while `especie` holds a person's correction, so the automatic
-- detection and the correction stay distinguishable (HU-06).
alter table public.detections add column if not exists especie_modelo text;
alter table public.detections add column if not exists revisada_por text;
alter table public.detections add column if not exists revisada_en timestamptz;

alter table public.detections
  add constraint detections_review_source check (revisada_por is null or revisada_por in ('persona', 'nube'));
alter table public.detections
  add constraint detections_review_dated check ((revisada_por is null) = (revisada_en is null));
alter table public.detections
  add constraint detections_model_species_length check (especie_modelo is null or char_length(especie_modelo) between 1 and 200);

-- 3. Private bucket: the API signs uploads and short-lived downloads with the service role, so no storage.objects
-- policy is needed and nothing is publicly readable. Guarded because local test databases have no Storage schema.
do $$
begin
  if to_regclass('storage.buckets') is not null then
    insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
    values ('recordings', 'recordings', false, 100000, array['audio/webm'])
    on conflict (id) do nothing;
  end if;
end;
$$;
