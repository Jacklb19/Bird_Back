-- Private metadata for outings, manually observed birds and personal organization.
-- Audio and observation photos are not accepted here; media stays under its existing device/clip permissions.
ALTER TABLE public.detections ADD CONSTRAINT detections_id_owner UNIQUE (id, user_id);
ALTER TABLE public.walks ADD CONSTRAINT walks_id_owner UNIQUE (id, user_id);

CREATE TABLE public.outings (
  id uuid PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
  title text CHECK (title IS NULL OR char_length(btrim(title)) BETWEEN 1 AND 80),
  kind text NOT NULL CHECK (kind IN ('stationary', 'walk', 'import')),
  started_at timestamptz NOT NULL,
  ended_at timestamptz,
  note text CHECK (note IS NULL OR char_length(note) <= 500),
  site_id uuid,
  walk_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  CHECK (ended_at IS NULL OR ended_at >= started_at),
  CHECK (walk_id IS NULL OR kind = 'walk'),
  FOREIGN KEY (site_id, owner_id) REFERENCES public.sites(id, owner_id) ON DELETE SET NULL (site_id),
  FOREIGN KEY (walk_id, owner_id) REFERENCES public.walks(id, user_id) ON DELETE SET NULL (walk_id)
);

CREATE TABLE public.observations (
  id uuid PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
  species text NOT NULL CHECK (char_length(species) BETWEEN 1 AND 200 AND species ~ '^[A-Z][a-z]+ [a-z]+(-[a-z]+)?$'),
  observed_at timestamptz NOT NULL,
  location geography(Point, 4326),
  individuals integer CHECK (individuals BETWEEN 1 AND 999),
  note text CHECK (note IS NULL OR char_length(note) <= 500),
  outing_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id),
  FOREIGN KEY (outing_id, owner_id) REFERENCES public.outings(id, owner_id) ON DELETE SET NULL (outing_id),
  CHECK (location IS NULL OR (
    abs(ST_Y(location::geometry) - round(ST_Y(location::geometry)::numeric, 4)) < 0.000000001
    AND abs(ST_X(location::geometry) - round(ST_X(location::geometry)::numeric, 4)) < 0.000000001
  ))
);

CREATE TABLE public.outing_detections (
  owner_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  outing_id uuid NOT NULL,
  detection_id uuid NOT NULL,
  PRIMARY KEY (detection_id),
  FOREIGN KEY (outing_id, owner_id) REFERENCES public.outings(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (detection_id, owner_id) REFERENCES public.detections(id, user_id) ON DELETE CASCADE
);

CREATE TABLE public.collections (
  id uuid PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
  name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
  note text CHECK (note IS NULL OR char_length(note) <= 500),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id)
);

CREATE TABLE public.collection_items (
  owner_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  collection_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('detection', 'observation')),
  detection_id uuid,
  observation_id uuid,
  FOREIGN KEY (collection_id, owner_id) REFERENCES public.collections(id, owner_id) ON DELETE CASCADE,
  FOREIGN KEY (detection_id, owner_id) REFERENCES public.detections(id, user_id) ON DELETE CASCADE,
  FOREIGN KEY (observation_id, owner_id) REFERENCES public.observations(id, owner_id) ON DELETE CASCADE,
  CHECK ((kind = 'detection' AND detection_id IS NOT NULL AND observation_id IS NULL)
    OR (kind = 'observation' AND observation_id IS NOT NULL AND detection_id IS NULL))
);
CREATE UNIQUE INDEX collection_detection_once ON public.collection_items(collection_id, detection_id) WHERE detection_id IS NOT NULL;
CREATE UNIQUE INDEX collection_observation_once ON public.collection_items(collection_id, observation_id) WHERE observation_id IS NOT NULL;

CREATE TABLE public.wishlist (
  owner_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  species text NOT NULL CHECK (char_length(species) BETWEEN 1 AND 200 AND species ~ '^[A-Z][a-z]+ [a-z]+(-[a-z]+)?$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, species)
);

-- Keep only the identity/version of a deletion, never the deleted personal metadata.
CREATE TABLE public.beta_deletions (
  owner_id uuid NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  entity_kind text NOT NULL CHECK (entity_kind IN ('outing', 'observation', 'collection')),
  entity_id uuid NOT NULL,
  version bigint NOT NULL CHECK (version >= 1),
  deleted_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, entity_kind, entity_id)
);

CREATE INDEX outings_owner_time ON public.outings(owner_id, started_at DESC, id);
CREATE INDEX observations_owner_time ON public.observations(owner_id, observed_at DESC, id);
CREATE INDEX observations_outing ON public.observations(outing_id, owner_id);
CREATE INDEX outing_detections_outing ON public.outing_detections(outing_id, owner_id);
CREATE INDEX collections_owner_time ON public.collections(owner_id, created_at DESC, id);
CREATE INDEX beta_deletions_owner_time ON public.beta_deletions(owner_id, deleted_at, entity_id, entity_kind);

ALTER TABLE public.outings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outing_detections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collections ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.collection_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.wishlist ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.beta_deletions ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE target text;
BEGIN
  FOREACH target IN ARRAY ARRAY['outings','observations','outing_detections','collections','collection_items','wishlist'] LOOP
    EXECUTE format('CREATE POLICY %I ON public.%I FOR ALL TO authenticated USING ((SELECT auth.uid()) = owner_id) WITH CHECK ((SELECT auth.uid()) = owner_id)', target || '_own', target);
  END LOOP;
  FOREACH target IN ARRAY ARRAY['outings','observations','outing_detections','collections','collection_items','wishlist','beta_deletions'] LOOP
    EXECUTE format('CREATE POLICY %I ON public.%I AS RESTRICTIVE FOR ALL TO authenticated USING ((SELECT private.session_meets_second_factor())) WITH CHECK ((SELECT private.session_meets_second_factor()))', target || '_second_factor', target);
  END LOOP;
