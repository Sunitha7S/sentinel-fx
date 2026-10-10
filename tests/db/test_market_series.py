"""Single-source series registry (M2.1 step 1, INV-MD-SINGLE-SOURCE, ADR 0014).

A market-data series (symbol, timeframe) is registered to the source that first wrote it.
Rows from any other source are refused by the database, for every role including the owner.
The registry is written only by an owner-controlled trigger; no service role can write it.
"""

from __future__ import annotations

import sys
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from psycopg import errors as pg
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from sentinel.domain.market_data import Timeframe
from sentinel.perception.market_data.ingest import ingest
from sentinel.store.postgres.engine import OWNER_ROLE, SERVICE_ROLES, role_engine
from sentinel.store.postgres.market_data_store import PostgresMarketDataStore

from db.conftest import REPO_ROOT, Db, _admin, _with_database
from support.market import ListProvider, candle, minutes

MON = datetime(2015, 1, 5, tzinfo=UTC)
CLOCK = lambda: datetime(2026, 10, 5, tzinfo=UTC)  # noqa: E731
TRIGGER_FN = "sentinel.enforce_series_source()"


def _store(db: Db, role: str = "svc_market_data") -> PostgresMarketDataStore:
    return PostgresMarketDataStore(db.engine(role))


def _registry(db: Db) -> list[tuple[str, str, str]]:
    with db.engine().connect() as conn:
        rows = conn.execute(
            text("SELECT symbol, timeframe, source FROM sentinel.market_series ORDER BY 1, 2")
        ).all()
    return [(r.symbol, r.timeframe, r.source) for r in rows]


def _count(db: Db, table: str = "market_candles") -> int:
    with db.engine().connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM sentinel.{table}")).scalar_one())


def _refused(fn: object, match: str = "registered to source") -> None:
    with pytest.raises(DBAPIError) as exc:
        fn()  # type: ignore[operator]
    assert isinstance(exc.value.orig, pg.RaiseException), exc.value.orig
    assert match in str(exc.value.orig)


