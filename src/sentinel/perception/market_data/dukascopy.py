"""Dukascopy historical-data provider: the planned fallback (ADR 0012). Not implemented in M2.

It exists so the fallback slots into the same port without redesign. Before it is
implemented: confirm Dukascopy's terms of use for this purpose, and decide whether to build
M1 candles from hourly tick files (true bid/ask spreads, larger download) or to use their
per-day candle files.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from sentinel.domain.market_data import Timeframe
from sentinel.perception.market_data.provider import CandleBatch, ProviderUnavailable

__all__ = ["DukascopyProvider"]


class DukascopyProvider:
    name = "dukascopy"

    def fetch_candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Iterator[CandleBatch]:
        raise ProviderUnavailable(
            "the Dukascopy provider is a planned fallback and is not implemented in M2"
        )
