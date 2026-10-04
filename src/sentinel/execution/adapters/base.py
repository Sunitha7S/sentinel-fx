"""Broker adapter port. Only a disabled implementation exists before M10."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sentinel.audit.chain import AuditLog
from sentinel.domain.decision import Approval, RiskDecision, SignalCandidate
from sentinel.execution.guard import ExecutionEnvironment, assert_execution_permitted

__all__ = ["BrokerAdapter", "DisabledBrokerAdapter"]


class BrokerAdapter(Protocol):
    def submit_bracket_order(
        self,
        *,
        decision: RiskDecision,
        candidate: SignalCandidate,
        approvals: Sequence[Approval],
    ) -> str:
        """Submit entry + broker-side stop + take-profit; return the broker order id."""
        ...


class DisabledBrokerAdapter:
    """Refuses every order via the execution guard. The only adapter in M0-M1."""

    def __init__(self, env: ExecutionEnvironment, *, audit: AuditLog | None = None) -> None:
        self._env = env
        self._audit = audit

    def submit_bracket_order(
        self,
        *,
        decision: RiskDecision,
        candidate: SignalCandidate,
        approvals: Sequence[Approval],
    ) -> str:
        assert_execution_permitted(self._env, audit=self._audit)
        raise NotImplementedError("order submission is implemented in M10")  # pragma: no cover
