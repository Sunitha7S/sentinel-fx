"""Approvals are bound to immutable state and expire; execution must re-check fresh state."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from sentinel.domain.decision import Approval, Authority, Outcome, RiskDecision, SignalCandidate
from sentinel.domain.types import DecisionId
from sentinel.risk.approvals import (
    REQUIRED_AUTHORITIES,
    issue_approvals,
    payload_digest,
    verify_approvals,
)
from sentinel.risk.kernel import RiskInputs, evaluate
from sentinel.risk.recheck import recheck_before_execution

from support.builders import NOW, inputs

TTL = timedelta(seconds=120)


def approved() -> tuple[RiskDecision, SignalCandidate, tuple[Approval, ...]]:
    i = inputs()
    d = evaluate(i)
    assert d.outcome is Outcome.APPROVED
    assert i.candidate is not None
    approvals = issue_approvals(
        d, i.candidate, authorities=REQUIRED_AUTHORITIES, issued_at=NOW, ttl=TTL
    )
    return d, i.candidate, approvals


def verify(
    approvals: tuple[Approval, ...], *, now_offset: timedelta = timedelta(seconds=5), **over: object
) -> list[str]:
    d, c, _ = approved()
    kwargs: dict[str, object] = {
        "now": NOW + now_offset,
        "decision_id": d.decision_id,
        "state_version": d.state_version,
        "snapshot_digest": d.snapshot_digest,
        "payload_sha256": payload_digest(c, d.units, d.policy_sha256),
    }
    kwargs.update(over)
    results = verify_approvals(approvals, **kwargs)  # type: ignore[arg-type]
    return [r.rule_id for r in results if not r.passed]


@pytest.mark.invariant("INV-APPROVAL-01")
def test_fresh_bound_approvals_verify() -> None:
    _, _, approvals = approved()
    assert verify(approvals) == []


@pytest.mark.invariant("INV-APPROVAL-01")
@pytest.mark.parametrize("offset", [TTL, TTL + timedelta(seconds=1), timedelta(days=1)])
def test_expired_approvals_fail(offset: timedelta) -> None:
    _, _, approvals = approved()
    assert "R-APR-05" in verify(approvals, now_offset=offset)


@pytest.mark.invariant("INV-APPROVAL-01")
def test_approvals_used_before_issue_time_fail() -> None:
    _, _, approvals = approved()
    assert "R-APR-05" in verify(approvals, now_offset=timedelta(seconds=-1))


@pytest.mark.invariant("INV-APPROVAL-01")
def test_approvals_cannot_outlive_policy_ttl() -> None:
    d, c, _ = approved()
    with pytest.raises(ValueError, match="ttl"):
        issue_approvals(d, c, authorities=REQUIRED_AUTHORITIES, issued_at=NOW, ttl=timedelta(0))


@pytest.mark.invariant("INV-APPROVAL-02")
def test_account_state_change_invalidates_approvals() -> None:
    _, _, approvals = approved()
    assert "R-APR-03" in verify(approvals, state_version=8)


@pytest.mark.invariant("INV-APPROVAL-02")
def test_snapshot_mismatch_invalidates_approvals() -> None:
    _, _, approvals = approved()
    assert "R-APR-03" in verify(approvals, snapshot_digest="f" * 64)
    mixed = (replace(approvals[0], snapshot_digest="e" * 64), *approvals[1:])
    assert "R-APR-03" in verify(mixed)


@pytest.mark.invariant("INV-APPROVAL-02")
def test_payload_tampering_invalidates_approvals() -> None:
    d, c, approvals = approved()
    bigger = payload_digest(c, d.units + 1, d.policy_sha256)
    assert "R-APR-04" in verify(approvals, payload_sha256=bigger)


@pytest.mark.invariant("INV-APPROVAL-02")
def test_missing_or_duplicate_authority_fails() -> None:
    _, _, approvals = approved()
    without_validation = tuple(a for a in approvals if a.authority is not Authority.VALIDATION)
    assert "R-APR-01" in verify(without_validation)
    assert "R-APR-01" in verify((*approvals, approvals[0]))


@pytest.mark.invariant("INV-APPROVAL-02")
def test_approval_for_another_decision_fails() -> None:
    _, _, approvals = approved()
    assert "R-APR-02" in verify(approvals, decision_id=DecisionId("other"))


@pytest.mark.invariant("INV-APPROVAL-02")
def test_blocked_decisions_cannot_be_approved() -> None:
    i = inputs(day_loss_pct=Decimal(2))
    d = evaluate(i)
    assert i.candidate is not None
    with pytest.raises(ValueError, match="APPROVED"):
        issue_approvals(d, i.candidate, authorities=REQUIRED_AUTHORITIES, issued_at=NOW, ttl=TTL)


# ----------------------------------------------------------------------------- recheck


def fresh(**changes: object) -> RiskInputs:
    return replace(
        inputs(now=NOW + timedelta(seconds=30), **changes),
        decision_id=DecisionId("dec-1:recheck"),
    )


@pytest.mark.invariant("INV-RECHECK-01")
def test_recheck_with_unchanged_fresh_state_approves_with_min_size() -> None:
    d, c, approvals = approved()
    r = recheck_before_execution(original=d, candidate=c, approvals=approvals, fresh=fresh())
    assert r.outcome is Outcome.APPROVED, r.blocking_rules
    assert r.units <= d.units


@pytest.mark.invariant("INV-RECHECK-01")
def test_recheck_blocks_when_fresh_account_breaches_loss_limit() -> None:
    d, c, approvals = approved()
    r = recheck_before_execution(
        original=d, candidate=c, approvals=approvals, fresh=fresh(day_loss_pct=Decimal("1.4"))
    )
    assert r.outcome is Outcome.BLOCKED
    assert "R-ACC-01" in r.blocking_rules


@pytest.mark.invariant("INV-RECHECK-01")
def test_recheck_blocks_when_account_state_version_changed() -> None:
    d, c, approvals = approved()
    r = recheck_before_execution(
        original=d, candidate=c, approvals=approvals, fresh=fresh(state_version=8)
    )
    assert "R-APR-03" in r.blocking_rules


@pytest.mark.invariant("INV-RECHECK-01")
def test_recheck_blocks_when_price_drifted() -> None:
    d, c, approvals = approved()
    f = fresh()
    eur = f.markets["EUR_USD"]
    moved = replace(eur, bid=eur.bid + Decimal("0.0006"), ask=eur.ask + Decimal("0.0006"))
    f2 = replace(f, markets={**f.markets, "EUR_USD": moved})
    r = recheck_before_execution(original=d, candidate=c, approvals=approvals, fresh=f2)
    assert "R-EXE-01" in r.blocking_rules


@pytest.mark.invariant("INV-RECHECK-01")
def test_recheck_blocks_when_policy_changed() -> None:
    d, c, approvals = approved()
    r = recheck_before_execution(
        original=replace(d, policy_sha256="a" * 64), candidate=c, approvals=approvals, fresh=fresh()
    )
    assert "R-EXE-03" in r.blocking_rules


@pytest.mark.invariant("INV-RECHECK-01")
def test_recheck_blocks_with_expired_approvals_even_if_state_is_fine() -> None:
    d, c, approvals = approved()
    late = replace(fresh(), as_of=NOW + timedelta(minutes=10))
    r = recheck_before_execution(original=d, candidate=c, approvals=approvals, fresh=late)
    assert "R-APR-05" in r.blocking_rules


@pytest.mark.invariant("INV-RECHECK-01")
def test_recheck_of_a_blocked_original_never_approves() -> None:
    i = inputs(day_loss_pct=Decimal(2))
    d = evaluate(i)
    assert i.candidate is not None
    r = recheck_before_execution(original=d, candidate=i.candidate, approvals=(), fresh=fresh())
    assert r.outcome is Outcome.BLOCKED
    assert "R-EXE-02" in r.blocking_rules
