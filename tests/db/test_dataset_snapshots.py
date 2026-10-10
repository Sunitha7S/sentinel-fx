"""Dataset snapshots in PostgreSQL: freeze, verified load, sealing, tampering, locks, roles.

INV-DATA-SNAPSHOT  verified datasets are reproducible and tamper-evident.
INV-DATA-SEALED    frozen ranges cannot change.
(ADR 0014)
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from sentinel.config.quality_loader import QUALITY_CONFIG_DIR, load_quality_config
from sentinel.domain.dataset import (
    DatasetError,
    DatasetSpec,
    VerifiedDataset,
    digest_candles,
    judge_dataset,
)
from sentinel.domain.market_data import Candle, Timeframe
from sentinel.store.postgres.dataset_store import SERIES_LOCK_CLASS, PostgresDatasetStore
from sentinel.store.postgres.engine import OWNER_ROLE, SERVICE_ROLES
from sentinel.store.postgres.market_data_store import PostgresMarketDataStore

from db.conftest import Db
from support.market import candle, minutes
from support.quality import lenient_config, quality_config

MON = datetime(2015, 1, 5, tzinfo=UTC)
SRC = "oanda-practice"
PASS = lenient_config()
"""Tests here are about storage, sealing and locking; quality gates are tested in their own
module. Integrity gates still apply; only completeness thresholds are relaxed."""
STRICT = quality_config()
CONFIGS = {c.sha256: c for c in (PASS, STRICT)}


def _store(db: Db, role: str = "svc_market_data", **kw: object) -> PostgresDatasetStore:
    return PostgresDatasetStore(db.engine(role), quality_configs=CONFIGS, **kw)  # type: ignore[arg-type]


def _bars(n: int, start: datetime = MON, symbol: str = "EUR_USD") -> list[Candle]:
    mid = "150.00" if symbol.endswith("JPY") else "1.1000"
    return [candle(symbol, t, mid=mid) for t in minutes(start, n)]


def _spec(start_min: int, end_min: int, **kw: object) -> DatasetSpec:
    fields: dict[str, object] = {
        "symbol": "EUR_USD",
        "timeframe": Timeframe.M1,
        "source": SRC,
        "start": MON + timedelta(minutes=start_min),
        "end": MON + timedelta(minutes=end_min),
    }
    fields.update(kw)
    return DatasetSpec(**fields)  # type: ignore[arg-type]


def _seeded(db: Db, n: int = 30) -> tuple[PostgresMarketDataStore, PostgresDatasetStore]:
    md = PostgresMarketDataStore(db.engine("svc_market_data"))
    md.insert(_bars(n), SRC)
    return md, _store(db, "svc_market_data")


def _snapshots(db: Db) -> int:
    with db.engine().connect() as conn:
        return int(
            conn.execute(text("SELECT count(*) FROM sentinel.dataset_snapshots")).scalar_one()
        )


def _tamper(db: Db, *sql: str) -> None:
    """A superuser edit that bypasses every trigger: what verification must still catch."""
    with db.engine().begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        for statement in sql:
            conn.execute(text(statement))


def _raises_db(fn: Callable[[], object], match: str) -> None:
    with pytest.raises(DBAPIError) as exc:
        fn()
    assert isinstance(exc.value.orig, pg.RaiseException), exc.value.orig
    assert match in str(exc.value.orig), str(exc.value.orig)


# ----------------------------------------------------------------------------- freeze + load


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_freeze_then_load_reproduces_the_exact_dataset(db: Db) -> None:
    _, ds = _seeded(db)
    spec = _spec(5, 25)
    record = ds.freeze(spec, PASS)
    expected = digest_candles(spec, _bars(20, MON + timedelta(minutes=5)))
    assert (record.row_count, record.rows_sha256, record.sha256) == (
        expected.rows,
        expected.rows_sha256,
        expected.sha256,
    )
    for role in ("svc_learning", "svc_risk", "svc_market_data", "human_admin"):
        loaded = _store(db, role).load(record.snapshot_id)
        assert isinstance(loaded, VerifiedDataset)
        assert loaded.sha256 == expected.sha256
        assert [c.ts for c in loaded.candles] == minutes(MON + timedelta(minutes=5), 20)
        assert loaded.record.quality == record.quality
        assert loaded.record.quality.config_sha256 == PASS.sha256
    assert [r.snapshot_id for r in ds.list()] == [record.snapshot_id]


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_unknown_or_malformed_snapshot_ids_are_refused(db: Db) -> None:
    _, ds = _seeded(db)
    for bad in (str(uuid.uuid4()), "not-a-uuid", ""):
        with pytest.raises(DatasetError, match="unknown snapshot"):
            ds.load(bad)


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_size_limit_fails_closed_on_freeze_and_load(db: Db) -> None:
    _, ds = _seeded(db)
    record = ds.freeze(_spec(0, 20), PASS)
    small = _store(db, "svc_learning", max_rows=10)
    with pytest.raises(DatasetError, match="limit"):
        small.load(record.snapshot_id)
    with pytest.raises(DatasetError, match="limit"):
        _store(db, "svc_market_data", max_rows=5).freeze(_spec(20, 29), PASS)


# ----------------------------------------------------------------------------- quality gates


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_freeze_judges_the_frozen_rows_at_its_own_transaction_time(db: Db) -> None:
    """The stored report hash is reproducible from outside: same rows, same config, and the
    record's created_at (the transaction time the gates were evaluated at)."""
    _, ds = _seeded(db)
    strict = STRICT
    spec = _spec(0, 20)
    record = ds.freeze(spec, strict)
    report = judge_dataset(spec, strict, record.created_at, _bars(20))
    assert report.passed
    assert record.quality.report_sha256 == report.sha256
    assert record.quality.config_sha256 == strict.sha256
    assert (record.row_count, record.sha256) == (report.digest.rows, report.digest.sha256)


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_freeze_refuses_a_failing_range_and_writes_nothing(db: Db) -> None:
    md = PostgresMarketDataStore(db.engine("svc_market_data"))
    bars = _bars(60)
    md.insert([*bars[:10], *bars[30:]], SRC)  # a 20-minute hole
    ds = _store(db, "svc_market_data")
    with pytest.raises(DatasetError, match=r"FAIL.*completeness_below_min, gap_above_max"):
        ds.freeze(_spec(0, 59), quality_config())
    assert _snapshots(db) == 0
    md.insert([candle("EUR_USD", MON + timedelta(minutes=60))], SRC)  # range not sealed


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_freeze_refuses_an_unapproved_configuration(db: Db) -> None:
    _, ds = _seeded(db)
    qc_v1 = load_quality_config(QUALITY_CONFIG_DIR / "qc_v1.yaml")  # PROVISIONAL / UNCALIBRATED
    with pytest.raises(DatasetError, match="PROVISIONAL_UNCALIBRATED"):
        ds.freeze(_spec(0, 20), qc_v1)
    with pytest.raises(DatasetError, match="quality configuration is required"):
        ds.freeze(_spec(0, 20), None)  # type: ignore[arg-type]
    assert _snapshots(db) == 0


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_load_refuses_a_snapshot_whose_quality_configuration_it_does_not_hold(db: Db) -> None:
    """Never re-judged under whatever configuration is current: only under its own."""
    _, ds = _seeded(db)
    record = ds.freeze(_spec(0, 20), STRICT)
    for configs in ({}, {PASS.sha256: PASS}):
        reader = PostgresDatasetStore(db.engine("svc_learning"), quality_configs=configs)
        with pytest.raises(DatasetError, match="not available"):
            reader.load(record.snapshot_id)
    assert len(_store(db, "svc_learning").load(record.snapshot_id)) == 20


