"""Issuing and verifying the approvals that execution requires.

An approval binds four things: the decision it came from, the account ``state_version``,
the digest of every snapshot the decision read, and a hash of the payload (candidate,
size, policy). It is valid only in ``[issued_at, expires_at)``. Any mismatch fails.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from sentinel.domain.canonical import sha256_hex
from sentinel.domain.decision import (
    Approval,
    Authority,
    Outcome,
    RiskDecision,
    RuleResult,
    SignalCandidate,
    Stage,
)
from sentinel.domain.types import ApprovalId, DecisionId

__all__ = ["REQUIRED_AUTHORITIES", "issue_approvals", "payload_digest", "verify_approvals"]

REQUIRED_AUTHORITIES: frozenset[Authority] = frozenset(Authority)


def payload_digest(candidate: SignalCandidate, units: Decimal, policy_sha256: str | None) -> str:
    return sha256_hex({"candidate": candidate, "units": units, "policy_sha256": policy_sha256})


def issue_approvals(
    decision: RiskDecision,
    candidate: SignalCandidate,
    *,
    authorities: Iterable[Authority],
    issued_at: datetime,
    ttl: timedelta,
) -> tuple[Approval, ...]:
    if decision.outcome is not Outcome.APPROVED:
        raise ValueError("approvals can only be issued for an APPROVED decision")
    if decision.candidate_id != candidate.candidate_id:
        raise ValueError("decision and candidate do not match")
    if ttl <= timedelta(0):
        raise ValueError("approval ttl must be positive")
    if decision.state_version is None or decision.snapshot_digest is None:
        raise ValueError("APPROVED decision lacks state or snapshot binding")
    payload = payload_digest(candidate, decision.units, decision.policy_sha256)
    return tuple(
        Approval(
            approval_id=ApprovalId(f"{decision.decision_id}:{authority.value}"),
            decision_id=decision.decision_id,
            candidate_id=candidate.candidate_id,
            authority=authority,
            state_version=decision.state_version,
            snapshot_digest=decision.snapshot_digest,
            payload_sha256=payload,
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )
        for authority in sorted(set(authorities))
    )


def _result(rule_id: str, ok: bool, message: str, observed: str | None = None) -> RuleResult:
    return RuleResult(rule_id, Stage.APPROVAL, ok, message, observed=observed)


def verify_approvals(
    approvals: Sequence[Approval],
    *,
    now: datetime,
    decision_id: DecisionId,
    state_version: int | None,
    snapshot_digest: str | None,
    payload_sha256: str,
    required: frozenset[Authority] = REQUIRED_AUTHORITIES,
) -> tuple[RuleResult, ...]:
    counts = Counter(a.authority for a in approvals)
    missing = sorted(a.value for a in required - set(counts))
    duplicated = sorted(a.value for a, n in counts.items() if n > 1)
    presence_ok = not missing and not duplicated and bool(approvals)

    decision_ok = all(a.decision_id == decision_id for a in approvals)
    binding_ok = (
        state_version is not None
        and snapshot_digest is not None
        and all(
            a.state_version == state_version and a.snapshot_digest == snapshot_digest
            for a in approvals
        )
    )
    payload_ok = all(a.payload_sha256 == payload_sha256 for a in approvals)
    expired = sorted(a.authority.value for a in approvals if not a.issued_at <= now < a.expires_at)

    return (
        _result(
            "R-APR-01",
            presence_ok,
            "all required approvals present exactly once"
            if presence_ok
            else f"missing {missing}, duplicated {duplicated}",
        ),
        _result("R-APR-02", decision_ok, "approvals belong to this decision"),
        _result(
            "R-APR-03",
            binding_ok,
            "approvals bound to the current account state and snapshots"
            if binding_ok
            else "account state or snapshots changed since approval",
            observed=f"state_version={state_version}",
        ),
        _result("R-APR-04", payload_ok, "payload (candidate, size, policy) unchanged"),
        _result(
            "R-APR-05",
            not expired,
            "approvals within their validity window" if not expired else f"expired: {expired}",
            observed=now.isoformat(),
        ),
    )
