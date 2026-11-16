"""Owner-scoped beta metadata and explicit event relationships."""
from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg import sql

from ..domain import DB_TO_API_STATUS, DbDetectionStatus
from ..errors import ErrorCode, raise_error
from ..model_species import require_model_species
from ..repository import DetectionRepository, require_row
from .contracts import (
    Collection, CollectionInput, CollectionItemPage, CollectionPage, Deletion, DeletionPage, DeletionQuery,
    DescendingQuery, DetectionEvent, EntityKind, EventKind, EventQuery, Observation, ObservationEvent,
    ObservationInput, ObservationPage, ObservationQuery, Outing, OutingInput, OutingPage, TimelineEvent,
    TimelinePage, WishlistItem, WishlistPage, WishlistQuery,
)

Connection = psycopg.Connection[dict[str, Any]]
TABLES: Final = {"outing": "outings", "observation": "observations", "collection": "collections"}
FIELDS: Final = {
    "outing": ("title", "kind", "started_at", "ended_at", "note", "site_id", "walk_id"),
    "observation": ("species", "observed_at", "location", "individuals", "note", "outing_id"),
    "collection": ("name", "note"),
}
OUTING_COUNTS: Final = """
    (SELECT count(*) FROM public.outing_detections x JOIN public.detections d ON d.id=x.detection_id
      WHERE x.outing_id=o.id AND x.owner_id=o.owner_id AND d.user_id=o.owner_id AND d.estado <> 'descartada') AS detections,
    (SELECT count(*) FROM public.observations v WHERE v.outing_id=o.id AND v.owner_id=o.owner_id) AS observations,
    (SELECT count(DISTINCT species) FROM (
        SELECT d.especie AS species FROM public.outing_detections x JOIN public.detections d ON d.id=x.detection_id
          WHERE x.outing_id=o.id AND x.owner_id=o.owner_id AND d.user_id=o.owner_id AND d.estado <> 'descartada'
        UNION ALL SELECT v.species FROM public.observations v WHERE v.outing_id=o.id AND v.owner_id=o.owner_id
    ) species_found) AS species"""


def lock_entity(connection: Connection, owner: UUID, kind: EntityKind, identifier: UUID) -> None:
    # Must match private.delete_beta_entity, so a delayed creation cannot cross a deletion.
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"{owner}:{kind}:{identifier}",))


def require_owned(connection: Connection, table: str, owner: UUID, identifier: UUID, owner_column: str = "owner_id") -> None:
    row = connection.execute(sql.SQL("SELECT 1 FROM public.{} WHERE id=%s AND {}=%s").format(
        sql.Identifier(table), sql.Identifier(owner_column)), (identifier, owner)).fetchone()
    if row is None:
        raise_error(ErrorCode.BETA_REFERENCE_NOT_FOUND)


def _metadata(connection: Connection, owner: UUID, kind: EntityKind, identifier: UUID, *, for_update: bool = False) -> dict[str, Any] | None:
    projection: sql.Composable = sql.SQL("e.*")
    if kind == "observation":
        projection += sql.SQL(", CASE WHEN location IS NULL THEN NULL ELSE jsonb_build_object('latitude',ST_Y(location::geometry),'longitude',ST_X(location::geometry)) END AS location")
    return connection.execute(sql.SQL("SELECT {} FROM public.{} e WHERE id=%s AND owner_id=%s {}").format(
        projection, sql.Identifier(TABLES[kind]), sql.SQL("FOR UPDATE" if for_update else "")), (identifier, owner)).fetchone()


def _payload_values(kind: EntityKind, payload: OutingInput | ObservationInput | CollectionInput) -> dict[str, Any]:
    values = payload.model_dump(exclude={"expected_version"})
    if kind == "observation":
        require_model_species(str(values["species"]))
    return values


def _check_references(connection: Connection, owner: UUID, kind: EntityKind, values: dict[str, Any]) -> None:
    for field, table, owner_column in (("site_id", "sites", "owner_id"), ("walk_id", "walks", "user_id"), ("outing_id", "outings", "owner_id")):
        if values.get(field) is not None:
            require_owned(connection, table, owner, values[field], owner_column)