# ----------------------------------------------------------------------------- freeze fails closed


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
@pytest.mark.parametrize(
    ("spec_args", "match"),
    [
        ((0, 31, {}), "newest stored bar"),  # end beyond the newest bar (29)
        ((0, 20, {"source": "dukascopy"}), "source mismatch"),
        ((0, 20, {"symbol": "USD_JPY"}), "not registered"),
        ((0, 20, {"timeframe": Timeframe.H1}), "not registered"),
    ],
)
def test_freeze_refuses_inconsistent_requests(
    db: Db, spec_args: tuple[int, int, dict[str, object]], match: str
) -> None:
    _, ds = _seeded(db)
    start, end, kw = spec_args
    with pytest.raises(DatasetError, match=match):
        ds.freeze(_spec(start, end, **kw), PASS)
    assert _snapshots(db) == 0


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
def test_freeze_refuses_an_empty_range_and_duplicates(db: Db) -> None:
    md, ds = _seeded(db, 10)
    md.insert(_bars(5, MON + timedelta(hours=2)), SRC)  # a gap from minute 10 to 120
    with pytest.raises(DatasetError, match="empty"):
        ds.freeze(_spec(30, 60), PASS)
    ds.freeze(_spec(0, 10), PASS)
    with pytest.raises(DatasetError, match="already"):
        ds.freeze(_spec(0, 10), PASS)
    assert _snapshots(db) == 1


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
@pytest.mark.parametrize(
    ("column", "value", "match"),
    [
        ("row_count", "19", "row count"),
        ("source", "'dukascopy'", "source"),
        ("range_end", "'2015-01-05T01:00:00Z'", "newest stored bar"),
        ("quality_verdict", "'FAIL'", "check constraint"),
        ("format", "'sentinel-dataset/v0'", "check constraint"),
    ],
)
def test_the_database_rechecks_snapshots_written_without_the_store(
    db: Db, column: str, value: str, match: str
) -> None:
    _seeded(db)
    good = {
        "format": "'sentinel-dataset/v1'",
        "symbol": "'EUR_USD'",
        "timeframe": "'M1'",
        "source": f"'{SRC}'",
        "range_start": "'2015-01-05T00:00:00Z'",
        "range_end": "'2015-01-05T00:20:00Z'",
        "row_count": "20",
        "rows_sha256": f"'{'c' * 64}'",
        "sha256": f"'{'d' * 64}'",
        "quality_verdict": "'PASS'",
        "quality_report_sha256": f"'{'a' * 64}'",
        "quality_config_sha256": f"'{'b' * 64}'",
    }
    good[column] = value
    cols = ", ".join(["snapshot_id", *good])
    vals = ", ".join([f"'{uuid.uuid4()}'", *good.values()])
    with pytest.raises(DBAPIError, match=match), db.engine("svc_market_data").begin() as conn:
        conn.execute(text(f"INSERT INTO sentinel.dataset_snapshots ({cols}) VALUES ({vals})"))
    assert _snapshots(db) == 0


