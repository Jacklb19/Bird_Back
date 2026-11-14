"""Recordings linked to the caller's detections and the quota they add up to.

Every query filters by owner explicitly: shared detections are readable by any signed-in user (collective map),
but a recording is private to its author.
"""
from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg

from .domain import MAX_RECORDING_BYTES, recording_object_path
from .errors import ErrorCode, raise_error
from .repository import DetectionRepository, require_row

# Supabase's signed upload credentials remain valid for two hours, including after an object is deleted.
UPLOAD_LIFETIME_SECONDS = 7200


class RecordingRepository(DetectionRepository):
    def usage(self, owner: UUID) -> tuple[int, int]:
        """How many recordings the caller keeps and the bytes they take."""
        with self.transaction(owner) as connection:
            row = connection.execute(
                "SELECT count(*) AS n, coalesce(sum(bytes_grabacion), 0) AS bytes FROM public.detections WHERE user_id = %s AND ruta_grabacion IS NOT NULL",
                (owner,)).fetchone()
        found = require_row(row)
        return found["n"], found["bytes"]

    def path(self, owner: UUID, detection: UUID) -> str | None:
        """Object path of the detection's recording; None when the detection is not the caller's or has none."""
        with self.transaction(owner) as connection:
            row = connection.execute("SELECT ruta_grabacion FROM public.detections WHERE id = %s AND user_id = %s", (detection, owner)).fetchone()
        return row["ruta_grabacion"] if row else None

    def reserve(self, owner: UUID, detection: UUID, size: int, quota: int) -> bool:
        """Reserve the bucket's maximum size: the signed token cannot enforce the client's declared size."""
        with self.transaction(owner) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(owner),))
            if self._taken(connection, owner, detection) + MAX_RECORDING_BYTES > quota:
                return False
            connection.execute(
                "INSERT INTO public.recording_uploads (detection_id, user_id) VALUES (%s, %s) ON CONFLICT (detection_id) DO UPDATE SET signed_at = now()",
                (detection, owner))
        return True

    def clean_expired(self, owner: UUID, remove: Callable[[list[str]], None]) -> None:
        """Delete abandoned objects before releasing quota, serialized with new authorizations and links."""
        with self.transaction(owner) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(owner),))
            rows = connection.execute(
                """SELECT u.detection_id FROM public.recording_uploads u JOIN public.detections d ON d.id = u.detection_id
                WHERE u.user_id = %s AND d.ruta_grabacion IS NULL AND u.signed_at < now() - %s * interval '1 second'""",
                (owner, UPLOAD_LIFETIME_SECONDS)).fetchall()
            identifiers = [r["detection_id"] for r in rows]
            remove([recording_object_path(owner, identifier) for identifier in identifiers])
            connection.execute("DELETE FROM public.recording_uploads WHERE user_id = %s AND detection_id = ANY(%s)", (owner, identifiers))

    @staticmethod
    def _taken(connection: psycopg.Connection[dict[str, Any]], owner: UUID, detection: UUID) -> int:
        # A live token can upload the bucket maximum, also after deletion. Abandoned objects count until cleaned.
        row = connection.execute(
            """SELECT coalesce(sum(CASE WHEN u.detection_id IS NOT NULL AND
                    (d.ruta_grabacion IS NULL OR u.signed_at >= now() - %(ttl)s * interval '1 second')
                THEN %(pending)s ELSE coalesce(d.bytes_grabacion, 0) END), 0) AS bytes
            FROM public.detections d LEFT JOIN public.recording_uploads u ON u.detection_id = d.id AND u.user_id = d.user_id
            WHERE d.user_id = %(owner)s AND d.id <> %(detection)s""",
            {"owner": owner, "detection": detection, "pending": MAX_RECORDING_BYTES, "ttl": UPLOAD_LIFETIME_SECONDS}).fetchone()
        return int(require_row(row)["bytes"])

    def link(self, owner: UUID, detection: UUID, path: str, size: int, quota: int) -> None:
        """Records the verified clip on its detection, unless the account's quota would be exceeded."""
        with self.transaction(owner) as connection:
            # Serializes the caller's concurrent uploads, so two of them cannot both fit in the last free bytes.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(owner),))
            pending = connection.execute(
                "SELECT 1 FROM public.recording_uploads WHERE user_id = %s AND detection_id = %s AND signed_at >= now() - %s * interval '1 second'",
                (owner, detection, UPLOAD_LIFETIME_SECONDS)).fetchone()
            if self._taken(connection, owner, detection) + (MAX_RECORDING_BYTES if pending else size) > quota:
                raise_error(ErrorCode.RECORDING_QUOTA_EXCEEDED)
            updated = connection.execute("UPDATE public.detections SET ruta_grabacion = %s, bytes_grabacion = %s WHERE id = %s AND user_id = %s",
                                         (path, size, detection, owner)).rowcount
            if updated == 0:
                raise_error(ErrorCode.DETECTION_NOT_FOUND)

    def unlink(self, owner: UUID, detection: UUID) -> None:
        with self.transaction(owner) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(owner),))
            connection.execute("UPDATE public.detections SET ruta_grabacion = NULL, bytes_grabacion = NULL WHERE id = %s AND user_id = %s", (detection, owner))

    def paths(self, owner: UUID) -> list[str]:
        with self.transaction(owner) as connection:
            rows = connection.execute("SELECT ruta_grabacion FROM public.detections WHERE user_id = %s AND ruta_grabacion IS NOT NULL", (owner,)).fetchall()
        return [row["ruta_grabacion"] for row in rows]

    def pending_paths(self, owner: UUID) -> list[str]:
        """Objects that may have been uploaded without being linked: they are deleted with the recordings."""
        with self.transaction(owner) as connection:
            rows = connection.execute("SELECT detection_id FROM public.recording_uploads WHERE user_id = %s", (owner,)).fetchall()
        return [recording_object_path(owner, row["detection_id"]) for row in rows]

    def unlink_all(self, owner: UUID, paths: list[str]) -> int:
        """Forgets the given clips, already deleted from Storage; one uploaded meanwhile keeps its link."""
        with self.transaction(owner) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(owner),))
            # Keep reservations until their credentials expire; an old URL can recreate a deleted object.
            return connection.execute(
                "UPDATE public.detections SET ruta_grabacion = NULL, bytes_grabacion = NULL WHERE user_id = %s AND ruta_grabacion = ANY(%s)",
                (owner, paths)).rowcount
