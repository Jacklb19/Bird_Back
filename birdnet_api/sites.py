"""Monitoring sites and their statistics; every query runs under the owner's RLS context."""
import csv
import io
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from .contracts import Site, SiteInput, SiteStats, SpeciesStat
from .domain import (
    CONFIDENCE_EXPORT_DECIMALS,
    DB_TO_API_STATUS,
    HOURS_PER_DAY,
    LOCATION_GRID_DECIMALS,
    MAX_MISSING_SPECIES,
    WGS84_SRID,
    DbDetectionStatus,
    ExportColumn,
    StatsPeriod,
)
from .errors import ErrorCode, raise_error
from .repository import DetectionRepository
from .settings import get_settings


class SiteRepository(DetectionRepository):
    def list_sites(self, owner: UUID) -> list[Site]:
        with self.transaction(owner) as connection:
            rows = connection.execute(
                """SELECT id, nombre, ST_Y(centro::geometry) AS latitude, ST_X(centro::geometry) AS longitude, created_at
                FROM public.sites ORDER BY created_at""",
            ).fetchall()
        return [Site(id=r["id"], name=r["nombre"], latitude=r["latitude"], longitude=r["longitude"], created_at=r["created_at"]) for r in rows]

    def create_site(self, owner: UUID, site: SiteInput) -> Site:
        with self.transaction(owner) as connection:
            row = connection.execute(
                """INSERT INTO public.sites (owner_id, nombre, centro)
                VALUES (%(owner)s, %(name)s, ST_SetSRID(ST_MakePoint(%(longitude)s, %(latitude)s), %(srid)s::integer)::geography)
                RETURNING id, nombre, ST_Y(centro::geometry) AS latitude, ST_X(centro::geometry) AS longitude, created_at""",
                {"owner": owner, "name": site.name, "longitude": site.location.longitude, "latitude": site.location.latitude, "srid": WGS84_SRID},
            ).fetchone()
        return Site(id=row["id"], name=row["nombre"], latitude=row["latitude"], longitude=row["longitude"], created_at=row["created_at"])

    @staticmethod
    def _require_site(connection: psycopg.Connection[dict[str, Any]], site_id: UUID) -> None:
        # Sites are private: RLS hides foreign rows, so "not found" also covers "not yours".
        if connection.execute("SELECT 1 FROM public.sites WHERE id = %s", (site_id,)).fetchone() is None:
            raise_error(ErrorCode.SITE_NOT_FOUND)

    def stats(self, owner: UUID, site_id: UUID, period: StatsPeriod, since: datetime | None, previous_since: datetime | None, until: datetime, tz: str) -> SiteStats:
        # Discarded detections never count as presence.
        window = "site_id = %(site)s AND estado <> %(discarded)s AND (%(since)s::timestamptz IS NULL OR momento >= %(since)s) AND momento < %(until)s"
        params = {"site": site_id, "since": since, "until": until, "tz": tz, "previous": previous_since, "discarded": DbDetectionStatus.DISCARDED.value}
        with self.transaction(owner) as connection:
            self._require_site(connection, site_id)
            species = connection.execute(
                f"""SELECT especie, count(*) AS n, count(DISTINCT (momento AT TIME ZONE %(tz)s)::date) AS days
                FROM public.detections WHERE {window} GROUP BY especie ORDER BY n DESC, especie""", params).fetchall()
            totals = connection.execute(
                f"SELECT count(*) AS n, count(DISTINCT (momento AT TIME ZONE %(tz)s)::date) AS days FROM public.detections WHERE {window}", params).fetchone()
            hours = connection.execute(
                f"""SELECT extract(hour FROM momento AT TIME ZONE %(tz)s)::int AS h, count(*) AS n
                FROM public.detections WHERE {window} GROUP BY h""", params).fetchall()
            history = connection.execute(
                """SELECT especie, min(momento) AS first, max(momento) AS last
                FROM public.detections WHERE site_id = %(site)s AND estado <> %(discarded)s GROUP BY especie""", params).fetchall()
            previous = None
            if since is not None and previous_since is not None:
                previous = connection.execute(
                    """SELECT count(DISTINCT especie) AS n FROM public.detections
                    WHERE site_id = %(site)s AND estado <> %(discarded)s AND momento >= %(previous)s AND momento < %(since)s""", params).fetchone()["n"]
        first_seen = {r["especie"]: r["first"] for r in history}
        hourly = [0] * HOURS_PER_DAY
        for row in hours:
            hourly[row["h"]] = row["n"]
        missing = [] if since is None else [r["especie"] for r in sorted(history, key=lambda r: r["last"], reverse=True) if r["last"] < since][:MAX_MISSING_SPECIES]
        return SiteStats(
            period=period, since=since, until=until, species_count=len(species), previous_species_count=previous,
            detections=totals["n"], active_days=totals["days"], hourly=hourly, missing=missing,
            species=[SpeciesStat(species=r["especie"], detections=r["n"], days=r["days"], first_seen=first_seen[r["especie"]],
                                 is_new=since is not None and first_seen[r["especie"]] >= since) for r in species],
        )

    def export_csv(self, owner: UUID, site_id: UUID, since: datetime | None, until: datetime | None) -> tuple[str, bool]:
        limit = get_settings().export_row_limit
        with self.transaction(owner) as connection:
            self._require_site(connection, site_id)
            rows = connection.execute(
                """SELECT d.id, d.especie, d.confianza, d.estado, d.momento, ST_Y(d.ubicacion::geometry) AS latitude,
                    ST_X(d.ubicacion::geometry) AS longitude, d.version_modelo, s.nombre
                FROM public.detections d JOIN public.sites s ON s.id = d.site_id
                WHERE d.site_id = %(site)s AND (%(since)s::timestamptz IS NULL OR d.momento >= %(since)s) AND (%(until)s::timestamptz IS NULL OR d.momento < %(until)s)
                ORDER BY d.momento LIMIT %(fetch)s""",
                # One extra row tells whether the export was cut at the limit.
                {"site": site_id, "since": since, "until": until, "fetch": limit + 1},
            ).fetchall()
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(ExportColumn))
        writer.writeheader()
        for r in rows[:limit]:
            writer.writerow({
                ExportColumn.ID: r["id"],
                ExportColumn.SPECIES: r["especie"],
                ExportColumn.CONFIDENCE: f"{r['confianza']:.{CONFIDENCE_EXPORT_DECIMALS}f}",
                ExportColumn.STATUS: DB_TO_API_STATUS[r["estado"]],
                ExportColumn.RECORDED_AT: r["momento"].isoformat(),
                ExportColumn.LATITUDE: f"{r['latitude']:.{LOCATION_GRID_DECIMALS}f}",
                ExportColumn.LONGITUDE: f"{r['longitude']:.{LOCATION_GRID_DECIMALS}f}",
                ExportColumn.MODEL_VERSION: r["version_modelo"],
                ExportColumn.SITE: r["nombre"],
            })
        return out.getvalue(), len(rows) > limit