# ----------------------------------------------------------------------------- tamper evidence


TAMPERING = [
    ("UPDATE sentinel.market_candles SET bid_c = bid_c + 0.00001 "
     "WHERE ts = '2015-01-05T00:07:00Z'", "hash mismatch"),
    ("UPDATE sentinel.market_candles SET tick_volume = tick_volume + 1 "
     "WHERE ts = '2015-01-05T00:07:00Z'", "hash mismatch"),
    ("DELETE FROM sentinel.spreads WHERE ts = '2015-01-05T00:07:00Z'; "
     "DELETE FROM sentinel.market_candles WHERE ts = '2015-01-05T00:07:00Z'", "row count"),
    ("INSERT INTO sentinel.market_candles SELECT symbol, timeframe, ts + interval '30 seconds', "
     "bid_o, bid_h, bid_l, bid_c, ask_o, ask_h, ask_l, ask_c, tick_volume, source, ingested_at "
     "FROM sentinel.market_candles WHERE ts = '2015-01-05T00:07:00Z'", "row count"),
    ("UPDATE sentinel.market_candles SET source = 'other' "
     "WHERE ts = '2015-01-05T00:07:00Z'", "source mismatch"),
    ("UPDATE sentinel.dataset_snapshots SET source = 'other'", "source mismatch"),
    ("UPDATE sentinel.market_series SET source = 'other'", "source mismatch"),
    ("UPDATE sentinel.dataset_snapshots SET range_end = range_end - interval '1 minute', "
     "row_count = row_count - 1", "hash mismatch"),
    ("UPDATE sentinel.dataset_snapshots SET sha256 = repeat('e', 64)", "hash mismatch"),
    ("UPDATE sentinel.dataset_snapshots SET quality_report_sha256 = repeat('e', 64)",
     "quality report mismatch"),
    ("UPDATE sentinel.dataset_snapshots SET created_at = created_at + interval '1 second'",
     "quality report mismatch"),
    ("UPDATE sentinel.dataset_snapshots SET quality_config_sha256 = repeat('e', 64)",
     "not available"),
    ("UPDATE sentinel.dataset_snapshots SET row_count = row_count + 1", "row count"),
    ("ALTER TABLE sentinel.dataset_snapshots DROP CONSTRAINT dataset_snapshots_pass_only; "
     "UPDATE sentinel.dataset_snapshots SET quality_verdict = 'FAIL'", "PASS"),
    ("ALTER TABLE sentinel.dataset_snapshots DROP CONSTRAINT dataset_snapshots_format; "
     "UPDATE sentinel.dataset_snapshots SET format = 'sentinel-dataset/v0'", "format"),
]  # fmt: skip


