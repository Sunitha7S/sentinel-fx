"""Dataset snapshots in PostgreSQL: freeze a verified range; load it only through verification.

``freeze`` (``svc_market_data``) runs in one transaction holding the series' exclusive
advisory lock, so no ingestion page can be in flight or start meanwhile. It checks the
source against the registry, requires the range to end at or before the newest stored bar,
then judges and digests every row in the range in one pass (``judge_dataset``) with the
given quality configuration, at the transaction's own timestamp (``now()``, which is also
the record's ``created_at``). Only a PASS from an APPROVED configuration is attested and
stored, so the rows judged are exactly the rows frozen. The database re-checks the source,
range and row count (``check_snapshot``) and from then on refuses inserts inside the range.

``load`` is the only way to obtain rows of a snapshot. It reads the record and the rows in a
single read-only REPEATABLE READ transaction, refuses an unknown snapshot, a non-PASS
verdict, an unknown format, an oversized snapshot, any source mismatch or a quality
configuration it does not hold (configurations are given to the store keyed by their hash;
a snapshot is never re-judged under a different one), and returns a ``VerifiedDataset`` only
if the recomputed row count, hashes and quality report are identical. There is no override.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator, Mapping
from contextlib import closing
from datetime import UTC
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import Connection, Engine, text

from sentinel.domain.dataset import (
    DATASET_FORMAT,
    DatasetError,
    DatasetSpec,
    QualityProvenance,
    SnapshotRecord,
    VerifiedDataset,
    attest,
    issue_verified_dataset,
    judge_dataset,
)
from sentinel.domain.market_data import OHLC, Candle, Timeframe
from sentinel.domain.quality import QualityConfig

__all__ = ["DEFAULT_MAX_ROWS", "SERIES_LOCK_CLASS", "PostgresDatasetStore"]

SERIES_LOCK_CLASS: Final = 7301
"""First key of the per-series advisory lock; must match migration 0007."""
DEFAULT_MAX_ROWS: Final = 1_000_000
"""Snapshots are materialised in memory when loaded; larger ranges must be split."""

_LOCK_KEY = f"{SERIES_LOCK_CLASS}, hashtext(:symbol || '/' || :timeframe)"
_RECORD_COLUMNS = (
    "snapshot_id, format, symbol, timeframe, source, range_start, range_end, row_count, "
    "rows_sha256, sha256, quality_verdict, quality_report_sha256, quality_config_sha256, "
    "created_at"
)
_ROWS = text(
    "SELECT symbol, timeframe, ts, bid_o, bid_h, bid_l, bid_c, ask_o, ask_h, ask_l, ask_c, "
    "tick_volume, source FROM sentinel.market_candles "
    "WHERE symbol = :symbol AND timeframe = :timeframe AND ts >= :start AND ts < :end "
    "ORDER BY ts"
)


def _candle(row: Any) -> Candle:
    return Candle(
        row.symbol,
        Timeframe(row.timeframe),
        row.ts.astimezone(UTC),
        OHLC(row.bid_o, row.bid_h, row.bid_l, row.bid_c),
        OHLC(row.ask_o, row.ask_h, row.ask_l, row.ask_c),
        int(row.tick_volume),
    )


def _params(spec: DatasetSpec) -> dict[str, object]:
    return {
        "symbol": spec.symbol,
        "timeframe": spec.timeframe.value,
        "start": spec.start,
        "end": spec.end,
    }


def _record(row: Any) -> SnapshotRecord:
    if row.format != DATASET_FORMAT:
        raise DatasetError(f"snapshot {row.snapshot_id}: unknown format {row.format!r}")
    return SnapshotRecord(
        snapshot_id=str(row.snapshot_id),
        spec=DatasetSpec(
            row.symbol,
            Timeframe(row.timeframe),
            row.source,
            row.range_start.astimezone(UTC),
            row.range_end.astimezone(UTC),
        ),
        row_count=int(row.row_count),
        rows_sha256=row.rows_sha256,
        sha256=row.sha256,
        quality=QualityProvenance(
            row.quality_verdict, row.quality_report_sha256, row.quality_config_sha256
        ),
        created_at=row.created_at.astimezone(UTC),
    )


class PostgresDatasetStore:
    def __init__(
        self,
        engine: Engine,
        *,
        quality_configs: Mapping[str, QualityConfig] = MappingProxyType({}),
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        """``quality_configs`` maps configuration hash to configuration; ``load`` refuses a
        snapshot whose configuration is not among them. ``freeze`` takes its own."""
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        self._engine = engine
        self._configs = MappingProxyType(dict(quality_configs))
        self._max_rows = max_rows

    @staticmethod
    def lock_series_exclusive(conn: Connection, symbol: str, timeframe: Timeframe) -> None:
        """Take the series' exclusive lock for the rest of ``conn``'s transaction."""
        conn.execute(
            text(f"SELECT pg_advisory_xact_lock({_LOCK_KEY})"),
            {"symbol": symbol, "timeframe": timeframe.value},
        )

    # ------------------------------------------------------------------ freeze

    def freeze(self, spec: DatasetSpec, quality: QualityConfig) -> SnapshotRecord:
        """Judge, attest and store ``spec`` atomically. Refuses anything but a PASS."""
        if not isinstance(quality, QualityConfig):
            raise DatasetError("a quality configuration is required to freeze a snapshot")
        params = _params(spec)
        with self._engine.begin() as conn:
            self.lock_series_exclusive(conn, spec.symbol, spec.timeframe)
            registered = conn.execute(
                text(
                    "SELECT source FROM sentinel.market_series "
                    "WHERE symbol = :symbol AND timeframe = :timeframe"
                ),
                params,
            ).scalar_one_or_none()
            if registered is None:
                raise DatasetError(f"series {spec.symbol} {spec.timeframe.value} is not registered")
            if registered != spec.source:
                raise DatasetError(
                    f"source mismatch: series is registered to {registered}, not {spec.source}"
                )
            newest = conn.execute(
                text(
                    "SELECT max(ts) FROM sentinel.market_candles "
                    "WHERE symbol = :symbol AND timeframe = :timeframe"
                ),
                params,
            ).scalar_one()
            if newest is None or spec.end > newest:
                raise DatasetError(
                    f"range end {spec.end.isoformat()} is after the newest stored bar "
                    f"{newest.isoformat() if newest else 'none'}"
                )
            duplicate = conn.execute(
                text(
                    "SELECT snapshot_id FROM sentinel.dataset_snapshots WHERE symbol = :symbol "
                    "AND timeframe = :timeframe AND source = :source AND range_start = :start "
                    "AND range_end = :end"
                ),
                {**params, "source": spec.source},
            ).scalar_one_or_none()
            if duplicate is not None:
                raise DatasetError(f"this range is already frozen as snapshot {duplicate}")
            as_of = conn.execute(text("SELECT now()")).scalar_one().astimezone(UTC)
            with closing(self._stream(conn, spec)) as candles:
                report = judge_dataset(spec, quality, as_of, candles)
            if report.digest.rows > self._max_rows:
                raise DatasetError(
                    f"{report.digest.rows:,} rows exceed the snapshot limit of "
                    f"{self._max_rows:,}; freeze a shorter range"
                )
            attestation = attest(report)  # refuses FAIL and unapproved configurations
            row = conn.execute(
                text(
                    "INSERT INTO sentinel.dataset_snapshots (snapshot_id, format, symbol, "  # noqa: S608
                    "timeframe, source, range_start, range_end, row_count, rows_sha256, sha256, "
                    "quality_verdict, quality_report_sha256, quality_config_sha256) VALUES "
                    "(:id, :format, :symbol, :timeframe, :source, :start, :end, :rows, "
                    ":rows_sha256, :sha256, :verdict, :report, :config) "
                    f"RETURNING {_RECORD_COLUMNS}"
                ),
                {
                    **params,
                    "id": uuid.uuid4(),
                    "format": DATASET_FORMAT,
                    "source": spec.source,
                    "rows": attestation.rows,
                    "rows_sha256": attestation.rows_sha256,
                    "sha256": attestation.sha256,
                    "verdict": attestation.provenance.verdict,
                    "report": attestation.report_sha256,
                    "config": attestation.config_sha256,
                },
            ).one()
            record = _record(row)
            if record.created_at != as_of:  # the report names as_of; load reproduces it from here
                raise DatasetError("snapshot time differs from the quality evaluation time")
        return record

    # ------------------------------------------------------------------ verified load

    def load(self, snapshot_id: str | uuid.UUID) -> VerifiedDataset:
        try:
            key = snapshot_id if isinstance(snapshot_id, uuid.UUID) else uuid.UUID(snapshot_id)
        except ValueError:
            raise DatasetError(f"unknown snapshot {snapshot_id!r}") from None
        with (
            self._engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            conn.execute(text("SET TRANSACTION READ ONLY"))
            row = conn.execute(
                text(
                    f"SELECT {_RECORD_COLUMNS} FROM sentinel.dataset_snapshots "  # noqa: S608
                    "WHERE snapshot_id = :id"
                ),
                {"id": key},
            ).one_or_none()
            if row is None:
                raise DatasetError(f"unknown snapshot {snapshot_id!s}")
            record = _record(row)  # refuses an unknown format or a non-PASS verdict
            if record.row_count > self._max_rows:
                raise DatasetError(
                    f"snapshot {record.snapshot_id} has {record.row_count:,} rows, above the "
                    f"limit of {self._max_rows:,}"
                )
            registered = conn.execute(
                text(
                    "SELECT source FROM sentinel.market_series "
                    "WHERE symbol = :symbol AND timeframe = :timeframe"
                ),
                _params(record.spec),
            ).scalar_one_or_none()
            if registered != record.spec.source:
                raise DatasetError(
                    f"snapshot {record.snapshot_id}: source mismatch (snapshot "
                    f"{record.spec.source}, series registered to {registered})"
                )
            quality = self._configs.get(record.quality.config_sha256)
            if quality is None or quality.sha256 != record.quality.config_sha256:
                raise DatasetError(
                    f"snapshot {record.snapshot_id}: quality configuration "
                    f"{record.quality.config_sha256} is not available; the snapshot cannot be "
                    "verified"
                )
            with closing(self._stream(conn, record.spec)) as candles:
                return issue_verified_dataset(record, candles, quality)

    def list(self) -> list[SnapshotRecord]:
        """Snapshot records (metadata only). Rows are available solely through ``load``."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {_RECORD_COLUMNS} FROM sentinel.dataset_snapshots "  # noqa: S608
                    "ORDER BY symbol, timeframe, range_start, created_at"
                )
            ).all()
        return [_record(r) for r in rows]

    # ------------------------------------------------------------------ rows

    @staticmethod
    def _stream(conn: Connection, spec: DatasetSpec) -> Generator[Candle]:
        # Streaming is set on this statement only. Connection.execution_options() would make
        # every later statement on the connection use a server-side cursor too, and the
        # INSERT ... RETURNING that freeze() runs next cannot be declared as a cursor.
        # The result is closed on every exit path; callers wrap this generator in closing()
        # so an error while consuming it releases the server-side cursor immediately.
        with conn.execute(
            _ROWS.execution_options(stream_results=True, max_row_buffer=10_000), _params(spec)
        ) as result:
            for row in result:
                if row.source != spec.source:
                    raise DatasetError(
                        f"source mismatch at {row.ts.isoformat()}: row from {row.source}, "
                        f"snapshot source {spec.source}"
                    )
                yield _candle(row)
