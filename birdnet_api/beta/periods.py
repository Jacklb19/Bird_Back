"""Resolve public calendar words locally; the provider never needs the person's time zone."""
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..domain import StatsPeriod
from .text_contracts import InterpretPeriod


def next_month(day: date) -> date:
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def period_bounds(period: InterpretPeriod, current: datetime, zone_name: str) -> tuple[datetime | None, datetime | None]:
    if period.kind == "all":
        return None, None
    utc = current.astimezone(UTC)
    zone = ZoneInfo(zone_name)
    today = utc.astimezone(zone).date()
    if period.kind == "dates":
        first, after = period.first, period.last + timedelta(days=1)
    elif period.value in ("last_7_days", "last_30_days"):
        days = StatsPeriod.WEEK.days if period.value == "last_7_days" else StatsPeriod.MONTH.days
        if days is None:
            raise ValueError("Rolling period requires a finite interval")
        return utc - timedelta(days=days), utc
    elif period.value in ("today", "yesterday"):
        first = today - timedelta(days=period.value == "yesterday")
        after = first + timedelta(days=1)
    elif period.value in ("this_week", "last_week"):
        first = today - timedelta(days=today.weekday())
        if period.value == "last_week":
            first -= timedelta(days=7)
        after = first + timedelta(days=7)
    elif period.value in ("this_month", "last_month"):
        first = today.replace(day=1)
        if period.value == "last_month":
            first = (first - timedelta(days=1)).replace(day=1)
        after = next_month(first)
    else:
        year = today.year - (period.value == "last_year")
        first, after = date(year, 1, 1), date(year + 1, 1, 1)
    return datetime.combine(first, time.min, zone).astimezone(UTC), datetime.combine(after, time.min, zone).astimezone(UTC)