END $$;

CREATE POLICY beta_deletions_read_own ON public.beta_deletions FOR SELECT TO authenticated USING ((SELECT auth.uid()) = owner_id);
-- RLS supplies no write policies, and direct table privileges are revoked as defence in depth.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.beta_deletions FROM anon, authenticated;

-- Versions and timestamps belong to the server even when a person uses PostgREST directly.
CREATE FUNCTION private.stamp_beta_entity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE entity_type text;
BEGIN
  IF current_setting('role', true) = 'authenticated' AND
     (auth.uid() IS DISTINCT FROM NEW.owner_id OR NOT private.session_meets_second_factor()) THEN
    RAISE EXCEPTION 'Session does not permit this operation' USING ERRCODE='42501';
  END IF;
  entity_type := CASE TG_TABLE_NAME WHEN 'outings' THEN 'outing' WHEN 'observations' THEN 'observation' ELSE 'collection' END;
  IF TG_OP = 'INSERT' THEN
    IF EXISTS (SELECT 1 FROM public.beta_deletions WHERE owner_id=NEW.owner_id AND entity_kind=entity_type AND entity_id=NEW.id) THEN
      RAISE EXCEPTION 'Entity was deleted' USING ERRCODE='23514';
    END IF;
    NEW.version := 1;
    NEW.created_at := now();
    NEW.updated_at := now();
  ELSE
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN
      RAISE EXCEPTION 'Entity identity is immutable' USING ERRCODE='23514';
    END IF;
    NEW.created_at := OLD.created_at;
    IF (to_jsonb(NEW) - 'version' - 'created_at' - 'updated_at') IS DISTINCT FROM
       (to_jsonb(OLD) - 'version' - 'created_at' - 'updated_at') THEN
      NEW.version := OLD.version + 1;
      NEW.updated_at := now();
    ELSE
      NEW.version := OLD.version;
      NEW.updated_at := OLD.updated_at;
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION private.remember_beta_deletion() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE entity_type text;
BEGIN
  -- During ON DELETE CASCADE the profile is already gone; adding a new FK row would obstruct account deletion.
  IF EXISTS (SELECT 1 FROM public.profiles WHERE id=OLD.owner_id) THEN
    entity_type := CASE TG_TABLE_NAME WHEN 'outings' THEN 'outing' WHEN 'observations' THEN 'observation' ELSE 'collection' END;
    INSERT INTO public.beta_deletions(owner_id, entity_kind, entity_id, version)
      VALUES (OLD.owner_id, entity_type, OLD.id, OLD.version + 1) ON CONFLICT DO NOTHING;
  END IF;
  RETURN OLD;
END $$;

CREATE TRIGGER outings_stamp BEFORE INSERT OR UPDATE ON public.outings FOR EACH ROW EXECUTE FUNCTION private.stamp_beta_entity();
CREATE TRIGGER observations_stamp BEFORE INSERT OR UPDATE ON public.observations FOR EACH ROW EXECUTE FUNCTION private.stamp_beta_entity();
CREATE TRIGGER collections_stamp BEFORE INSERT OR UPDATE ON public.collections FOR EACH ROW EXECUTE FUNCTION private.stamp_beta_entity();
CREATE TRIGGER outings_deleted AFTER DELETE ON public.outings FOR EACH ROW EXECUTE FUNCTION private.remember_beta_deletion();
CREATE TRIGGER observations_deleted AFTER DELETE ON public.observations FOR EACH ROW EXECUTE FUNCTION private.remember_beta_deletion();
CREATE TRIGGER collections_deleted AFTER DELETE ON public.collections FOR EACH ROW EXECUTE FUNCTION private.remember_beta_deletion();

-- A narrow operation can remember a cancelled creation that never arrived, without granting arbitrary ledger writes.
CREATE FUNCTION private.delete_beta_entity(entity_type text, identifier uuid, expected bigint) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE person uuid; target text; current_version bigint;
BEGIN
  person := auth.uid();
  IF person IS NULL OR NOT private.session_meets_second_factor() THEN
    RAISE EXCEPTION 'Session does not permit this operation' USING ERRCODE='42501';
  END IF;
  target := CASE entity_type WHEN 'outing' THEN 'outings' WHEN 'observation' THEN 'observations' WHEN 'collection' THEN 'collections' END;
  IF target IS NULL OR expected IS NULL OR expected < 1 THEN
    RAISE EXCEPTION 'Invalid deletion request' USING ERRCODE='22023';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.profiles WHERE id=person) THEN RETURN true; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(person::text || ':' || entity_type || ':' || identifier::text, 0));
  EXECUTE format('SELECT version FROM public.%I WHERE id=$1 AND owner_id=$2 FOR UPDATE', target) INTO current_version USING identifier, person;
  IF current_version IS NOT NULL THEN
    IF current_version <> expected THEN RETURN false; END IF;
    EXECUTE format('DELETE FROM public.%I WHERE id=$1 AND owner_id=$2', target) USING identifier, person;
  ELSE
    INSERT INTO public.beta_deletions(owner_id, entity_kind, entity_id, version)
      VALUES (person, entity_type, identifier, 1) ON CONFLICT DO NOTHING;
  END IF;
  RETURN true;
END $$;

REVOKE ALL ON FUNCTION private.stamp_beta_entity(), private.remember_beta_deletion(), private.delete_beta_entity(text,uuid,bigint) FROM public;
GRANT EXECUTE ON FUNCTION private.delete_beta_entity(text,uuid,bigint) TO authenticated;
