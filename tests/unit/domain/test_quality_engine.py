"""Quality engine v1: every gate, flags that never fail, determinism, monotonicity, no cleaning.

Invalid candles cannot be built through the constructor, so ``_forge`` bypasses validation
to feed the engine the corrupt rows it must still catch (defence in depth: a row could reach
the engine from a source other than the validated constructor).
"""

from __future__ import annotations

import copy
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sentinel.domain import calendar
from sentinel.domain.canonical import canonical_json
from sentinel.domain.market_data import OHLC, Candle, Timeframe
from sentinel.domain.quality import (
    FAIL,
    FLAGS,
    INTEGRITY_GATES,
    PASS,
    QualityChecker,
    QualityConfig,
    QualityConfigError,
    QualityResult,
)
from sentinel.domain.types import DomainError
from sentinel.risk import sessions

from support.market import candle, minutes
from support.quality import lenient_config, quality_config

MON = datetime(2015, 1, 5, tzinfo=UTC)  # Sunday 19:00 New York: market open
SAT = datetime(2015, 1, 10, 12, tzinfo=UTC)
AS_OF = datetime(2020, 1, 1, tzinfo=UTC)
CFG = quality_config()


def _bars(n: int, start: datetime = MON) -> list[Candle]:
    return [candle("EUR_USD", t) for t in minutes(start, n)]


def _judge(
    bars: list[Candle],
    *,
    config: QualityConfig = CFG,
    start: datetime = MON,
    end: datetime | None = None,
    as_of: datetime = AS_OF,
    symbol: str = "EUR_USD",
    timeframe: Timeframe = Timeframe.M1,
) -> QualityResult:
    checker = QualityChecker(
        config,
        symbol=symbol,
        timeframe=timeframe,
        start=start,
        end=end or start + timedelta(minutes=len(bars)),
        as_of=as_of,
    )
    for c in bars:
        checker.add(c)
    return checker.finish()


def _forge(c: Candle, **fields: object) -> Candle:
    """A copy of ``c`` with fields set without validation (as corrupt data might arrive)."""
    forged = copy.copy(c)
    for name, value in fields.items():
        object.__setattr__(forged, name, value)
    return forged


def _ohlc(o: str, h: str, l: str, c: str) -> OHLC:  # noqa: E741
    forged = object.__new__(OHLC)
    for name, value in zip("ohlc", (o, h, l, c), strict=True):
        object.__setattr__(forged, name, Decimal(value))
    return forged


def _gates(result: QualityResult) -> set[str]:
    return {f.gate for f in result.failures}


# ----------------------------------------------------------------------------- clean data


def test_a_clean_series_passes_with_complete_metrics() -> None:
    result = _judge(_bars(60))
    assert result.verdict == PASS
    assert result.passed
    assert result.failures == ()
    m = result.metrics
    assert (m["rows"], m["expected_bars"], m["present_bars"], m["missing_bars"]) == (60, 60, 60, 0)
    assert (m["unexpected_bars"], m["gaps"], m["longest_gap_bars"]) == (0, 0, 0)
    assert m["completeness_pct"] == "100"
    assert m["max_spread"] == "0.0001"
    assert all(v == 0 for v in result.counts.values())
    assert result.examples == {}
    assert result.largest_gaps == ()


# ----------------------------------------------------------------------------- integrity gates


def _corrupt(gate: str, c: Candle) -> Candle:  # noqa: PLR0911 - one corruption per gate
    if gate == "non_positive_price":
        return _forge(c, bid=_ohlc("0", "1.1002", "0", "1.1001"))
    if gate == "ohlc_inconsistent":
        return _forge(c, bid=_ohlc("1.1000", "1.0990", "1.0998", "1.1001"))  # high below open
    if gate == "crossed_quote":
        return _forge(c, ask=_ohlc("1.0999", "1.1003", "1.0990", "1.1002"))
    if gate == "crossed_extremes":
        return _forge(c, ask=_ohlc("1.1001", "1.1001", "1.0999", "1.1001"))  # ask high < bid high
    if gate == "negative_volume":
        return _forge(c, tick_volume=-1)
    if gate == "spread_above_cap":
        wide = Decimal("0.0200")
        return replace(c, ask=OHLC(c.ask.o + wide, c.ask.h + wide, c.ask.l + wide, c.ask.c + wide))
    if gate == "foreign_row":
        return _forge(c, symbol="USD_JPY")
    if gate == "off_grid":
        return replace(c, ts=c.ts + timedelta(seconds=30))
    if gate == "out_of_range":
        return replace(c, ts=c.ts + timedelta(hours=1))
    raise AssertionError(gate)