# ----------------------------------------------------------------------------- registration


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_first_insert_registers_the_series_to_its_source(db: Db) -> None:
    store = _store(db)
    assert store.registered_source("EUR_USD", Timeframe.M1) is None
    store.insert([candle("EUR_USD", t) for t in minutes(MON, 3)], "oanda-practice")
    assert _registry(db) == [("EUR_USD", "M1", "oanda-practice")]
    assert store.registered_source("EUR_USD", Timeframe.M1) == "oanda-practice"
    # same source again: idempotent, still one registration
    store.insert([candle("EUR_USD", t) for t in minutes(MON, 5)], "oanda-practice")
    assert _registry(db) == [("EUR_USD", "M1", "oanda-practice")]
    assert _count(db) == 5


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_series_are_independent(db: Db) -> None:
    store = _store(db)
    store.insert([candle("EUR_USD", MON)], "a")
    store.insert([candle("USD_JPY", MON, mid="150.00")], "b")
    store.insert([candle("EUR_USD", MON, tf=Timeframe.H1)], "b")
    assert _registry(db) == [("EUR_USD", "H1", "b"), ("EUR_USD", "M1", "a"), ("USD_JPY", "M1", "b")]


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_a_second_source_is_refused_for_new_and_existing_bars(db: Db) -> None:
    store = _store(db)
    store.insert([candle("EUR_USD", t) for t in minutes(MON, 3)], "oanda-practice")
    # existing bars (previously skipped silently by ON CONFLICT DO NOTHING) and new bars
    _refused(lambda: store.insert([candle("EUR_USD", MON)], "dukascopy"))
    _refused(lambda: store.insert([candle("EUR_USD", MON + timedelta(minutes=10))], "dukascopy"))
    assert _count(db) == 3
    assert _count(db, "spreads") == 3
    assert _registry(db) == [("EUR_USD", "M1", "oanda-practice")]


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_a_refused_batch_writes_nothing(db: Db) -> None:
    store = _store(db)
    store.insert([candle("EUR_USD", MON)], "a")
    mixed = [candle("USD_JPY", MON, mid="150.00"), candle("EUR_USD", MON + timedelta(minutes=1))]
    _refused(lambda: store.insert(mixed, "b"))
    assert _registry(db) == [("EUR_USD", "M1", "a")]  # USD_JPY registration rolled back
    assert _count(db) == 1


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_the_owner_is_refused_too(db: Db) -> None:
    _store(db).insert([candle("EUR_USD", MON)], "a")
    owner = db.engine(OWNER_ROLE)

    def insert_as_owner() -> None:
        with owner.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sentinel.market_candles (symbol, timeframe, ts, bid_o, bid_h, "
                    "bid_l, bid_c, ask_o, ask_h, ask_l, ask_c, tick_volume, source) VALUES "
                    "('EUR_USD', 'M1', :ts, 1, 1, 1, 1, 1, 1, 1, 1, 0, 'b')"
                ),
                {"ts": MON + timedelta(minutes=5)},
            )

    _refused(insert_as_owner)


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_spreads_must_carry_the_series_source(db: Db) -> None:
    _store(db).insert([candle("EUR_USD", MON)], "a")
    engine = db.engine("svc_market_data")

    def spread(source: str, ts: datetime) -> None:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sentinel.spreads (symbol, timeframe, ts, spread_open, "
                    "spread_close, source) VALUES ('EUR_USD', 'M1', :ts, 0.0001, 0.0001, :s) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"ts": ts, "s": source},
            )

    _refused(lambda: spread("b", MON))
    spread("a", MON)  # same source, existing row: skipped, not refused


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_concurrent_first_writers_cannot_register_two_sources(db: Db) -> None:
    """Two ingestions racing to register the same new series: exactly one source wins."""
    first = db.engine("svc_market_data").connect()
    tx = first.begin()
    first.execute(
        text(
            "INSERT INTO sentinel.market_candles (symbol, timeframe, ts, bid_o, bid_h, bid_l, "
            "bid_c, ask_o, ask_h, ask_l, ask_c, tick_volume, source) VALUES "
            "('EUR_USD', 'M1', :ts, 1, 1, 1, 1, 1, 1, 1, 1, 0, 'a')"
        ),
        {"ts": MON},
    )
    outcome: list[BaseException | None] = []

    def second_writer() -> None:
        try:
            _store(db).insert([candle("EUR_USD", MON + timedelta(minutes=1))], "b")
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            outcome.append(exc)

    thread = threading.Thread(target=second_writer)
    thread.start()
    time.sleep(0.5)  # the second writer is now waiting on the first one's registration
    assert outcome == []
    tx.commit()
    first.close()
    thread.join(timeout=30)
    assert len(outcome) == 1
    assert isinstance(outcome[0], DBAPIError)
    assert isinstance(outcome[0].orig, pg.RaiseException)
    assert _registry(db) == [("EUR_USD", "M1", "a")]


# ----------------------------------------------------------------------------- privilege escalation


@pytest.mark.invariant("INV-DB-ROLES")
@pytest.mark.parametrize("role", SERVICE_ROLES)
def test_no_service_role_can_write_the_registry(db: Db, role: str) -> None:
    _store(db).insert([candle("EUR_USD", MON)], "a")
    engine = db.engine(role)
    for sql in (
        "INSERT INTO sentinel.market_series (symbol, timeframe, source) "
        "VALUES ('USD_JPY', 'M1', 'evil')",
        "UPDATE sentinel.market_series SET source = 'evil'",
        "DELETE FROM sentinel.market_series",
        "TRUNCATE sentinel.market_series",
        "ALTER TABLE sentinel.market_series DISABLE TRIGGER ALL",
        "ALTER TABLE sentinel.market_candles DISABLE TRIGGER ALL",
        "DROP TRIGGER market_candles_series_source ON sentinel.market_candles",
        f"SELECT {TRIGGER_FN}",
        f"ALTER FUNCTION {TRIGGER_FN} SECURITY INVOKER",
        "CREATE OR REPLACE FUNCTION sentinel.enforce_series_source() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
    ):
        with pytest.raises(DBAPIError) as exc, engine.begin() as conn:
            conn.execute(text(sql))
        assert isinstance(exc.value.orig, pg.InsufficientPrivilege | pg.FeatureNotSupported), (
            role,
            sql,
            exc.value.orig,
        )
    assert _registry(db) == [("EUR_USD", "M1", "a")]


