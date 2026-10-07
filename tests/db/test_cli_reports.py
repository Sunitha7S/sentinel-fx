"""CLI: ingest (with a fake OANDA server), data-quality and security reports."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from sentinel.cli import main
from sentinel.perception.market_data import oanda
from sentinel.store.postgres.market_data_store import PostgresMarketDataStore

from db.conftest import Db
from support.market import FakeOanda, candle, minutes

MON = datetime(2015, 1, 5, tzinfo=UTC)


@pytest.fixture
def fake_oanda(monkeypatch: pytest.MonkeyPatch) -> FakeOanda:
    fake = FakeOanda(minutes(MON, 30))
    real = oanda.OandaProvider

    def factory(token: str, **_: object) -> oanda.OandaProvider:
        return real(token, transport=fake.transport(), sleep=lambda _: None, min_interval_s=0)

    monkeypatch.setattr(oanda, "OandaProvider", factory)
    return fake


def test_ingest_then_reports(
    db: Db,
    fake_oanda: FakeOanda,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SENTINEL_DATABASE_URL", db.url)
    monkeypatch.setenv("SENTINEL_OANDA_TOKEN", "test-token")
    end = (MON + timedelta(minutes=30)).isoformat()
    assert (
        main(
            [
                "ingest",
                "--symbols",
                "EUR_USD",
                "--timeframes",
                "M1",
                "--from",
                MON.isoformat(),
                "--to",
                end,
            ]
        )
        == 0
    )
    assert "SUCCEEDED EUR_USD M1: received 30, inserted 30" in capsys.readouterr().out
    assert all(r.method == "GET" for r in fake_oanda.requests)

    out = tmp_path / "quality.md"
    assert main(["data-report", "--output", str(out)]) == 0
    report = out.read_text(encoding="utf-8")
    assert "| EUR_USD | M1 | 30 |" in report
    assert "100.000%" in report
    assert "oanda-practice" in report

    assert main(["db-security-report"]) == 0
    security = capsys.readouterr().out
    assert "Matrix drift: **none**" in security
    assert "Object owners in schema `sentinel`: sentinel_owner" in security
    assert "| `decisions` | decisions_no_truncate, decisions_no_update_delete, " in security
    assert "audit_records_extend_head" in security
    assert "—" not in security


def test_ingest_without_configuration_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], db: Db
) -> None:
    monkeypatch.delenv("SENTINEL_DATABASE_URL", raising=False)
    assert main(["ingest"]) == 2
    assert main(["data-report"]) == 2
    monkeypatch.setenv("SENTINEL_DATABASE_URL", db.url)
    monkeypatch.delenv("SENTINEL_OANDA_TOKEN", raising=False)
    assert main(["ingest"]) == 2
    assert "token" in capsys.readouterr().err


def test_empty_database_report(
    db: Db, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SENTINEL_DATABASE_URL", db.url)
    assert main(["data-report"]) == 0
    assert "No market data stored." in capsys.readouterr().out


# ----------------------------------------------------------------------------- data-gate


@pytest.mark.invariant("INV-DATA-QUALITY-GATED")
def test_data_gate_reports_and_exits_by_verdict(
    db: Db, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    md = PostgresMarketDataStore(db.engine("svc_market_data"))
    bars = [candle("EUR_USD", t) for t in minutes(MON, 90)]
    md.insert([*bars[:60], *bars[80:]], "oanda-practice")  # minutes 60..79 missing
    monkeypatch.setenv("SENTINEL_DATABASE_URL", db.url)

    def gate(*extra: str, start: int = 0, end: int = 60) -> int:
        return main(
            [
                "data-gate",
                "--symbol",
                "EUR_USD",
                "--timeframe",
                "M1",
                "--from",
                (MON + timedelta(minutes=start)).isoformat(),
                "--to",
                (MON + timedelta(minutes=end)).isoformat(),
                *extra,
            ]
        )

    json_out, md_out = tmp_path / "gate.json", tmp_path / "gate.md"
    assert gate("--json", str(json_out), "--output", str(md_out)) == 0
    verdict, digest = capsys.readouterr().err.split()
    assert verdict == "PASS"
    assert hashlib.sha256(json_out.read_bytes()).hexdigest() == digest  # the hashed bytes
    body = json.loads(json_out.read_text(encoding="ascii"))
    assert body["result"]["verdict"] == "PASS"
    assert body["config"]["status"] == "PROVISIONAL_UNCALIBRATED"
    rendered = md_out.read_text(encoding="utf-8")
    assert "**Verdict: PASS**" in rendered
    assert "PROVISIONAL_UNCALIBRATED" in rendered
    assert digest in rendered

    assert gate(end=89) == 1  # the 20-minute hole fails completeness and the gap gate
    out = capsys.readouterr()
    assert "**Verdict: FAIL**" in out.out
    assert "gap_above_max" in out.out
    assert out.err.startswith("FAIL ")

    bad = tmp_path / "bad.yaml"
    bad.write_text("version: [", encoding="utf-8")
    assert gate("--config", str(bad)) == 2
    assert gate("--source", "dukascopy") == 3  # not the registered source
    assert gate(start=200, end=300) == 3  # nothing stored there: no dataset to judge
    monkeypatch.delenv("SENTINEL_DATABASE_URL")
    assert gate() == 2
    with db.engine().connect() as conn:  # read-only: nothing was frozen
        assert conn.execute(text("SELECT count(*) FROM sentinel.dataset_snapshots")).scalar() == 0
