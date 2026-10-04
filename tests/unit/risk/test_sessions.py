"""Session calendar: DST-aware rollover window, weekend close, Friday cutoff."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from sentinel.risk.sessions import (
    after_friday_cutoff,
    crosses_weekly_close,
    in_local_window,
    market_closed,
    next_weekly_close,
)

NY = "America/New_York"


@pytest.mark.parametrize(
    ("utc", "inside"),
    [
        # Summer (EDT, UTC-4): 16:45-17:30 NY == 20:45-21:30 UTC
        (datetime(2026, 7, 7, 20, 44, tzinfo=UTC), False),
        (datetime(2026, 7, 7, 20, 45, tzinfo=UTC), True),
        (datetime(2026, 7, 7, 21, 29, tzinfo=UTC), True),
        (datetime(2026, 7, 7, 21, 30, tzinfo=UTC), False),
        # Winter (EST, UTC-5): the same window is an hour later in UTC
        (datetime(2026, 1, 6, 20, 50, tzinfo=UTC), False),
        (datetime(2026, 1, 6, 21, 50, tzinfo=UTC), True),
        # US/EU mismatch week (US on DST, EU not yet): still follows New York
        (datetime(2026, 3, 10, 20, 50, tzinfo=UTC), True),
    ],
)
def test_rollover_window_follows_new_york_dst(utc: datetime, inside: bool) -> None:
    assert in_local_window(utc, time(16, 45), time(17, 30), NY) is inside


def test_window_crossing_midnight() -> None:
    at = datetime(2026, 7, 7, 4, 30, tzinfo=UTC)  # 00:30 NY
    assert in_local_window(at, time(23, 0), time(1, 0), NY)
    assert not in_local_window(at, time(1, 0), time(2, 0), NY)


@pytest.mark.parametrize(
    ("utc", "closed"),
    [
        (datetime(2026, 10, 9, 20, 59, tzinfo=UTC), False),  # Fri 16:59 NY
        (datetime(2026, 10, 9, 21, 0, tzinfo=UTC), True),  # Fri 17:00 NY
        (datetime(2026, 10, 10, 12, 0, tzinfo=UTC), True),  # Saturday
        (datetime(2026, 10, 11, 20, 59, tzinfo=UTC), True),  # Sun 16:59 NY
        (datetime(2026, 10, 11, 21, 0, tzinfo=UTC), False),  # Sun 17:00 NY
        (datetime(2026, 1, 9, 21, 30, tzinfo=UTC), False),  # Fri 16:30 EST
        (datetime(2026, 1, 9, 22, 0, tzinfo=UTC), True),  # Fri 17:00 EST
    ],
)
def test_weekend_close(utc: datetime, closed: bool) -> None:
    assert market_closed(utc) is closed


def test_next_weekly_close_and_crossing() -> None:
    tue = datetime(2026, 10, 6, 13, 0, tzinfo=UTC)
    close = next_weekly_close(tue)
    assert close == datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
    after = next_weekly_close(datetime(2026, 10, 9, 21, 30, tzinfo=UTC))
    assert after == datetime(2026, 10, 16, 21, 0, tzinfo=UTC)
    assert not crosses_weekly_close(tue, tue + timedelta(hours=4))
    assert crosses_weekly_close(tue, tue + timedelta(days=4))
    assert crosses_weekly_close(
        datetime(2026, 10, 10, 1, 0, tzinfo=UTC), datetime(2026, 10, 10, 2, 0, tzinfo=UTC)
    )


def test_friday_cutoff_is_in_utc() -> None:
    assert after_friday_cutoff(datetime(2026, 10, 9, 18, 0, tzinfo=UTC), time(18, 0))
    assert not after_friday_cutoff(datetime(2026, 10, 9, 17, 59, tzinfo=UTC), time(18, 0))
    assert not after_friday_cutoff(datetime(2026, 10, 8, 19, 0, tzinfo=UTC), time(18, 0))
