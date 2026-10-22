-- Texts written by the language model (Sprint 11, RF-14 and RF-15; ADR-37).
-- The model never hears audio nor names a species: it only puts into words facts the API gathered (public sources
-- for a species card, deterministic statistics for a site report). Apply before deploying the API that reads it.
-- Only the API writes these tables, through its own database connection: no client may write a card, a report or
-- its own usage count, so none of them has a policy for insert, update or delete by the authenticated role.

-- 1. Species cards, shared by everyone: one per species and language, written once from public data (Wikipedia,
-- GBIF) and reused by every person who opens that species. The API writes them again after a while, or when the
-- model or its instructions change, never because someone asks.
create table if not exists public.species_cards (
  especie text not null,
  idioma text not null,
  -- Paragraphs of the card, in order.
  parrafos jsonb not null,
  -- Where the facts came from: [{name, url, license}], shown under the card.
  fuentes jsonb not null,
  -- Model that wrote it and version of the instructions it followed.
  modelo text not null,
  version_instrucciones integer not null,
  generated_at timestamptz not null default now(),
  primary key (especie, idioma),
  constraint species_cards_species_length check (char_length(especie) between 1 and 200),
  constraint species_cards_language check (idioma in ('es', 'en')),
  constraint species_cards_paragraphs check (jsonb_typeof(parrafos) = 'array' and jsonb_array_length(parrafos) between 1 and 6),
  constraint species_cards_sources check (jsonb_typeof(fuentes) = 'array')
);

alter table public.species_cards enable row level security;

-- Cards hold only public facts, so anyone may read them, with or without an account.
create policy "species_cards_select_all"
  on public.species_cards for select
  to anon, authenticated
  using (true);

-- 2. Site reports, private to the site's owner: the last one asked for each site, period and language. They are
-- written only when the person asks (never on a schedule) and keep the figures they were written from, so every
-- number in the text can be checked against them.
create table if not exists public.site_reports (
  site_id uuid not null references public.sites(id) on delete cascade,
  user_id uuid not null references public.profiles(id) on delete cascade,
  periodo text not null,
  idioma text not null,
  zona_horaria text not null,
  -- The statistics the text was written from, as the API returns them (GET /v1/sites/{id}/stats).
  cifras jsonb not null,
  -- Digest of those figures: asking again with the same figures returns this report instead of writing a new one.
  huella text not null,
  parrafos jsonb not null,
  modelo text not null,
  version_instrucciones integer not null,
  generated_at timestamptz not null default now(),
  primary key (site_id, periodo, idioma),
  constraint site_reports_period check (periodo in ('week', 'month', 'year', 'all')),
  constraint site_reports_language check (idioma in ('es', 'en')),
  constraint site_reports_paragraphs check (jsonb_typeof(parrafos) = 'array' and jsonb_array_length(parrafos) between 1 and 6)
);

alter table public.site_reports enable row level security;

create index if not exists idx_site_reports_user on public.site_reports (user_id);

create policy "site_reports_select_own"
  on public.site_reports for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "site_reports_delete_own"
  on public.site_reports for delete
  to authenticated
  using ((select auth.uid()) = user_id);

-- 3. Texts each person had the model write per day, against the per-person and total daily caps of the API.
-- Reading a card or a report already written does not count. The day is that of the free tier's quota, which
-- Google resets at midnight Pacific time.
create table if not exists public.llm_usage (
  user_id uuid not null references public.profiles(id) on delete cascade,
  dia date not null,
  textos integer not null default 0,
  primary key (user_id, dia),
  constraint llm_usage_texts check (textos >= 0)
);

alter table public.llm_usage enable row level security;

-- The total for a day is summed across people.
create index if not exists idx_llm_usage_day on public.llm_usage (dia);

create policy "llm_usage_select_own"
  on public.llm_usage for select
  to authenticated
  using ((select auth.uid()) = user_id);
