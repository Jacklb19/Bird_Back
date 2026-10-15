"""Walks the caller saved in their account (ADR-35): private paths whose songs are the caller's own detections.

RLS already limits every walk to its owner; each query also filters by owner explicitly, because the detections it
joins are readable by other signed-in users while shared (collective map).
"""
import json
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import psycopg

from .contracts import Walk, WalkDetail, WalkInput, WalkPage, WalkPlace, WalkSpecies
from .domain import MAX_WALK_PLACES, MAX_WALK_SPECIES, DbDetectionStatus, track_meters
from .errors import ErrorCode, raise_error
from .repository import DetectionRepository

# The walk's songs: the owner's detections within its period that a person did not discard.
WALK_SONGS: Final = "d.user_id = w.user_id AND d.estado <> %(discarded)s AND d.momento BETWEEN w.inicio AND w.fin"

WALK_COLUMNS: Final = f"""w.id, w.nombre, w.inicio, w.fin, w.distancia_m, jsonb_array_length(w.recorrido) AS points,
    coalesce(s.detections, 0) AS detections, coalesce(s.species, '[]'::jsonb) AS species
FROM public.walks w
LEFT JOIN LATERAL (
    SELECT sum(g.n)::int AS detections,
        jsonb_agg(jsonb_build_object('species', g.especie, 'detections', g.n, 'best_confidence', g.best) ORDER BY g.n DESC, g.especie) AS species
    FROM (
        SELECT d.especie, count(*) AS n, max(d.confianza) AS best
        FROM public.detections d WHERE {WALK_SONGS}
        GROUP BY d.especie ORDER BY n DESC, d.especie LIMIT %(species_limit)s
    ) g
) s ON true"""


def _walk(row: dict[str, Any]) -> Walk:
    return Walk(id=row["id"], name=row["nombre"], started_at=row["inicio"], ended_at=row["fin"], distance_m=row["distancia_m"],
                points=row["points"], detections=row["detections"], species=[WalkSpecies.model_validate(item) for item in row["species"]])


