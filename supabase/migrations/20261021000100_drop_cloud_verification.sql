-- Remove what the retired cloud verification left in the schema (Sprint 11; ADR-32 retired RF-09, ADR-38).
-- Nothing has written or read these since ADR-32: no fragment leaves the phone to be verified. Apply only after
-- the API of Sprint 11 is deployed; no deployed code names the table or the column by then.
-- Dropping the table also drops its trigger, indexes and policies.

drop table if exists public.verification_jobs;

alter table public.detections drop column if exists ruta_audio;
