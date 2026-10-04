"""Pre-execution re-check: risk is recomputed from fresh broker state before any order.

An approval says "this was safe at the time". Execution needs "this is still safe now".
The re-check therefore runs the full kernel again on fresh inputs, verifies the approvals
against the *fresh* account state version, checks that price has not drifted from the
signal's entry, and that the policy is unchanged. Size is the smaller of the two decisions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

from sentinel.domain.decision import (
    Approval,
    Outcome,
    RiskDecision,
    RuleResult,
    SignalCandidate,
    Stage,
)
from sentinel.risk.approvals import payload_digest, verify_approvals
from sentinel.risk.context import RiskInputs
from sentinel.risk.kernel import evaluate

__all__ = ["recheck_before_execution"]


def _drift(original: SignalCandidate, fresh: RiskInputs) -> RuleResult:
    market = fresh.markets.get(original.symbol)
    if market is None or fresh.policy is None:
        return RuleResult(
            "R-EXE-01", Stage.EXECUTION_RECHECK, False, "no fresh price to check drift"
        )
    drift = abs(market.mid - original.entry)
    limit = fresh.policy.max_entry_drift_atr * market.atr_h1
    return RuleResult(
        "R-EXE-01",
        Stage.EXECUTION_RECHECK,
        drift <= limit,
        f"price moved {drift} from signal entry",
        observed=str(drift),
        threshold=f"<= {limit}",
    )


def recheck_before_execution(
    *,
    original: RiskDecision,
    candidate: SignalCandidate,
    approvals: Sequence[Approval],
    fresh: RiskInputs,
) -> RiskDecision:
    fresh_inputs = replace(fresh, candidate=candidate)
    fresh_decision = evaluate(fresh_inputs)

    approval_results = verify_approvals(
        approvals,
        now=fresh.as_of,
        decision_id=original.decision_id,
        state_version=fresh.account.state_version if fresh.account else None,
        snapshot_digest=original.snapshot_digest,
        payload_sha256=payload_digest(candidate, original.units, original.policy_sha256),
    )
    extra = (
        RuleResult(
            "R-EXE-02",
            Stage.EXECUTION_RECHECK,
            original.outcome is Outcome.APPROVED
            and original.candidate_id == candidate.candidate_id,
            "original decision approved this candidate",
        ),
        _drift(candidate, fresh_inputs),
        RuleResult(
            "R-EXE-03",
            Stage.EXECUTION_RECHECK,
            original.policy_sha256 is not None
            and original.policy_sha256 == fresh_decision.policy_sha256,
            "policy unchanged since approval",
            observed=fresh_decision.policy_sha256,
            threshold=original.policy_sha256,
        ),
    )
    results = (*fresh_decision.rule_results, *approval_results, *extra)
    approved = fresh_decision.approved and not any(r.blocks for r in results)
    units = min(original.units, fresh_decision.units) if approved else Decimal(0)
    approved = approved and units > 0
    return replace(
        fresh_decision,
        outcome=Outcome.APPROVED if approved else Outcome.BLOCKED,
        units=units if approved else Decimal(0),
        risk_amount=fresh_decision.risk_amount if approved else None,
        rule_results=results,
    )
