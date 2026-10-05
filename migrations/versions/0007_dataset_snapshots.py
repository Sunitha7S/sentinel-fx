"""Dataset snapshots and sealed ranges (INV-DATA-SNAPSHOT, INV-DATA-SEALED, ADR 0014).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-05

* ``dataset_snapshots`` records frozen ranges ``[range_start, range_end)`` of one series:
  format version, source, row count, the rows hash and the snapshot hash (computed by
  ``sentinel.domain.dataset``), and the PASS quality attestation. Append-only. Only
  ``svc_market_data`` may insert; everyone else reads.
* ``check_snapshot()`` (BEFORE INSERT, SECURITY DEFINER, fixed search_path) re-checks every
  snapshot the database is asked to store, whoever writes it: the source is the series'
  registered source, the range ends at or before the newest stored bar, and ``row_count``
  equals the rows actually stored in the range. Only PASS verdicts and format v1 pass the
  CHECK constraints. The hash itself is recomputed from the rows on every load.
* ``enforce_sealed_ranges()`` (BEFORE INSERT on candles and spreads) refuses any row whose
  open time lies inside a frozen range, for every role including the owner. Updates,
  deletes and truncation were already refused everywhere (append-only triggers).
* Locks: each insert takes a transaction-scoped *shared* advisory lock on its series; a
  freeze takes the *exclusive* one. A freeze therefore waits for ingestion pages in flight
  and sees all of their rows, and ingestion that starts during a freeze waits and is then
  checked against the new snapshot. Key: (7301, hashtext(symbol || '/' || timeframe)),
  shared with ``sentinel.store.postgres.dataset_store.SERIES_LOCK_CLASS``.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

READERS = "human_admin, svc_risk, svc_learning, svc_execution"
LOCK = "7301, hashtext(NEW.symbol || '/' || NEW.timeframe)"


def upgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    op.execute(
        """
        CREATE TABLE sentinel.dataset_snapshots (
          snapshot_id            uuid PRIMARY KEY,
          format                 text NOT NULL,
          symbol                 text NOT NULL,
          timeframe              text NOT NULL,
          source                 text NOT NULL,
          range_start            timestamptz NOT NULL,
          range_end              timestamptz NOT NULL,
          row_count              bigint NOT NULL CHECK (row_count > 0),
          rows_sha256            char(64) NOT NULL CHECK (rows_sha256 ~ '^[0-9a-f]{64}$'),
          sha256                 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
          quality_verdict        text NOT NULL,
          quality_report_sha256  char(64) NOT NULL
                                 CHECK (quality_report_sha256 ~ '^[0-9a-f]{64}$'),
          quality_config_sha256  char(64) NOT NULL
                                 CHECK (quality_config_sha256 ~ '^[0-9a-f]{64}$'),
          created_by             text NOT NULL DEFAULT session_user,
          created_role           text NOT NULL DEFAULT current_user,
          created_at             timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT dataset_snapshots_format CHECK (format = 'sentinel-dataset/v1'),
          CONSTRAINT dataset_snapshots_pass_only CHECK (quality_verdict = 'PASS'),
          CONSTRAINT dataset_snapshots_range CHECK (range_end > range_start),
          CONSTRAINT dataset_snapshots_unique_range
            UNIQUE (symbol, timeframe, source, range_start, range_end),
          FOREIGN KEY (symbol, timeframe) REFERENCES sentinel.market_series (symbol, timeframe)
        )
        """
    )
    op.execute(
        "CREATE INDEX dataset_snapshots_series_range "
        "ON sentinel.dataset_snapshots (symbol, timeframe, range_start, range_end)"
    )
    op.execute(
        f"""
        CREATE FUNCTION sentinel.check_snapshot() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = sentinel, pg_temp AS $$
        DECLARE
          registered text;
          newest timestamptz;
          stored bigint;
        BEGIN
          PERFORM pg_advisory_xact_lock({LOCK});
          SELECT s.source INTO registered FROM sentinel.market_series s
            WHERE s.symbol = NEW.symbol AND s.timeframe = NEW.timeframe;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'snapshot refused: series % % is not registered',
              NEW.symbol, NEW.timeframe;
          END IF;
          IF registered <> NEW.source THEN
            RAISE EXCEPTION 'snapshot refused: source mismatch (series % % is registered to %, '
              'snapshot names %)', NEW.symbol, NEW.timeframe, registered, NEW.source;
          END IF;
          SELECT max(c.ts) INTO newest FROM sentinel.market_candles c
            WHERE c.symbol = NEW.symbol AND c.timeframe = NEW.timeframe;
          IF newest IS NULL OR NEW.range_end > newest THEN
            RAISE EXCEPTION 'snapshot refused: range end % is after the newest stored bar %',
              NEW.range_end, newest;
          END IF;
          SELECT count(*) INTO stored FROM sentinel.market_candles c
            WHERE c.symbol = NEW.symbol AND c.timeframe = NEW.timeframe
              AND c.ts >= NEW.range_start AND c.ts < NEW.range_end;
          IF stored <> NEW.row_count THEN
            RAISE EXCEPTION 'snapshot refused: row count % does not match % stored rows',
              NEW.row_count, stored;
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION sentinel.enforce_sealed_ranges() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = sentinel, pg_temp AS $$
        DECLARE
          frozen uuid;
        BEGIN
          PERFORM pg_advisory_xact_lock_shared({LOCK});
          SELECT d.snapshot_id INTO frozen FROM sentinel.dataset_snapshots d
            WHERE d.symbol = NEW.symbol AND d.timeframe = NEW.timeframe
              AND d.range_start <= NEW.ts AND NEW.ts < d.range_end
            LIMIT 1;
          IF FOUND THEN
            RAISE EXCEPTION '% % at % lies inside frozen snapshot %; frozen history cannot change',
              NEW.symbol, NEW.timeframe, NEW.ts, frozen;
          END IF;
          RETURN NEW;
        END $$
        """
    )
    for fn in ("check_snapshot", "enforce_sealed_ranges"):
        op.execute(f"REVOKE ALL ON FUNCTION sentinel.{fn}() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER dataset_snapshots_check BEFORE INSERT ON sentinel.dataset_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION sentinel.check_snapshot()"
    )
    for table in ("market_candles", "spreads"):
        op.execute(
            f"CREATE TRIGGER {table}_sealed_range BEFORE INSERT ON sentinel.{table} "
            "FOR EACH ROW EXECUTE FUNCTION sentinel.enforce_sealed_ranges()"
        )
    op.execute(
        "CREATE TRIGGER dataset_snapshots_no_update_delete BEFORE UPDATE OR DELETE "
        "ON sentinel.dataset_snapshots FOR EACH ROW EXECUTE FUNCTION sentinel.forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER dataset_snapshots_no_truncate BEFORE TRUNCATE "
        "ON sentinel.dataset_snapshots "
        "FOR EACH STATEMENT EXECUTE FUNCTION sentinel.forbid_mutation()"
    )
    op.execute(f"GRANT SELECT ON sentinel.dataset_snapshots TO {READERS}")
    op.execute("GRANT SELECT, INSERT ON sentinel.dataset_snapshots TO svc_market_data")
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET LOCAL ROLE sentinel_owner")
    for table in ("market_candles", "spreads"):
        op.execute(f"DROP TRIGGER {table}_sealed_range ON sentinel.{table}")
    op.execute("DROP TABLE sentinel.dataset_snapshots")
    op.execute("DROP FUNCTION sentinel.enforce_sealed_ranges()")
    op.execute("DROP FUNCTION sentinel.check_snapshot()")
    op.execute("RESET ROLE")
