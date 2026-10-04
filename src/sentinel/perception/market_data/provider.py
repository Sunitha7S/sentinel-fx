"""The market-data provider port.

Providers are **read-only** sources of historical candles. Nothing in this interface can
place, modify or query orders, and implementations must refuse to issue any request other
than a historical-data read (INV-MD-READONLY). Switching provider (e.g. OANDA to Dukascopy)
replaces the implementation only; the pipeline, schema and reports stay the same (ADR 0012).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sentinel.domain.market_data import Candle, Timeframe

__all__ = [
    "CandleBatch",
    "MarketDataProvider",
    "ProviderError",
    "ProviderUnavailable",
    "ReadOnlyViolation",
]


class ProviderError(Exception):
    """The provider returned an error or malformed data."""


class ProviderUnavailable(ProviderError):
    """The provider cannot be used (authentication, region, not implemented). Not retried."""


class ReadOnlyViolation(ProviderError):
    """Something attempted a request outside the provider's read-only allow-list."""


@dataclass(frozen=True, slots=True)
class CandleBatch:
    """One page of complete, validated candles, in ascending time order."""

    candles: tuple[Candle, ...]
    rejected: int
    """Candles the provider returned that failed domain validation (never stored)."""
    incomplete: int
    """Candles still forming at request time (never stored)."""


class MarketDataProvider(Protocol):
    name: str

    def fetch_candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Iterator[CandleBatch]:
        """Complete candles with ``start <= ts < end``, page by page, oldest first."""
        ...
