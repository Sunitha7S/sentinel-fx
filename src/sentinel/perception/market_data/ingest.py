"""Historical ingestion: provider -> validated candles -> append-only store, resumable.

* Pages are written as they arrive (each in its own transaction), so an interrupted run keeps
  its progress and a re-run continues from the last stored bar.
* Inserts skip bars that already exist; stored history is never overwritten.
* Every run, successful or not, is recorded in ``ingestion_runs``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sentinel.domain.market_data import Candle, Timeframe
from sentinel.domain.types import require_utc
from sentinel.perception.market_data.provider import MarketDataProvider, ProviderError

__all__ = ["IngestionRun", "MarketDataSink", "ingest"]


@dataclass(frozen=True, slots=True)
class IngestionRun:
    run_id: uuid.UUID
    provider: str
    symbol: str
    timeframe: Timeframe
    requested_from: datetime
    requested_to: datetime
    started_at: datetime
    finished_at: datetime
    status: str  # SUCCEEDED | FAILED
    candles_received: int
    candles_inserted: int
    candles_rejected: int
    first_ts: datetime | None
    last_ts: datetime | None
    error: str | None


class MarketDataSink(Protocol):
    def latest_ts(self, symbol: str, timeframe: Timeframe) -> datetime | None: ...

    def insert(self, candles: Sequence[Candle], source: str) -> int:
        """Insert candles and their per-bar spreads; return how many were new."""
        ...

    def record_run(self, run: IngestionRun) -> None: ...


def ingest(
    provider: MarketDataProvider,
    sink: MarketDataSink,
    *,
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    clock: Callable[[], datetime],
    resume: bool = True,
) -> IngestionRun:
    start = require_utc(start, field="start")
    end = require_utc(end, field="end")
    if end <= start:
        raise ValueError("end must be after start")
    started = clock()
    fetch_from = start
    if resume:
        latest = sink.latest_ts(symbol, timeframe)
        if latest is not None and latest >= start:
            fetch_from = latest  # the bar itself is skipped on insert
    received = inserted = rejected = 0
    first: datetime | None = None
    last: datetime | None = None
    error: str | None = None
    try:
        if fetch_from < end:
            for batch in provider.fetch_candles(symbol, timeframe, fetch_from, end):
                received += len(batch.candles) + batch.rejected
                rejected += batch.rejected
                if batch.candles:
                    inserted += sink.insert(batch.candles, provider.name)
                    first = first or batch.candles[0].ts
                    last = batch.candles[-1].ts
    except ProviderError as exc:
        error = f"{type(exc).__name__}: {exc}"
    run = IngestionRun(
        run_id=uuid.uuid4(),
        provider=provider.name,
        symbol=symbol,
        timeframe=timeframe,
        requested_from=start,
        requested_to=end,
        started_at=started,
        finished_at=max(clock(), started),
        status="FAILED" if error else "SUCCEEDED",
        candles_received=received,
        candles_inserted=inserted,
        candles_rejected=rejected,
        first_ts=first,
        last_ts=last,
        error=error,
    )
    sink.record_run(run)
    return run
