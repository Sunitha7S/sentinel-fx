"""Audited trading-state machine: every transition and every refusal goes on the record."""

from __future__ import annotations

from datetime import datetime

from sentinel.audit.chain import AuditKind, AuditLog
from sentinel.domain.system import Actor, SystemState, TradingState
from sentinel.risk.state_machine import TransitionRefused, transition

__all__ = ["AuditedStateMachine"]


class AuditedStateMachine:
    def __init__(self, initial: SystemState, audit: AuditLog) -> None:
        self._state = initial
        self._audit = audit

    @property
    def state(self) -> SystemState:
        return self._state

    def request(
        self, target: TradingState, *, actor: Actor, reason: str, at: datetime
    ) -> SystemState:
        try:
            new_state, record = transition(self._state, target, actor=actor, reason=reason, at=at)
        except TransitionRefused as refusal:
            self._audit.record(
                AuditKind.STATE_TRANSITION_REFUSED,
                "trading_state",
                {
                    "from": self._state.trading_state,
                    "to": target,
                    "actor": str(actor),
                    "reason": reason,
                    "refusal": str(refusal),
                },
            )
            raise
        # Audit first: if the record cannot be written, the state does not change.
        self._audit.record(AuditKind.STATE_TRANSITION, "trading_state", record)
        self._state = new_state
        return new_state
