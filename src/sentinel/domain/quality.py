"""Data-quality configuration (and, from engine v1, the gates that judge a dataset).

A ``QualityConfig`` is a strict, immutable set of thresholds. Its identity is the SHA-256
of its canonical form: version, status, engine, calendar and every threshold, with decimals
normalised. Comments, key order, line endings and decimal scale in the source file do not
change it; any semantic change does.

``engine`` and ``calendar`` name the exact gate semantics and market calendar the
thresholds are judged with. This code implements one engine and one calendar; a config
naming any other is refused, so a snapshot judged under v1 can never be silently re-judged
under different rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final

from sentinel.domain.canonical import sha256_hex
from sentinel.domain.market_data import Timeframe
from sentinel.domain.types import DomainError, to_decimal

__all__ = [
    "APPROVED",
    "PROVISIONAL",
    "QUALITY_CALENDAR",
    "QUALITY_ENGINE",
    "QualityConfig",
    "QualityConfigError",
    "SymbolThresholds",
    "TimeframeThresholds",
]

QUALITY_ENGINE: Final = "sentinel-quality/v1"
QUALITY_CALENDAR: Final = "fx-ny1700-xmas-newyear/v1"
PROVISIONAL: Final = "PROVISIONAL_UNCALIBRATED"
APPROVED: Final = "APPROVED"

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
