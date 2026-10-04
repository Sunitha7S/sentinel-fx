"""CLI: ingest (with a fake OANDA server), data-quality and security reports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sentinel.cli import main
from sentinel.perception.market_data import oanda

from db.conftest import Db
from support.market import FakeOanda, minutes

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
