"""Data-quality configuration and the gates that judge a candle series (engine v1).

A ``QualityConfig`` is a strict, immutable set of thresholds. Its identity is the SHA-256
of its canonical form: version, status, engine, calendar and every threshold, with decimals
normalised. Comments, key order, line endings and decimal scale in the source file do not
change it; any semantic change does.

``engine`` and ``calendar`` name the exact gate semantics and market calendar the
thresholds are judged with. This code implements one engine and one calendar; a config
naming any other is refused, so a snapshot judged under v1 can never be silently re-judged
under different rules.

``QualityChecker`` is the engine: it consumes candles in order, in constant memory, and
produces a ``QualityResult`` with a PASS or FAIL verdict, complete counts, metrics and a
bounded list of examples. It **detects; it never cleans**: no row is dropped, altered,
re-timed, interpolated or smoothed, and a real extreme move is at most flagged.

FAIL gates (objective integrity conditions and the configured thresholds):

* per bar: rows not strictly increasing, outside [from, to), off the timeframe grid, not
  yet closed at ``as_of`` (future), for another symbol or timeframe, a non-positive price,
  inconsistent OHLC, ask below bid at open/close (crossed quote) or at high/low (crossed
  extremes: the highest and lowest ask cannot be below the highest and lowest bid), negative
  volume, spread at open or close above the symbol's cap;
* per series: no expected bars at all, completeness below the minimum, a gap longer than
  the maximum, more bars than allowed where the calendar says the market is closed.

Flags (never FAIL): a jump between consecutive bars or a single-bar range above the
timeframe's percentage, and bars with zero volume.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from types import MappingProxyType
from typing import Any, Final

from sentinel.domain.calendar import CALENDAR_ID, expected_bars, on_grid
from sentinel.domain.canonical import JsonValue, canonical, sha256_hex
from sentinel.domain.market_data import Candle, Timeframe
from sentinel.domain.types import DomainError, require_utc, to_decimal

__all__ = [
    "APPROVED",
    "FAIL",
    "FLAGS",
    "INTEGRITY_GATES",
    "PASS",
    "PROVISIONAL",
    "QUALITY_CALENDAR",
    "QUALITY_ENGINE",
    "SERIES_GATES",
    "Example",
    "GapRecord",
    "GateFailure",
    "QualityChecker",
    "QualityConfig",
    "QualityConfigError",
    "QualityResult",
    "SymbolThresholds",
    "TimeframeThresholds",
]

QUALITY_ENGINE: Final = "sentinel-quality/v1"
QUALITY_CALENDAR: Final = CALENDAR_ID
PROVISIONAL: Final = "PROVISIONAL_UNCALIBRATED"
APPROVED: Final = "APPROVED"
PASS: Final = "PASS"  # noqa: S105 - a quality verdict, not a credential
FAIL: Final = "FAIL"

INTEGRITY_GATES: Final = (
    "not_increasing",
    "out_of_range",
    "off_grid",
    "future_bar",
    "foreign_row",
    "non_positive_price",
    "ohlc_inconsistent",
    "crossed_quote",
    "crossed_extremes",
    "negative_volume",
    "spread_above_cap",
)
"""Per-bar conditions; any occurrence is a FAIL."""
SERIES_GATES: Final = (
    "no_expected_bars",
    "completeness_below_min",
    "gap_above_max",
    "unexpected_above_max",
)
FLAGS: Final = ("price_jump", "bar_range", "zero_volume")
"""Reported for review; never a FAIL."""

_VERSION = re.compile(r"^qc_v[1-9][0-9]{0,3}$")
_SYMBOL = re.compile(r"^[A-Z]{3}_[A-Z]{3}$")
_MAX_LISTED = 1000
_CONFIG_KEYS = ("version", "status", "engine", "calendar", "report", "timeframes", "symbols")
_TIMEFRAME_KEYS = (
    "min_completeness_pct",
    "max_gap_bars",
    "max_unexpected_bars",
    "jump_flag_pct",
    "range_flag_pct",
)
_SYMBOL_KEYS = ("max_spread",)


class QualityConfigError(DomainError):
    """A quality configuration is malformed, unknown or unavailable. Always fatal."""


def _fields(where: str, raw: object, keys: tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise QualityConfigError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in raw if k not in keys)
    if unknown:
        raise QualityConfigError(f"{where}: unknown key(s) {', '.join(unknown)}")
    missing = [k for k in keys if k not in raw]
    if missing:
        raise QualityConfigError(f"{where}: missing key(s) {', '.join(missing)}")
    return raw


def _count(where: str, value: object, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int:
        raise QualityConfigError(f"{where} must be an integer, got {type(value).__name__}")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
        raise QualityConfigError(f"{where} must be {bound}, got {value}")
    return value


def _decimal(where: str, value: object) -> Decimal:
    if isinstance(value, float):
        raise QualityConfigError(f"{where}: float is not allowed; write a decimal literal")
    try:
        result = to_decimal(value, field=where)
    except (TypeError, DomainError) as exc:
        message = str(exc)
        if "finite" not in message:
            message = f"{where} must be a decimal number ({exc})"
        raise QualityConfigError(message) from exc
    return result


def _positive(where: str, value: object) -> Decimal:
    result = _decimal(where, value)
    if result <= 0:
        raise QualityConfigError(f"{where} must be positive, got {result}")
    return result


def _text(where: str, value: object) -> str:
    if not isinstance(value, str):
        raise QualityConfigError(f"{where} must be a string")
    return value


@dataclass(frozen=True, slots=True)
class TimeframeThresholds:
    min_completeness_pct: Decimal
    max_gap_bars: int
    max_unexpected_bars: int
    jump_flag_pct: Decimal
    range_flag_pct: Decimal

    @classmethod
    def parse(cls, where: str, raw: object) -> TimeframeThresholds:
        m = _fields(where, raw, _TIMEFRAME_KEYS)
        completeness = _decimal(f"{where}.min_completeness_pct", m["min_completeness_pct"])
        if not Decimal(0) < completeness <= Decimal(100):
            raise QualityConfigError(
                f"{where}.min_completeness_pct: completeness must be in (0, 100], "
                f"got {completeness}"
            )
        return cls(
            min_completeness_pct=completeness,
            max_gap_bars=_count(f"{where}.max_gap_bars", m["max_gap_bars"]),
            max_unexpected_bars=_count(f"{where}.max_unexpected_bars", m["max_unexpected_bars"]),
            jump_flag_pct=_positive(f"{where}.jump_flag_pct", m["jump_flag_pct"]),
            range_flag_pct=_positive(f"{where}.range_flag_pct", m["range_flag_pct"]),
        )


@dataclass(frozen=True, slots=True)
class SymbolThresholds:
    max_spread: Decimal

    @classmethod
    def parse(cls, where: str, raw: object) -> SymbolThresholds:
        m = _fields(where, raw, _SYMBOL_KEYS)
        return cls(max_spread=_positive(f"{where}.max_spread", m["max_spread"]))


@dataclass(frozen=True, slots=True)
class QualityConfig:
    version: str
    status: str
    engine: str
    calendar: str
    max_listed: int
    timeframes: Mapping[Timeframe, TimeframeThresholds]
    symbols: Mapping[str, SymbolThresholds]
    sha256: str

    @classmethod
    def from_mapping(cls, raw: object) -> QualityConfig:
        m = _fields("quality config", raw, _CONFIG_KEYS)
        version = _text("version", m["version"])
        if not _VERSION.match(version):
            raise QualityConfigError(f"version must look like qc_v1, got {version!r}")
        status = _text("status", m["status"])
        if status not in (PROVISIONAL, APPROVED):
            raise QualityConfigError(f"status must be {PROVISIONAL} or {APPROVED}, got {status!r}")
        engine = _text("engine", m["engine"])
        if engine != QUALITY_ENGINE:
            raise QualityConfigError(
                f"engine {engine!r} is not implemented (only {QUALITY_ENGINE})"
            )
        calendar = _text("calendar", m["calendar"])
        if calendar != QUALITY_CALENDAR:
            raise QualityConfigError(
                f"calendar {calendar!r} is not implemented (only {QUALITY_CALENDAR})"
            )
        report = _fields("report", m["report"], ("max_listed",))
        max_listed = _count(
            "report.max_listed", report["max_listed"], minimum=1, maximum=_MAX_LISTED
        )

        raw_tfs = m["timeframes"]
        if not isinstance(raw_tfs, Mapping) or not raw_tfs:
            raise QualityConfigError("timeframes must list at least one timeframe")
        timeframes: dict[Timeframe, TimeframeThresholds] = {}
        for key, value in raw_tfs.items():
            try:
                tf = Timeframe(key)
            except ValueError:
                raise QualityConfigError(f"unknown timeframe {key!r}") from None
            if not tf.fixed_utc_grid:
                raise QualityConfigError(
                    f"timeframe {tf}: engine {QUALITY_ENGINE} judges only fixed-grid timeframes "
                    "(M1, M5, H1)"
                )
            timeframes[tf] = TimeframeThresholds.parse(f"timeframes.{tf}", value)

        raw_symbols = m["symbols"]
        if not isinstance(raw_symbols, Mapping) or not raw_symbols:
            raise QualityConfigError("symbols must list at least one symbol")
        symbols: dict[str, SymbolThresholds] = {}
        for key, value in raw_symbols.items():
            if not isinstance(key, str) or not _SYMBOL.match(key):
                raise QualityConfigError(f"symbol must look like EUR_USD, got {key!r}")
            symbols[key] = SymbolThresholds.parse(f"symbols.{key}", value)

        identity = {
            "version": version,
            "status": status,
            "engine": engine,
            "calendar": calendar,
            "report": {"max_listed": max_listed},
            "timeframes": {
                tf.value: {k: getattr(t, k) for k in _TIMEFRAME_KEYS}
                for tf, t in timeframes.items()
            },
            "symbols": {s: {"max_spread": t.max_spread} for s, t in symbols.items()},
        }
        return cls(
            version=version,
            status=status,
            engine=engine,
            calendar=calendar,
            max_listed=max_listed,
            timeframes=MappingProxyType(timeframes),
            symbols=MappingProxyType(symbols),
            sha256=sha256_hex(identity),
        )


# ============================================================================= engine v1


def _ts(ts: datetime) -> str:
    return f"{ts.astimezone(UTC):%Y-%m-%dT%H:%M:%S.%f}Z"


_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


def _micros(ts: datetime) -> int:
    return (ts - _EPOCH) // _MICROSECOND


@dataclass(frozen=True, slots=True)
class Example:
    ts: datetime
    detail: str

    def canonical(self) -> dict[str, JsonValue]:
        return {"ts": _ts(self.ts), "detail": self.detail}


@dataclass(frozen=True, slots=True)
class GapRecord:
    start: datetime
    """Open time of the first missing bar."""
    missing_bars: int

    def canonical(self) -> dict[str, JsonValue]:
        return {"start": _ts(self.start), "missing_bars": self.missing_bars}


@dataclass(frozen=True, slots=True)
class GateFailure:
    gate: str
    observed: str
    limit: str

    def canonical(self) -> dict[str, JsonValue]:
        return {"gate": self.gate, "observed": self.observed, "limit": self.limit}


@dataclass(frozen=True, slots=True)
class QualityResult:
    """The engine's judgement of one series range. Pure data; deterministic."""

    verdict: str
    failures: tuple[GateFailure, ...]
    counts: Mapping[str, int]
    """Complete counts for every per-bar gate, every flag and ``unexpected_bar``."""
    metrics: Mapping[str, JsonValue]
    examples: Mapping[str, tuple[Example, ...]]
    """At most ``max_listed`` per category: the first ones, or the largest for jumps/ranges."""
    largest_gaps: tuple[GapRecord, ...]

    @property
    def passed(self) -> bool:
        return self.verdict == PASS

    def canonical(self) -> dict[str, JsonValue]:
        return {
            "verdict": self.verdict,
            "failures": [f.canonical() for f in self.failures],
            "counts": dict(self.counts),
            "metrics": dict(self.metrics),
            "examples": {k: [e.canonical() for e in v] for k, v in self.examples.items()},
            "largest_gaps": [g.canonical() for g in self.largest_gaps],
        }


