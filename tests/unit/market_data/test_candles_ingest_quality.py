"""Candle validation, the ingestion pipeline (in memory), and gap/completeness analysis."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.domain.market_data import OHLC, Timeframe
from sentinel.domain.types import DomainError
from sentinel.perception.market_data.ingest import ingest
from sentinel.perception.market_data.quality import (
    analyse_fixed_grid,
    analyse_weekly,
    expected_bars,
    is_holiday_closure,
)

from support.market import ListProvider, MemorySink, candle, minutes

MON = datetime(2015, 1, 5, 0, 0, tzinfo=UTC)
CLOCK = lambda: datetime(2026, 10, 4, tzinfo=UTC)  # noqa: E731


# ----------------------------------------------------------------------------- candles


def test_candle_validation_and_spread() -> None:
    c = candle("EUR_USD", MON)
    assert c.spread.open == Decimal("0.0001")
    assert c.spread.close == Decimal("0.0001")
    with pytest.raises(DomainError):
        OHLC(Decimal(1), Decimal("0.9"), Decimal("0.8"), Decimal("0.95"))  # high < open
    with pytest.raises(DomainError):
        replace(c, symbol="EURUSD")
    with pytest.raises(DomainError):
        replace(c, tick_volume=-1)
    with pytest.raises(DomainError, match="crossed"):
        replace(c, ask=replace(c.ask, o=Decimal("1.0990"), l=Decimal("1.0980")))
    with pytest.raises(DomainError):
        replace(c, ts=datetime(2015, 1, 5))  # noqa: DTZ001
    assert Timeframe.H4.duration == timedelta(hours=4)
    assert Timeframe.M1.fixed_utc_grid
    assert not Timeframe.D1.fixed_utc_grid


# ----------------------------------------------------------------------------- calendar


def test_expected_bars_skip_weekend_and_holiday_closures() -> None:
    # Friday 2015-01-09 16:55 New York (21:55 UTC, EST) to Sunday 17:05 New York.
    start = datetime(2015, 1, 9, 21, 55, tzinfo=UTC)
    end = datetime(2015, 1, 11, 22, 5, tzinfo=UTC)
    bars = list(expected_bars(Timeframe.M1, start, end))
    assert bars[:5] == minutes(start, 5)  # 16:55..16:59 Friday
    assert bars[5] == datetime(2015, 1, 11, 22, 0, tzinfo=UTC)  # Sunday 17:00 reopening
    assert len(bars) == 10
    assert is_holiday_closure(datetime(2015, 12, 25, 15, tzinfo=UTC))
    assert not is_holiday_closure(datetime(2015, 12, 26, 15, tzinfo=UTC))
    with pytest.raises(ValueError, match="fixed UTC grid"):
        next(expected_bars(Timeframe.D1, start, end))


def test_grid_alignment_starts_on_the_next_bar() -> None:
    start = MON + timedelta(minutes=7)
    assert next(expected_bars(Timeframe.M5, start, start + timedelta(hours=1))) == MON + timedelta(
        minutes=10
    )


# ----------------------------------------------------------------------------- gaps


@given(st.sets(st.integers(0, 299), max_size=60))
def test_gap_analysis_accounts_for_every_missing_bar(removed: set[int]) -> None:
    grid = minutes(MON, 300)
    present = [t for i, t in enumerate(grid) if i not in removed]
    q = analyse_fixed_grid(present, Timeframe.M1, MON, MON + timedelta(minutes=300))
    assert q.expected == 300
    assert q.present == 300 - len(removed)
    assert q.missing == len(removed)
    assert sum(g.missing_bars for g in q.gaps) == len(removed)
    assert q.unexpected == 0
    missing_from_gaps = {
        int((g.start - MON).total_seconds() // 60) + k
        for g in q.gaps
        for k in range(g.missing_bars)
    }
    assert missing_from_gaps == removed


def test_short_and_material_gaps_and_unexpected_bars() -> None:
    grid = minutes(MON, 120)
    present = [t for i, t in enumerate(grid) if not (10 <= i < 12 or 50 <= i < 80)]
    saturday_bar = datetime(2015, 1, 10, 12, 0, tzinfo=UTC)
    q = analyse_fixed_grid(
        [*present, saturday_bar],
        Timeframe.M1,
        MON,
        datetime(2015, 1, 11, tzinfo=UTC),
        short_gap_bars=5,
    )
    assert q.short_gaps == 1
    material = q.material_gaps
    assert material[0].missing_bars == 30
    assert material[0].start == MON + timedelta(minutes=50)
    assert q.unexpected == 1
    assert q.first == MON
    assert Decimal(0) < q.completeness_pct < Decimal(100)
    empty = analyse_fixed_grid([], Timeframe.M1, MON, MON)
    assert empty.completeness_pct == 0


def test_weekly_completeness_for_ny_aligned_timeframes() -> None:
    # Four weeks of D1 bars opening 17:00 New York (22:00 UTC in winter), Sun-Thu.
    bars = []
    for week in range(4):
        sunday = datetime(2015, 1, 4, 22, tzinfo=UTC) + timedelta(weeks=week)
        days = 4 if week == 2 else 5  # one short week
        bars += [sunday + timedelta(days=d) for d in range(days)]
    q = analyse_weekly(bars, Timeframe.D1)
    assert q.weeks == 2  # partial first and last weeks are not judged
    assert q.complete_weeks == 1
    assert [count for _, count in q.short_weeks] == [4]
    assert q.completeness_pct == Decimal("90.000")
    with pytest.raises(ValueError, match="H4/D1"):
        analyse_weekly(bars, Timeframe.M1)
    assert analyse_weekly([], Timeframe.H4).completeness_pct == 0


# ----------------------------------------------------------------------------- ingestion


def test_ingest_resumes_skips_duplicates_and_records_runs() -> None:
    times = minutes(MON, 10)
    batches = [[candle("EUR_USD", t) for t in times[:5]], [candle("EUR_USD", t) for t in times[4:]]]
    sink = MemorySink()
    end = MON + timedelta(minutes=10)
    first = ingest(
        ListProvider("fake", batches),
        sink,
        symbol="EUR_USD",
        timeframe=Timeframe.M1,
        start=MON,
        end=end,
        clock=CLOCK,
    )
    assert first.status == "SUCCEEDED"
    assert first.candles_inserted == 10
    assert first.candles_received == 11  # the overlapping bar is received twice, stored once
    again = ingest(
        ListProvider("fake", batches),
        sink,
        symbol="EUR_USD",
        timeframe=Timeframe.M1,
        start=MON,
        end=end,
        clock=CLOCK,
    )
    assert again.candles_inserted == 0
    assert again.candles_received == 1  # resumed from the latest stored bar
    assert len(sink.rows) == 10
    assert [r.status for r in sink.runs] == ["SUCCEEDED", "SUCCEEDED"]


def test_ingest_failure_keeps_progress_and_is_recorded() -> None:
    times = minutes(MON, 6)
    batches = [[candle("EUR_USD", t) for t in times[:3]], [candle("EUR_USD", t) for t in times[3:]]]
    sink = MemorySink()
    run = ingest(
        ListProvider("fake", batches, fail_after=1),
        sink,
        symbol="EUR_USD",
        timeframe=Timeframe.M1,
        start=MON,
        end=MON + timedelta(minutes=6),
        clock=CLOCK,
    )
    assert run.status == "FAILED"
    assert run.error is not None
    assert "connection reset" in run.error
    assert run.candles_inserted == 3
    assert len(sink.rows) == 3
    with pytest.raises(ValueError, match="after start"):
        ingest(
            ListProvider("fake", []),
            sink,
            symbol="EUR_USD",
            timeframe=Timeframe.M1,
            start=MON,
            end=MON,
            clock=CLOCK,
        )


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_ingest_refuses_a_series_registered_to_another_source_without_fetching() -> None:
    sink = MemorySink()
    sink.insert([candle("EUR_USD", MON)], "oanda-practice")
    provider = ListProvider("dukascopy", [[candle("EUR_USD", t) for t in minutes(MON, 5)]])
    run = ingest(
        provider,
        sink,
        symbol="EUR_USD",
        timeframe=Timeframe.M1,
        start=MON,
        end=MON + timedelta(hours=1),
        clock=CLOCK,
    )
    assert run.status == "FAILED"
    assert run.error is not None
    assert "registered to source oanda-practice" in run.error
    assert provider.calls == 0
    assert len(sink.rows) == 1
    assert sink.runs == [run]


def test_ingest_into_an_unregistered_or_same_source_series_proceeds() -> None:
    sink = MemorySink()
    provider = ListProvider("fake", [[candle("EUR_USD", t) for t in minutes(MON, 3)]])
    for _ in range(2):
        run = ingest(
            provider,
            sink,
            symbol="EUR_USD",
            timeframe=Timeframe.M1,
            start=MON,
            end=MON + timedelta(hours=1),
            clock=CLOCK,
        )
        assert run.status == "SUCCEEDED"
    assert sink.registered_source("EUR_USD", Timeframe.M1) == "fake"
