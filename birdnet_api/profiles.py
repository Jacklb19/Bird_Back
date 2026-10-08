"""The caller's profile row; RLS on public.profiles limits every query to it."""
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from psycopg import sql

from .repository import DetectionRepository

RETURNED_COLUMNS: Final = "alias, avatar_path, created_at"


@dataclass(frozen=True, slots=True)
class StoredProfile:
    alias: str | None
    avatar_path: str | None
    created_at: datetime | None


# Accounts created before the sign-up trigger existed may have no row yet; they read as an empty profile.
EMPTY_PROFILE: Final = StoredProfile(alias=None, avatar_path=None, created_at=None)


class ProfileRepository(DetectionRepository):
    def get(self, owner: UUID) -> StoredProfile:
        with self.transaction(owner) as connection:
            row = connection.execute(f"SELECT {RETURNED_COLUMNS} FROM public.profiles WHERE id = %s", (owner,)).fetchone()
        return StoredProfile(**row) if row else EMPTY_PROFILE

    def update(self, owner: UUID, changes: dict[str, str | None]) -> StoredProfile:
        """Write only the given columns, creating the row when missing; keys are validated contract field names."""
        if not changes:
            return self.get(owner)
        columns = [sql.Identifier(name) for name in changes]
        query = sql.SQL(
            "INSERT INTO public.profiles (id, {columns}) VALUES (%(id)s, {values}) "
            "ON CONFLICT (id) DO UPDATE SET {updates} RETURNING " + RETURNED_COLUMNS
        ).format(
            columns=sql.SQL(", ").join(columns),
            values=sql.SQL(", ").join(sql.Placeholder(name) for name in changes),
            updates=sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(column) for column in columns),
        )
        with self.transaction(owner) as connection:
            row = connection.execute(query, {**changes, "id": owner}).fetchone()
        return StoredProfile(**row) if row else EMPTY_PROFILE
