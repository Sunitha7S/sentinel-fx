"""Provider connectivity check (M2.1 step 0): verdicts, fail-closed defaults, CLI exit codes."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sentinel import cli
from sentinel.domain.market_data import Timeframe
from sentinel.perception.market_data import connectivity, oanda
from sentinel.perception.market_data.connectivity import (
    DEFAULT_PROBES,
    CheckStatus,
    Probe,
    check_provider,
    render_check,
)
from sentinel.perception.market_data.dukascopy import DukascopyProvider
from sentinel.perception.market_data.oanda import OandaProvider
from sentinel.perception.market_data.provider import ProviderAccessDenied, ProviderUnavailable
from sentinel.risk.sessions import market_closed

from support.market import FakeOanda, minutes

TOKEN = "practice-token-SECRET"  # noqa: S105 - fake value for tests
T = datetime(2024, 1, 10, 14, 0, tzinfo=UTC)  # a Wednesday, London/New York overlap
NEW_YORK = ZoneInfo("America/New_York")


def provider(fake: FakeOanda) -> OandaProvider:
    return OandaProvider(TOKEN, transport=fake.transport(), sleep=lambda _: None, min_interval_s=0)


def m1_probe(min_bars: int = 55) -> Probe:
    return Probe("EUR_USD", Timeframe.M1, T, T + timedelta(hours=1), min_bars, "recent M1")


def d1_times(first_ny_date: date, n: int) -> list[datetime]:
    """D1 bars open at 17:00 New York on the previous calendar day."""
    out: list[datetime] = []
    day = first_ny_date
    while len(out) < n:
        if day.weekday() < 5:
            open_ny = datetime(day.year, day.month, day.day, 17, tzinfo=NEW_YORK) - timedelta(1)
            out.append(open_ny.astimezone(UTC))
        day += timedelta(days=1)
    return out


# ----------------------------------------------------------------------------- verdicts


def test_all_probes_satisfied_gives_ok() -> None:
    check = check_provider(provider(FakeOanda(minutes(T, 60))), [m1_probe()])
    assert check.verdict is CheckStatus.OK
    [result] = check.results
    assert result.bars == 60
    assert (result.first_ts, result.last_ts) == (T, T + timedelta(minutes=59))


def test_too_few_bars_is_insufficient_not_ok() -> None:
    check = check_provider(provider(FakeOanda(minutes(T, 20))), [m1_probe(min_bars=55)])
    assert check.verdict is CheckStatus.INSUFFICIENT_DATA
    assert "20" in check.results[0].detail


def test_an_empty_window_is_insufficient() -> None:
    check = check_provider(provider(FakeOanda([])), [m1_probe()])
    assert check.verdict is CheckStatus.INSUFFICIENT_DATA


def test_no_probes_is_never_ok() -> None:
    check = check_provider(provider(FakeOanda([])), [])
    assert check.verdict is CheckStatus.ERROR


def test_rejected_candles_make_the_probe_unexpected() -> None:
    times = minutes(T, 60)
    fake = FakeOanda(times, crossed={times[3]})
    check = check_provider(provider(fake), [m1_probe(min_bars=50)])
    assert check.verdict is CheckStatus.UNEXPECTED_DATA
    assert check.results[0].rejected == 1


@pytest.mark.parametrize("status", [401, 403])
def test_access_denied_stops_immediately_and_names_the_status(status: int) -> None:
    fake = FakeOanda(minutes(T, 60), fail_first=[status] * 5)
    check = check_provider(provider(fake), [m1_probe(), m1_probe()])
    assert check.verdict is CheckStatus.ACCESS_DENIED
    assert len(check.results) == 1  # the second probe was never attempted
    assert len(fake.requests) == 1
    assert str(status) in check.results[0].detail
    assert TOKEN not in render_check(check)


def test_access_denied_carries_the_http_status() -> None:
    fake = FakeOanda([], fail_first=[403])
    with pytest.raises(ProviderAccessDenied) as exc:
        list(provider(fake).fetch_candles("EUR_USD", Timeframe.M1, T, T + timedelta(hours=1)))
    assert exc.value.status_code == 403
    assert isinstance(exc.value, ProviderUnavailable)  # existing handlers still apply


def test_server_errors_are_errors() -> None:
    fake = FakeOanda(minutes(T, 60), fail_first=[500] * 10)
    p = OandaProvider(
        TOKEN, transport=fake.transport(), sleep=lambda _: None, min_interval_s=0, max_retries=1
    )
    check = check_provider(p, [m1_probe()])
    assert check.verdict is CheckStatus.ERROR


def test_an_unimplemented_provider_is_an_error_not_access_denied() -> None:
    check = check_provider(DukascopyProvider(), [m1_probe()])
    assert check.verdict is CheckStatus.ERROR


def test_misaligned_daily_bars_are_unexpected() -> None:
    start = datetime(2015, 1, 4, tzinfo=UTC)
    probe = Probe("EUR_USD", Timeframe.D1, start, start + timedelta(days=6), 4, "D1 depth")
    good = d1_times(date(2015, 1, 5), 5)
    assert check_provider(provider(FakeOanda(good)), [probe]).verdict is CheckStatus.OK
    midnight = [datetime(2015, 1, d, tzinfo=UTC) for d in (5, 6, 7, 8, 9)]
    check = check_provider(provider(FakeOanda(midnight)), [probe])
    assert check.verdict is CheckStatus.UNEXPECTED_DATA
    assert "17:00 New York" in check.results[0].detail


def test_worst_result_decides_the_verdict() -> None:
    assert connectivity.combine([CheckStatus.OK, CheckStatus.INSUFFICIENT_DATA]) is (
        CheckStatus.INSUFFICIENT_DATA
    )
    assert connectivity.combine([CheckStatus.UNEXPECTED_DATA, CheckStatus.ERROR]) is (
        CheckStatus.ERROR
    )
    assert connectivity.combine([CheckStatus.ERROR, CheckStatus.ACCESS_DENIED]) is (
        CheckStatus.ACCESS_DENIED
    )
    assert connectivity.combine([]) is CheckStatus.ERROR


# ----------------------------------------------------------------------------- default probes


def test_default_probes_cover_both_pairs_recent_and_2015_depth() -> None:
    symbols = {p.symbol for p in DEFAULT_PROBES}
    assert symbols == {"EUR_USD", "USD_JPY"}
    for symbol in symbols:
        mine = [p for p in DEFAULT_PROBES if p.symbol == symbol]
        assert any(p.timeframe is Timeframe.M1 and p.start.year == 2015 for p in mine)
        assert any(p.timeframe is Timeframe.M1 and p.start.year >= 2024 for p in mine)
        assert any(p.timeframe is Timeframe.D1 and p.start.year == 2015 for p in mine)


def test_default_probe_windows_are_fixed_open_market_history() -> None:
    for p in DEFAULT_PROBES:
        assert p.start.tzinfo is UTC
        assert p.end > p.start
        assert p.end < datetime(2026, 1, 1, tzinfo=UTC)  # fixed history, not "now"
        assert p.min_bars > 0
        if p.timeframe is Timeframe.M1:
            # every minute of an intraday probe is inside trading hours
            assert not any(market_closed(t) for t in minutes(p.start, 60))


def test_render_explains_the_next_step_for_each_verdict() -> None:
    ok = check_provider(provider(FakeOanda(minutes(T, 60))), [m1_probe()])
    assert "PROVIDER OK" in render_check(ok)
    denied = check_provider(provider(FakeOanda([], fail_first=[403])), [m1_probe()])
    text = render_check(denied)
    assert "ACCESS_DENIED" in text
    assert "fallback" in text.lower()
    short = check_provider(provider(FakeOanda(minutes(T, 5))), [m1_probe()])
    assert "INSUFFICIENT_DATA" in render_check(short)


# ----------------------------------------------------------------------------- CLI


def test_cli_without_a_token_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SENTINEL_OANDA_TOKEN", raising=False)
    monkeypatch.delenv("SENTINEL_DATABASE_URL", raising=False)

    def forbidden(*args: object, **kwargs: object) -> OandaProvider:
        raise AssertionError("no provider may be built without a token")

    monkeypatch.setattr(oanda, "OandaProvider", forbidden)
    assert cli.main(["provider-check"]) == 2
    assert "SENTINEL_OANDA_TOKEN" in capsys.readouterr().err


def _patch_provider(monkeypatch: pytest.MonkeyPatch, fake: FakeOanda) -> None:
    monkeypatch.setenv("SENTINEL_OANDA_TOKEN", TOKEN)
    monkeypatch.delenv("SENTINEL_DATABASE_URL", raising=False)  # never needs the database
    monkeypatch.setattr(oanda, "OandaProvider", lambda token: provider(fake))
    monkeypatch.setattr(connectivity, "DEFAULT_PROBES", (m1_probe(),))


def test_cli_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_provider(monkeypatch, FakeOanda(minutes(T, 60)))
    assert cli.main(["provider-check"]) == 0
    assert "PROVIDER OK" in capsys.readouterr().out

    _patch_provider(monkeypatch, FakeOanda([], fail_first=[403]))
    assert cli.main(["provider-check"]) == 3

    _patch_provider(monkeypatch, FakeOanda(minutes(T, 3)))
    assert cli.main(["provider-check"]) == 1
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("timeframe", "ts", "misaligned"),
    [
        (Timeframe.M1, datetime(2024, 1, 10, 14, 1, tzinfo=UTC), False),
        (Timeframe.M1, datetime(2024, 1, 10, 14, 1, 30, tzinfo=UTC), True),
        (Timeframe.H1, datetime(2024, 1, 10, 14, 30, tzinfo=UTC), True),
        (Timeframe.H4, datetime(2024, 1, 9, 22, tzinfo=UTC), False),  # 17:00 New York (EST)
        (Timeframe.H4, datetime(2024, 7, 9, 21, tzinfo=UTC), False),  # 17:00 New York (EDT)
        (Timeframe.H4, datetime(2024, 1, 9, 20, tzinfo=UTC), True),
        (Timeframe.H4, datetime(2024, 1, 9, 22, 30, tzinfo=UTC), True),
        (Timeframe.D1, datetime(2024, 7, 9, 21, tzinfo=UTC), False),
        (Timeframe.D1, datetime(2024, 7, 9, 22, tzinfo=UTC), True),
    ],
)
def test_alignment_rules(timeframe: Timeframe, ts: datetime, misaligned: bool) -> None:
    assert connectivity._misaligned(ts, timeframe) is misaligned
    assert connectivity._alignment_rule(timeframe)
