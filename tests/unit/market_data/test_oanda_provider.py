"""OANDA provider: pagination, validation, retries, and the read-only guarantee."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sentinel.domain.market_data import Timeframe
from sentinel.perception.market_data.dukascopy import DukascopyProvider
from sentinel.perception.market_data.oanda import (
    PRACTICE_HOST,
    OandaProvider,
    format_oanda_time,
    parse_oanda_time,
)
from sentinel.perception.market_data.provider import (
    ProviderError,
    ProviderUnavailable,
    ReadOnlyViolation,
)

from support.market import FakeOanda, minutes

T = datetime(2015, 1, 5, 0, 0, tzinfo=UTC)  # a Monday
TOKEN = "practice-token-SECRET"  # noqa: S105 - fake value for tests


def provider(fake: FakeOanda, **kwargs: object) -> OandaProvider:
    return OandaProvider(
        TOKEN,
        transport=fake.transport(),
        sleep=lambda _: None,
        min_interval_s=0,
        **kwargs,  # type: ignore[arg-type]
    )


def fetch(p: OandaProvider, start: datetime, end: datetime) -> list[datetime]:
    return [c.ts for b in p.fetch_candles("EUR_USD", Timeframe.M1, start, end) for c in b.candles]


def test_time_format_round_trips_with_nanoseconds() -> None:
    ts = datetime(2015, 1, 2, 22, 0, 0, 123456, tzinfo=UTC)
    assert format_oanda_time(ts) == "2015-01-02T22:00:00.123456000Z"
    assert parse_oanda_time("2015-01-02T22:00:00.123456789Z") == ts
    assert parse_oanda_time("2015-01-02T22:00:00Z") == ts.replace(microsecond=0)
    for bad in ("2015-01-02T22:00:00", "garbage Z"):
        with pytest.raises(ProviderError):
            parse_oanda_time(bad)


def test_paginates_without_duplicates_and_respects_the_range() -> None:
    fake = FakeOanda(minutes(T, 23))
    got = fetch(provider(fake, page_size=5), T + timedelta(minutes=2), T + timedelta(minutes=20))
    assert got == minutes(T + timedelta(minutes=2), 18)
    params = [r.url.params for r in fake.requests]
    assert params[0]["includeFirst"] == "true"
    assert all(p["includeFirst"] == "false" for p in params[1:])
    assert all(p["price"] == "BA" for p in params)
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in fake.requests)


def test_incomplete_and_invalid_candles_are_never_returned() -> None:
    times = minutes(T, 6)
    fake = FakeOanda(times, incomplete={times[5]}, crossed={times[2]})
    batches = list(provider(fake).fetch_candles("EUR_USD", Timeframe.M1, T, T + timedelta(hours=1)))
    stored = [c.ts for b in batches for c in b.candles]
    assert stored == [times[i] for i in (0, 1, 3, 4)]
    assert sum(b.rejected for b in batches) == 1
    assert sum(b.incomplete for b in batches) == 1


def test_retries_transient_errors_then_fails_clearly() -> None:
    fake = FakeOanda(minutes(T, 3), fail_first=[429, 503])
    assert fetch(provider(fake), T, T + timedelta(minutes=3)) == minutes(T, 3)
    hopeless = FakeOanda(minutes(T, 3), fail_first=[500] * 10)
    with pytest.raises(ProviderError, match="500"):
        fetch(provider(hopeless, max_retries=2), T, T + timedelta(minutes=3))


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_failure_is_not_retried_and_hides_the_token(status: int) -> None:
    fake = FakeOanda([], fail_first=[status, status, status])
    with pytest.raises(ProviderUnavailable) as exc:
        fetch(provider(fake), T, T + timedelta(minutes=3))
    assert len(fake.requests) == 1
    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(provider(FakeOanda([])))


def test_live_environment_and_empty_token_are_refused() -> None:
    with pytest.raises(ProviderUnavailable, match="practice"):
        OandaProvider(TOKEN, environment="live")
    with pytest.raises(ProviderUnavailable, match="token"):
        OandaProvider("  ")
    with pytest.raises(ValueError, match="page_size"):
        OandaProvider(TOKEN, page_size=6000)


# ----------------------------------------------------------------------------- read-only


@pytest.mark.invariant("INV-MD-READONLY")
@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", f"https://{PRACTICE_HOST}/v3/accounts/1/orders"),
        ("PUT", f"https://{PRACTICE_HOST}/v3/accounts/1/trades/1/close"),
        ("PATCH", f"https://{PRACTICE_HOST}/v3/accounts/1/configuration"),
        ("DELETE", f"https://{PRACTICE_HOST}/v3/accounts/1/orders/1"),
        ("POST", f"https://{PRACTICE_HOST}/v3/instruments/EUR_USD/candles"),
        ("GET", f"https://{PRACTICE_HOST}/v3/accounts"),
        ("GET", f"https://{PRACTICE_HOST}/v3/accounts/1/pricing/stream"),
        ("GET", "https://api-fxtrade.oanda.com/v3/instruments/EUR_USD/candles"),
        ("GET", f"http://{PRACTICE_HOST}/v3/instruments/EUR_USD/candles"),
        ("GET", f"https://{PRACTICE_HOST}/v3/instruments/EUR_USD/candles/../../accounts"),
    ],
)
def test_only_historical_candle_reads_can_leave_the_process(method: str, url: str) -> None:
    fake = FakeOanda([])
    p = provider(fake)
    with pytest.raises(ReadOnlyViolation):
        p._client.request(method, url)
    assert fake.requests == []  # nothing reached the transport


@pytest.mark.invariant("INV-MD-READONLY")
def test_provider_interface_exposes_no_write_operations() -> None:
    public = {n for n in dir(OandaProvider) if not n.startswith("_")}
    assert public == {"name", "fetch_candles", "close"}
    assert {n for n in dir(DukascopyProvider) if not n.startswith("_")} == {"name", "fetch_candles"}


def test_dukascopy_fallback_is_declared_but_not_implemented() -> None:
    with pytest.raises(ProviderUnavailable, match="not implemented"):
        list(DukascopyProvider().fetch_candles("EUR_USD", Timeframe.M1, T, T + timedelta(hours=1)))


def test_non_json_and_malformed_bodies_fail_cleanly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    p = OandaProvider(TOKEN, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    with pytest.raises(ProviderError, match="400"):
        fetch(p, T, T + timedelta(minutes=1))

    def no_list(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candles": "nope"})

    p2 = OandaProvider(TOKEN, transport=httpx.MockTransport(no_list), sleep=lambda _: None)
    with pytest.raises(ProviderError, match="candle list"):
        fetch(p2, T, T + timedelta(minutes=1))
