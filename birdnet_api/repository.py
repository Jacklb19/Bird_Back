"""Use one database transaction per batch; RLS remains active for every query."""
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .contracts import BatchResponse, DetectionInput, MapDetection, MapQuery, MapResponse

MAP_LIMIT = 2000
STATUS = {"confirmada": "confirmed", "provisional": "provisional", "verificada": "verified", "corregida": "corrected"}


class DetectionRepository:
    @contextmanager
    def transaction(self, owner: UUID) -> Iterator[psycopg.Connection]:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise HTTPException(503, "Database is not configured")
        try:
            # Supabase's transaction pooler cannot keep server-side prepared statements.
            with psycopg.connect(url, connect_timeout=10, row_factory=dict_row, prepare_threshold=None) as connection:
                connection.execute("SET LOCAL ROLE authenticated")
                connection.execute("SELECT set_config('request.jwt.claims', %s, true)", (json.dumps({"sub": str(owner), "role": "authenticated"}),))
                yield connection
        except psycopg.Error:
            raise HTTPException(503, "Database operation failed; retry later") from None

    def get_owned(self, owner: UUID, detection: UUID) -> dict | None:
        with self.transaction(owner) as connection:
            return connection.execute("SELECT id, estado FROM public.detections WHERE id = %s AND user_id = %s", (detection, owner)).fetchone()

    def batch(self, owner: UUID, detections: list[DetectionInput]) -> BatchResponse:
        accepted: list[UUID] = []
        existing: list[UUID] = []
        with self.transaction(owner) as connection:
            sites = list({row.site_id for row in detections if row.site_id})
            if sites:
                # A foreign site would pass the foreign key check; RLS on sites makes it invisible here.
                visible = {r["id"] for r in connection.execute("SELECT id FROM public.sites WHERE id = ANY(%s)", (sites,)).fetchall()}
                if visible != set(sites):
                    raise HTTPException(422, "Unknown site")
            for row in detections:
                inserted = connection.execute(
                    """INSERT INTO public.detections (id,user_id,site_id,especie,confianza,estado,momento,ubicacion,version_modelo)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s)
                    ON CONFLICT (id) DO NOTHING RETURNING id""",
                    (row.id, owner, row.site_id, row.species, row.confidence, "confirmada" if row.status == "confirmed" else "provisional", row.recorded_at, row.location.longitude, row.location.latitude, row.model_version),
                ).fetchone()
                owned = connection.execute("SELECT id, especie, version_modelo, ruta_audio FROM public.detections WHERE id=%s AND user_id=%s", (row.id, owner)).fetchone()
                if not owned or owned["especie"] != row.species or owned["version_modelo"] != row.model_version:
                    raise HTTPException(409, "Detection identifier conflict")
                if row.audio_path:
                    if owned["ruta_audio"] not in (None, row.audio_path):
                        raise HTTPException(409, "Audio identifier conflict")
                    connection.execute("UPDATE public.detections SET ruta_audio=%s WHERE id=%s AND user_id=%s", (row.audio_path, row.id, owner))
                    connection.execute("INSERT INTO public.verification_jobs(detection_id) VALUES (%s) ON CONFLICT (detection_id) DO NOTHING", (row.id,))
                (accepted if inserted else existing).append(row.id)
        return BatchResponse(accepted_ids=accepted, existing_ids=existing)

    def map(self, viewer: UUID, query: MapQuery) -> MapResponse:
        # Discarded detections stay private to their author; the map only shows usable indications.
        with self.transaction(viewer) as connection:
            rows = connection.execute(
                """SELECT id, especie, confianza, estado, momento,
                    ST_Y(ubicacion::geometry) AS latitude, ST_X(ubicacion::geometry) AS longitude
                FROM public.detections
                WHERE estado <> 'descartada'
                  AND ST_Intersects(ubicacion::geometry, ST_MakeEnvelope(%s, %s, %s, %s, 4326))
                  AND (%s::text IS NULL OR especie = %s)
                  AND (%s::timestamptz IS NULL OR momento >= %s)
                  AND (%s::timestamptz IS NULL OR momento < %s)
                ORDER BY momento DESC LIMIT %s""",
                (query.west, query.south, query.east, query.north, query.species, query.species, query.since, query.since, query.until, query.until, MAP_LIMIT + 1),
            ).fetchall()
        detections = [
            MapDetection(id=row["id"], species=row["especie"], confidence=row["confianza"], status=STATUS[row["estado"]],
                         recorded_at=row["momento"], latitude=row["latitude"], longitude=row["longitude"])
            for row in rows[:MAP_LIMIT]
        ]
        return MapResponse(detections=detections, truncated=len(rows) > MAP_LIMIT)
