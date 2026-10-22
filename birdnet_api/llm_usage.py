"""Daily caps on the texts the language model writes (ADR-37): per person and in total.

The free tier's limits belong to the whole project, so one person must not be able to spend them for everyone.
Counting happens before the model is asked and is given back when it does not answer.
"""
from datetime import datetime
from typing import Final
from uuid import UUID
from zoneinfo import ZoneInfo

from .errors import ErrorCode, raise_error
from .repository import DetectionRepository

# Google resets the free tier's daily quota at midnight Pacific time; the caps follow the same day.
QUOTA_TIME_ZONE: Final = ZoneInfo("America/Los_Angeles")
# Serializes counting so two requests cannot both take the last text of the day (transaction-scoped lock).
USAGE_LOCK_KEY: Final = 2026_10_21


def quota_day(current: datetime) -> datetime:
    return current.astimezone(QUOTA_TIME_ZONE)


class UsageRepository(DetectionRepository):
    def spend(self, owner: UUID, current: datetime, per_person: int, total: int) -> None:
        """Takes one text of the day for `owner`; raises LLM_QUOTA_EXCEEDED when either cap is reached."""
        day = quota_day(current).date()
        with self.trusted_transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (USAGE_LOCK_KEY,))
            used = connection.execute("SELECT coalesce(sum(textos), 0) AS n FROM public.llm_usage WHERE dia = %s", (day,)).fetchone()["n"]
            taken = None
            if used < total:
                taken = connection.execute(
                    """INSERT INTO public.llm_usage (user_id, dia, textos) VALUES (%(owner)s, %(day)s, 1)
                    ON CONFLICT (user_id, dia) DO UPDATE SET textos = public.llm_usage.textos + 1
                    WHERE public.llm_usage.textos < %(cap)s RETURNING textos""",
                    {"owner": owner, "day": day, "cap": per_person}).fetchone()
        if taken is None:
            raise_error(ErrorCode.LLM_QUOTA_EXCEEDED)

    def give_back(self, owner: UUID, current: datetime) -> None:
        """Returns the text taken for a request the model or a public source did not answer."""
        with self.trusted_transaction() as connection:
            connection.execute("UPDATE public.llm_usage SET textos = textos - 1 WHERE user_id = %s AND dia = %s AND textos > 0",
                               (owner, quota_day(current).date()))
