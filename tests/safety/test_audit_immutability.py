"""INV-AUDIT-IMMUTABLE: no historical decision can be modified without breaking verification.

Mutations are generated over chains of real risk decisions and arbitrary records. Two
classes of tampering are distinguished honestly:

* edits, deletions, insertions and reorders anywhere *inside* the chain are detected by the
  hash chain alone;
* truncating the newest records, appending forged records, or re-hashing the whole chain
  after an edit are detected only when verifying against an anchored ``AuditHead``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.audit.chain import (
    GENESIS_HASH,
    AuditChainError,
    AuditHead,
    AuditKind,
    AuditLog,
    AuditRecord,
    InMemoryAuditSink,
    seal,
    verify_chain,
)
from sentinel.domain.types import DecisionId
from sentinel.risk.kernel import evaluate
from sentinel.schemas.messages import RiskDecisionMsg

from support.builders import NOW, inputs
from support.fakes import FixedClock

PAYLOADS = st.dictionaries(
    st.text(min_size=1, max_size=8),
    st.one_of(st.integers(), st.text(max_size=12), st.booleans(), st.none()),
    max_size=4,
)


def build_chain(payloads: list[dict[str, object]]) -> list[AuditRecord]:
    sink = InMemoryAuditSink()
    log = AuditLog(sink, clock=FixedClock(NOW))
    for i, payload in enumerate(payloads):
        log.record(AuditKind.RISK_DECISION, f"dec-{i}", payload)
    return list(sink)


def reseal_from(records: list[AuditRecord], start: int) -> list[AuditRecord]:
    """What an attacker with write access does: recompute every hash after an edit."""
    out = records[:start]
    prev = out[-1].hash if out else GENESIS_HASH
    for i, r in enumerate(records[start:], start=start):
        sealed = seal(
            seq=i + 1,
            recorded_at=r.recorded_at,
            kind=r.kind,
            subject_id=r.subject_id,
            payload=r.payload,
            prev_hash=prev,
        )
        out.append(sealed)
        prev = sealed.hash
    return out


Mutation = Callable[[list[AuditRecord], int], list[AuditRecord]]


def _edit(records: list[AuditRecord], i: int, **changes: object) -> list[AuditRecord]:
    return [*records[:i], dataclasses.replace(records[i], **changes), *records[i + 1 :]]  # type: ignore[arg-type]


INTERNAL: dict[str, Mutation] = {
    "edit payload": lambda r, i: _edit(r, i, payload={"outcome": "APPROVED_BY_HAND"}),
    "edit subject": lambda r, i: _edit(r, i, subject_id="someone-else"),
    "edit kind": lambda r, i: _edit(r, i, kind=AuditKind.POLICY_ACTIVATED),
    "edit time": lambda r, i: _edit(r, i, recorded_at=NOW + timedelta(seconds=1)),
    "edit seq": lambda r, i: _edit(r, i, seq=r[i].seq + 7),
    "edit hash": lambda r, i: _edit(r, i, hash="f" * 64),
    "delete": lambda r, i: [*r[:i], *r[i + 1 :]],
    "duplicate": lambda r, i: [*r[: i + 1], r[i], *r[i + 1 :]],
}


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
@pytest.mark.parametrize("name", sorted(INTERNAL))
@given(payloads=st.lists(PAYLOADS, min_size=2, max_size=10), data=st.data())
def test_any_internal_mutation_is_detected_without_an_anchor(
    name: str, payloads: list[dict[str, object]], data: st.DataObject
) -> None:
    records = build_chain(payloads)
    # Every record except the newest: "internal" means a later record still follows it.
    i = data.draw(st.integers(0, len(records) - 2))
    with pytest.raises(AuditChainError):
        verify_chain(INTERNAL[name](records, i))


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
@given(payloads=st.lists(PAYLOADS, min_size=2, max_size=10), data=st.data())
def test_reordering_is_detected(payloads: list[dict[str, object]], data: st.DataObject) -> None:
    records = build_chain(payloads)
    i = data.draw(st.integers(0, len(records) - 2))
    swapped = [*records[:i], records[i + 1], records[i], *records[i + 2 :]]
    with pytest.raises(AuditChainError):
        verify_chain(swapped)


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
@pytest.mark.parametrize("name", sorted(INTERNAL))
@given(payloads=st.lists(PAYLOADS, min_size=1, max_size=10), data=st.data())
def test_any_mutation_including_the_newest_record_is_detected_with_an_anchor(
    name: str, payloads: list[dict[str, object]], data: st.DataObject
) -> None:
    records = build_chain(payloads)
    head = AuditHead.of(records[-1])
    i = data.draw(st.integers(0, len(records) - 1))
    with pytest.raises(AuditChainError):
        verify_chain(INTERNAL[name](records, i), expected_head=head)


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
@given(payloads=st.lists(PAYLOADS, min_size=2, max_size=10), data=st.data())
def test_tail_truncation_needs_the_anchor(
    payloads: list[dict[str, object]], data: st.DataObject
) -> None:
    records = build_chain(payloads)
    head = AuditHead.of(records[-1])
    keep = data.draw(st.integers(0, len(records) - 1))
    truncated = records[:keep]
    assert verify_chain(truncated) == keep  # the honest limit of an unkeyed chain
    with pytest.raises(AuditChainError):
        verify_chain(truncated, expected_head=head)


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
@given(payloads=st.lists(PAYLOADS, min_size=1, max_size=10), data=st.data())
def test_full_reseal_after_an_edit_needs_the_anchor(
    payloads: list[dict[str, object]], data: st.DataObject
) -> None:
    records = build_chain(payloads)
    head = AuditHead.of(records[-1])
    i = data.draw(st.integers(0, len(records) - 1))
    forged = reseal_from(_edit(records, i, payload={"outcome": "APPROVED_BY_HAND"}), i)
    assert verify_chain(forged) == len(records)  # internally consistent forgery
    with pytest.raises(AuditChainError):
        verify_chain(forged, expected_head=head)


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
def test_appended_forgery_is_detected_with_the_anchor() -> None:
    records = build_chain([{"a": 1}, {"a": 2}])
    head = AuditHead.of(records[-1])
    forged = seal(
        seq=3,
        recorded_at=NOW,
        kind=AuditKind.RISK_DECISION,
        subject_id="dec-x",
        payload={"outcome": "APPROVED"},
        prev_hash=records[-1].hash,
    )
    assert verify_chain([*records, forged]) == 3
    with pytest.raises(AuditChainError):
        verify_chain([*records, forged], expected_head=head)


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
def test_a_recorded_risk_decision_cannot_be_altered_in_place() -> None:
    decisions = [
        evaluate(inputs()),
        evaluate(inputs(day_loss_pct=Decimal(2))),
    ]
    sink = InMemoryAuditSink()
    log = AuditLog(sink, clock=FixedClock(NOW))
    for n, d in enumerate(decisions):
        log.record(
            AuditKind.RISK_DECISION,
            f"{d.decision_id}-{n}",
            RiskDecisionMsg.from_domain(d).model_dump(mode="json"),
        )
    records = list(sink)
    head = AuditHead.of(records[-1])
    assert verify_chain(records, expected_head=head) == 2

    # Flip the blocked decision to approved, with units, in the stored record.
    payload = dict(records[1].payload)  # type: ignore[arg-type]
    payload.update(outcome="APPROVED", units="9999")
    with pytest.raises(AuditChainError):
        verify_chain(_edit(records, 1, payload=payload), expected_head=head)

    # And the in-memory decision object itself is immutable.
    with pytest.raises(dataclasses.FrozenInstanceError):
        decisions[0].units = Decimal(1)  # type: ignore[misc]
    assert decisions[0].decision_id == DecisionId("dec-1")


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
def test_empty_chain_does_not_satisfy_an_anchor() -> None:
    assert verify_chain([]) == 0
    with pytest.raises(AuditChainError):
        verify_chain([], expected_head=AuditHead(1, "a" * 64))
