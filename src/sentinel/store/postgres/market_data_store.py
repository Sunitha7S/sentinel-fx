"""PostgreSQL market-data store (``svc_market_data`` writes; any service role reads)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, column, table, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from sentinel.domain.market_data import Candle, Timeframe
from sentinel.perception.market_data.ingest import IngestionRun

__all__ = ["PostgresMarketDataStore", "SeriesSummary"]

_CANDLES = table(
    "market_candles",
    *(
        column(c)
        for c in (
            "symbol",
            "timeframe",
            "ts",
            "bid_o",
            "bid_h",
            "bid_l",
            "bid_c",
            "ask_o",
            "ask_h",
            "ask_l",
            "ask_c",
            "tick_volume",
            "source",
        )
    ),
    schema="sentinel",
)
_SPREADS = table(
    "spreads",
    *(column(c) for c in ("symbol", "timeframe", "ts", "spread_open", "spread_close", "source")),
    schema="sentinel",
)
_KEY = ["symbol", "timeframe", "ts"]
# Multi-row inserts (batched by SQLAlchemy); existing bars are skipped, never overwritten.
_INSERT_CANDLES = (
    pg_insert(_CANDLES).on_conflict_do_nothing(index_elements=_KEY).returning(_CANDLES.c.ts)
)
_INSERT_SPREADS = pg_insert(_SPREADS).on_conflict_do_nothing(index_elements=_KEY)
_INSERT_RUN = text(
    """
    INSERT INTO sentinel.ingestion_runs
      (run_id, provider, symbol, timeframe, requested_from, requested_to, started_at,
       finished_at, status, candles_received, candles_inserted, candles_rejected, first_ts,
       last_ts, error)
    VALUES (:run_id, :provider, :symbol, :timeframe, :requested_from, :requested_to,
            :started_at, :finished_at, :status, :candles_received, :candles_inserted,
            :candles_rejected, :first_ts, :last_ts, :error)
    """
)


@dataclass(frozen=True, slots=True)
class SeriesSummary:
    symbol: str
    timeframe: Timeframe
    count: int
    first: datetime
    last: datetime


class PostgresMarketDataStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------ MarketDataSink

    def latest_ts(self, symbol: str, timeframe: Timeframe) -> datetime | None:
        with self._engine.connect() as conn:
            value = conn.execute(
                text(
                    "SELECT max(ts) FROM sentinel.market_candles "
                    "WHERE symbol = :s AND timeframe = :tf"
                ),
                {"s": symbol, "tf": timeframe.value},
            ).scalar_one()
        return None if value is None else value.astimezone(UTC)

    def insert(self, candles: Sequence[Candle], source: str) -> int:
        if not candles:
            return 0
        rows = [
            {
                "symbol": c.symbol,
                "timeframe": c.timeframe.value,
                "ts": c.ts,
                "bid_o": c.bid.o,
                "bid_h": c.bid.h,
                "bid_l": c.bid.l,
                "bid_c": c.bid.c,
                "ask_o": c.ask.o,
                "ask_h": c.ask.h,
                "ask_l": c.ask.l,
                "ask_c": c.ask.c,
                "tick_volume": c.tick_volume,
                "source": source,
            }
            for c in candles
        ]
        with self._engine.begin() as conn:
            inserted = len(conn.execute(_INSERT_CANDLES, rows).all())
            conn.execute(
                _INSERT_SPREADS,
                [
                    {
                        "symbol": c.symbol,
                        "timeframe": c.timeframe.value,
                        "ts": c.ts,
                        "spread_open": c.spread.open,
                        "spread_close": c.spread.close,
                        "source": source,
                    }
                    for c in candles
                ],
            )
        return inserted

    def record_run(self, run: IngestionRun) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                _INSERT_RUN,
                {
                    "run_id": run.run_id,
                    "provider": run.provider,
                    "symbol": run.symbol,
                    "timeframe": run.timeframe.value,
                    "requested_from": run.requested_from,
                    "requested_to": run.requested_to,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "status": run.status,
                    "candles_received": run.candles_received,
                    "candles_inserted": run.candles_inserted,
                    "candles_rejected": run.candles_rejected,
                    "first_ts": run.first_ts,
                    "last_ts": run.last_ts,
                    "error": run.error,
                },
            )

    # ------------------------------------------------------------------ reads

    def timestamps(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Iterator[datetime]:
        """Stored bar open times in ascending order, streamed with a server-side cursor."""
        with self._engine.connect() as conn:
            result = conn.execution_options(stream_results=True, yield_per=20_000).execute(
                text(
                    "SELECT ts FROM sentinel.market_candles WHERE symbol = :s AND timeframe = :tf "
                    "AND ts >= :a AND ts < :b ORDER BY ts"
                ),
                {"s": symbol, "tf": timeframe.value, "a": start, "b": end},
            )
            for (ts,) in result:
                yield ts.astimezone(UTC)

    def summaries(self) -> list[SeriesSummary]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT symbol, timeframe, count(*) AS n, min(ts) AS first, max(ts) AS last "
                    "FROM sentinel.market_candles GROUP BY symbol, timeframe "
                    "ORDER BY symbol, timeframe"
                )
            ).all()
        return [
            SeriesSummary(
                r.symbol,
                Timeframe(r.timeframe),
                r.n,
                r.first.astimezone(UTC),
                r.last.astimezone(UTC),
            )
            for r in rows
        ]

    def spread_stats(self, symbol: str, timeframe: Timeframe) -> dict[str, object]:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT count(*) AS n, "
                    "percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_close) AS median, "
                    "percentile_cont(0.99) WITHIN GROUP (ORDER BY spread_close) AS p99, "
                    "max(spread_close) AS max FROM sentinel.spreads "
                    "WHERE symbol = :s AND timeframe = :tf"
                ),
                {"s": symbol, "tf": timeframe.value},
            ).one()
        return {"n": row.n, "median": row.median, "p99": row.p99, "max": row.max}

    def runs(self) -> list[IngestionRun]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(text("SELECT * FROM sentinel.ingestion_runs ORDER BY started_at"))
                .mappings()
                .all()
            )
        return [
            IngestionRun(
                run_id=r["run_id"],
                provider=r["provider"],
                symbol=r["symbol"],
                timeframe=Timeframe(r["timeframe"]),
                requested_from=r["requested_from"].astimezone(UTC),
                requested_to=r["requested_to"].astimezone(UTC),
                started_at=r["started_at"].astimezone(UTC),
                finished_at=r["finished_at"].astimezone(UTC),
                status=r["status"],
                candles_received=r["candles_received"],
                candles_inserted=r["candles_inserted"],
                candles_rejected=r["candles_rejected"],
                first_ts=r["first_ts"].astimezone(UTC) if r["first_ts"] else None,
                last_ts=r["last_ts"].astimezone(UTC) if r["last_ts"] else None,
                error=r["error"],
            )
            for r in rows
        ]
