-- Profile name and photo (Sprint 7).
-- The limits mirror birdnet_api/domain.py: MAX_ALIAS_LENGTH, AVATAR_OBJECT_PATH_TEMPLATE, MAX_AVATAR_BYTES and
-- AVATAR_CONTENT_TYPE. Row level security on public.profiles already limits every operation to the owner's row
-- (initial migration), so no policy is added here.

alter table public.profiles add column if not exists avatar_path text;

-- New accounts get the e-mail's local part as alias, which may exceed the limit or be blank: bound it here so
-- that sign-up never fails on the constraint below.
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
    nullif(left(btrim(coalesce(new.raw_user_meta_data->>'alias', split_part(new.email, '@', 1))), 40), ''),
    '{}'::jsonb
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

-- Existing aliases were filled the same way; normalize them before the constraint is validated.
update public.profiles
set alias = nullif(left(btrim(alias), 40), '')
where alias is distinct from nullif(left(btrim(alias), 40), '');

alter table public.profiles
  add constraint profiles_alias_length check (alias is null or char_length(alias) between 1 and 40);

-- The photo can only live under the owner's own folder, so a profile never points at someone else's object.
alter table public.profiles
  add constraint profiles_avatar_path_owned check (avatar_path is null or avatar_path = id::text || '/avatar.webp');

-- Private bucket: the API signs uploads and short-lived downloads with the service role, so no storage.objects
-- policy is needed and nothing is publicly readable. Guarded because local test databases have no Storage schema.
do $$
begin
  if to_regclass('storage.buckets') is not null then
    insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
    values ('avatars', 'avatars', false, 200000, array['image/webp'])
    on conflict (id) do nothing;
  end if;
end;
$$;
