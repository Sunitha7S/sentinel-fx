"""Fail-closed decision gate, audit trail and shadow-trade persistence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.audit.chain import (
    AuditChainError,
    AuditKind,
    AuditLog,
    InMemoryAuditSink,
    verify_chain,
)
from sentinel.decision.fail_closed import FAIL_CLOSED_RULE_ID, fail_closed, fail_closed_async
from sentinel.decision.risk_gate import RiskGate
from sentinel.domain.decision import Outcome, RiskDecision, SignalCandidate
from sentinel.domain.types import CandidateId, DecisionId
from sentinel.risk.kernel import RiskInputs
from sentinel.store.audit_jsonl import JsonlAuditSink
from sentinel.store.shadow_jsonl import JsonlShadowStore

from support.builders import NOW, inputs
from support.fakes import PROVIDER_METHODS, FixedClock, ListShadowSink, StaticProvider


class FailingAuditSink(InMemoryAuditSink):
    def append(self, record: Any) -> None:
        raise OSError("disk full")


def gate(
    source: RiskInputs | None = None,
    *,
    raise_on: frozenset[str] = frozenset(),
    missing: frozenset[str] = frozenset(),
    audit_sink: InMemoryAuditSink | None = None,
    shadow: ListShadowSink | None = None,
    kernel: Any = None,
) -> tuple[RiskGate, InMemoryAuditSink, ListShadowSink]:
    sink = audit_sink if audit_sink is not None else InMemoryAuditSink()
    shadows = shadow if shadow is not None else ListShadowSink()
    kwargs: dict[str, Any] = {}
    if kernel is not None:
        kwargs["kernel"] = kernel
    g = RiskGate(
        provider=StaticProvider(source or inputs(), raise_on=raise_on, missing=missing),
        audit=AuditLog(sink, clock=FixedClock(NOW)),
        shadow=shadows,
        clock=FixedClock(NOW),
        **kwargs,
    )
    return g, sink, shadows


def candidate() -> SignalCandidate:
    c = inputs().candidate
    assert c is not None
    return c


# ----------------------------------------------------------------------------- fail closed


@pytest.mark.invariant("INV-FAILCLOSED-01")
def test_gate_approves_baseline_and_records_it() -> None:
    g, sink, shadows = gate()
    d = g.decide(candidate(), DecisionId("dec-1"))
    assert d.outcome is Outcome.APPROVED
    assert [r.kind for r in sink] == [AuditKind.RISK_DECISION]
    assert len(shadows.records) == 1


@pytest.mark.invariant("INV-FAILCLOSED-01")
@pytest.mark.parametrize("method", PROVIDER_METHODS)
def test_any_provider_exception_blocks(method: str) -> None:
    g, sink, _ = gate(raise_on=frozenset({method}))
    d = g.decide(candidate(), DecisionId("dec-1"))
    assert d.outcome is Outcome.BLOCKED
    assert FAIL_CLOSED_RULE_ID in d.blocking_rules
    assert any(r.kind is AuditKind.RISK_DECISION for r in sink)


@pytest.mark.invariant("INV-FAILCLOSED-01")
@pytest.mark.parametrize("method", [m for m in PROVIDER_METHODS if m != "holidays"])
def test_any_provider_returning_nothing_blocks(method: str) -> None:
    g, _, _ = gate(missing=frozenset({method}))
    assert g.decide(candidate(), DecisionId("dec-1")).outcome is Outcome.BLOCKED


@pytest.mark.invariant("INV-FAILCLOSED-01")
def test_kernel_exception_blocks() -> None:
    def broken(_: RiskInputs) -> RiskDecision:
        raise ZeroDivisionError("bug")

    g, _, _ = gate(kernel=broken)
    d = g.decide(candidate(), DecisionId("dec-1"))
    assert d.outcome is Outcome.BLOCKED
    assert FAIL_CLOSED_RULE_ID in d.blocking_rules


@pytest.mark.invariant("INV-FAILCLOSED-01")
def test_kernel_returning_garbage_blocks() -> None:
    g, _, _ = gate(kernel=lambda _: "APPROVED")
    assert g.decide(candidate(), DecisionId("dec-1")).outcome is Outcome.BLOCKED


@pytest.mark.invariant("INV-FAILCLOSED-01")
def test_kernel_approving_another_candidate_blocks() -> None:
    from sentinel.risk.kernel import evaluate

    def wrong_candidate(i: RiskInputs) -> RiskDecision:
        return replace(evaluate(i), candidate_id=CandidateId("someone-else"))

    g, _, _ = gate(kernel=wrong_candidate)
    assert g.decide(candidate(), DecisionId("dec-1")).outcome is Outcome.BLOCKED


@pytest.mark.invariant("INV-FAILCLOSED-01")
def test_audit_failure_turns_approval_into_block() -> None:
    g, _, shadows = gate(audit_sink=FailingAuditSink())
    d = g.decide(candidate(), DecisionId("dec-1"))
    assert d.outcome is Outcome.BLOCKED
    assert FAIL_CLOSED_RULE_ID in d.blocking_rules
    assert all(r.decision_outcome is Outcome.BLOCKED for r in shadows.records)


@pytest.mark.invariant("INV-FAILCLOSED-01")
def test_shadow_failure_turns_approval_into_block() -> None:
    g, sink, _ = gate(shadow=ListShadowSink(fail=True))
    d = g.decide(candidate(), DecisionId("dec-1"))
    assert d.outcome is Outcome.BLOCKED
    # the block itself is audited
    assert list(sink)[-1].payload["outcome"] == "BLOCKED"  # type: ignore[index,call-overload]


@pytest.mark.invariant("INV-FAILCLOSED-01")
def test_fail_closed_wrapper_handles_exceptions_and_wrong_types() -> None:
    def boom() -> RiskDecision:
        raise RuntimeError("x")

    for fn in (boom, lambda: None):
        d = fail_closed(fn, decision_id=DecisionId("d"), candidate_id=None, as_of=NOW)  # type: ignore[arg-type]
        assert d.outcome is Outcome.BLOCKED
        assert d.blocking_rules == (FAIL_CLOSED_RULE_ID,)


@pytest.mark.invariant("INV-FAILCLOSED-01")
def test_async_timeout_blocks() -> None:
    async def slow() -> RiskDecision:
        await asyncio.sleep(1)
        raise AssertionError("unreachable")

    d = asyncio.run(
        fail_closed_async(
            slow, timeout_s=0.01, decision_id=DecisionId("d"), candidate_id=None, as_of=NOW
        )
    )
    assert d.outcome is Outcome.BLOCKED
    assert "timeout" in d.rule_results[0].message.lower()


# ----------------------------------------------------------------------------- audit


@pytest.mark.invariant("INV-AUDIT-01")
def test_every_decision_is_audited_with_full_rule_results() -> None:
    g, sink, _ = gate()
    g.decide(candidate(), DecisionId("dec-1"))
    g2, _, _ = gate(inputs(day_loss_pct=Decimal(2)), audit_sink=sink)
    g2.decide(candidate(), DecisionId("dec-2"))
    decisions = [r for r in sink if r.kind is AuditKind.RISK_DECISION]
    assert [r.subject_id for r in decisions] == ["dec-1", "dec-2"]
    for record in decisions:
        payload = record.payload
        assert isinstance(payload, dict)
        assert payload["rule_results"]
        assert payload["policy_sha256"]
    assert verify_chain(sink) == len(list(sink))


@pytest.mark.invariant("INV-AUDIT-01")
def test_tampering_with_any_audit_record_is_detected() -> None:
    g, sink, _ = gate()
    g.decide(candidate(), DecisionId("dec-1"))
    g.decide(candidate(), DecisionId("dec-2"))
    records = list(sink)
    assert len(records) == 2
    forged_payload = dict(records[0].payload)  # type: ignore[arg-type]
    forged_payload["outcome"] = "APPROVED_BY_HAND"
    forged = [replace(records[0], payload=forged_payload), *records[1:]]
    with pytest.raises(AuditChainError):
        verify_chain(forged)
    with pytest.raises(AuditChainError):
        verify_chain(records[1:])  # deleting the first record breaks the chain
    with pytest.raises(AuditChainError):
        verify_chain([records[1], records[0]])  # reordering breaks it too


@pytest.mark.invariant("INV-AUDIT-01")
def test_jsonl_audit_log_survives_restart_and_detects_edits(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    g, _, _ = gate(audit_sink=JsonlAuditSink(path))  # type: ignore[arg-type]
    g.decide(candidate(), DecisionId("dec-1"))
    reopened = JsonlAuditSink(path)
    assert verify_chain(reopened) == 1
    log = AuditLog(reopened, clock=FixedClock(NOW))
    record = log.record(AuditKind.STATE_TRANSITION, "sys", {"to": "HALTED"})
    assert record.seq == 2

    lines = path.read_text(encoding="utf-8").splitlines()
    doc = json.loads(lines[0])
    doc["payload"]["outcome"] = "APPROVED_BY_HAND"
    lines[0] = json.dumps(doc)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(AuditChainError):
        JsonlAuditSink(path)


# ----------------------------------------------------------------------------- shadow trades


DAY_LOSS = st.decimals(min_value=Decimal(0), max_value=Decimal(3), places=2)


@pytest.mark.invariant("INV-SHADOW-01")
@given(DAY_LOSS, st.integers(0, 6))
def test_every_candidate_is_persisted_as_a_shadow_trade(day_loss: Decimal, losses: int) -> None:
    g, _, shadows = gate(inputs(day_loss_pct=day_loss, consecutive_losses=losses))
    d = g.decide(candidate(), DecisionId("dec-1"))
    assert len(shadows.records) == 1
    record = shadows.records[0]
    assert record.decision_id == d.decision_id
    assert record.decision_outcome is d.outcome
    assert record.blocking_rules == d.blocking_rules
    assert record.snapshot_digest == d.snapshot_digest


@pytest.mark.invariant("INV-SHADOW-01")
def test_shadow_store_round_trip(tmp_path: Path) -> None:
    store = JsonlShadowStore(tmp_path / "shadow.jsonl")
    g = RiskGate(
        provider=StaticProvider(inputs(day_loss_pct=Decimal(2))),
        audit=AuditLog(InMemoryAuditSink(), clock=FixedClock(NOW)),
        shadow=store,
        clock=FixedClock(NOW),
    )
    d = g.decide(candidate(), DecisionId("dec-9"))
    loaded = list(JsonlShadowStore(tmp_path / "shadow.jsonl"))
    assert len(loaded) == 1
    assert loaded[0].decision_outcome is Outcome.BLOCKED
    assert loaded[0].blocking_rules == d.blocking_rules
    assert loaded[0].entry == candidate().entry
