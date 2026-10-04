"""Fail-closed wrappers: any failure to produce a valid decision produces BLOCKED."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal

from sentinel.domain.decision import Outcome, RiskDecision, RuleResult, Stage
from sentinel.domain.types import CandidateId, DecisionId

__all__ = ["FAIL_CLOSED_RULE_ID", "blocked", "fail_closed", "fail_closed_async"]

FAIL_CLOSED_RULE_ID = "R-SYS-99"


def blocked(
    *, decision_id: DecisionId, candidate_id: CandidateId | None, as_of: datetime, reason: str
) -> RiskDecision:
    return RiskDecision(
        decision_id=decision_id,
        candidate_id=candidate_id,
        as_of=as_of,
        outcome=Outcome.BLOCKED,
        units=Decimal(0),
        risk_amount=None,
        effective_risk=None,
        rule_results=(RuleResult(FAIL_CLOSED_RULE_ID, Stage.SYSTEM, False, reason),),
        policy_id=None,
        policy_sha256=None,
        state_version=None,
        snapshot_digest=None,
    )


def _validated(
    result: object, *, decision_id: DecisionId, candidate_id: CandidateId | None, as_of: datetime
) -> RiskDecision:
    if not isinstance(result, RiskDecision):
        return blocked(
            decision_id=decision_id,
            candidate_id=candidate_id,
            as_of=as_of,
            reason=f"fail-closed: expected RiskDecision, got {type(result).__name__}",
        )
    if result.approved and (
        result.decision_id != decision_id or result.candidate_id != candidate_id
    ):
        return blocked(
            decision_id=decision_id,
            candidate_id=candidate_id,
            as_of=as_of,
            reason="fail-closed: decision does not match the requested decision/candidate",
        )
    return result


def fail_closed(
    fn: Callable[[], RiskDecision],
    *,
    decision_id: DecisionId,
    candidate_id: CandidateId | None,
    as_of: datetime,
) -> RiskDecision:
    try:
        result: object = fn()
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return blocked(
            decision_id=decision_id,
            candidate_id=candidate_id,
            as_of=as_of,
            reason=f"fail-closed: {type(exc).__name__}: {str(exc)[:300]}",
        )
    return _validated(result, decision_id=decision_id, candidate_id=candidate_id, as_of=as_of)


async def fail_closed_async(
    fn: Callable[[], Awaitable[RiskDecision]],
    *,
    timeout_s: float,
    decision_id: DecisionId,
    candidate_id: CandidateId | None,
    as_of: datetime,
) -> RiskDecision:
    try:
        result: object = await asyncio.wait_for(fn(), timeout_s)
    except TimeoutError:
        return blocked(
            decision_id=decision_id,
            candidate_id=candidate_id,
            as_of=as_of,
            reason=f"fail-closed: timeout after {timeout_s}s",
        )
    except Exception as exc:  # noqa: BLE001 - deliberately total
        return blocked(
            decision_id=decision_id,
            candidate_id=candidate_id,
            as_of=as_of,
            reason=f"fail-closed: {type(exc).__name__}: {str(exc)[:300]}",
        )
    return _validated(result, decision_id=decision_id, candidate_id=candidate_id, as_of=as_of)
