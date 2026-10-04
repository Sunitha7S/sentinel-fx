"""Fakes for the decision-layer ports: snapshot provider, sinks, clocks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

from sentinel.domain.decision import ShadowTradeRecord
from sentinel.domain.instrument import Instrument
from sentinel.domain.snapshots import (
    AccountSnapshot,
    CalendarSnapshot,
    MarketSnapshot,
    NewsRiskSnapshot,
)
from sentinel.domain.system import SystemState
from sentinel.risk.kernel import RiskInputs
from sentinel.risk.policy import RiskPolicy

PROVIDER_METHODS = (
    "policy",
    "active_policy_sha256",
    "system_state",
    "account",
    "markets",
    "calendar",
    "news",
    "instruments",
    "holidays",
)


class ProviderFailure(RuntimeError):
    pass


@dataclass
class StaticProvider:
    """Serves the snapshots of a ``RiskInputs``; can be told to fail or go missing."""

    source: RiskInputs
    raise_on: frozenset[str] = frozenset()
    missing: frozenset[str] = frozenset()

    def _get(self, name: str, value: object) -> object:
        if name in self.raise_on:
            raise ProviderFailure(f"{name} unavailable")
        if name in self.missing:
            return {} if name in {"markets", "instruments"} else None
        return value

    def policy(self) -> RiskPolicy | None:
        return self._get("policy", self.source.policy)  # type: ignore[return-value]

    def active_policy_sha256(self) -> str | None:
        return self._get("active_policy_sha256", self.source.active_policy_sha256)  # type: ignore[return-value]

    def system_state(self) -> SystemState | None:
        return self._get("system_state", self.source.system)  # type: ignore[return-value]

    def account(self) -> AccountSnapshot | None:
        return self._get("account", self.source.account)  # type: ignore[return-value]

    def markets(self) -> Mapping[str, MarketSnapshot]:
        return self._get("markets", self.source.markets)  # type: ignore[return-value]

    def calendar(self) -> CalendarSnapshot | None:
        return self._get("calendar", self.source.calendar)  # type: ignore[return-value]

    def news(self) -> NewsRiskSnapshot | None:
        return self._get("news", self.source.news)  # type: ignore[return-value]

    def instruments(self) -> Mapping[str, Instrument]:
        return self._get("instruments", self.source.instruments)  # type: ignore[return-value]

    def holidays(self) -> frozenset[date]:
        value = self._get("holidays", self.source.holidays)
        return frozenset() if value is None else value  # type: ignore[return-value]


@dataclass
class ListShadowSink:
    records: list[ShadowTradeRecord] = field(default_factory=list)
    fail: bool = False

    def record(self, record: ShadowTradeRecord) -> None:
        if self.fail:
            raise OSError("shadow store unavailable")
        self.records.append(record)


@dataclass
class FixedClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now
