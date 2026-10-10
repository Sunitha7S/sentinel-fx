"""Spot FX market calendar (``fx-ny1700-xmas-newyear/v1``), pure and DST-aware.

* The market is closed from Friday 17:00 to Sunday 17:00 New York time.
* Bars whose New York date is 25 December or 1 January are holiday closures.
* For fixed-grid timeframes (M1, M5, H1) a bar is *expected* when its open time falls in
  neither. ``expected_bars`` enumerates them on the UTC grid anchored at 2000-01-03.

New York wall-clock time comes from the IANA time-zone database (``zoneinfo``); its rules
for the dates covered here (2007 onwards) are settled. The risk kernel's session rules and
the data-quality gates both use this module, so they cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from sentinel.domain.market_data import Timeframe

__all__ = [
    "CALENDAR_ID",
    "NEW_YORK",
    "WEEKLY_EDGE",
    "expected_bars",
    "is_expected",
    "is_holiday_closure",
    "market_closed",
    "on_grid",
]

CALENDAR_ID: Final = "fx-ny1700-xmas-newyear/v1"
NEW_YORK: Final = ZoneInfo("America/New_York")
WEEKLY_EDGE: Final = time(17, 0)
"""Spot FX closes on Friday and reopens on Sunday at 17:00 New York time."""

_FRIDAY, _SATURDAY, _SUNDAY = 4, 5, 6
_HOLIDAYS: Final = ((12, 25), (1, 1))
_GRID_EPOCH: Final = datetime(2000, 1, 3, tzinfo=UTC)


def _weekend(ny: datetime) -> bool:
    t = ny.time().replace(tzinfo=None)
    return (
        ny.weekday() == _SATURDAY
        or (ny.weekday() == _FRIDAY and t >= WEEKLY_EDGE)
        or (ny.weekday() == _SUNDAY and t < WEEKLY_EDGE)
    )


def market_closed(at: datetime) -> bool:
    return _weekend(at.astimezone(NEW_YORK))


def is_holiday_closure(ts: datetime) -> bool:
    ny = ts.astimezone(NEW_YORK)
    return (ny.month, ny.day) in _HOLIDAYS


def is_expected(ts: datetime) -> bool:
    """Whether the market is assumed open at ``ts`` (so a bar opening then should exist)."""
    ny = ts.astimezone(NEW_YORK)
    return not _weekend(ny) and (ny.month, ny.day) not in _HOLIDAYS


def on_grid(timeframe: Timeframe, ts: datetime) -> bool:
    return (ts - _GRID_EPOCH) % timeframe.duration == timedelta(0)


def expected_bars(timeframe: Timeframe, start: datetime, end: datetime) -> Iterator[datetime]:
    """Open times of every bar the market should produce in [start, end)."""
    if not timeframe.fixed_utc_grid:
        raise ValueError(f"{timeframe} is not on a fixed UTC grid; use analyse_weekly")
    step = timeframe.duration
    t = start + (-(start - _GRID_EPOCH)) % step  # first grid point >= start
    while t < end:
        if is_expected(t):
            yield t
        t += step