@pytest.mark.invariant("INV-DATA-SNAPSHOT")
@pytest.mark.parametrize(("sql", "match"), TAMPERING, ids=range(len(TAMPERING)))
@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_any_tampering_is_detected_on_load(db: Db, sql: str, match: str) -> None:
    _, ds = _seeded(db)
    record = ds.freeze(_spec(0, 20), PASS)
    _tamper(db, *sql.split("; "))
    with pytest.raises(DatasetError, match=match):
        _store(db, "svc_learning").load(record.snapshot_id)


# ----------------------------------------------------------------------------- sealing


def _insert_candle(db: Db, role: str, ts: datetime, source: str = SRC) -> None:
    with db.engine(role).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sentinel.market_candles (symbol, timeframe, ts, bid_o, bid_h, bid_l, "
                "bid_c, ask_o, ask_h, ask_l, ask_c, tick_volume, source) VALUES "
                "('EUR_USD', 'M1', :ts, 1, 1, 1, 1, 1, 1, 1, 1, 0, :src) ON CONFLICT DO NOTHING"
            ),
            {"ts": ts, "src": source},
        )


@pytest.mark.invariant("INV-DATA-SEALED")
def test_nothing_can_be_inserted_inside_a_frozen_range(db: Db) -> None:
    md = PostgresMarketDataStore(db.engine("svc_market_data"))
    md.insert(_bars(10), SRC)
    md.insert(_bars(20, MON + timedelta(minutes=15)), SRC)  # gap at minutes 10..14
    ds = _store(db, "svc_market_data")
    ds.freeze(_spec(5, 25), PASS)

    def store_insert(bars: list[Candle]) -> Callable[[], object]:
        return lambda: md.insert(bars, SRC)

    def raw_insert(role: str, ts: datetime) -> Callable[[], None]:
        return lambda: _insert_candle(db, role, ts)

    for minute in (5, 12, 24):  # start (inclusive), inside the gap, last bar
        ts = MON + timedelta(minutes=minute)
        _raises_db(store_insert([candle("EUR_USD", ts)]), "frozen")
        for role in ("svc_market_data", OWNER_ROLE):
            _raises_db(raw_insert(role, ts), "frozen")
    # re-sending identical existing bars fails loudly too; nothing is silently skipped
    _raises_db(store_insert(_bars(3, MON + timedelta(minutes=6))), "frozen")
    # outside the range: allowed (before start, at the exclusive end, after)
    for minute in (4, 25, 40):
        md.insert([candle("EUR_USD", MON + timedelta(minutes=minute))], SRC)


@pytest.mark.invariant("INV-DATA-SEALED")
def test_spreads_inside_a_frozen_range_are_refused(db: Db) -> None:
    _, ds = _seeded(db)
    ds.freeze(_spec(0, 20), PASS)

    def spread() -> None:
        with db.engine("svc_market_data").begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sentinel.spreads (symbol, timeframe, ts, spread_open, "
                    "spread_close, source) VALUES ('EUR_USD', 'M1', :ts, 0, 0, :s) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"ts": MON + timedelta(minutes=3), "s": SRC},
            )

    _raises_db(spread, "frozen")


