"""Provider connectivity check: can this account read the history M2.1 needs? (step 0)

Runs a handful of small, fixed, read-only probes before any bulk ingestion and turns the
outcome into one verdict with a next step. It never touches the database and never stores
anything, so it is safe to run with a fresh token.

* Probes are fixed historical windows inside trading hours, so the expected bar count is
  known in advance and the result does not depend on when the check runs.
* The verdict is the worst probe result. No probes, or any doubt, is never ``OK``.
* Access denied (HTTP 401/403) stops the check at once: repeating a refused request only
  risks the provider flagging the token.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sentinel.domain.market_data import Timeframe
from sentinel.perception.market_data.provider import (
    MarketDataProvider,
    ProviderAccessDenied,
    ProviderError,
)

__all__ = [
    "DEFAULT_PROBES",
    "CheckStatus",
    "Probe",
    "ProbeResult",
    "ProviderCheck",
    "check_provider",
    "combine",
    "render_check",
]

NEW_YORK = ZoneInfo("America/New_York")


class CheckStatus(StrEnum):
    OK = "OK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNEXPECTED_DATA = "UNEXPECTED_DATA"
    ERROR = "ERROR"
    ACCESS_DENIED = "ACCESS_DENIED"


_SEVERITY = {status: rank for rank, status in enumerate(CheckStatus)}


def combine(statuses: Iterable[CheckStatus]) -> CheckStatus:
    """The worst status; an empty set of results is an error, never OK."""
    return max(statuses, key=_SEVERITY.__getitem__, default=CheckStatus.ERROR)


@dataclass(frozen=True, slots=True)
class Probe:
    symbol: str
    timeframe: Timeframe
    start: datetime
    end: datetime
    min_bars: int
    purpose: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    probe: Probe
    status: CheckStatus
    bars: int
    rejected: int
    incomplete: int
    first_ts: datetime | None
    last_ts: datetime | None
    detail: str


@dataclass(frozen=True, slots=True)
class ProviderCheck:
    provider: str
    results: tuple[ProbeResult, ...]

    @property
    def verdict(self) -> CheckStatus:
        return combine(r.status for r in self.results)


def _hour(day: datetime, hour: int) -> tuple[datetime, datetime]:
    start = day.replace(hour=hour)
    return start, start + timedelta(hours=1)


def _probes() -> tuple[Probe, ...]:
    recent = datetime(2024, 1, 10, tzinfo=UTC)  # Wednesday
    early = datetime(2015, 1, 7, tzinfo=UTC)  # Wednesday
    daily = datetime(2015, 1, 4, tzinfo=UTC), datetime(2015, 1, 10, tzinfo=UTC)
    out: list[Probe] = []
    for symbol in ("EUR_USD", "USD_JPY"):
        out.append(Probe(symbol, Timeframe.M1, *_hour(recent, 14), 55, "recent M1 bid/ask"))
        out.append(Probe(symbol, Timeframe.M1, *_hour(early, 14), 45, "M1 history from 2015"))
        # Bars opening Sun 4 Jan .. Thu 8 Jan 2015 at 17:00 New York: five trading days.
        out.append(Probe(symbol, Timeframe.D1, *daily, 5, "D1 alignment and 2015 depth"))
    return tuple(out)


DEFAULT_PROBES: tuple[Probe, ...] = _probes()


def _misaligned(ts: datetime, timeframe: Timeframe) -> bool:
    if timeframe.fixed_utc_grid:
        step = int(timeframe.duration.total_seconds())
        return int(ts.timestamp()) % step != 0
    ny = ts.astimezone(NEW_YORK)
    if ny.minute or ny.second or ny.microsecond:
        return True
    if timeframe is Timeframe.D1:
        return ny.hour != 17
    return ny.hour % 4 != 1  # H4: 17, 21, 01, 05, 09, 13 New York


def _alignment_rule(timeframe: Timeframe) -> str:
    if timeframe is Timeframe.D1:
        return "bars must open at 17:00 New York"
    if timeframe is Timeframe.H4:
        return "bars must open on 4-hour boundaries from 17:00 New York"
    return f"bars must open on the {timeframe.value} UTC grid"


def _run(provider: MarketDataProvider, probe: Probe) -> ProbeResult:
    bars = rejected = incomplete = misaligned = 0
    first: datetime | None = None
    last: datetime | None = None
    status: CheckStatus | None = None
    detail = ""
    try:
        for batch in provider.fetch_candles(probe.symbol, probe.timeframe, probe.start, probe.end):
            rejected += batch.rejected
            incomplete += batch.incomplete
            for c in batch.candles:
                bars += 1
                first = first or c.ts
                last = c.ts
                misaligned += _misaligned(c.ts, probe.timeframe)
    except ProviderAccessDenied as exc:
        status, detail = CheckStatus.ACCESS_DENIED, str(exc)
    except ProviderError as exc:
        status, detail = CheckStatus.ERROR, f"{type(exc).__name__}: {exc}"
    if status is None:
        if misaligned:
            status = CheckStatus.UNEXPECTED_DATA
            detail = f"{misaligned} misaligned bar(s): {_alignment_rule(probe.timeframe)}"
        elif rejected or incomplete:
            status = CheckStatus.UNEXPECTED_DATA
            detail = (
                f"{rejected} bar(s) failed validation and {incomplete} were incomplete "
                "in a closed historical window"
            )
        elif bars < probe.min_bars:
            status = CheckStatus.INSUFFICIENT_DATA
            detail = f"{bars} bar(s), expected at least {probe.min_bars}"
        else:
            status = CheckStatus.OK
            detail = f"{bars} bar(s)"
    return ProbeResult(probe, status, bars, rejected, incomplete, first, last, detail)


def check_provider(provider: MarketDataProvider, probes: Sequence[Probe]) -> ProviderCheck:
    results: list[ProbeResult] = []
    for probe in probes:
        result = _run(provider, probe)
        results.append(result)
        if result.status is CheckStatus.ACCESS_DENIED:
            break
    return ProviderCheck(provider.name, tuple(results))


_NEXT_STEP = {
    CheckStatus.OK: (
        "PROVIDER OK. Next: run the full ingestion into a disposable database first, then "
        "the strict data-quality report."
    ),
    CheckStatus.INSUFFICIENT_DATA: (
        "INSUFFICIENT_DATA. The provider answers but does not have the required history. "
        "Do not ingest. Decide whether a shorter history is acceptable or use the fallback "
        "provider (see ops/runbooks/market-data-provider.md)."
    ),
    CheckStatus.UNEXPECTED_DATA: (
        "UNEXPECTED_DATA. The provider's data does not match the pipeline's assumptions. "
        "Do not ingest; the difference needs a reviewed fix with a regression test."
    ),
    CheckStatus.ERROR: (
        "ERROR. The check could not complete. Retry later; if it persists, investigate "
        "before ingesting anything."
    ),
    CheckStatus.ACCESS_DENIED: (
        "ACCESS_DENIED. HTTP 401: the token is invalid or revoked; generate a new practice "
        "token. HTTP 403: the account may not use this API, which is how a regional "
        "restriction appears. Do not retry repeatedly. The fallback decision path is in "
        "ops/runbooks/market-data-provider.md and needs human approval."
    ),
}


def _fmt(ts: datetime | None) -> str:
    return ts.isoformat() if ts else "-"


def render_check(check: ProviderCheck) -> str:
    lines = [f"Provider check: {check.provider}", ""]
    for r in check.results:
        p = r.probe
        lines.append(
            f"  [{r.status.value}] {p.symbol} {p.timeframe.value} "
            f"{p.start:%Y-%m-%d %H:%M}..{p.end:%Y-%m-%d %H:%M} UTC ({p.purpose}): "
            f"{r.detail}; first {_fmt(r.first_ts)}, last {_fmt(r.last_ts)}"
        )
    lines += ["", f"Verdict: {check.verdict.value}", _NEXT_STEP[check.verdict]]
    return "\n".join(lines)