def _save(connection: Connection, owner: UUID, kind: EntityKind, identifier: UUID,
          payload: OutingInput | ObservationInput | CollectionInput) -> None:
    lock_entity(connection, owner, kind, identifier)
    values = _payload_values(kind, payload)
    stored = _metadata(connection, owner, kind, identifier, for_update=True)
    deleted = connection.execute("SELECT 1 FROM public.beta_deletions WHERE owner_id=%s AND entity_kind=%s AND entity_id=%s",
                                 (owner, kind, identifier)).fetchone()
    if deleted:
        raise_error(ErrorCode.BETA_ENTITY_DELETED)
    if stored is not None:
        if all(stored[field] == values[field] for field in FIELDS[kind]):
            return
        if payload.expected_version != stored["version"]:
            raise_error(ErrorCode.BETA_VERSION_CONFLICT)
    elif payload.expected_version is not None:
        raise_error(ErrorCode.BETA_REFERENCE_NOT_FOUND)
    require_owned(connection, "profiles", owner, owner, "id")
    _check_references(connection, owner, kind, values)
    parameters: list[Any] = []
    expressions: list[sql.Composable] = []
    for field in FIELDS[kind]:
        value = values[field]
        if field == "location":
            value = f"SRID=4326;POINT({value['longitude']} {value['latitude']})" if value else None
            expressions.append(sql.SQL("ST_GeogFromText(%s)"))
        else:
            expressions.append(sql.Placeholder())
        parameters.append(value)
    table = sql.Identifier(TABLES[kind])
    if stored is None:
        statement = sql.SQL("INSERT INTO public.{} (id,owner_id,{}) VALUES (%s,%s,{}) ON CONFLICT(id) DO NOTHING RETURNING id").format(
            table, sql.SQL(",").join(map(sql.Identifier, FIELDS[kind])), sql.SQL(",").join(expressions))
        if connection.execute(statement, [identifier, owner, *parameters]).fetchone() is None:
            raise_error(ErrorCode.BETA_VERSION_CONFLICT)
    else:
        assignments = sql.SQL(",").join(sql.SQL("{}={}").format(sql.Identifier(field), expression)
                                       for field, expression in zip(FIELDS[kind], expressions, strict=True))
        connection.execute(sql.SQL("UPDATE public.{} SET {} WHERE id=%s AND owner_id=%s").format(table, assignments), [*parameters, identifier, owner])


