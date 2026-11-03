"""Recordings linked to the caller's detections and the quota they add up to.

Every query filters by owner explicitly: shared detections are readable by any signed-in user (collective map),
but a recording is private to its author.
"""
from typing import Any
from uuid import UUID

import psycopg

from .domain import MAX_RECORDING_BYTES, recording_object_path
from .errors import ErrorCode, raise_error
from .repository import DetectionRepository


class RecordingRepository(DetectionRepository):
    def usage(self, owner: UUID) -> tuple[int, int]:
        """How many recordings the caller keeps and the bytes they take."""
        with self.transaction(owner) as connection:
            row = connection.execute(
                "SELECT count(*) AS n, coalesce(sum(bytes_grabacion), 0) AS bytes FROM public.detections WHERE user_id = %s AND ruta_grabacion IS NOT NULL",
                (owner,)).fetchone()
        return row["n"], row["bytes"]

    def path(self, owner: UUID, detection: UUID) -> str | None:
        """Object path of the detection's recording; None when the detection is not the caller's or has none."""
        with self.transaction(owner) as connection:
            row = connection.execute("SELECT ruta_grabacion FROM public.detections WHERE id = %s AND user_id = %s", (detection, owner)).fetchone()
        return row["ruta_grabacion"] if row else None

    def reserve(self, owner: UUID, detection: UUID, size: int, quota: int) -> bool:
        """Records that a clip of `size` bytes for `detection` is about to be uploaded, if it fits; the one it would replace does not count."""
        with self.transaction(owner) as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(owner),))
            if self._taken(connection, owner, detection) + size > quota:
                return False
            connection.execute(
                "INSERT INTO public.recording_uploads (detection_id, user_id) VALUES (%s, %s) ON CONFLICT (detection_id) DO UPDATE SET signed_at = now()",
                (detection, owner))
        return True

    def release(self, owner: UUID, detection: UUID) -> None:
        """Forgets a reservation whose upload could not be signed."""
        with self.transaction(owner) as connection:
            connection.execute("DELETE FROM public.recording_uploads WHERE detection_id = %s AND user_id = %s", (detection, owner))

    @staticmethod
    def _taken(connection: psycopg.Connection[dict[str, Any]], owner: UUID, detection: UUID) -> int:
        # A clip signed but never linked may still sit in Storage: it counts as the largest one the bucket accepts.
        row = connection.execute(
            """SELECT
                 (SELECT coalesce(sum(bytes_grabacion), 0) FROM public.detections WHERE user_id = %(owner)s AND ruta_grabacion IS NOT NULL AND id <> %(detection)s)
                 + (SELECT count(*) FROM public.recording_uploads u WHERE u.user_id = %(owner)s AND u.detection_id <> %(detection)s
                      AND NOT EXISTS (SELECT 1 FROM public.detections d WHERE d.id = u.detection_id AND d.ruta_grabacion IS NOT NULL)) * %(pending)s
               AS bytes""",
            {"owner": owner, "detection": detection, "pending": MAX_RECORDING_BYTES}).fetchone()
        return int(row["bytes"])

    def link(self, owner: UUID, detection: UUID, path: str, size: int, quota: int) -> None:
        """Records the verified clip on its detection, unless the account's quota would be exceeded."""
        with self.transaction(owner) as connection:
            # Serializes the caller's concurrent uploads, so two of them cannot both fit in the last free bytes.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(owner),))
            if self._taken(connection, owner, detection) + size > quota:
                raise_error(ErrorCode.RECORDING_QUOTA_EXCEEDED)
            updated = connection.execute("UPDATE public.detections SET ruta_grabacion = %s, bytes_grabacion = %s WHERE id = %s AND user_id = %s",
                                         (path, size, detection, owner)).rowcount
            if updated == 0:
                raise_error(ErrorCode.DETECTION_NOT_FOUND)
            connection.execute("DELETE FROM public.recording_uploads WHERE detection_id = %s AND user_id = %s", (detection, owner))

    def unlink(self, owner: UUID, detection: UUID) -> None:
        with self.transaction(owner) as connection:
            connection.execute("UPDATE public.detections SET ruta_grabacion = NULL, bytes_grabacion = NULL WHERE id = %s AND user_id = %s", (detection, owner))
            connection.execute("DELETE FROM public.recording_uploads WHERE detection_id = %s AND user_id = %s", (detection, owner))

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
            # Reservations always go: their objects were deleted too, whether or not they had been uploaded.
            connection.execute("DELETE FROM public.recording_uploads WHERE user_id = %s", (owner,))
            return connection.execute(
                "UPDATE public.detections SET ruta_grabacion = NULL, bytes_grabacion = NULL WHERE user_id = %s AND ruta_grabacion = ANY(%s)",
                (owner, paths)).rowcount
