"""Market-data value types: timeframes, bid/ask candles, per-bar spreads.

A ``Candle`` cannot be constructed in an invalid state: prices are positive decimals, each
side's high/low bound its open/close, and the ask is never below the bid at the bar's open
or close (the only simultaneous bid/ask pairs a candle carries).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sentinel.domain.types import DomainError, require_positive, require_utc, to_decimal

__all__ = ["OHLC", "BarSpread", "Candle", "Timeframe"]


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def duration(self) -> timedelta:
        return _DURATION[self]

    @property
    def fixed_utc_grid(self) -> bool:
        """M1/M5/H1 bars open on a fixed UTC grid. H4/D1 bars are aligned to 17:00 New York
        and move by an hour in UTC when daylight saving time changes."""
        return self in (Timeframe.M1, Timeframe.M5, Timeframe.H1)


_DURATION = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}


@dataclass(frozen=True, slots=True)
class OHLC:
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741 - conventional OHLC field name
    c: Decimal

    def __post_init__(self) -> None:
        for name in ("o", "h", "l", "c"):
            value = to_decimal(getattr(self, name), field=f"OHLC.{name}")
            object.__setattr__(self, name, require_positive(value, field=f"OHLC.{name}"))
        if self.h < max(self.o, self.c) or self.l > min(self.o, self.c) or self.l > self.h:
            raise DomainError(f"inconsistent OHLC: {self.o} {self.h} {self.l} {self.c}")


@dataclass(frozen=True, slots=True)
class Candle:
    symbol: str
    timeframe: Timeframe
    ts: datetime
    """Bar open time (UTC)."""
    bid: OHLC
    ask: OHLC
    tick_volume: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", require_utc(self.ts, field="Candle.ts"))
        if (
            len(self.symbol) != 7
            or self.symbol[3] != "_"
            or not self.symbol.replace("_", "").isalpha()
            or not self.symbol.isupper()
        ):
            raise DomainError(f"Candle.symbol must look like EUR_USD, got {self.symbol!r}")
        if isinstance(self.tick_volume, bool) or self.tick_volume < 0:
            raise DomainError("Candle.tick_volume must be a non-negative int")
        if self.ask.o < self.bid.o or self.ask.c < self.bid.c:
            raise DomainError(f"crossed quote in {self.symbol} {self.ts.isoformat()}")

    @property
    def spread(self) -> BarSpread:
        return BarSpread(
            symbol=self.symbol,
            timeframe=self.timeframe,
            ts=self.ts,
            open=self.ask.o - self.bid.o,
            close=self.ask.c - self.bid.c,
        )


@dataclass(frozen=True, slots=True)
class BarSpread:
    """Spread at the bar's open and close. Not a sampled median: that needs tick data."""

    symbol: str
    timeframe: Timeframe
    ts: datetime
    open: Decimal
    close: Decimal
