"""Trading-state transitions.

* Automatic actors may only move to a *more* restrictive state.
* Humans may tighten freely, but loosen only one step at a time:
  HALTED -> NO_NEW_TRADES -> ACTIVE. FLATTEN can only end in HALTED.
* Every transition needs a non-empty reason and cannot go back in time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sentinel.domain.system import Actor, ActorKind, SystemMode, SystemState, TradingState
from sentinel.domain.types import DomainError, require_utc

__all__ = ["StateTransition", "TransitionRefused", "transition"]

_HUMAN_LOOSENING: dict[TradingState, frozenset[TradingState]] = {
    TradingState.HALTED: frozenset({TradingState.NO_NEW_TRADES}),
    TradingState.NO_NEW_TRADES: frozenset({TradingState.ACTIVE}),
}


class TransitionRefused(DomainError):
    pass


@dataclass(frozen=True, slots=True)
class StateTransition:
    from_state: TradingState
    to_state: TradingState
    mode: SystemMode
    at: datetime
    actor: Actor
    reason: str


def transition(
    current: SystemState,
    target: TradingState,
    *,
    actor: Actor,
    reason: str,
    at: datetime,
) -> tuple[SystemState, StateTransition]:
    at = require_utc(at, field="at")
    if not reason.strip():
        raise TransitionRefused("a transition needs a reason")
    if at < current.since:
        raise TransitionRefused("a transition cannot precede the current state")
    source = current.trading_state
    if target is source:
        raise TransitionRefused(f"already {source}")
    tightening = target.restrictiveness > source.restrictiveness
    if actor.kind is ActorKind.AUTO and not tightening:
        raise TransitionRefused(f"automatic actor may only tighten ({source} -> {target})")
    if actor.kind is ActorKind.HUMAN and not tightening:
        allowed = _HUMAN_LOOSENING.get(source, frozenset())
        if target not in allowed:
            raise TransitionRefused(f"{source} -> {target} is not a permitted human step")
    if source is TradingState.FLATTEN and target is not TradingState.HALTED:
        raise TransitionRefused("FLATTEN can only be followed by HALTED")
    new_state = SystemState(
        mode=current.mode, trading_state=target, since=at, reason=reason, actor=actor
    )
    return new_state, StateTransition(source, target, current.mode, at, actor, reason)