class WalkRepository(DetectionRepository):
    @staticmethod
    def _params(owner: UUID, **extra: Any) -> dict[str, Any]:
        return {"owner": owner, "discarded": DbDetectionStatus.DISCARDED.value, "species_limit": MAX_WALK_SPECIES, **extra}

    @staticmethod
    def _read(connection: psycopg.Connection[dict[str, Any]], owner: UUID, walk: UUID) -> Walk | None:
        row = connection.execute(f"SELECT {WALK_COLUMNS} WHERE w.id = %(walk)s AND w.user_id = %(owner)s",
                                 WalkRepository._params(owner, walk=walk)).fetchone()
        return _walk(row) if row else None

    def save(self, owner: UUID, walk_id: UUID, walk: WalkInput, quota: int) -> Walk:
        """Stores a new walk, or the later state of one already stored; refuses a new one beyond the quota."""
        cells = [(point[0], point[1]) for point in walk.track]
        params = {"id": walk_id, "owner": owner, "name": walk.name, "started": walk.started_at, "ended": walk.ended_at,
                  "track": json.dumps([list(point) for point in walk.track]), "distance": track_meters(cells)}
        with self.transaction(owner) as connection:
            # Serializes the caller's concurrent uploads, so two of them cannot both take the last free place.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (str(owner),))
            # RLS hides another person's walk, so an identifier taken by someone else looks absent here.
            stored = connection.execute("SELECT id FROM public.walks WHERE id = %(id)s AND user_id = %(owner)s", params).fetchone()
            if stored:
                # A retry of the same upload rewrites the same values; a later one (a new name) replaces them.
                connection.execute(
                    """UPDATE public.walks SET nombre = %(name)s, inicio = %(started)s, fin = %(ended)s, recorrido = %(track)s::jsonb,
                        distancia_m = %(distance)s WHERE id = %(id)s AND user_id = %(owner)s""", params)
            else:
                count = connection.execute("SELECT count(*) AS n FROM public.walks WHERE user_id = %(owner)s", params).fetchone()
                if count["n"] >= quota:
                    raise_error(ErrorCode.WALK_QUOTA_EXCEEDED)
                inserted = connection.execute(
                    """INSERT INTO public.walks (id, user_id, nombre, inicio, fin, recorrido, distancia_m)
                    VALUES (%(id)s, %(owner)s, %(name)s, %(started)s, %(ended)s, %(track)s::jsonb, %(distance)s)
                    ON CONFLICT (id) DO NOTHING RETURNING id""", params).fetchone()
                if inserted is None:
                    raise_error(ErrorCode.WALK_ID_CONFLICT)
            saved = self._read(connection, owner, walk_id)
        if saved is None:
            raise_error(ErrorCode.WALK_NOT_FOUND)
        return saved

    def rename(self, owner: UUID, walk_id: UUID, name: str | None) -> Walk | None:
        with self.transaction(owner) as connection:
            updated = connection.execute("UPDATE public.walks SET nombre = %s WHERE id = %s AND user_id = %s", (name, walk_id, owner)).rowcount
            return self._read(connection, owner, walk_id) if updated else None

    def page(self, owner: UUID, before: datetime | None, limit: int) -> WalkPage:
        with self.transaction(owner) as connection:
            rows = connection.execute(
                f"""SELECT {WALK_COLUMNS}
                WHERE w.user_id = %(owner)s AND (%(before)s::timestamptz IS NULL OR w.inicio < %(before)s)
                ORDER BY w.inicio DESC, w.id LIMIT %(fetch)s""",
                # One extra row tells whether another page follows.
                self._params(owner, before=before, fetch=limit + 1)).fetchall()
        return WalkPage(walks=[_walk(row) for row in rows[:limit]], has_more=len(rows) > limit)

    def detail(self, owner: UUID, walk_id: UUID) -> WalkDetail | None:
        params = self._params(owner, walk=walk_id, places=MAX_WALK_PLACES)
        with self.transaction(owner) as connection:
            walk = self._read(connection, owner, walk_id)
            if walk is None:
                return None
            track = connection.execute("SELECT recorrido FROM public.walks WHERE id = %(walk)s AND user_id = %(owner)s", params).fetchone()
            # Stored points are already rounded to the location grid, so grouping by them never reveals more.
            places = connection.execute(
                f"""SELECT d.especie, ST_Y(d.ubicacion::geometry) AS latitude, ST_X(d.ubicacion::geometry) AS longitude,
                    count(*) AS n, max(d.confianza) AS best, min(d.momento) AS first
                FROM public.walks w JOIN public.detections d ON {WALK_SONGS}
                WHERE w.id = %(walk)s AND w.user_id = %(owner)s
                GROUP BY d.especie, latitude, longitude ORDER BY first, d.especie LIMIT %(places)s""", params).fetchall()
        return WalkDetail(**walk.model_dump(), track=track["recorrido"],
                          places=[WalkPlace(species=r["especie"], latitude=r["latitude"], longitude=r["longitude"], detections=r["n"],
                                            best_confidence=r["best"], first_recorded_at=r["first"]) for r in places])

    def delete(self, owner: UUID, walk_id: UUID) -> None:
        with self.transaction(owner) as connection:
            connection.execute("DELETE FROM public.walks WHERE id = %s AND user_id = %s", (walk_id, owner))

    def delete_all(self, owner: UUID) -> int:
        with self.transaction(owner) as connection:
            return connection.execute("DELETE FROM public.walks WHERE user_id = %s", (owner,)).rowcount

    def usage(self, owner: UUID) -> int:
        with self.transaction(owner) as connection:
            return connection.execute("SELECT count(*) AS n FROM public.walks WHERE user_id = %s", (owner,)).fetchone()["n"]
