"""Recordings linked to the caller's detections and the quota they add up to.

Every query filters by owner explicitly: shared detections are readable by any signed-in user (collective map),
but a recording is private to its author.
"""
from uuid import UUID

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

    def has_room(self, owner: UUID, detection: UUID, size: int, quota: int) -> bool:
        """Whether a clip of `size` bytes for `detection` fits; the one it would replace does not count."""
        with self.transaction(owner) as connection:
            row = connection.execute(
                "SELECT coalesce(sum(bytes_grabacion), 0) AS bytes FROM public.detections WHERE user_id = %s AND ruta_grabacion IS NOT NULL AND id <> %s",
                (owner, detection)).fetchone()
        return row["bytes"] + size <= quota

    def link(self, owner: UUID, detection: UUID, path: str, size: int, quota: int) -> None:
        """Records the verified clip on its detection, unless the account's quota would be exceeded."""
        with self.transaction(owner) as connection:
            # Serializes the caller's concurrent uploads, so two of them cannot both fit in the last free bytes.
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(owner),))
            others = connection.execute(
                "SELECT coalesce(sum(bytes_grabacion), 0) AS bytes FROM public.detections WHERE user_id = %s AND ruta_grabacion IS NOT NULL AND id <> %s",
                (owner, detection)).fetchone()
            if others["bytes"] + size > quota:
                raise_error(ErrorCode.RECORDING_QUOTA_EXCEEDED)
            updated = connection.execute("UPDATE public.detections SET ruta_grabacion = %s, bytes_grabacion = %s WHERE id = %s AND user_id = %s",
                                         (path, size, detection, owner)).rowcount
            if updated == 0:
                raise_error(ErrorCode.DETECTION_NOT_FOUND)

    def unlink(self, owner: UUID, detection: UUID) -> None:
        with self.transaction(owner) as connection:
            connection.execute("UPDATE public.detections SET ruta_grabacion = NULL, bytes_grabacion = NULL WHERE id = %s AND user_id = %s", (detection, owner))

    def paths(self, owner: UUID) -> list[str]:
        with self.transaction(owner) as connection:
            rows = connection.execute("SELECT ruta_grabacion FROM public.detections WHERE user_id = %s AND ruta_grabacion IS NOT NULL", (owner,)).fetchall()
        return [row["ruta_grabacion"] for row in rows]

    def unlink_all(self, owner: UUID, paths: list[str]) -> int:
        """Forgets the given clips, already deleted from Storage; one uploaded meanwhile keeps its link."""
        with self.transaction(owner) as connection:
            return connection.execute(
                "UPDATE public.detections SET ruta_grabacion = NULL, bytes_grabacion = NULL WHERE user_id = %s AND ruta_grabacion = ANY(%s)",
                (owner, paths)).rowcount
