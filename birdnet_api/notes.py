"""Notes and count of individuals that a person writes about their own detections (ADR-40).

Private to their author: they never reach the collective map, other people or the language model; only the CSV of
their own record carries them.
"""
from uuid import UUID

from .contracts import DetectionNote, NoteInput
from .repository import DetectionRepository


class NoteRepository(DetectionRepository):
    def save(self, owner: UUID, detection: UUID, note: NoteInput) -> DetectionNote | None:
        """Stores the whole note, or deletes it when both fields are empty; None when the detection is not theirs."""
        with self.transaction(owner) as connection:
            if connection.execute("SELECT 1 FROM public.detections WHERE id = %s AND user_id = %s", (detection, owner)).fetchone() is None:
                return None
            if note.note is None and note.individuals is None:
                connection.execute("DELETE FROM public.detection_notes WHERE detection_id = %s", (detection,))
            else:
                connection.execute(
                    """INSERT INTO public.detection_notes (detection_id, user_id, nota, individuos) VALUES (%(id)s, %(owner)s, %(note)s, %(individuals)s)
                    ON CONFLICT (detection_id) DO UPDATE SET nota = excluded.nota, individuos = excluded.individuos""",
                    {"id": detection, "owner": owner, "note": note.note, "individuals": note.individuals})
        return DetectionNote(id=detection, note=note.note, individuals=note.individuals)