class JournalRepository(DetectionRepository):
    @staticmethod
    def _outing(connection: Connection, owner: UUID, identifier: UUID) -> Outing:
        row = connection.execute(f"SELECT o.*, {OUTING_COUNTS} FROM public.outings o WHERE o.id=%s AND o.owner_id=%s", (identifier, owner)).fetchone()
        if row is None:
            raise_error(ErrorCode.BETA_REFERENCE_NOT_FOUND)
        return Outing.model_validate({key: value for key, value in row.items() if key != "owner_id"})

    def save_outing(self, owner: UUID, identifier: UUID, payload: OutingInput) -> Outing:
        with self.transaction(owner) as c:
            _save(c, owner, "outing", identifier, payload)
            return self._outing(c, owner, identifier)

    def outing(self, owner: UUID, identifier: UUID) -> Outing:
        with self.transaction(owner) as c:
            return self._outing(c, owner, identifier)

    def outings(self, owner: UUID, query: DescendingQuery) -> OutingPage:
        with self.transaction(owner) as c:
            rows = c.execute("""SELECT id FROM public.outings WHERE owner_id=%s AND
                (%s::timestamptz IS NULL OR started_at < %s OR (started_at=%s AND id>%s::uuid))
                ORDER BY started_at DESC,id LIMIT %s""", (owner, query.before, query.before, query.before, query.after_id, query.limit + 1)).fetchall()
            return OutingPage(outings=[self._outing(c, owner, row["id"]) for row in rows[:query.limit]], has_more=len(rows) > query.limit)

    def remove(self, owner: UUID, kind: EntityKind, identifier: UUID, expected_version: int) -> None:
        with self.transaction(owner) as c:
            removed = require_row(c.execute("SELECT private.delete_beta_entity(%s,%s,%s) AS removed", (kind, identifier, expected_version)).fetchone())
            if not removed["removed"]:
                raise_error(ErrorCode.BETA_VERSION_CONFLICT)

    def link_detection(self, owner: UUID, outing: UUID, detection: UUID) -> None:
        with self.transaction(owner) as c:
            require_owned(c, "outings", owner, outing)
            require_owned(c, "detections", owner, detection, "user_id")
            # Serialize membership changes for this detection, independent of mutable outing metadata.
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"{owner}:outing-detection:{detection}",))
            current = c.execute("SELECT outing_id FROM public.outing_detections WHERE detection_id=%s AND owner_id=%s", (detection, owner)).fetchone()
            if current is not None and current["outing_id"] != outing:
                raise_error(ErrorCode.BETA_VERSION_CONFLICT)
            c.execute("INSERT INTO public.outing_detections(owner_id,outing_id,detection_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (owner, outing, detection))

    def unlink_detection(self, owner: UUID, outing: UUID, detection: UUID) -> None:
        with self.transaction(owner) as c:
            c.execute("DELETE FROM public.outing_detections WHERE owner_id=%s AND outing_id=%s AND detection_id=%s", (owner, outing, detection))

    def save_observation(self, owner: UUID, identifier: UUID, payload: ObservationInput) -> Observation:
        with self.transaction(owner) as c:
            _save(c, owner, "observation", identifier, payload)
            return self._observation(c, owner, identifier)

    @staticmethod
    def _observation(connection: Connection, owner: UUID, identifier: UUID) -> Observation:
        row = _metadata(connection, owner, "observation", identifier)
        if row is None:
            raise_error(ErrorCode.BETA_REFERENCE_NOT_FOUND)
        return Observation.model_validate({key: value for key, value in row.items() if key != "owner_id"})

    def observation(self, owner: UUID, identifier: UUID) -> Observation:
        with self.transaction(owner) as c:
            return self._observation(c, owner, identifier)

    def observations(self, owner: UUID, query: ObservationQuery) -> ObservationPage:
        if query.species is not None:
            require_model_species(query.species)
        with self.transaction(owner) as c:
            if query.outing_id:
                require_owned(c, "outings", owner, query.outing_id)
            rows = c.execute("""SELECT id FROM public.observations WHERE owner_id=%(owner)s
                AND (%(outing)s::uuid IS NULL OR outing_id=%(outing)s)
                AND (%(species)s::text IS NULL OR species=%(species)s)
                AND (%(before)s::timestamptz IS NULL OR observed_at < %(before)s OR (observed_at=%(before)s AND id>%(after_id)s::uuid))
                ORDER BY observed_at DESC,id LIMIT %(fetch)s""",
                {"owner": owner, "outing": query.outing_id, "species": query.species, "before": query.before,
                 "after_id": query.after_id, "fetch": query.limit + 1}).fetchall()
            return ObservationPage(observations=[self._observation(c, owner, r["id"]) for r in rows[:query.limit]], has_more=len(rows) > query.limit)

    def deletions(self, owner: UUID, query: DeletionQuery) -> DeletionPage:
        with self.transaction(owner) as c:
            rows = c.execute("""SELECT entity_kind AS kind,entity_id AS id,version,deleted_at FROM public.beta_deletions WHERE owner_id=%s
                AND (%s::timestamptz IS NULL OR (deleted_at,entity_id,entity_kind) > (%s,%s::uuid,%s::text))
                ORDER BY deleted_at,entity_id,entity_kind LIMIT %s""", (owner, query.after, query.after, query.after_id, query.after_kind, query.limit + 1)).fetchall()
        return DeletionPage(deletions=[Deletion.model_validate(row) for row in rows[:query.limit]], has_more=len(rows) > query.limit)

    def events(self, owner: UUID, identifier: UUID, query: EventQuery, *, collection: bool = False) -> list[TimelineEvent]:
        with self.transaction(owner) as c:
            require_owned(c, "collections" if collection else "outings", owner, identifier)
            acoustic_link = ("JOIN public.collection_items x ON x.detection_id=d.id AND x.kind='detection'" if collection
                             else "JOIN public.outing_detections x ON x.detection_id=d.id")
            visual_link = "JOIN public.collection_items x ON x.observation_id=v.id AND x.kind='observation'" if collection else ""
            acoustic_scope = "x.collection_id=%(parent)s" if collection else "x.outing_id=%(parent)s"
            visual_scope = "x.collection_id=%(parent)s AND x.owner_id=%(owner)s" if collection else "v.outing_id=%(parent)s"
            rows = c.execute(f"""SELECT * FROM (
                SELECT 'detection'::text AS kind,d.id,d.momento AS recorded_at,d.especie AS species,d.confianza AS confidence,
                    d.estado AS status,d.ruta_grabacion IS NOT NULL AS has_recording,NULL::integer AS individuals
                  FROM public.detections d {acoustic_link} WHERE d.user_id=%(owner)s AND x.owner_id=%(owner)s AND {acoustic_scope}
                UNION ALL
                SELECT 'observation'::text,v.id,v.observed_at,v.species,NULL,NULL,NULL,v.individuals
                  FROM public.observations v {visual_link} WHERE v.owner_id=%(owner)s AND {visual_scope}
                ) events WHERE (%(after)s::timestamptz IS NULL OR (recorded_at,id,kind)>(%(after)s,%(after_id)s::uuid,%(after_kind)s::text))
                ORDER BY recorded_at,id,kind LIMIT %(fetch)s""", {"owner": owner, "parent": identifier, "after": query.after,
                    "after_id": query.after_id, "after_kind": query.after_kind, "fetch": query.limit + 1}).fetchall()
        result: list[TimelineEvent] = []
        for row in rows:
            common = {key: row[key] for key in ("id", "recorded_at", "species")}
            if row["kind"] == "detection":
                result.append(DetectionEvent(**common, confidence=row["confidence"], status=DB_TO_API_STATUS[DbDetectionStatus(row["status"])], has_recording=row["has_recording"]))
            else:
                result.append(ObservationEvent(**common, individuals=row["individuals"]))
        return result

    def timeline(self, owner: UUID, identifier: UUID, query: EventQuery) -> TimelinePage:
        events = self.events(owner, identifier, query)
        return TimelinePage(events=events[:query.limit], has_more=len(events) > query.limit)


class OrganizationRepository(JournalRepository):
    @staticmethod
    def _collection(connection: Connection, owner: UUID, identifier: UUID) -> Collection:
        row = connection.execute("""SELECT c.*,(SELECT count(*) FROM public.collection_items i WHERE i.collection_id=c.id AND i.owner_id=c.owner_id) AS items
            FROM public.collections c WHERE c.id=%s AND c.owner_id=%s""", (identifier, owner)).fetchone()
        if row is None:
            raise_error(ErrorCode.BETA_REFERENCE_NOT_FOUND)
        return Collection.model_validate({key: value for key, value in row.items() if key != "owner_id"})

    def save_collection(self, owner: UUID, identifier: UUID, payload: CollectionInput) -> Collection:
        with self.transaction(owner) as c:
            _save(c, owner, "collection", identifier, payload)
            return self._collection(c, owner, identifier)

    def collection(self, owner: UUID, identifier: UUID) -> Collection:
        with self.transaction(owner) as c:
            return self._collection(c, owner, identifier)

    def collections(self, owner: UUID, query: DescendingQuery) -> CollectionPage:
        with self.transaction(owner) as c:
            rows = c.execute("""SELECT id FROM public.collections WHERE owner_id=%s AND
                (%s::timestamptz IS NULL OR created_at < %s OR (created_at=%s AND id>%s::uuid))
                ORDER BY created_at DESC,id LIMIT %s""", (owner, query.before, query.before, query.before, query.after_id, query.limit + 1)).fetchall()
            return CollectionPage(collections=[self._collection(c, owner, r["id"]) for r in rows[:query.limit]], has_more=len(rows) > query.limit)

    def link_item(self, owner: UUID, collection: UUID, kind: EventKind, identifier: UUID) -> None:
        with self.transaction(owner) as c:
            require_owned(c, "collections", owner, collection)
            require_owned(c, "detections" if kind == "detection" else "observations", owner, identifier, "user_id" if kind == "detection" else "owner_id")
            c.execute("INSERT INTO public.collection_items(owner_id,collection_id,kind,detection_id,observation_id) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                      (owner, collection, kind, identifier if kind == "detection" else None, identifier if kind == "observation" else None))

    def unlink_item(self, owner: UUID, collection: UUID, kind: EventKind, identifier: UUID) -> None:
        with self.transaction(owner) as c:
            c.execute("DELETE FROM public.collection_items WHERE owner_id=%s AND collection_id=%s AND kind=%s AND (detection_id=%s OR observation_id=%s)",
                      (owner, collection, kind, identifier, identifier))

    def items(self, owner: UUID, identifier: UUID, query: EventQuery) -> CollectionItemPage:
        events = self.events(owner, identifier, query, collection=True)
        return CollectionItemPage(items=events[:query.limit], has_more=len(events) > query.limit)

    def wish(self, owner: UUID, species: str) -> WishlistItem:
        require_model_species(species)
        with self.transaction(owner) as c:
            require_owned(c, "profiles", owner, owner, "id")
            c.execute("INSERT INTO public.wishlist(owner_id,species) VALUES (%s,%s) ON CONFLICT DO NOTHING", (owner, species))
            row = require_row(c.execute("SELECT species,created_at FROM public.wishlist WHERE owner_id=%s AND species=%s", (owner, species)).fetchone())
        return WishlistItem.model_validate(row)

    def unwish(self, owner: UUID, species: str) -> None:
        require_model_species(species)
        with self.transaction(owner) as c:
            c.execute("DELETE FROM public.wishlist WHERE owner_id=%s AND species=%s", (owner, species))

    def wishlist(self, owner: UUID, query: WishlistQuery) -> WishlistPage:
        with self.transaction(owner) as c:
            rows = c.execute("SELECT species,created_at FROM public.wishlist WHERE owner_id=%s AND (%s::text IS NULL OR species>%s) ORDER BY species LIMIT %s",
                             (owner, query.after_species, query.after_species, query.limit + 1)).fetchall()
        return WishlistPage(species=[WishlistItem.model_validate(row) for row in rows[:query.limit]], has_more=len(rows) > query.limit)
