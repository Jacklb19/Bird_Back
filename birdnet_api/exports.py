"""CSV export of the caller's detections (RF-16): one site, one walk, or the whole record.

Discarded detections are exported with their state, so the file shows what a person ruled out; every query filters by
owner explicitly, because shared detections are readable by other signed-in users.
"""
import csv
import io
from datetime import datetime
from typing import Final
from uuid import UUID

from .domain import CONFIDENCE_EXPORT_DECIMALS, DB_TO_API_REVIEWER, DB_TO_API_STATUS, LOCATION_GRID_DECIMALS, ExportColumn, spreadsheet_text
from .errors import ErrorCode, raise_error
from .repository import DetectionRepository
from .settings import get_settings

EXPORT_QUERY: Final = """SELECT d.id, d.especie, d.confianza, d.estado, d.momento, ST_Y(d.ubicacion::geometry) AS latitude,
    ST_X(d.ubicacion::geometry) AS longitude, d.version_modelo, s.nombre, d.especie_modelo, d.revisada_por, n.nota, n.individuos
FROM public.detections d LEFT JOIN public.sites s ON s.id = d.site_id
LEFT JOIN public.detection_notes n ON n.detection_id = d.id
WHERE d.user_id = %(owner)s
  AND (%(site)s::uuid IS NULL OR d.site_id = %(site)s)
  AND (%(started)s::timestamptz IS NULL OR d.momento >= %(started)s)
  AND (%(ended)s::timestamptz IS NULL OR d.momento <= %(ended)s)
  AND (%(since)s::timestamptz IS NULL OR d.momento >= %(since)s)
  AND (%(until)s::timestamptz IS NULL OR d.momento < %(until)s)
ORDER BY d.momento LIMIT %(fetch)s"""


class ExportRepository(DetectionRepository):
    def export_csv(self, owner: UUID, site_id: UUID | None, walk_id: UUID | None, since: datetime | None, until: datetime | None) -> tuple[str, bool]:
        """The CSV text and whether it stopped at the configured row limit."""
        limit = get_settings().export_row_limit
        with self.transaction(owner) as connection:
            # Sites and walks are private: RLS hides foreign rows, so "not found" also covers "not yours".
            if site_id and connection.execute("SELECT 1 FROM public.sites WHERE id = %s", (site_id,)).fetchone() is None:
                raise_error(ErrorCode.SITE_NOT_FOUND)
            started = ended = None
            if walk_id:
                walk = connection.execute("SELECT inicio, fin FROM public.walks WHERE id = %s AND user_id = %s", (walk_id, owner)).fetchone()
                if walk is None:
                    raise_error(ErrorCode.WALK_NOT_FOUND)
                started, ended = walk["inicio"], walk["fin"]
            rows = connection.execute(EXPORT_QUERY, {
                "owner": owner, "site": site_id, "started": started, "ended": ended, "since": since, "until": until,
                # One extra row tells whether the export was cut at the limit.
                "fetch": limit + 1,
            }).fetchall()
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
                ExportColumn.SITE: spreadsheet_text(r["nombre"] or ""),
                ExportColumn.MODEL_SPECIES: r["especie_modelo"] or "",
                ExportColumn.REVIEWED_BY: DB_TO_API_REVIEWER[r["revisada_por"]] if r["revisada_por"] else "",
                ExportColumn.INDIVIDUALS: r["individuos"] if r["individuos"] is not None else "",
                ExportColumn.NOTE: spreadsheet_text(r["nota"] or ""),
            })
        return out.getvalue(), len(rows) > limit