@pytest.mark.invariant("INV-DATA-SEALED")
def test_overlapping_snapshots_are_all_sealed_and_all_verify(db: Db) -> None:
    md, ds = _seeded(db)
    a = ds.freeze(_spec(0, 20), PASS)
    b = ds.freeze(_spec(10, 29), PASS)
    for minute in (2, 15, 28):
        bar = [candle("EUR_USD", MON + timedelta(minutes=minute))]
        _raises_db(partial(md.insert, bar, SRC), "frozen")
    reader = _store(db, "svc_learning")
    assert len(reader.load(a.snapshot_id)) == 20
    assert len(reader.load(b.snapshot_id)) == 19


@pytest.mark.invariant("INV-DATA-SEALED")
def test_update_delete_truncate_are_refused_for_snapshots_and_sealed_rows(db: Db) -> None:
    _, ds = _seeded(db)
    ds.freeze(_spec(0, 20), PASS)
    owner = db.engine(OWNER_ROLE)
    for sql in (
        "UPDATE sentinel.dataset_snapshots SET row_count = 1",
        "DELETE FROM sentinel.dataset_snapshots",
        "TRUNCATE sentinel.dataset_snapshots",
        "UPDATE sentinel.market_candles SET bid_c = bid_c",
        "DELETE FROM sentinel.market_candles",
        "TRUNCATE sentinel.market_candles CASCADE",
    ):

        def run(sql: str = sql) -> None:
            with owner.begin() as conn:
                conn.execute(text(sql))

        _raises_db(run, "append-only")


# ----------------------------------------------------------------------------- locks


@pytest.mark.invariant("INV-DATA-SEALED")
def test_freeze_waits_for_an_ingestion_in_progress_and_includes_its_rows(db: Db) -> None:
    md = PostgresMarketDataStore(db.engine("svc_market_data"))
    bars = _bars(20)
    md.insert([*bars[:5], *bars[6:]], SRC)  # minute 5 arrives in the in-flight page
    ds = _store(db, "svc_market_data")
    writer = db.engine("svc_market_data").connect()
    tx = writer.begin()
    writer.execute(  # an ingestion page still in flight, inside the range about to be frozen
        text(
            "INSERT INTO sentinel.market_candles (symbol, timeframe, ts, bid_o, bid_h, bid_l, "
            "bid_c, ask_o, ask_h, ask_l, ask_c, tick_volume, source) VALUES "
            "('EUR_USD', 'M1', :ts, 1, 1, 1, 1, 1, 1, 1, 1, 0, :s)"
        ),
        {"ts": MON + timedelta(minutes=5), "s": SRC},
    )
    result: list[object] = []

    def freeze() -> None:
        try:
            result.append(ds.freeze(_spec(0, 19), PASS))
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            result.append(exc)

    thread = threading.Thread(target=freeze)
    thread.start()
    time.sleep(0.5)
    assert result == []  # blocked on the ingestion's shared lock
    tx.commit()
    writer.close()
    thread.join(timeout=30)
    [record] = result
    assert not isinstance(record, BaseException), record
    loaded = _store(db, "svc_learning").load(record.snapshot_id)  # type: ignore[attr-defined]
    assert len(loaded) == 19  # 18 committed bars + the one that was in flight
    assert MON + timedelta(minutes=5) in [c.ts for c in loaded.candles]