@pytest.mark.invariant("INV-DB-ROLES")
def test_a_temporary_table_cannot_shadow_the_registry(db: Db) -> None:
    """The trigger runs with a fixed search_path, so a session-local look-alike is ignored."""
    _store(db).insert([candle("EUR_USD", MON)], "a")
    engine = db.engine("svc_market_data")

    def attack() -> None:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL search_path = pg_temp, sentinel, public"))
            conn.execute(
                text("CREATE TEMP TABLE market_series (symbol text, timeframe text, source text)")
            )
            conn.execute(text("INSERT INTO market_series VALUES ('EUR_USD', 'M1', 'evil')"))
            conn.execute(
                text(
                    "INSERT INTO sentinel.market_candles (symbol, timeframe, ts, bid_o, bid_h, "
                    "bid_l, bid_c, ask_o, ask_h, ask_l, ask_c, tick_volume, source) VALUES "
                    "('EUR_USD', 'M1', :ts, 1, 1, 1, 1, 1, 1, 1, 1, 0, 'evil')"
                ),
                {"ts": MON + timedelta(minutes=1)},
            )

    _refused(attack)
    assert _registry(db) == [("EUR_USD", "M1", "a")]


@pytest.mark.invariant("INV-DB-ROLES")
def test_security_definer_functions_are_owned_pinned_and_not_executable(db: Db) -> None:
    with db.engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT p.oid::regprocedure::text AS sig, pg_get_userbyid(p.proowner) AS owner, "
                "p.proconfig AS config FROM pg_proc p JOIN pg_namespace n "
                "ON n.oid = p.pronamespace WHERE n.nspname = 'sentinel' AND p.prosecdef"
            )
        ).all()
        signatures = {r.sig for r in rows}
        assert {"sentinel.advance_audit_head()", "sentinel.enforce_series_source()"} <= signatures
        for r in rows:
            assert r.owner == OWNER_ROLE, r.sig
            assert r.config is not None, r.sig
            assert "search_path=sentinel, pg_temp" in r.config, (r.sig, r.config)
            for role in SERVICE_ROLES:
                assert not conn.execute(
                    text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                    {"r": role, "f": r.sig},
                ).scalar_one(), (role, r.sig)


# ----------------------------------------------------------------------------- ingestion


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_ingestion_from_another_source_fails_before_fetching(db: Db) -> None:
    store = _store(db)
    store.insert([candle("EUR_USD", MON)], "oanda-practice")
    provider = ListProvider("dukascopy", [[candle("EUR_USD", t) for t in minutes(MON, 5)]])
    run = ingest(
        provider,
        store,
        symbol="EUR_USD",
        timeframe=Timeframe.M1,
        start=MON,
        end=MON + timedelta(hours=1),
        clock=CLOCK,
    )
    assert run.status == "FAILED"
    assert run.error is not None
    assert "oanda-practice" in run.error
    assert provider.calls == 0
    assert _count(db) == 1


# ----------------------------------------------------------------------------- migration