@pytest.mark.parametrize(
    "gate", [g for g in INTEGRITY_GATES if g not in ("not_increasing", "future_bar")]
)
@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_each_integrity_violation_fails(gate: str) -> None:
    bars = _bars(30)
    bars[10] = _corrupt(gate, bars[10])
    if gate == "out_of_range":
        bars = [*bars[:10], *bars[11:], bars[10]]  # keep the order increasing
    result = _judge(bars, config=lenient_config(), end=MON + timedelta(minutes=30))
    assert result.verdict == FAIL
    assert gate in _gates(result)
    assert result.counts[gate] == 1
    assert result.examples[gate][0].ts == bars[-1 if gate == "out_of_range" else 10].ts


def test_rows_out_of_order_or_duplicated_fail() -> None:
    bars = _bars(10)
    result = _judge([*bars[:5], bars[3], *bars[5:]], config=lenient_config())
    assert _gates(result) == {"not_increasing"}
    assert result.counts["not_increasing"] == 1
    result = _judge([*bars[:5], bars[4], *bars[5:]], config=lenient_config())
    assert _gates(result) == {"not_increasing"}


def test_bars_not_closed_at_evaluation_time_fail() -> None:
    bars = _bars(10)
    as_of = bars[-1].ts + timedelta(seconds=59)  # the last bar closes one second later
    result = _judge(bars, as_of=as_of)
    assert _gates(result) == {"future_bar"}
    assert result.counts["future_bar"] == 1
    assert _judge(bars, as_of=bars[-1].ts + timedelta(minutes=1)).passed


# ----------------------------------------------------------------------------- series gates


def test_completeness_below_minimum_fails_exactly_at_the_threshold() -> None:
    bars = _bars(200)
    cfg = quality_config(min_completeness_pct="99.0", max_gap_bars=10)
    assert _judge(bars[:198], config=cfg, end=MON + timedelta(minutes=200)).passed  # 99.0%
    result = _judge(bars[:197], config=cfg, end=MON + timedelta(minutes=200))  # 98.5%
    assert _gates(result) == {"completeness_below_min"}
    [failure] = result.failures
    assert (failure.observed, failure.limit) == ("98.5%", "99%")


def test_a_gap_longer_than_the_maximum_fails_even_with_high_completeness() -> None:
    bars = _bars(4000)  # Monday to Wednesday: no weekend inside
    end = MON + timedelta(minutes=4000)
    holed = bars[:2000] + bars[2016:]  # one 16-minute hole: 99.6% complete
    result = _judge(holed, end=end)
    assert _gates(result) == {"gap_above_max"}
    assert result.metrics["longest_gap_bars"] == 16
    assert result.largest_gaps[0].start == bars[2000].ts
    assert result.largest_gaps[0].missing_bars == 16
    assert _judge(bars[:2000] + bars[2015:], end=end).passed


def test_bars_where_the_market_is_assumed_closed_fail() -> None:
    bars = [*_bars(30), candle("EUR_USD", SAT)]
    result = _judge(
        bars, config=lenient_config(max_unexpected_bars=0), end=SAT + timedelta(minutes=1)
    )
    assert "unexpected_above_max" in _gates(result)
    assert result.counts["unexpected_bar"] == 1
    assert result.examples["unexpected_bar"][0].ts == SAT
    assert _judge(bars, config=lenient_config(), end=SAT + timedelta(minutes=1)).passed


def test_holiday_closures_are_unexpected_bars_too() -> None:
    xmas = datetime(2015, 12, 25, 15, tzinfo=UTC)
    result = _judge([candle("EUR_USD", xmas)], start=xmas, end=xmas + timedelta(minutes=1))
    assert "no_expected_bars" in _gates(result)
    assert result.counts["unexpected_bar"] == 1


def test_a_range_with_no_expected_bars_cannot_pass() -> None:
    result = _judge([], start=SAT, end=SAT + timedelta(hours=2))
    assert _gates(result) == {"no_expected_bars"}


def test_missing_bars_at_the_edges_of_the_range_are_gaps() -> None:
    bars = _bars(20)
    result = _judge(bars[3:17], config=lenient_config(), end=MON + timedelta(minutes=20))
    assert result.metrics["gaps"] == 2
    assert result.metrics["missing_bars"] == 6
    assert sorted(g.missing_bars for g in result.largest_gaps) == [3, 3]


