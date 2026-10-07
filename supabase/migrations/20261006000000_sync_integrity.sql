-- A detection can create at most one verification job, including repeated uploads.
create unique index if not exists verification_jobs_detection_id_unique
  on public.verification_jobs (detection_id);
