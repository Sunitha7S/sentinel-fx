"""Immutable perception snapshots: market, calendar, news and account state.

Each snapshot has an ``as_of`` timestamp, an id, and a content hash. A decision records
the references of every snapshot it read, so it can be replayed and so that approvals can
be bound to the exact state they were computed from.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import IntEnum, StrEnum

from sentinel.domain.canonical import sha256_hex
from sentinel.domain.types import (
    Currency,
    DomainError,
    Money,
    PositionId,
    Side,
    SnapshotId,
    require_non_negative,
    require_positive,
    require_utc,
    to_decimal,
)

__all__ = [
    "AccountSnapshot",
    "CalendarSnapshot",
    "CurrencyNewsRisk",
    "EconomicEvent",
    "EventTier",
    "MarketSnapshot",
    "NewsRiskSnapshot",
    "OpenPosition",
    "SnapshotRef",
    "VolRegime",
    "age",
    "snapshot_digest",
]


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    kind: str
    snapshot_id: SnapshotId
    as_of: datetime
    sha256: str


def snapshot_digest(refs: Iterable[SnapshotRef]) -> str:
    """Order-independent digest of a set of snapshot references."""
    ordered = sorted(refs, key=lambda r: (r.kind, r.snapshot_id))
    return sha256_hex(ordered)


def _ref(kind: str, snapshot_id: SnapshotId, as_of: datetime, obj: object) -> SnapshotRef:
    return SnapshotRef(kind=kind, snapshot_id=snapshot_id, as_of=as_of, sha256=sha256_hex(obj))


# ----------------------------------------------------------------------------- market


class VolRegime(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    EXTREME = "EXTREME"


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    snapshot_id: SnapshotId
    symbol: str
    as_of: datetime
    last_tick_at: datetime
    bid: Decimal
    ask: Decimal
    atr_h1: Decimal
    atr_d1: Decimal
    session_median_spread: Decimal
    regime: VolRegime
    circuit_breaker_active: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc(self.as_of, field="MarketSnapshot.as_of"))
        object.__setattr__(
            self,
            "last_tick_at",
            require_utc(self.last_tick_at, field="MarketSnapshot.last_tick_at"),
        )
        for name in ("bid", "ask", "atr_h1", "atr_d1", "session_median_spread"):
            value = to_decimal(getattr(self, name), field=f"MarketSnapshot.{name}")
            object.__setattr__(self, name, require_positive(value, field=f"MarketSnapshot.{name}"))
        if self.ask < self.bid:
            raise DomainError("MarketSnapshot: ask must be >= bid")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def ref(self) -> SnapshotRef:
        return _ref(f"market:{self.symbol}", self.snapshot_id, self.as_of, self)


# ----------------------------------------------------------------------------- calendar


class EventTier(IntEnum):
    TIER1 = 1
    TIER2 = 2
    TIER3 = 3


@dataclass(frozen=True, slots=True)
class EconomicEvent:
    event_id: str
    currency: Currency
    title: str
    tier: EventTier
    scheduled_at: datetime
    is_central_bank: bool = False
    ends_at: datetime | None = None
    """End of the event (e.g. press conference). ``None`` means it is instantaneous."""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scheduled_at", require_utc(self.scheduled_at, field="EconomicEvent.scheduled_at")
        )
        if self.ends_at is not None:
            ends = require_utc(self.ends_at, field="EconomicEvent.ends_at")
            if ends < self.scheduled_at:
                raise DomainError("EconomicEvent: ends_at must be >= scheduled_at")
            object.__setattr__(self, "ends_at", ends)


@dataclass(frozen=True, slots=True)
class CalendarSnapshot:
    snapshot_id: SnapshotId
    as_of: datetime
    events: tuple[EconomicEvent, ...]
    sources: tuple[str, ...]
    source_agreement: bool
    discrepancies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc(self.as_of, field="CalendarSnapshot.as_of"))

    def ref(self) -> SnapshotRef:
        return _ref("calendar", self.snapshot_id, self.as_of, self)


# ----------------------------------------------------------------------------- news


@dataclass(frozen=True, slots=True)
class CurrencyNewsRisk:
    currency: Currency
    score: Decimal
    """0-100. Produced by deterministic tripwires, optionally raised (never lowered) by an
    LLM classification; see ``sentinel.perception.news.merge``."""
    tripwire_active: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        score = to_decimal(self.score, field="CurrencyNewsRisk.score")
        if not Decimal(0) <= score <= Decimal(100):
            raise DomainError(f"CurrencyNewsRisk.score must be within 0..100, got {score}")
        object.__setattr__(self, "score", score)


@dataclass(frozen=True, slots=True)
class NewsRiskSnapshot:
    snapshot_id: SnapshotId
    as_of: datetime
    per_currency: tuple[CurrencyNewsRisk, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc(self.as_of, field="NewsRiskSnapshot.as_of"))
        seen = [r.currency for r in self.per_currency]
        if len(seen) != len(set(seen)):
            raise DomainError("NewsRiskSnapshot: duplicate currency entries")

    def for_currency(self, currency: Currency) -> CurrencyNewsRisk | None:
        return next((r for r in self.per_currency if r.currency is currency), None)

    def ref(self) -> SnapshotRef:
        return _ref("news", self.snapshot_id, self.as_of, self)


# ----------------------------------------------------------------------------- account


@dataclass(frozen=True, slots=True)
class OpenPosition:
    position_id: PositionId
    symbol: str
    side: Side
    units: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None
    """Broker-side stop. ``None`` means the position is unprotected, which blocks new risk."""
    risk_amount: Money
    """Loss in account currency if the stop fills, including costs. Zero once the stop is at
    or beyond breakeven."""

    def __post_init__(self) -> None:
        units = require_positive(to_decimal(self.units, field="OpenPosition.units"), field="units")
        object.__setattr__(self, "units", units)
        entry = to_decimal(self.entry_price, field="OpenPosition.entry_price")
        object.__setattr__(self, "entry_price", require_positive(entry, field="entry_price"))
        if self.stop_loss is not None:
            stop = to_decimal(self.stop_loss, field="OpenPosition.stop_loss")
            object.__setattr__(self, "stop_loss", require_positive(stop, field="stop_loss"))
        require_non_negative(self.risk_amount.amount, field="OpenPosition.risk_amount")


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Account state as reported by the broker and reconciled against local records.

    ``state_version`` increases on every change to balance, positions or orders. Approvals
    are bound to it: any account change between approval and execution invalidates them.
    """

    snapshot_id: SnapshotId
    as_of: datetime
    state_version: int
    currency: Currency
    balance: Money
    equity: Money
    peak_equity: Money
    day_start_equity: Money
    week_start_equity: Money
    month_start_equity: Money
    margin_used: Money
    open_positions: tuple[OpenPosition, ...]
    consecutive_losses: int
    last_loss_at: datetime | None
    trades_today: int
    reconciled_at: datetime | None
    reconciliation_ok: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc(self.as_of, field="AccountSnapshot.as_of"))
        for name in ("last_loss_at", "reconciled_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_utc(value, field=f"AccountSnapshot.{name}"))
        for name in (
            "balance",
            "equity",
            "peak_equity",
            "day_start_equity",
            "week_start_equity",
            "month_start_equity",
            "margin_used",
        ):
            money: Money = getattr(self, name)
            if money.currency is not self.currency:
                raise DomainError(f"AccountSnapshot.{name} must be in {self.currency}")
        for position in self.open_positions:
            if position.risk_amount.currency is not self.currency:
                raise DomainError("OpenPosition.risk_amount must be in account currency")
        for name in ("state_version", "consecutive_losses", "trades_today"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DomainError(f"AccountSnapshot.{name} must be a non-negative int")

    def ref(self) -> SnapshotRef:
        return _ref("account", self.snapshot_id, self.as_of, self)


def age(as_of: datetime, now: datetime) -> timedelta:
    """``now - as_of``; negative when the snapshot claims to come from the future."""
    return now - as_of