class _Largest:
    """The ``n`` largest values with their example; ties keep the earliest. Constant memory."""

    def __init__(self, n: int) -> None:
        self._n = n
        self._heap: list[tuple[Decimal, int, Example]] = []

    def offer(self, value: Decimal, ts: datetime, detail: str) -> None:
        item = (value, -_micros(ts), Example(ts, detail))
        if len(self._heap) < self._n:
            heapq.heappush(self._heap, item)
        elif item[:2] > self._heap[0][:2]:
            heapq.heapreplace(self._heap, item)

    def items(self) -> tuple[Example, ...]:
        return tuple(e for *_, e in sorted(self._heap, key=lambda i: i[:2], reverse=True))


_HUNDRED = Decimal(100)
_TWO = Decimal(2)
_PCT = Decimal("0.0001")


def _d(value: Decimal) -> str:
    """Canonical decimal text: the same for 1.10 and 1.1000, never an exponent."""
    text = canonical(value)
    if not isinstance(text, str):  # pragma: no cover - canonical() maps Decimal to str
        raise DomainError(f"cannot serialise {value!r}")
    return text


def _pct(value: Decimal) -> str:
    return _d(value.quantize(_PCT, rounding=ROUND_FLOOR))


class QualityChecker:
    """Judge one series range in a single ordered pass. Never modifies what it is given."""

    def __init__(
        self,
        config: QualityConfig,
        *,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        as_of: datetime,
    ) -> None:
        tf = config.timeframes.get(timeframe)
        if tf is None:
            raise QualityConfigError(
                f"{config.version} has no thresholds for timeframe {timeframe}"
            )
        sym = config.symbols.get(symbol)
        if sym is None:
            raise QualityConfigError(f"{config.version} has no thresholds for symbol {symbol}")
        self._tf = tf
        self._max_spread = sym.max_spread
        self._symbol = symbol
        self._timeframe = timeframe
        self._start = require_utc(start, field="start")
        self._end = require_utc(end, field="end")
        self._as_of = require_utc(as_of, field="as_of")
        self._n = config.max_listed
        self._duration = timeframe.duration

        self._counts: dict[str, int] = dict.fromkeys(
            (*INTEGRITY_GATES, *FLAGS, "unexpected_bar"), 0
        )
        self._first: dict[str, list[Example]] = {
            k: [] for k in (*INTEGRITY_GATES, "zero_volume", "unexpected_bar")
        }
        self._jumps = _Largest(self._n)
        self._ranges = _Largest(self._n)
        self._gaps = _Largest(self._n)
        self._rows = 0
        self._last: datetime | None = None
        self._prev_mid_close: Decimal | None = None
        self._max_spread_seen: Decimal | None = None
        self._largest_jump = Decimal(0)
        self._largest_range = Decimal(0)

        self._expected = expected_bars(timeframe, self._start, self._end)
        self._next: datetime | None = next(self._expected, None)
        self._expected_count = 0
        self._present = 0
        self._unexpected = 0
        self._gap_start: datetime | None = None
        self._gap_len = 0
        self._gap_count = 0
        self._longest_gap = 0
        self._done = False

    # ------------------------------------------------------------------ recording

    def _hit(self, gate: str, ts: datetime, detail: str) -> None:
        self._counts[gate] += 1
        first = self._first[gate]
        if len(first) < self._n:
            first.append(Example(ts, detail))

    def _miss_next(self) -> None:
        if self._gap_start is None:
            self._gap_start = self._next
        self._gap_len += 1
        self._advance_expected()

    def _close_gap(self) -> None:
        if self._gap_start is not None:
            self._gap_count += 1
            self._longest_gap = max(self._longest_gap, self._gap_len)
            self._gaps.offer(Decimal(self._gap_len), self._gap_start, str(self._gap_len))
            self._gap_start, self._gap_len = None, 0

    def _advance_expected(self) -> None:
        self._expected_count += 1
        self._next = next(self._expected, None)

    def _merge(self, ts: datetime) -> None:
        """Walk the expected grid up to ``ts``: missing bars form gaps, others are present."""
        while self._next is not None and self._next < ts:
            self._miss_next()
        if self._next == ts:
            self._present += 1
            self._close_gap()
            self._advance_expected()
        else:
            self._unexpected += 1
            self._hit("unexpected_bar", ts, "bar where the calendar assumes the market is closed")

    # ------------------------------------------------------------------ per bar

    def add(self, c: Candle) -> None:
        if self._done:
            raise DomainError("quality check already finished")
        self._rows += 1
        ordered, mergeable = self._check_time(c)
        positive = self._check_prices(c)
        if positive:
            self._check_moves(c)
        if ordered:
            self._last = c.ts
            if mergeable:
                self._merge(c.ts)

    def _check_time(self, c: Candle) -> tuple[bool, bool]:
        ts = c.ts
        if c.symbol != self._symbol or c.timeframe is not self._timeframe:
            self._hit("foreign_row", ts, f"{c.symbol} {c.timeframe}")
        last = self._last
        ordered = last is None or ts > last
        if last is not None and not ordered:
            self._hit("not_increasing", ts, f"after {_ts(last)}")
        in_range = self._start <= ts < self._end
        if not in_range:
            self._hit("out_of_range", ts, "outside [from, to)")
        aligned = on_grid(self._timeframe, ts)
        if not aligned:
            self._hit("off_grid", ts, f"not on the {self._timeframe} grid")
        if ts + self._duration > self._as_of:
            self._hit("future_bar", ts, f"closes after {_ts(self._as_of)}")
        return ordered, in_range and aligned

    def _check_prices(self, c: Candle) -> bool:
        ts, bid, ask = c.ts, c.bid, c.ask
        positive = all(p > 0 for p in (bid.o, bid.h, bid.l, bid.c, ask.o, ask.h, ask.l, ask.c))
        if not positive:
            self._hit("non_positive_price", ts, "a price is zero or negative")
        for side, q in (("bid", bid), ("ask", ask)):
            if q.h < max(q.o, q.c) or q.l > min(q.o, q.c) or q.l > q.h:
                self._hit(
                    "ohlc_inconsistent",
                    ts,
                    f"{side} o={_d(q.o)} h={_d(q.h)} l={_d(q.l)} c={_d(q.c)}",
                )
        if ask.o < bid.o or ask.c < bid.c:
            self._hit(
                "crossed_quote", ts, f"open {_d(bid.o)}/{_d(ask.o)} close {_d(bid.c)}/{_d(ask.c)}"
            )
        if ask.h < bid.h or ask.l < bid.l:
            self._hit(
                "crossed_extremes", ts, f"high {_d(bid.h)}/{_d(ask.h)} low {_d(bid.l)}/{_d(ask.l)}"
            )
        if c.tick_volume < 0:
            self._hit("negative_volume", ts, str(c.tick_volume))
        elif c.tick_volume == 0:
            self._hit("zero_volume", ts, "0")
        spread = max(ask.o - bid.o, ask.c - bid.c)
        if self._max_spread_seen is None or spread > self._max_spread_seen:
            self._max_spread_seen = spread
        if spread > self._max_spread:
            self._hit("spread_above_cap", ts, f"{_d(spread)} > {_d(self._max_spread)}")
        return positive

    def _check_moves(self, c: Candle) -> None:
        """Flags only: a large move is reported, never removed and never a FAIL by itself."""
        ts, bid, ask = c.ts, c.bid, c.ask
        prev = self._prev_mid_close
        if prev is not None:
            jump = abs((bid.o + ask.o) / _TWO - prev) * _HUNDRED / prev
            self._largest_jump = max(self._largest_jump, jump)
            if jump > self._tf.jump_flag_pct:
                self._counts["price_jump"] += 1
                self._jumps.offer(jump, ts, f"{_pct(jump)}% from {_d(prev)}")
        mid_low = (bid.l + ask.l) / _TWO
        bar_range = ((bid.h + ask.h) / _TWO - mid_low) * _HUNDRED / mid_low
        self._largest_range = max(self._largest_range, bar_range)
        if bar_range > self._tf.range_flag_pct:
            self._counts["bar_range"] += 1
            self._ranges.offer(bar_range, ts, f"{_pct(bar_range)}%")
        self._prev_mid_close = (bid.c + ask.c) / _TWO

    # ------------------------------------------------------------------ verdict

    def finish(self) -> QualityResult:
        if self._done:
            raise DomainError("quality check already finished")
        self._done = True
        while self._next is not None:
            self._miss_next()
        self._close_gap()

        tf = self._tf
        failures = [
            GateFailure(gate, str(self._counts[gate]), "0")
            for gate in INTEGRITY_GATES
            if self._counts[gate]
        ]
        expected, present = self._expected_count, self._present
        completeness = (
            (Decimal(present) * _HUNDRED / Decimal(expected)).quantize(
                Decimal("0.001"), rounding=ROUND_FLOOR
            )
            if expected
            else Decimal(0)
        )
        if expected == 0:
            failures.append(GateFailure("no_expected_bars", "0", ">= 1"))
        elif Decimal(present) * _HUNDRED < tf.min_completeness_pct * Decimal(expected):
            failures.append(
                GateFailure(
                    "completeness_below_min",
                    f"{_d(completeness)}%",
                    f"{_d(tf.min_completeness_pct)}%",
                )
            )
        if self._longest_gap > tf.max_gap_bars:
            failures.append(
                GateFailure("gap_above_max", str(self._longest_gap), str(tf.max_gap_bars))
            )
        if self._unexpected > tf.max_unexpected_bars:
            failures.append(
                GateFailure(
                    "unexpected_above_max", str(self._unexpected), str(tf.max_unexpected_bars)
                )
            )

        examples: dict[str, tuple[Example, ...]] = {
            k: tuple(v) for k, v in self._first.items() if v
        }
        if jumps := self._jumps.items():
            examples["price_jump"] = jumps
        if ranges := self._ranges.items():
            examples["bar_range"] = ranges
        metrics: dict[str, JsonValue] = {
            "rows": self._rows,
            "expected_bars": expected,
            "present_bars": present,
            "missing_bars": expected - present,
            "unexpected_bars": self._unexpected,
            "completeness_pct": _d(completeness),
            "gaps": self._gap_count,
            "longest_gap_bars": self._longest_gap,
            "max_spread": None if self._max_spread_seen is None else _d(self._max_spread_seen),
            "largest_jump_pct": _pct(self._largest_jump),
            "largest_range_pct": _pct(self._largest_range),
        }
        return QualityResult(
            verdict=FAIL if failures else PASS,
            failures=tuple(failures),
            counts=MappingProxyType(dict(self._counts)),
            metrics=MappingProxyType(metrics),
            examples=MappingProxyType(examples),
            largest_gaps=tuple(GapRecord(e.ts, int(e.detail)) for e in self._gaps.items()),
        )