@pytest.mark.invariant("INV-DATA-SEALED")
def test_ingestion_waits_for_a_freeze_and_is_then_refused(db: Db) -> None:
    """Uses the store's lock key, so a drift between Python and the trigger fails this test."""
    md, _ = _seeded(db, 20)
    freezer = db.engine("svc_market_data").connect()
    tx = freezer.begin()
    PostgresDatasetStore.lock_series_exclusive(freezer, "EUR_USD", Timeframe.M1)
    freezer.execute(
        text(
            "INSERT INTO sentinel.dataset_snapshots (snapshot_id, format, symbol, timeframe, "
            "source, range_start, range_end, row_count, rows_sha256, sha256, quality_verdict, "
            "quality_report_sha256, quality_config_sha256) VALUES (:id, 'sentinel-dataset/v1', "
            "'EUR_USD', 'M1', :s, :a, :b, 10, :h, :h, 'PASS', :h, :h)"
        ),
        {
            "id": uuid.uuid4(),
            "s": SRC,
            "a": MON,
            "b": MON + timedelta(minutes=10),
            "h": "f" * 64,
        },
    )
    outcome: list[BaseException | None] = []

    def ingest_page() -> None:
        try:
            md.insert([candle("EUR_USD", MON + timedelta(minutes=3, seconds=30))], SRC)
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    thread = threading.Thread(target=ingest_page)
    thread.start()
    time.sleep(0.5)
    assert outcome == []  # waiting for the freeze
    tx.commit()
    freezer.close()
    thread.join(timeout=30)
    [exc] = outcome
    assert isinstance(exc, DBAPIError)
    assert "frozen" in str(exc.orig)
    assert SERIES_LOCK_CLASS > 0


# ----------------------------------------------------------------------------- roles


@pytest.mark.invariant("INV-DB-ROLES")
@pytest.mark.parametrize("role", [r for r in SERVICE_ROLES if r != "svc_market_data"])
def test_only_the_market_data_role_can_freeze(db: Db, role: str) -> None:
    _seeded(db)
    with pytest.raises(DBAPIError, match="permission denied"):
        _store(db, role).freeze(_spec(0, 20), PASS)
    assert _snapshots(db) == 0


# ----------------------------------------------------------------------------- benchmarks


def _report(line: str, capsys: pytest.CaptureFixture[str]) -> None:
    with capsys.disabled():
        sys.stdout.write(f"\n[bench] {line}\n")


def test_freeze_load_and_seal_overhead(db: Db, capsys: pytest.CaptureFixture[str]) -> None:
    n, batch = 20_000, 5_000
    md = PostgresMarketDataStore(db.engine("svc_market_data"))
    bars = _bars(n + 1)
    for i in range(0, n + 1, batch):
        md.insert(bars[i : i + batch], SRC)
    ds = _store(db, "svc_market_data")

    started = time.perf_counter()
    record = ds.freeze(_spec(0, n), PASS)
    freeze_s = time.perf_counter() - started
    started = time.perf_counter()
    loaded = _store(db, "svc_learning").load(record.snapshot_id)
    load_s = time.perf_counter() - started
    assert len(loaded) == n
    started = time.perf_counter()
    report = _store(db, "svc_learning").judge(_spec(0, n), PASS)
    judge_s = time.perf_counter() - started
    assert report.passed
    assert report.digest.sha256 == record.sha256
    _report(
        f"freeze {n:,} rows (quality-gated): {freeze_s:.2f}s ({n / freeze_s:,.0f} rows/s); "
        f"verified load (quality re-run): {load_s:.2f}s ({n / load_s:,.0f} rows/s); "
        f"read-only gate report: {judge_s:.2f}s ({n / judge_s:,.0f} rows/s)",
        capsys,
    )

    # Insert path after the series has a snapshot: sealing trigger off vs on, beyond the range.
    def timed(start: datetime, symbol: str = "EUR_USD") -> float:
        page = _bars(n, start, symbol)
        t0 = time.perf_counter()
        for i in range(0, n, batch):
            md.insert(page[i : i + batch], SRC)
        return time.perf_counter() - t0

    later = MON + timedelta(days=30)
    with db.engine().begin() as conn:
        for t in ("market_candles", "spreads"):
            conn.execute(text(f"ALTER TABLE sentinel.{t} DISABLE TRIGGER {t}_sealed_range"))
    without = timed(later)
    with db.engine().begin() as conn:
        for t in ("market_candles", "spreads"):
            conn.execute(text(f"ALTER TABLE sentinel.{t} ENABLE TRIGGER {t}_sealed_range"))
    with_seal = timed(later + timedelta(days=30))
    _report(
        f"seal+lock trigger on insert ({n:,} candles+spreads, pages of {batch:,}): without "
        f"{without:.2f}s, with {with_seal:.2f}s, overhead {100 * (with_seal / without - 1):+.1f}%",
        capsys,
    )
    assert with_seal / without < 3.0