def _alembic(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture
def at_0005(admin_url: str) -> Iterator[str]:
    """A fresh database migrated only up to M2 (before the registry existed)."""
    name = f"sentinel_m_{uuid.uuid4().hex[:10]}"
    admin = _admin(admin_url)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = _with_database(admin_url, name)
    command.upgrade(_alembic(url), "0005")
    yield url
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _seed(url: str, rows: list[tuple[str, str, str]]) -> None:
    engine = role_engine(url)
    with engine.begin() as conn:
        for i, (symbol, tf, source) in enumerate(rows):
            conn.execute(
                text(
                    "INSERT INTO sentinel.market_candles (symbol, timeframe, ts, bid_o, bid_h, "
                    "bid_l, bid_c, ask_o, ask_h, ask_l, ask_c, tick_volume, source) VALUES "
                    "(:s, :tf, :ts, 1, 1, 1, 1, 1, 1, 1, 1, 0, :src)"
                ),
                {"s": symbol, "tf": tf, "ts": MON + timedelta(minutes=i), "src": source},
            )
    engine.dispose()


def test_migration_backfills_the_registry_from_existing_history(at_0005: str) -> None:
    _seed(at_0005, [("EUR_USD", "M1", "a"), ("EUR_USD", "M1", "a"), ("USD_JPY", "H1", "b")])
    command.upgrade(_alembic(at_0005), "head")
    engine = role_engine(at_0005)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT symbol, timeframe, source FROM sentinel.market_series ORDER BY 1")
        ).all()
    engine.dispose()
    assert [tuple(r) for r in rows] == [("EUR_USD", "M1", "a"), ("USD_JPY", "H1", "b")]


@pytest.mark.invariant("INV-MD-SINGLE-SOURCE")
def test_migration_refuses_history_that_already_mixes_sources(at_0005: str) -> None:
    _seed(at_0005, [("EUR_USD", "M1", "a"), ("EUR_USD", "M1", "b")])
    with pytest.raises(DBAPIError, match="more than one source"):
        command.upgrade(_alembic(at_0005), "head")


# ----------------------------------------------------------------------------- performance


def _timed_insert(store: PostgresMarketDataStore, symbol: str, n: int, batch: int) -> float:
    mid = "150.00" if symbol.endswith("JPY") else "1.1000"
    bars = [candle(symbol, t, mid=mid) for t in minutes(MON, n)]
    started = time.perf_counter()
    for i in range(0, n, batch):
        store.insert(bars[i : i + batch], "bench")
    return time.perf_counter() - started


def test_registry_trigger_overhead_on_bulk_insert(
    db: Db, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bulk insert with and without the registry trigger, as the ingest path writes pages.

    Reports the measured overhead in the CI log. The bound is deliberately loose: it catches a
    pathological trigger (e.g. a scan per row), not normal variance between runners.
    """
    n, batch = 20_000, 5_000
    store = _store(db)
    superuser = db.engine()
    _timed_insert(store, "GBP_USD", 2_000, batch)  # warm-up: caches, plans, registry row
    with superuser.begin() as conn:
        for t in ("market_candles", "spreads"):
            conn.execute(text(f"ALTER TABLE sentinel.{t} DISABLE TRIGGER {t}_series_source"))
    without = _timed_insert(store, "EUR_USD", n, batch)
    with superuser.begin() as conn:
        for t in ("market_candles", "spreads"):
            conn.execute(text(f"ALTER TABLE sentinel.{t} ENABLE TRIGGER {t}_series_source"))
    with_trigger = _timed_insert(store, "USD_JPY", n, batch)
    ratio = with_trigger / without
    with capsys.disabled():
        sys.stdout.write(
            f"\n[bench] registry trigger: {n:,} candles+spreads in pages of {batch:,}: "
            f"without {without:.2f}s ({n / without:,.0f} rows/s), with {with_trigger:.2f}s "
            f"({n / with_trigger:,.0f} rows/s), overhead {100 * (ratio - 1):+.1f}%\n"
        )
    assert ratio < 3.0
