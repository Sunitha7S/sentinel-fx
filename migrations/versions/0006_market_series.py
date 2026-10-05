"""Single-source series registry (INV-MD-SINGLE-SOURCE, ADR 0014).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-05

* ``market_series`` maps each (symbol, timeframe) to the one source allowed to write it.
  It is append-only, and **no service role can write it**: rows are added only by
  ``enforce_series_source()``, a trigger function that runs as ``sentinel_owner``
  (SECURITY DEFINER) with a fixed ``search_path``, like ``advance_audit_head()``.
* The first candle of a new series registers it. Every later candle or spread row must
  carry the registered source, otherwise the insert is refused, for every role including the
  owner. Before this, a row from a second provider for an existing bar was silently skipped
  by ``ON CONFLICT DO NOTHING`` and new bars were mixed into the series.
* Two ingestions racing to register the same series cannot both win: the second waits on
  the first's registration (unique key) and is then compared with it.
* Existing history is backfilled. If any series already mixes sources, the upgrade fails
  rather than choosing one.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

READERS = "human_admin, svc_risk, svc_learning, svc_execution, svc_market_data"
TIMEFRAMES = "('M1', 'M5', 'H1', 'H4', 'D1')"


def upgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    op.execute(
        f"""
        CREATE TABLE sentinel.market_series (
          symbol         text NOT NULL CHECK (symbol ~ '^[A-Z]{{3}}_[A-Z]{{3}}$'),
          timeframe      text NOT NULL CHECK (timeframe IN {TIMEFRAMES}),
          source         text NOT NULL CHECK (source <> ''),
          registered_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (symbol, timeframe)
        )
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM sentinel.market_candles
            GROUP BY symbol, timeframe HAVING count(DISTINCT source) > 1
          ) OR EXISTS (
            SELECT 1 FROM sentinel.spreads s
            JOIN sentinel.market_candles c USING (symbol, timeframe, ts)
            WHERE s.source <> c.source
          ) THEN
            RAISE EXCEPTION 'stored market history has more than one source for a series; '
              'resolve it before upgrading (ADR 0014)';
          END IF;
        END $$
        """
    )
    op.execute(
        """
        INSERT INTO sentinel.market_series (symbol, timeframe, source, registered_at)
        SELECT symbol, timeframe, min(source), min(ingested_at)
        FROM sentinel.market_candles GROUP BY symbol, timeframe
        """
    )
    op.execute(
        """
        CREATE FUNCTION sentinel.enforce_series_source() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = sentinel, pg_temp AS $$
        DECLARE
          registered text;
        BEGIN
          SELECT s.source INTO registered FROM sentinel.market_series s
            WHERE s.symbol = NEW.symbol AND s.timeframe = NEW.timeframe;
          IF NOT FOUND THEN
            IF TG_TABLE_NAME <> 'market_candles' THEN
              RAISE EXCEPTION 'series % % is not registered: % rows need its candles first',
                NEW.symbol, NEW.timeframe, TG_TABLE_NAME;
            END IF;
            -- Waits on a concurrent first writer; its committed source is then compared.
            INSERT INTO sentinel.market_series (symbol, timeframe, source)
              VALUES (NEW.symbol, NEW.timeframe, NEW.source)
              ON CONFLICT (symbol, timeframe) DO NOTHING;
            SELECT s.source INTO STRICT registered FROM sentinel.market_series s
              WHERE s.symbol = NEW.symbol AND s.timeframe = NEW.timeframe;
          END IF;
          IF registered <> NEW.source THEN
            RAISE EXCEPTION 'series % % is registered to source %; refused a % row from %',
              NEW.symbol, NEW.timeframe, registered, TG_TABLE_NAME, NEW.source;
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION sentinel.enforce_series_source() FROM PUBLIC")
    for table in ("market_candles", "spreads"):
        op.execute(
            f"CREATE TRIGGER {table}_series_source BEFORE INSERT ON sentinel.{table} "
            "FOR EACH ROW EXECUTE FUNCTION sentinel.enforce_series_source()"
        )
    op.execute(
        "CREATE TRIGGER market_series_no_update_delete BEFORE UPDATE OR DELETE "
        "ON sentinel.market_series FOR EACH ROW EXECUTE FUNCTION sentinel.forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER market_series_no_truncate BEFORE TRUNCATE ON sentinel.market_series "
        "FOR EACH STATEMENT EXECUTE FUNCTION sentinel.forbid_mutation()"
    )
    op.execute(f"GRANT SELECT ON sentinel.market_series TO {READERS}")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    for table in ("market_candles", "spreads"):
        op.execute(f"DROP TRIGGER {table}_series_source ON sentinel.{table}")
    op.execute("DROP TABLE sentinel.market_series")
    op.execute("DROP FUNCTION sentinel.enforce_series_source()")
    op.execute("RESET ROLE")
