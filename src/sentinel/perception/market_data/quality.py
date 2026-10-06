"""Data-quality analysis: completeness and gaps of a stored candle series.

For fixed-grid timeframes (M1, M5, H1) every bar the market should have produced is
enumerated and compared with the stored bars in one streaming pass (constant memory):

* **expected** bars open while spot FX trades: from Sunday 17:00 to Friday 17:00 New York,
  excluding the Christmas and New Year closures (bars whose New York date is 25 December or
  1 January). Weekend and holiday absences are therefore not gaps.
* a **gap** is a run of consecutive expected bars that are missing. Short gaps (a few
  minutes in quiet hours) are normal for M1, because providers emit no bar for a minute
  without ticks; they are reported separately from material gaps.
* **unexpected** bars exist where the market is assumed closed; they are counted, since
  they reveal a wrong calendar assumption rather than a data problem.

H4 and D1 bars are aligned to 17:00 New York and shift in UTC with daylight saving time,
so their completeness is measured per trading week instead (30 H4 or 5 D1 bars).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sentinel.domain.calendar import NEW_YORK, expected_bars, is_holiday_closure
from sentinel.domain.market_data import Timeframe
from sentinel.domain.types import require_utc

__all__ = [
    "Gap",
    "SeriesQuality",
    "WeeklyQuality",
    "analyse_fixed_grid",
    "analyse_weekly",
    "expected_bars",
    "is_holiday_closure",
]

_PER_WEEK = {Timeframe.H4: 30, Timeframe.D1: 5}


@dataclass(frozen=True, slots=True)
class Gap:
    start: datetime
    """Open time of the first missing bar."""
    end: datetime
    """Open time of the bar after the last missing one."""
    missing_bars: int


@dataclass
class SeriesQuality:
    timeframe: Timeframe
    start: datetime
    end: datetime
    short_gap_bars: int
    expected: int = 0
    present: int = 0
    unexpected: int = 0
    gaps: list[Gap] = field(default_factory=list)
    first: datetime | None = None
    last: datetime | None = None

    @property
    def missing(self) -> int:
        return self.expected - self.present

    @property
    def completeness_pct(self) -> Decimal:
        if self.expected == 0:
            return Decimal(0)
        return (Decimal(self.present) / Decimal(self.expected) * 100).quantize(Decimal("0.001"))

    @property
    def material_gaps(self) -> list[Gap]:
        return [g for g in self.gaps if g.missing_bars > self.short_gap_bars]

    @property
    def short_gaps(self) -> int:
        return len(self.gaps) - len(self.material_gaps)


def analyse_fixed_grid(
    present: Iterable[datetime],
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    short_gap_bars: int = 5,
) -> SeriesQuality:
    """Merge the expected grid with stored bar times (ascending) in one pass."""
    start = require_utc(start, field="start")
    end = require_utc(end, field="end")
    q = SeriesQuality(timeframe, start, end, short_gap_bars)
    stored = iter(present)
    current = next(stored, None)
    gap_start: datetime | None = None
    gap_len = 0

    def close_gap(at: datetime) -> None:
        nonlocal gap_start, gap_len
        if gap_start is not None:
            q.gaps.append(Gap(gap_start, at, gap_len))
            gap_start, gap_len = None, 0

    for t in expected_bars(timeframe, start, end):
        q.expected += 1
        while current is not None and current < t:
            q.unexpected += 1  # stored bar the calendar did not expect
            current = next(stored, None)
        if current == t:
            q.present += 1
            q.first = q.first or t
            q.last = t
            close_gap(t)
            current = next(stored, None)
        else:
            if gap_start is None:
                gap_start = t
            gap_len += 1
    close_gap(end)
    while current is not None:
        if current < end:
            q.unexpected += 1
        current = next(stored, None)
    return q


@dataclass
class WeeklyQuality:
    timeframe: Timeframe
    expected_per_week: int
    weeks: int = 0
    complete_weeks: int = 0
    short_weeks: list[tuple[date, int]] = field(default_factory=list)
    """(week start date in New York, bar count) for weeks below the expected count."""
    present: int = 0

    @property
    def completeness_pct(self) -> Decimal:
        if self.weeks == 0:
            return Decimal(0)
        expected = self.weeks * self.expected_per_week
        return (Decimal(min(self.present, expected)) / Decimal(expected) * 100).quantize(
            Decimal("0.001")
        )


def analyse_weekly(present: Iterable[datetime], timeframe: Timeframe) -> WeeklyQuality:
    """Count bars per New York trading week (Sunday 17:00 to Friday 17:00)."""
    if timeframe not in _PER_WEEK:
        raise ValueError(f"weekly analysis applies to H4/D1, not {timeframe}")
    counts: Counter[date] = Counter()
    for ts in present:
        ny = ts.astimezone(NEW_YORK)
        # A bar opening Sunday 17:00 belongs to the week starting that Sunday.
        week = (ny + timedelta(hours=7)).date()
        week -= timedelta(days=(week.weekday() + 1) % 7)
        counts[week] += 1
    q = WeeklyQuality(timeframe, _PER_WEEK[timeframe])
    weeks = sorted(counts)
    # Partial first/last weeks of the requested range are not judged.
    for week in weeks[1:-1]:
        q.weeks += 1
        q.present += counts[week]
        if counts[week] >= q.expected_per_week:
            q.complete_weeks += 1
        else:
            q.short_weeks.append((week, counts[week]))
    return q
