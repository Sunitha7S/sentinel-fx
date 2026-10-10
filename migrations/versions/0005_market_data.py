"""Market candles, per-bar spreads and ingestion runs (plain PostgreSQL, ADR 0010).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-04

* Bid and ask OHLC are stored separately; the backtester fills longs at the ask and exits
  at the bid. Only complete candles are stored.
* ``spreads`` are per-bar spreads at the bar's open and close, derived from the bid/ask
  candle (the only simultaneous bid/ask pairs a candle contains). True sampled spreads
  (median within the bar) need tick data and belong to a later milestone.
* All three tables are append-only. Re-ingesting a range inserts only missing bars.
* Only ``svc_market_data`` writes; other services read.
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

READERS = "human_admin, svc_risk, svc_learning, svc_execution"
TIMEFRAMES = "('M1', 'M5', 'H1', 'H4', 'D1')"


def upgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    op.execute(
        f"""
        CREATE TABLE sentinel.market_candles (
          symbol       text NOT NULL CHECK (symbol ~ '^[A-Z]{{3}}_[A-Z]{{3}}$'),
          timeframe    text NOT NULL CHECK (timeframe IN {TIMEFRAMES}),
          ts           timestamptz NOT NULL,
          bid_o numeric NOT NULL, bid_h numeric NOT NULL,
          bid_l numeric NOT NULL, bid_c numeric NOT NULL,
          ask_o numeric NOT NULL, ask_h numeric NOT NULL,
          ask_l numeric NOT NULL, ask_c numeric NOT NULL,
          tick_volume  integer NOT NULL CHECK (tick_volume >= 0),
          source       text NOT NULL,
          ingested_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (symbol, timeframe, ts),
          CHECK (bid_l > 0 AND ask_l > 0),
          CHECK (bid_h >= greatest(bid_o, bid_c) AND bid_l <= least(bid_o, bid_c)),
          CHECK (ask_h >= greatest(ask_o, ask_c) AND ask_l <= least(ask_o, ask_c)),
          CHECK (ask_o >= bid_o AND ask_c >= bid_c)
        )
        """
    )
    op.execute("CREATE INDEX market_candles_ts_brin ON sentinel.market_candles USING brin (ts)")
    op.execute(
        f"""
        CREATE TABLE sentinel.spreads (
          symbol        text NOT NULL,
          timeframe     text NOT NULL CHECK (timeframe IN {TIMEFRAMES}),
          ts            timestamptz NOT NULL,
          spread_open   numeric NOT NULL CHECK (spread_open >= 0),
          spread_close  numeric NOT NULL CHECK (spread_close >= 0),
          source        text NOT NULL,
          ingested_at   timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (symbol, timeframe, ts),
          FOREIGN KEY (symbol, timeframe, ts)
            REFERENCES sentinel.market_candles (symbol, timeframe, ts)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE sentinel.ingestion_runs (
          run_id            uuid PRIMARY KEY,
          provider          text NOT NULL,
          symbol            text NOT NULL,
          timeframe         text NOT NULL CHECK (timeframe IN {TIMEFRAMES}),
          requested_from    timestamptz NOT NULL,
          requested_to      timestamptz NOT NULL,
          started_at        timestamptz NOT NULL,
          finished_at       timestamptz NOT NULL,
          status            text NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
          candles_received  integer NOT NULL CHECK (candles_received >= 0),
          candles_inserted  integer NOT NULL CHECK (candles_inserted >= 0),
          candles_rejected  integer NOT NULL CHECK (candles_rejected >= 0),
          first_ts          timestamptz,
          last_ts           timestamptz,
          error             text,
          CHECK (requested_to > requested_from),
          CHECK (finished_at >= started_at),
          CHECK (status = 'SUCCEEDED' OR error IS NOT NULL)
        )
        """
    )
    for table in ("market_candles", "spreads", "ingestion_runs"):
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON sentinel.{table} "
            "FOR EACH ROW EXECUTE FUNCTION sentinel.forbid_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON sentinel.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION sentinel.forbid_mutation()"
        )
        op.execute(f"GRANT SELECT ON sentinel.{table} TO {READERS}")
        op.execute(f"GRANT SELECT, INSERT ON sentinel.{table} TO svc_market_data")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    for table in ("spreads", "ingestion_runs", "market_candles"):
        op.execute(f"DROP TABLE sentinel.{table}")
    op.execute("RESET ROLE")
