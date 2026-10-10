"""Synthetic candles and an OANDA-shaped fake server for market-data tests."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from sentinel.domain.market_data import OHLC, Candle, Timeframe
from sentinel.perception.market_data.ingest import IngestionRun
from sentinel.perception.market_data.oanda import format_oanda_time, parse_oanda_time
from sentinel.perception.market_data.provider import CandleBatch, ProviderError


def candle(symbol: str, ts: datetime, tf: Timeframe = Timeframe.M1, mid: str = "1.1000") -> Candle:
    m = Decimal(mid)
    pip = Decimal("0.01") if symbol.endswith("JPY") else Decimal("0.0001")
    bid = OHLC(m, m + 2 * pip, m - 2 * pip, m + pip)
    ask = OHLC(m + pip, m + 3 * pip, m - pip, m + 2 * pip)
    return Candle(symbol, tf, ts, bid, ask, tick_volume=42)


def oanda_candle(ts: datetime, *, complete: bool = True, crossed: bool = False) -> dict[str, Any]:
    ask_c = "1.09990" if crossed else "1.10020"
    return {
        "time": format_oanda_time(ts),
        "complete": complete,
        "volume": 12,
        "bid": {"o": "1.10000", "h": "1.10030", "l": "1.09980", "c": "1.10010"},
        "ask": {"o": "1.10010", "h": "1.10040", "l": "1.09990", "c": ask_c},
    }


@dataclass
class FakeOanda:
    """Serves ``/v3/instruments/{i}/candles`` like OANDA: from + count + includeFirst."""

    times: list[datetime]
    incomplete: set[datetime] = field(default_factory=set)
    crossed: set[datetime] = field(default_factory=set)
    fail_first: list[int] = field(default_factory=list)  # status codes returned first
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_first:
            return httpx.Response(self.fail_first.pop(0), json={"errorMessage": "try later"})
        params = request.url.params
        start = parse_oanda_time(params["from"])
        count = int(params["count"])
        include_first = params.get("includeFirst", "true") == "true"
        matching = [t for t in self.times if t > start or (include_first and t == start)]
        page = matching[:count]
        return httpx.Response(
            200,
            json={
                "instrument": request.url.path.split("/")[3],
                "granularity": params["granularity"],
                "candles": [
                    oanda_candle(t, complete=t not in self.incomplete, crossed=t in self.crossed)
                    for t in page
                ],
            },
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def minutes(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(minutes=i) for i in range(n)]


@dataclass
class ListProvider:
    name: str
    batches: Sequence[Sequence[Candle]]
    fail_after: int | None = None
    calls: int = 0

    def fetch_candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Iterator[CandleBatch]:
        self.calls += 1
        for i, batch in enumerate(self.batches):
            if self.fail_after is not None and i >= self.fail_after:
                raise ProviderError("connection reset")
            yield CandleBatch(tuple(c for c in batch if start <= c.ts < end), 0, 0)


@dataclass
class MemorySink:
    """Mirrors the database: a series is registered to the first source that writes it."""

    rows: dict[tuple[str, str, datetime], Candle] = field(default_factory=dict)
    runs: list[IngestionRun] = field(default_factory=list)
    sources: dict[tuple[str, str], str] = field(default_factory=dict)

    def registered_source(self, symbol: str, timeframe: Timeframe) -> str | None:
        return self.sources.get((symbol, timeframe.value))

    def latest_ts(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        ts = [k[2] for k in self.rows if k[0] == symbol and k[1] == timeframe.value]
        return max(ts) if ts else None

    def insert(self, candles: Sequence[Candle], source: str) -> int:
        for c in candles:
            registered = self.sources.setdefault((c.symbol, c.timeframe.value), source)
            if registered != source:
                raise AssertionError(f"series registered to source {registered}")
        new = 0
        for c in candles:
            key = (c.symbol, c.timeframe.value, c.ts)
            if key not in self.rows:
                self.rows[key] = c
                new += 1
        return new

    def record_run(self, run: IngestionRun) -> None:
        self.runs.append(run)
