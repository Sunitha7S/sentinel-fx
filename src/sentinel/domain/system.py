"""System mode and trading state.

The default state of a freshly started system is HALTED: nothing can trade until a human
has explicitly moved it to NO_NEW_TRADES and then to ACTIVE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sentinel.domain.types import DomainError, require_utc

__all__ = [
    "Actor",
    "ActorKind",
    "BrokerEnvironment",
    "SystemMode",
    "SystemState",
    "TradingState",
    "initial_system_state",
]


class SystemMode(StrEnum):
    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE_MICRO = "LIVE_MICRO"
    LIVE = "LIVE"

    @property
    def is_live(self) -> bool:
        return self in (SystemMode.LIVE_MICRO, SystemMode.LIVE)

    @property
    def sends_broker_orders(self) -> bool:
        return self in (SystemMode.PAPER, SystemMode.LIVE_MICRO, SystemMode.LIVE)


class BrokerEnvironment(StrEnum):
    """Which broker account a deployment is wired to. Practice and live never mix."""

    NONE = "NONE"
    PRACTICE = "PRACTICE"
    LIVE = "LIVE"


class TradingState(StrEnum):
    ACTIVE = "ACTIVE"
    NO_NEW_TRADES = "NO_NEW_TRADES"
    FLATTEN = "FLATTEN"
    HALTED = "HALTED"

    @property
    def restrictiveness(self) -> int:
        """Higher is more restrictive."""
        return _RANK[self]


_RANK = {
    TradingState.ACTIVE: 0,
    TradingState.NO_NEW_TRADES: 1,
    TradingState.FLATTEN: 2,
    TradingState.HALTED: 3,
}


class ActorKind(StrEnum):
    HUMAN = "HUMAN"
    AUTO = "AUTO"


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    identity: str

    def __post_init__(self) -> None:
        if not self.identity.strip():
            raise DomainError("Actor.identity must not be empty")

    def __str__(self) -> str:
        return f"{self.kind.value.lower()}:{self.identity}"


@dataclass(frozen=True, slots=True)
class SystemState:
    mode: SystemMode
    trading_state: TradingState
    since: datetime
    reason: str
    actor: Actor

    def __post_init__(self) -> None:
        object.__setattr__(self, "since", require_utc(self.since, field="SystemState.since"))


def initial_system_state(now: datetime, mode: SystemMode = SystemMode.SHADOW) -> SystemState:
    return SystemState(
        mode=mode,
        trading_state=TradingState.HALTED,
        since=now,
        reason="initial state: trading requires explicit human activation",
        actor=Actor(ActorKind.AUTO, "bootstrap"),
    )