# ----------------------------------------------------------------------------- flags


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_extreme_moves_are_flagged_kept_and_never_fail_by_themselves() -> None:
    """A flash crash the provider really reported must survive: flagged, not removed."""
    bars = _bars(30)
    crash = Decimal("0.80")  # -27% in one bar, then recovery
    bars[10] = candle("EUR_USD", bars[10].ts, mid=str(crash))
    bars[11] = candle("EUR_USD", bars[11].ts, mid=str(crash))
    wide = bars[20]
    bars[20] = replace(
        wide,
        bid=replace(wide.bid, h=wide.bid.h + Decimal("0.0200")),
        ask=replace(wide.ask, h=wide.ask.h + Decimal("0.0200")),
    )
    result = _judge(bars)
    assert result.verdict == PASS
    assert result.counts["price_jump"] == 2  # down, then back up
    assert result.counts["bar_range"] == 1
    assert result.metrics["rows"] == 30
    # listed largest first: the recovery (+37.5%) before the crash (-27%)
    assert [e.ts for e in result.examples["price_jump"]] == [bars[12].ts, bars[10].ts]
    assert Decimal(result.metrics["largest_jump_pct"]) > Decimal(27)  # type: ignore[arg-type]


def test_zero_volume_is_reported_not_failed() -> None:
    bars = _bars(10)
    bars[4] = replace(bars[4], tick_volume=0)
    result = _judge(bars)
    assert result.passed
    assert result.counts["zero_volume"] == 1


def test_examples_are_bounded_but_counts_are_complete() -> None:
    bars = [replace(c, tick_volume=0) for c in _bars(50)]
    result = _judge(bars, config=quality_config(max_listed=3))
    assert result.counts["zero_volume"] == 50
    assert [e.ts for e in result.examples["zero_volume"]] == [c.ts for c in bars[:3]]


def test_largest_jumps_are_listed_largest_first() -> None:
    bars = _bars(40)
    for i, mid in ((5, "1.1060"), (15, "1.1200"), (25, "1.1100"), (35, "1.1500")):
        bars[i] = candle("EUR_USD", bars[i].ts, mid=mid)
    result = _judge(bars, config=quality_config(max_listed=2))
    listed = result.examples["price_jump"]
    assert len(listed) == 2
    assert [e.ts for e in listed] == [bars[35].ts, bars[36].ts]


# ----------------------------------------------------------------------------- determinism


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_result_is_deterministic_and_independent_of_decimal_scale() -> None:
    bars = _bars(100)
    bars[50] = replace(bars[50], tick_volume=0)
    del bars[60:70]
    a = _judge(bars, config=lenient_config(), end=MON + timedelta(minutes=100))
    b = _judge(list(bars), config=lenient_config(), end=MON + timedelta(minutes=100))
    assert canonical_json(a.canonical()) == canonical_json(b.canonical())
    q = Decimal("0.0000001")
    rescaled = [
        replace(
            c,
            bid=OHLC(*(p.quantize(q) for p in (c.bid.o, c.bid.h, c.bid.l, c.bid.c))),
            ask=OHLC(*(p.quantize(q) for p in (c.ask.o, c.ask.h, c.ask.l, c.ask.c))),
        )
        for c in bars
    ]
    r = _judge(rescaled, config=lenient_config(), end=MON + timedelta(minutes=100))
    assert canonical_json(r.canonical()) == canonical_json(a.canonical())


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_the_engine_never_modifies_or_drops_its_input() -> None:
    bars = _bars(30)
    bars[5] = _corrupt("crossed_quote", bars[5])
    bars[7] = candle("EUR_USD", bars[7].ts, mid="0.5")
    snapshot = [copy.copy(c) for c in bars]
    result = _judge(bars, config=lenient_config())
    assert bars == snapshot
    assert result.metrics["rows"] == len(bars)


# ----------------------------------------------------------------------------- monotonicity

_N = 240


