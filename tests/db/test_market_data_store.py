"""Market-data ingestion into PostgreSQL: idempotence, spreads, roles, append-only, quality."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from sentinel.domain.market_data import Timeframe
from sentinel.perception.market_data.ingest import ingest
from sentinel.perception.market_data.oanda import OandaProvider
from sentinel.perception.market_data.quality import analyse_fixed_grid
from sentinel.store.postgres.engine import OWNER_ROLE
from sentinel.store.postgres.market_data_store import PostgresMarketDataStore

from db.conftest import Db
from support.market import FakeOanda, candle, minutes

MON = datetime(2015, 1, 5, tzinfo=UTC)
CLOCK = lambda: datetime(2026, 10, 4, tzinfo=UTC)  # noqa: E731


def test_insert_is_idempotent_and_stores_per_bar_spreads(db: Db) -> None:
    store = PostgresMarketDataStore(db.engine("svc_market_data"))
    bars = [candle("EUR_USD", t) for t in minutes(MON, 5)]
    assert store.insert(bars, "test") == 5
    assert store.insert(bars, "test") == 0
    assert store.insert([], "test") == 0
    assert store.latest_ts("EUR_USD", Timeframe.M1) == MON + timedelta(minutes=4)
    assert store.latest_ts("USD_JPY", Timeframe.M1) is None
    stats = store.spread_stats("EUR_USD", Timeframe.M1)
    assert stats["n"] == 5
    assert Decimal(str(stats["median"])) == Decimal("0.0001")
    [summary] = store.summaries()
    assert (summary.symbol, summary.count, summary.first) == ("EUR_USD", 5, MON)


def test_end_to_end_ingestion_from_a_fake_oanda_server(db: Db) -> None:
    times = [t for i, t in enumerate(minutes(MON, 60)) if i not in (10, 11, 12)]
    fake = FakeOanda(times)
    provider = OandaProvider(
        "t", transport=fake.transport(), sleep=lambda _: None, min_interval_s=0, page_size=20
    )
    store = PostgresMarketDataStore(db.engine("svc_market_data"))
    run = ingest(
        provider,
        store,
        symbol="EUR_USD",
        timeframe=Timeframe.M1,
        start=MON,
        end=MON + timedelta(minutes=60),
        clock=CLOCK,
    )
    assert run.status == "SUCCEEDED"
    assert run.candles_inserted == 57
    [stored_run] = PostgresMarketDataStore(db.engine("svc_learning")).runs()
    assert stored_run.candles_inserted == 57
    assert stored_run.provider == "oanda-practice"
    q = analyse_fixed_grid(
        store.timestamps("EUR_USD", Timeframe.M1, MON, MON + timedelta(minutes=60)),
        Timeframe.M1,
        MON,
        MON + timedelta(minutes=60),
    )
    assert (q.expected, q.present, q.missing) == (60, 57, 3)
    assert q.gaps[0].start == MON + timedelta(minutes=10)


def test_market_data_is_append_only_and_written_only_by_its_role(db: Db) -> None:
    PostgresMarketDataStore(db.engine("svc_market_data")).insert([candle("EUR_USD", MON)], "t")
    for role in ("svc_risk", "svc_learning", "svc_execution", "svc_audit", "human_admin"):
        with pytest.raises(DBAPIError, match="permission denied"):
            PostgresMarketDataStore(db.engine(role)).insert(
                [candle("EUR_USD", MON + timedelta(minutes=1))], "t"
            )
    owner = db.engine(OWNER_ROLE)
    for sql in (
        "UPDATE sentinel.market_candles SET bid_c = bid_c",
        "UPDATE sentinel.spreads SET spread_close = 0",
        "DELETE FROM sentinel.spreads",
        "DELETE FROM sentinel.market_candles",
    ):
        with pytest.raises(DBAPIError, match="append-only"), owner.begin() as conn:
            conn.execute(text(sql))


def test_database_rejects_inconsistent_candles_even_if_application_checks_are_bypassed(
    db: Db,
) -> None:
    with (
        pytest.raises(DBAPIError, match="check constraint"),
        db.engine("svc_market_data").begin() as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO sentinel.market_candles (symbol, timeframe, ts, bid_o, bid_h, bid_l, "
                "bid_c, ask_o, ask_h, ask_l, ask_c, tick_volume, source) VALUES "
                "('EUR_USD', 'M1', now(), 1.1, 1.0, 1.0, 1.1, 1.1, 1.2, 1.0, 1.1, 1, 'x')"
            )
        )
