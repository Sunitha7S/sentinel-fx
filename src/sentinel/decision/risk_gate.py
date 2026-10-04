"""The risk gate: assemble inputs, evaluate, audit, persist the shadow trade, fail closed.

Order of operations, and why:

1. Assemble inputs from the snapshot provider. Any provider failure gives BLOCKED.
2. Evaluate with the kernel, wrapped fail-closed.
3. Audit the decision. If the audit write fails, an approval is downgraded to BLOCKED: an
   approval that is not on the record does not exist.
4. Persist the shadow-trade record. If that fails, an approval is also downgraded (and the
   downgrade is audited), so every approved candidate is measurable later.

The gate never raises for operational failures; it returns a decision.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Protocol

from sentinel.audit.chain import AuditKind, AuditLog
from sentinel.decision.fail_closed import blocked, fail_closed
from sentinel.domain.decision import (
    RiskDecision,
    ShadowTradeRecord,
    SignalCandidate,
    shadow_record_from,
)
from sentinel.domain.instrument import Instrument
from sentinel.domain.snapshots import (
    AccountSnapshot,
    CalendarSnapshot,
    MarketSnapshot,
    NewsRiskSnapshot,
)
from sentinel.domain.system import SystemState
from sentinel.domain.types import DecisionId
from sentinel.risk.kernel import RiskInputs, evaluate
from sentinel.risk.policy import RiskPolicy
from sentinel.schemas.messages import RiskDecisionMsg

__all__ = ["RiskGate", "ShadowSink", "SnapshotProvider"]


class SnapshotProvider(Protocol):
    def policy(self) -> RiskPolicy | None: ...
    def active_policy_sha256(self) -> str | None: ...
    def system_state(self) -> SystemState | None: ...
    def account(self) -> AccountSnapshot | None: ...
    def markets(self) -> Mapping[str, MarketSnapshot]: ...
    def calendar(self) -> CalendarSnapshot | None: ...
    def news(self) -> NewsRiskSnapshot | None: ...
    def instruments(self) -> Mapping[str, Instrument]: ...
    def holidays(self) -> frozenset[date]: ...


class ShadowSink(Protocol):
    def record(self, record: ShadowTradeRecord) -> None: ...


class RiskGate:
    def __init__(
        self,
        *,
        provider: SnapshotProvider,
        audit: AuditLog,
        shadow: ShadowSink,
        clock: Callable[[], datetime],
        kernel: Callable[[RiskInputs], RiskDecision] = evaluate,
    ) -> None:
        self._provider = provider
        self._audit = audit
        self._shadow = shadow
        self._clock = clock
        self._kernel = kernel

    def _assemble(
        self, candidate: SignalCandidate, decision_id: DecisionId, as_of: datetime
    ) -> RiskInputs:
        p = self._provider
        return RiskInputs(
            decision_id=decision_id,
            as_of=as_of,
            candidate=candidate,
            policy=p.policy(),
            active_policy_sha256=p.active_policy_sha256(),
            system=p.system_state(),
            account=p.account(),
            markets=dict(p.markets() or {}),
            calendar=p.calendar(),
            news=p.news(),
            instruments=dict(p.instruments() or {}),
            holidays=frozenset(p.holidays() or ()),
        )

    def _audit_decision(self, decision: RiskDecision) -> None:
        payload = RiskDecisionMsg.from_domain(decision).model_dump(mode="json")
        self._audit.record(AuditKind.RISK_DECISION, decision.decision_id, payload)

    def _downgrade(self, decision: RiskDecision, reason: str) -> RiskDecision:
        return blocked(
            decision_id=decision.decision_id,
            candidate_id=decision.candidate_id,
            as_of=decision.as_of,
            reason=reason,
        )

    def decide(self, candidate: SignalCandidate, decision_id: DecisionId) -> RiskDecision:
        as_of = self._clock()
        decision = fail_closed(
            lambda: self._kernel(self._assemble(candidate, decision_id, as_of)),
            decision_id=decision_id,
            candidate_id=candidate.candidate_id,
            as_of=as_of,
        )

        try:
            self._audit_decision(decision)
        except Exception as exc:  # noqa: BLE001 - unaudited approvals must not exist
            decision = self._downgrade(decision, f"fail-closed: audit write failed: {exc}")
            with contextlib.suppress(Exception):  # already blocked; nothing more to do
                self._audit_decision(decision)

        try:
            self._shadow.record(shadow_record_from(candidate, decision))
        except Exception as exc:  # noqa: BLE001 - every approval must stay measurable
            if decision.approved:
                decision = self._downgrade(decision, f"fail-closed: shadow write failed: {exc}")
                with contextlib.suppress(Exception):  # already blocked; nothing more to do
                    self._audit_decision(decision)
        return decision