@st.composite
def _degradation(draw: st.DrawFn) -> tuple[frozenset[int], ...]:
    """A base (removed bars, wide spreads) and a worse series: more defects, and more missing
    *clean* bars. Removing a defective bar is not "worse": it is cleaning, which is exactly
    what can turn a FAIL into a PASS and why the engine never drops a row."""
    removed = draw(st.frozensets(st.integers(0, _N - 1), max_size=20))
    wide = draw(st.frozensets(st.integers(0, _N - 1), max_size=3)) - removed
    more_wide = wide | (draw(st.frozensets(st.integers(0, _N - 1), max_size=3)) - removed)
    extra = draw(st.frozensets(st.integers(0, _N - 1), max_size=20)) - more_wide
    return removed, wide, removed | extra, more_wide


def _series(removed: frozenset[int], wide: frozenset[int]) -> list[Candle]:
    return [
        _corrupt("spread_above_cap", c) if i in wide else c
        for i, c in enumerate(_bars(_N))
        if i not in removed
    ]


_STRICT = quality_config(min_completeness_pct="96", max_gap_bars=4)
_END = MON + timedelta(minutes=_N)


@given(_degradation())
@settings(max_examples=150, deadline=None)
@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_worse_data_never_turns_fail_into_pass(case: tuple[frozenset[int], ...]) -> None:
    removed, wide, more_removed, more_wide = case
    base = _judge(_series(removed, wide), config=_STRICT, end=_END)
    worse = _judge(_series(more_removed, more_wide), config=_STRICT, end=_END)
    if base.verdict == FAIL:
        assert worse.verdict == FAIL
    assert worse.metrics["missing_bars"] == len(more_removed)  # every missing bar is counted


@given(
    st.frozensets(st.integers(0, _N - 1), max_size=25),
    st.decimals("90", "100", places=1),
    st.integers(0, 10),
    st.decimals("90", "100", places=1),
    st.integers(0, 10),
)
@settings(max_examples=150, deadline=None)
@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_tighter_thresholds_never_turn_fail_into_pass(
    removed: frozenset[int], pct: Decimal, gap: int, tighter_pct: Decimal, tighter_gap: int
) -> None:
    bars = _series(removed, frozenset())
    loose = quality_config(min_completeness_pct=str(pct), max_gap_bars=gap)
    tight = quality_config(
        min_completeness_pct=str(max(pct, tighter_pct)), max_gap_bars=min(gap, tighter_gap)
    )
    if _judge(bars, config=loose, end=_END).verdict == FAIL:
        assert _judge(bars, config=tight, end=_END).verdict == FAIL


# ----------------------------------------------------------------------------- fail closed


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_unknown_timeframe_or_symbol_is_refused_before_judging() -> None:
    with pytest.raises(QualityConfigError, match="timeframe H4"):
        _judge(_bars(5), timeframe=Timeframe.H4)
    with pytest.raises(QualityConfigError, match="symbol GBP_USD"):
        _judge(_bars(5), symbol="GBP_USD")
    with pytest.raises(QualityConfigError, match="timeframe M5"):
        _judge(_bars(5), config=quality_config(timeframes=("M1",)), timeframe=Timeframe.M5)


def test_a_checker_cannot_be_reused() -> None:
    checker = QualityChecker(
        CFG,
        symbol="EUR_USD",
        timeframe=Timeframe.M1,
        start=MON,
        end=MON + timedelta(1),
        as_of=AS_OF,
    )
    checker.finish()
    with pytest.raises(DomainError):
        checker.add(_bars(1)[0])
    with pytest.raises(DomainError):
        checker.finish()


def test_naive_times_are_refused() -> None:
    with pytest.raises(DomainError):
        _judge(_bars(5), as_of=datetime(2020, 1, 1))  # noqa: DTZ001 - naive on purpose


def test_every_gate_and_flag_is_counted_and_reported() -> None:
    result = _judge(_bars(5))
    assert set(result.counts) == {*INTEGRITY_GATES, *FLAGS, "unexpected_bar"}


def test_the_risk_kernel_and_the_quality_gates_share_one_calendar() -> None:
    assert sessions.market_closed is calendar.market_closed


# ----------------------------------------------------------------------------- benchmark


def test_quality_engine_throughput(capsys: pytest.CaptureFixture[str]) -> None:
    n = 100_000
    bars = _bars(n)
    started = time.perf_counter()
    result = _judge(bars, end=MON + timedelta(minutes=n))
    elapsed = time.perf_counter() - started
    assert result.metrics["rows"] == n
    with capsys.disabled():
        import sys

        sys.stdout.write(
            f"\n[bench] quality engine v1: {n:,} M1 candles in {elapsed:.2f}s "
            f"({n / elapsed:,.0f} candles/s)\n"
        )
    assert elapsed < 120
