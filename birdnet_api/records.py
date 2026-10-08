"""The caller's personal record: totals, species list and one species in detail.

Detections are readable by every signed-in user (collective map), so each query filters by owner explicitly.
Discarded detections never count as presence.
"""
from typing import Any, Final
from uuid import UUID

from .contracts import OwnSpecies, OwnSpeciesList, RecordSummary, SpeciesCell, SpeciesDetection, SpeciesRecord, SpeciesSite
from .domain import (
    DB_TO_API_STATUS,
    HOURS_PER_DAY,
    MAX_OWN_SPECIES,
    MAX_RECENT_DETECTIONS,
    MAX_SPECIES_CELLS,
    DbDetectionStatus,
)
from .repository import DetectionRepository

OWN_RECORD: Final = "d.user_id = %(owner)s AND d.estado <> %(discarded)s"
OWN_SPECIES: Final = OWN_RECORD + " AND d.especie = %(species)s"


class RecordRepository(DetectionRepository):
    @staticmethod
    def _params(owner: UUID, **extra: Any) -> dict[str, Any]:
        return {"owner": owner, "discarded": DbDetectionStatus.DISCARDED.value, **extra}

    def summary(self, owner: UUID, tz: str) -> RecordSummary:
        params = self._params(owner, tz=tz)
        with self.transaction(owner) as connection:
            totals = connection.execute(
                f"""SELECT count(*) AS detections, count(DISTINCT d.especie) AS species, min(d.momento) AS first,
                    max(d.momento) AS last, count(DISTINCT (d.momento AT TIME ZONE %(tz)s)::date) AS days
                FROM public.detections d WHERE {OWN_RECORD}""", params).fetchone()
            sites = connection.execute("SELECT count(*) AS n FROM public.sites WHERE owner_id = %(owner)s", params).fetchone()
        return RecordSummary(detections=totals["detections"], species=totals["species"], sites=sites["n"],
                             first_recorded_at=totals["first"], last_recorded_at=totals["last"], active_days=totals["days"])

    def species(self, owner: UUID) -> OwnSpeciesList:
        with self.transaction(owner) as connection:
            rows = connection.execute(
                f"""SELECT d.especie, count(*) AS n, max(d.confianza) AS best, min(d.momento) AS first, max(d.momento) AS last,
                    count(DISTINCT d.site_id) AS sites
                FROM public.detections d WHERE {OWN_RECORD}
                GROUP BY d.especie ORDER BY last DESC, d.especie LIMIT %(limit)s""",
                self._params(owner, limit=MAX_OWN_SPECIES)).fetchall()
        return OwnSpeciesList(species=[
            OwnSpecies(species=r["especie"], detections=r["n"], best_confidence=r["best"], first_recorded_at=r["first"],
                       last_recorded_at=r["last"], sites=r["sites"])
            for r in rows
        ])

    def species_record(self, owner: UUID, species: str, tz: str) -> SpeciesRecord:
        params = self._params(owner, species=species, tz=tz, cells=MAX_SPECIES_CELLS, recent=MAX_RECENT_DETECTIONS)
        with self.transaction(owner) as connection:
            totals = connection.execute(
                f"""SELECT count(*) AS n, max(d.confianza) AS best, min(d.momento) AS first, max(d.momento) AS last
                FROM public.detections d WHERE {OWN_SPECIES}""", params).fetchone()
            hours = connection.execute(
                f"""SELECT extract(hour FROM d.momento AT TIME ZONE %(tz)s)::int AS h, count(*) AS n
                FROM public.detections d WHERE {OWN_SPECIES} GROUP BY h""", params).fetchall()
            sites = connection.execute(
                f"""SELECT s.id, s.nombre, count(*) AS n
                FROM public.detections d JOIN public.sites s ON s.id = d.site_id
                WHERE {OWN_SPECIES} GROUP BY s.id, s.nombre ORDER BY n DESC, s.nombre""", params).fetchall()
            # Stored points are already rounded to the ~100 m grid, so grouping by them never reveals more.
            cells = connection.execute(
                f"""SELECT ST_Y(d.ubicacion::geometry) AS latitude, ST_X(d.ubicacion::geometry) AS longitude, count(*) AS n
                FROM public.detections d WHERE {OWN_SPECIES}
                GROUP BY latitude, longitude ORDER BY n DESC, latitude, longitude LIMIT %(cells)s""", params).fetchall()
            recent = connection.execute(
                f"""SELECT d.id, d.momento, d.confianza, d.estado, d.site_id, d.ruta_audio IS NOT NULL AS has_audio
                FROM public.detections d WHERE {OWN_SPECIES} ORDER BY d.momento DESC LIMIT %(recent)s""", params).fetchall()
        hourly = [0] * HOURS_PER_DAY
        for row in hours:
            hourly[row["h"]] = row["n"]
        return SpeciesRecord(
            species=species, detections=totals["n"], best_confidence=totals["best"], first_recorded_at=totals["first"],
            last_recorded_at=totals["last"], hours=hourly,
            sites=[SpeciesSite(id=r["id"], name=r["nombre"], detections=r["n"]) for r in sites],
            cells=[SpeciesCell(latitude=r["latitude"], longitude=r["longitude"], detections=r["n"]) for r in cells],
            recent=[SpeciesDetection(id=r["id"], recorded_at=r["momento"], confidence=r["confianza"], status=DB_TO_API_STATUS[r["estado"]],
                                     site_id=r["site_id"], has_audio=r["has_audio"]) for r in recent],
        )
