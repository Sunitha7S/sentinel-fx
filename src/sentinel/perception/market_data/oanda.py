"""OANDA v20 historical-candle provider (practice environment only, read-only).

Read-only is enforced at the transport layer: an httpx request hook refuses any request that
is not ``GET`` to ``/v3/instruments/{INSTRUMENT}/candles`` on the practice host. Order,
trade, position and account endpoints are therefore unreachable from this class even if
code elsewhere tried to call them. The live host is refused at construction (M2 has no
live-account dependency).

The API token is read by the caller (``SENTINEL_OANDA_TOKEN``) and is never logged or
included in error messages.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import httpx

from sentinel.domain.market_data import OHLC, Candle, Timeframe
from sentinel.domain.types import DomainError
from sentinel.perception.market_data.provider import (
    CandleBatch,
    ProviderError,
    ProviderUnavailable,
    ReadOnlyViolation,
)

__all__ = ["PRACTICE_HOST", "OandaProvider", "format_oanda_time", "parse_oanda_time"]

PRACTICE_HOST: Final = "api-fxpractice.oanda.com"
_CANDLES_PATH: Final = re.compile(r"^/v3/instruments/[A-Z]{3}_[A-Z]{3}/candles$")
_MAX_PAGE: Final = 5000
_RETRYABLE: Final = frozenset({429, 500, 502, 503, 504})


def format_oanda_time(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def parse_oanda_time(value: str) -> datetime:
    """Parse RFC3339 with up to nanosecond precision (``2015-01-02T22:00:00.000000000Z``)."""
    if not value.endswith("Z"):
        raise ProviderError(f"unexpected time format {value!r}")
    main, _, frac = value[:-1].partition(".")
    micro = (frac + "000000")[:6] if frac else "000000"
    try:
        return datetime.strptime(f"{main}.{micro}", "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ProviderError(f"unexpected time format {value!r}") from exc


def _guard(request: httpx.Request) -> None:
    if (
        request.method != "GET"
        or request.url.host != PRACTICE_HOST
        or request.url.scheme != "https"
        or not _CANDLES_PATH.match(request.url.path)
    ):
        raise ReadOnlyViolation(
            f"refused {request.method} {request.url.scheme}://{request.url.host}"
            f"{request.url.path}: only historical candle reads are allowed"
        )


class OandaProvider:
    name = "oanda-practice"

    def __init__(
        self,
        token: str,
        *,
        environment: str = "practice",
        page_size: int = _MAX_PAGE,
        max_retries: int = 5,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_s: float = 0.05,
    ) -> None:
        if environment != "practice":
            raise ProviderUnavailable("only the OANDA practice environment is allowed in M2")
        if not token.strip():
            raise ProviderUnavailable("an OANDA practice API token is required")
        if not 1 <= page_size <= _MAX_PAGE:
            raise ValueError(f"page_size must be within 1..{_MAX_PAGE}")
        self._page_size = page_size
        self._max_retries = max_retries
        self._sleep = sleep
        self._min_interval = min_interval_s
        self._client = httpx.Client(
            base_url=f"https://{PRACTICE_HOST}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Datetime-Format": "RFC3339",
            },
            transport=transport,
            timeout=httpx.Timeout(30.0),
            event_hooks={"request": [_guard]},
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"OandaProvider(host={PRACTICE_HOST!r})"  # never expose the token

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ transport

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.get(path, params=params)
            except ReadOnlyViolation:
                raise
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise ProviderError(
                        f"network error after {attempt + 1} attempts: {exc}"
                    ) from exc
                self._sleep(0.5 * 2**attempt)
                continue
            if response.status_code in (401, 403):
                raise ProviderUnavailable(
                    f"OANDA refused the request (HTTP {response.status_code}): check the "
                    "practice token and that the account's region has API access"
                )
            if response.status_code in _RETRYABLE and attempt < self._max_retries:
                self._sleep(0.5 * 2**attempt)
                continue
            if response.status_code != 200:
                raise ProviderError(
                    f"OANDA HTTP {response.status_code}: {_error_message(response)}"
                )
            body = response.json()
            if not isinstance(body, dict):
                raise ProviderError("unexpected response body")
            return body
        raise ProviderError("retries exhausted")  # pragma: no cover - loop always returns/raises

    # ------------------------------------------------------------------ candles

    def fetch_candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Iterator[CandleBatch]:
        path = f"/v3/instruments/{symbol}/candles"
        cursor, include_first = start, True
        while cursor < end:
            body = self._get(
                path,
                {
                    "price": "BA",
                    "granularity": timeframe.value,
                    "from": format_oanda_time(cursor),
                    "count": str(self._page_size),
                    "includeFirst": "true" if include_first else "false",
                    "alignmentTimezone": "America/New_York",
                    "dailyAlignment": "17",
                },
            )
            raw = body.get("candles")
            if not isinstance(raw, list):
                raise ProviderError("response has no candle list")
            if not raw:
                return
            batch, last_ts, reached_end = _parse_page(raw, symbol, timeframe, start, end)
            yield batch
            if reached_end or len(raw) < self._page_size or last_ts <= cursor:
                return
            cursor, include_first = last_ts, False
            if self._min_interval:
                self._sleep(self._min_interval)


def _parse_page(
    raw: list[Any], symbol: str, timeframe: Timeframe, start: datetime, end: datetime
) -> tuple[CandleBatch, datetime, bool]:
    candles: list[Candle] = []
    rejected = incomplete = 0
    last_ts = start
    reached_end = False
    for item in raw:
        try:
            ts = parse_oanda_time(item["time"])
        except (KeyError, TypeError) as exc:
            raise ProviderError("candle without a time") from exc
        last_ts = max(last_ts, ts)
        if ts >= end:
            reached_end = True
            continue
        if ts < start:
            continue
        if not item.get("complete", False):
            incomplete += 1
            continue
        try:
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    ts=ts,
                    bid=_ohlc(item["bid"]),
                    ask=_ohlc(item["ask"]),
                    tick_volume=int(item.get("volume", 0)),
                )
            )
        except (DomainError, KeyError, TypeError, ValueError):
            rejected += 1
    return CandleBatch(tuple(candles), rejected, incomplete), last_ts, reached_end


def _ohlc(side: dict[str, str]) -> OHLC:
    return OHLC(Decimal(side["o"]), Decimal(side["h"]), Decimal(side["l"]), Decimal(side["c"]))


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict) and isinstance(body.get("errorMessage"), str):
        return str(body["errorMessage"])[:200]
    return str(body)[:200]
