"""Use one database transaction per batch; RLS remains active for every query except the trusted ones."""
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .auth import AUTHENTICATED_ROLE, ROLE_CLAIM, SUBJECT_CLAIM
from .contracts import BatchResponse, DetectionInput, MapDetection, MapQuery, MapResponse, ReviewedDetection
from .domain import API_TO_DB_STATUS, DB_TO_API_REVIEWER, DB_TO_API_STATUS, WGS84_SRID, DbDetectionStatus, DbReviewer, ReviewAction
from .errors import ErrorCode, raise_error
from .settings import get_settings

# Transaction-local setting that Supabase's auth.uid() reads, so RLS policies see the requesting user.
JWT_CLAIMS_SETTING: Final = "request.jwt.claims"


class DetectionRepository:
    @contextmanager
    def transaction(self, owner: UUID) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        with self._connect() as connection:
            connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(AUTHENTICATED_ROLE)))
            claims = json.dumps({SUBJECT_CLAIM: str(owner), ROLE_CLAIM: AUTHENTICATED_ROLE})
            connection.execute("SELECT set_config(%s, %s, true)", (JWT_CLAIMS_SETTING, claims))
            yield connection

    @contextmanager
    def trusted_transaction(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        """A transaction as the API's own database role, outside RLS.

        Only for tables no client may write (texts of the language model and their usage counts): their policies
        let people read, never write, so the API writes them here after checking ownership itself.
        """
        with self._connect() as connection:
            yield connection

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        settings = get_settings()
        if not settings.database_url:
            raise_error(ErrorCode.DATABASE_NOT_CONFIGURED)
        try:
            # Supabase's transaction pooler cannot keep server-side prepared statements.
            with psycopg.connect(settings.database_url, connect_timeout=settings.database_connect_timeout_seconds, row_factory=dict_row, prepare_threshold=None) as connection:
                yield connection
        except psycopg.Error:
            raise_error(ErrorCode.DATABASE_UNAVAILABLE)

    def get_owned(self, owner: UUID, detection: UUID) -> dict[str, Any] | None:
        with self.transaction(owner) as connection:
            return connection.execute("SELECT id FROM public.detections WHERE id = %s AND user_id = %s", (detection, owner)).fetchone()

    def batch(self, owner: UUID, detections: list[DetectionInput]) -> BatchResponse:
        accepted: list[UUID] = []
        existing: list[UUID] = []
        with self.transaction(owner) as connection:
            sites = list({row.site_id for row in detections if row.site_id})
            if sites:
                # A foreign site would pass the foreign key check; RLS on sites makes it invisible here.
                visible = {r["id"] for r in connection.execute("SELECT id FROM public.sites WHERE id = ANY(%s)", (sites,)).fetchall()}
                if visible != set(sites):
                    raise_error(ErrorCode.UNKNOWN_SITE)
            for row in detections:
                # A retry never rewrites a stored row, so it cannot undo a later sharing choice either.
                inserted = connection.execute(
                    """INSERT INTO public.detections (id,user_id,site_id,especie,confianza,estado,momento,ubicacion,version_modelo,compartida)
                    VALUES (%(id)s,%(owner)s,%(site)s,%(species)s,%(confidence)s,%(status)s,%(recorded_at)s,
                        ST_SetSRID(ST_MakePoint(%(longitude)s,%(latitude)s),%(srid)s::integer)::geography,%(model_version)s,%(shared)s)
                    ON CONFLICT (id) DO NOTHING RETURNING id""",
                    {"id": row.id, "owner": owner, "site": row.site_id, "species": row.species, "confidence": row.confidence,
                     "status": API_TO_DB_STATUS[row.status].value, "recorded_at": row.recorded_at, "longitude": row.location.longitude,
                     "latitude": row.location.latitude, "srid": WGS84_SRID, "model_version": row.model_version, "shared": row.shared},
                ).fetchone()
                # A retry carries the species the model gave, which a later correction keeps in `especie_modelo`.
                owned = connection.execute("SELECT id, coalesce(especie_modelo, especie) AS especie, version_modelo FROM public.detections WHERE id=%s AND user_id=%s", (row.id, owner)).fetchone()
                if not owned or owned["especie"] != row.species or owned["version_modelo"] != row.model_version:
                    raise_error(ErrorCode.DETECTION_ID_CONFLICT)
                (accepted if inserted else existing).append(row.id)
        return BatchResponse(accepted_ids=accepted, existing_ids=existing)

    def review(self, owner: UUID, detection: UUID, action: ReviewAction, species: str | None) -> ReviewedDetection | None:
        """Applies a person's verdict to one of their detections; None when it is not theirs.

        Confirming restores the species the model gave, and a correction back to that species is a confirmation,
        so the state never says "corrected" about a detection that names what the model said. Repeating a verdict
        changes nothing but its date.
        """
        params = {"id": detection, "owner": owner, "species": species, "person": DbReviewer.PERSON.value,
                  "verified": DbDetectionStatus.VERIFIED.value, "corrected": DbDetectionStatus.CORRECTED.value,
                  "discarded": DbDetectionStatus.DISCARDED.value}
        with self.transaction(owner) as connection:
            if action == ReviewAction.DISCARD:
                row = connection.execute(
                    """UPDATE public.detections SET estado = %(discarded)s, revisada_por = %(person)s, revisada_en = now()
                    WHERE id = %(id)s AND user_id = %(owner)s RETURNING id, especie, especie_modelo, estado, revisada_por""", params).fetchone()
            else:
                # Every right-hand side reads the row as it was before the update.
                row = connection.execute(
                    """UPDATE public.detections SET
                        especie = coalesce(%(species)s::text, especie_modelo, especie),
                        especie_modelo = CASE WHEN coalesce(%(species)s::text, especie_modelo, especie) = coalesce(especie_modelo, especie)
                            THEN NULL ELSE coalesce(especie_modelo, especie) END,
                        estado = CASE WHEN coalesce(%(species)s::text, especie_modelo, especie) = coalesce(especie_modelo, especie)
                            THEN %(verified)s ELSE %(corrected)s END,
                        revisada_por = %(person)s, revisada_en = now()
                    WHERE id = %(id)s AND user_id = %(owner)s RETURNING id, especie, especie_modelo, estado, revisada_por""", params).fetchone()
        if row is None:
            return None
        return ReviewedDetection(id=row["id"], species=row["especie"], model_species=row["especie_modelo"],
                                 status=DB_TO_API_STATUS[row["estado"]], reviewed_by=DB_TO_API_REVIEWER[row["revisada_por"]])

    def set_sharing(self, owner: UUID, shared: bool) -> int:
        """Apply the choice to everything the caller already uploaded; returns how many rows changed."""
        with self.transaction(owner) as connection:
            # Rows that already match are skipped so a repeated request rewrites nothing.
            return connection.execute("UPDATE public.detections SET compartida=%(shared)s WHERE user_id=%(owner)s AND compartida<>%(shared)s",
                                      {"shared": shared, "owner": owner}).rowcount

    def map(self, viewer: UUID, query: MapQuery) -> MapResponse:
        limit = get_settings().map_result_limit
        # Discarded detections stay private to their author; the map only shows usable indications.
        # Unshared detections reach only their author: RLS already hides them, and the filter keeps them out even without it.
        # Site names are private too: RLS already hides foreign sites, and the CASE keeps them out even without it.
        with self.transaction(viewer) as connection:
            rows = connection.execute(
                """SELECT d.id, d.especie, d.confianza, d.estado, d.momento,
                    ST_Y(d.ubicacion::geometry) AS latitude, ST_X(d.ubicacion::geometry) AS longitude,
                    d.user_id = %(viewer)s AS own, CASE WHEN d.user_id = %(viewer)s THEN s.nombre END AS site_name
                FROM public.detections d LEFT JOIN public.sites s ON s.id = d.site_id
                WHERE d.estado <> %(discarded)s
                  AND (d.compartida OR d.user_id = %(viewer)s)
                  AND ST_Intersects(d.ubicacion::geometry, ST_MakeEnvelope(%(west)s, %(south)s, %(east)s, %(north)s, %(srid)s::integer))
                  AND (%(species)s::text IS NULL OR d.especie = %(species)s)
                  AND (%(since)s::timestamptz IS NULL OR d.momento >= %(since)s)
                  AND (%(until)s::timestamptz IS NULL OR d.momento < %(until)s)
                ORDER BY d.momento DESC LIMIT %(fetch)s""",
                {"viewer": viewer, "discarded": DbDetectionStatus.DISCARDED.value, "west": query.west, "south": query.south, "east": query.east,
                 "north": query.north, "srid": WGS84_SRID, "species": query.species, "since": query.since, "until": query.until,
                 # One extra row tells whether the result was cut at the limit.
                 "fetch": limit + 1},
            ).fetchall()
        detections = [
            MapDetection(id=row["id"], species=row["especie"], confidence=row["confianza"], status=DB_TO_API_STATUS[row["estado"]],
                         recorded_at=row["momento"], latitude=row["latitude"], longitude=row["longitude"], own=row["own"],
                         site_name=row["site_name"])
            for row in rows[:limit]
        ]
        return MapResponse(detections=detections, truncated=len(rows) > limit)
