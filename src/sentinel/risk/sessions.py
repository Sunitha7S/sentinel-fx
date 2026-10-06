"""Deterministic trading-calendar calculations (exchange-local, DST-aware via zoneinfo)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sentinel.domain.calendar import NEW_YORK, WEEKLY_EDGE, market_closed

__all__ = [
    "after_friday_cutoff",
    "crosses_weekly_close",
    "in_local_window",
    "market_closed",
    "next_weekly_close",
]

_FRIDAY = 4


def in_local_window(at: datetime, start: time, end: time, tz: str) -> bool:
    """Whether ``at`` falls in [start, end) in the given zone's wall-clock time."""
    local = at.astimezone(ZoneInfo(tz)).time().replace(tzinfo=None)
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def next_weekly_close(at: datetime) -> datetime:
    ny = at.astimezone(NEW_YORK)
    days_ahead = (_FRIDAY - ny.weekday()) % 7
    close_date = ny.date() + timedelta(days=days_ahead)
    close = datetime.combine(close_date, WEEKLY_EDGE, tzinfo=NEW_YORK)
    if close <= ny:
        close = datetime.combine(close_date + timedelta(days=7), WEEKLY_EDGE, tzinfo=NEW_YORK)
    return close.astimezone(UTC)


def crosses_weekly_close(start: datetime, end: datetime) -> bool:
    return market_closed(start) or end >= next_weekly_close(start)


def after_friday_cutoff(at: datetime, cutoff_utc: time) -> bool:
    utc = at.astimezone(UTC)
    return utc.weekday() == _FRIDAY and utc.time().replace(tzinfo=None) >= cutoff_utc
