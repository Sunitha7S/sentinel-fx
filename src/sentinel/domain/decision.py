"""Signal candidates, rule results, risk decisions, approvals and shadow-trade records.

``RiskDecision`` enforces its own invariants at construction time, so an approval that
violates them cannot exist as a value:

* APPROVED requires every HARD rule to have passed, a positive size, and a snapshot digest
  that binds the decision to the exact inputs it was computed from.
* BLOCKED always carries a size of zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sentinel.domain.types import (
    ApprovalId,
    CandidateId,
    DecisionId,
    DomainError,
    Money,
    Percent,
    Side,
    require_positive,
    require_utc,
    to_decimal,
)

__all__ = [
    "Approval",
    "Authority",
    "Outcome",
    "RiskDecision",
    "RuleResult",
    "Severity",
    "ShadowTradeRecord",
    "SignalCandidate",
    "Stage",
    "shadow_record_from",
]


# ----------------------------------------------------------------------------- candidates


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    """A proposed trade. Produced by a detector (M4+); in M1 only constructed by tests.

    Construction validates types and positivity only. Whether the stop is on the correct
    side, far enough away, or the reward worth the risk is decided by the risk kernel, so
    that a malformed candidate produces an explained BLOCKED decision rather than an
    exception.
    """

    candidate_id: CandidateId
    symbol: str
    side: Side
    detector: str
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    created_at: datetime
    expires_at: datetime
    expected_hold: timedelta

    def __post_init__(self) -> None:
        for name in ("entry", "stop_loss", "take_profit"):
            value = to_decimal(getattr(self, name), field=f"SignalCandidate.{name}")
            object.__setattr__(self, name, require_positive(value, field=f"SignalCandidate.{name}"))
        object.__setattr__(
            self, "created_at", require_utc(self.created_at, field="SignalCandidate.created_at")
        )
        object.__setattr__(
            self, "expires_at", require_utc(self.expires_at, field="SignalCandidate.expires_at")
        )
        if self.expected_hold <= timedelta(0):
            raise DomainError("SignalCandidate.expected_hold must be positive")


# ----------------------------------------------------------------------------- rule results


class Outcome(StrEnum):
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"


class Severity(StrEnum):
    HARD = "HARD"
    SOFT = "SOFT"


class Stage(StrEnum):
    SYSTEM = "SYSTEM"
    ACCOUNT = "ACCOUNT"
    MARKET = "MARKET"
    TRADE = "TRADE"
    PORTFOLIO = "PORTFOLIO"
    APPROVAL = "APPROVAL"
    EXECUTION_RECHECK = "EXECUTION_RECHECK"


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    stage: Stage
    passed: bool
    message: str
    observed: str | None = None
    threshold: str | None = None
    severity: Severity = Severity.HARD

    @property
    def blocks(self) -> bool:
        return self.severity is Severity.HARD and not self.passed


# ----------------------------------------------------------------------------- decisions


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: DecisionId
    candidate_id: CandidateId | None
    as_of: datetime
    outcome: Outcome
    units: Decimal
    risk_amount: Money | None
    effective_risk: Percent | None
    rule_results: tuple[RuleResult, ...]
    policy_id: str | None
    policy_sha256: str | None
    state_version: int | None
    snapshot_digest: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc(self.as_of, field="RiskDecision.as_of"))
        object.__setattr__(self, "units", to_decimal(self.units, field="RiskDecision.units"))
        if not self.rule_results:
            raise DomainError("RiskDecision must report at least one rule result")
        if self.outcome is Outcome.APPROVED:
            failures = [r.rule_id for r in self.rule_results if r.blocks]
            if failures:
                raise DomainError(f"APPROVED decision with failing hard rules: {failures}")
            if self.units <= 0:
                raise DomainError("APPROVED decision must have positive units")
            missing = [
                name
                for name in (
                    "candidate_id",
                    "risk_amount",
                    "effective_risk",
                    "policy_sha256",
                    "state_version",
                    "snapshot_digest",
                )
                if getattr(self, name) is None
            ]
            if missing:
                raise DomainError(f"APPROVED decision missing bindings: {missing}")
        elif self.units != 0:
            raise DomainError("BLOCKED decision must have zero units")

    @property
    def approved(self) -> bool:
        return self.outcome is Outcome.APPROVED

    @property
    def blocking_rules(self) -> tuple[str, ...]:
        return tuple(r.rule_id for r in self.rule_results if r.blocks)


# ----------------------------------------------------------------------------- approvals


class Authority(StrEnum):
    VALIDATION = "VALIDATION"
    RISK = "RISK"
    PORTFOLIO = "PORTFOLIO"


@dataclass(frozen=True, slots=True)
class Approval:
    """One of the keys execution requires. Bound to an account state version, a snapshot
    digest and a payload hash (candidate + size + policy); valid only until ``expires_at``."""

    approval_id: ApprovalId
    decision_id: DecisionId
    candidate_id: CandidateId
    authority: Authority
    state_version: int
    snapshot_digest: str
    payload_sha256: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "issued_at", require_utc(self.issued_at, field="issued_at"))
        object.__setattr__(self, "expires_at", require_utc(self.expires_at, field="expires_at"))
        if self.expires_at <= self.issued_at:
            raise DomainError("Approval.expires_at must be after issued_at")


# ----------------------------------------------------------------------------- shadow trades


@dataclass(frozen=True, slots=True)
class ShadowTradeRecord:
    """A candidate kept for counterfactual evaluation, whatever the decision was.

    Resolution fields stay ``None`` until the shadow simulator (M9/M12) replays the
    candidate against later prices. Comparing resolved outcomes of blocked and approved
    candidates measures what each filter actually costs or saves.
    """

    candidate_id: CandidateId
    decision_id: DecisionId
    symbol: str
    side: Side
    detector: str
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    created_at: datetime
    expires_at: datetime
    decision_outcome: Outcome
    blocking_rules: tuple[str, ...]
    policy_sha256: str | None
    snapshot_digest: str | None
    resolved_at: datetime | None = None
    filled: bool | None = None
    r_multiple: Decimal | None = None
    exit_reason: str | None = None


def shadow_record_from(candidate: SignalCandidate, decision: RiskDecision) -> ShadowTradeRecord:
    if decision.candidate_id is not None and decision.candidate_id != candidate.candidate_id:
        raise DomainError("decision does not belong to this candidate")
    return ShadowTradeRecord(
        candidate_id=candidate.candidate_id,
        decision_id=decision.decision_id,
        symbol=candidate.symbol,
        side=candidate.side,
        detector=candidate.detector,
        entry=candidate.entry,
        stop_loss=candidate.stop_loss,
        take_profit=candidate.take_profit,
        created_at=candidate.created_at,
        expires_at=candidate.expires_at,
        decision_outcome=decision.outcome,
        blocking_rules=decision.blocking_rules,
        policy_sha256=decision.policy_sha256,
        snapshot_digest=decision.snapshot_digest,
    )
