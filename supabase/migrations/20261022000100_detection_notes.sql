-- Notes and count of individuals per record (Sprint 11, ADR-40). The person writes them on the phone; they are
-- synchronized like a review and go out in the CSV, never to the collective map nor to the language model.
-- They live in their own table, readable only by their owner: as columns of `detections`, anyone could read them
-- through the shared rows of the collective map with the public key. The limits mirror birdnet_api/domain.py:
-- MAX_NOTE_LENGTH and MAX_INDIVIDUALS.

create table if not exists public.detection_notes (
  detection_id uuid primary key references public.detections(id) on delete cascade,
  user_id uuid not null references public.profiles(id) on delete cascade,
  nota text,
  -- Individuals the person heard; null when they did not count them.
  individuos integer,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint detection_notes_text check (nota is null or char_length(nota) between 1 and 500),
  constraint detection_notes_individuals check (individuos is null or individuos between 1 and 999),
  -- An empty note is deleted rather than stored.
  constraint detection_notes_not_empty check (nota is not null or individuos is not null)
);

alter table public.detection_notes enable row level security;

create trigger trg_detection_notes_updated_at
  before update on public.detection_notes
  for each row
  execute function public.set_current_timestamp_updated_at();

create index if not exists idx_detection_notes_user on public.detection_notes (user_id);

-- Private to their owner, and only on the owner's own detections.
create policy "detection_notes_select_own"
  on public.detection_notes for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "detection_notes_insert_own"
  on public.detection_notes for insert
  to authenticated
  with check (
    (select auth.uid()) = user_id
    and exists (select 1 from public.detections d where d.id = detection_id and d.user_id = (select auth.uid()))
  );

create policy "detection_notes_update_own"
  on public.detection_notes for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "detection_notes_delete_own"
  on public.detection_notes for delete
  to authenticated
  using ((select auth.uid()) = user_id);

-- Same second step as every table with data of a person (20261022000000_second_factor.sql).
create policy "detection_notes_second_factor"
  on public.detection_notes as restrictive for all
  to authenticated
  using ((select private.session_meets_second_factor()))
  with check ((select private.session_meets_second_factor()));
