"""Audit chain in PostgreSQL (INV-AUDIT-IMMUTABLE) and the decision/approval/shadow stores."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from db.conftest import Db
from db.test_policy_immutability import ADMIN, T0
from sentinel.audit.chain import AuditChainError, AuditKind, AuditLog, seal
from sentinel.decision.risk_gate import RiskGate
from sentinel.domain.decision import Outcome, shadow_record_from
from sentinel.domain.types import DecisionId
from sentinel.risk.approvals import REQUIRED_AUTHORITIES, issue_approvals
from sentinel.risk.kernel import evaluate
from sentinel.schemas.messages import RiskDecisionMsg
from sentinel.store.postgres.audit_sink import PostgresAuditSink
from sentinel.store.postgres.decision_store import PostgresDecisionStore, PostgresShadowStore
from sentinel.store.postgres.policy_store import PostgresPolicyStore

from support.builders import NOW, inputs, policy
from support.fakes import FixedClock, StaticProvider


def _sink_and_log(db: Db) -> tuple[PostgresAuditSink, AuditLog]:
    sink = PostgresAuditSink(db.engine("svc_audit"))
    return sink, AuditLog(sink, clock=FixedClock(NOW))


def _seed_policy(db: Db) -> None:
    PostgresPolicyStore(db.engine("human_admin")).install_initial(
        policy(), installed_by=ADMIN, request_id="init", reason="M2", at=T0
    )


# ----------------------------------------------------------------------------- audit chain


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
def test_audit_chain_round_trips_and_verifies_against_the_anchored_head(db: Db) -> None:
    sink, log = _sink_and_log(db)
    assert sink.verify() == 0
    for i in range(5):
        decision = evaluate(inputs(day_loss_pct=Decimal(i)))
        log.record(
            AuditKind.RISK_DECISION,
            f"dec-{i}",
            RiskDecisionMsg.from_domain(decision).model_dump(mode="json"),
        )
    assert sink.verify() == 5
    assert sink.head().seq == 5
    tail = sink.tail()
    assert tail is not None
    assert sink.head().hash == tail.hash


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    st.dictionaries(
        st.text(max_size=10), st.one_of(st.text(max_size=20), st.integers()), max_size=4
    )
)
def test_arbitrary_payloads_verify_byte_exactly(db: Db, payload: dict[str, object]) -> None:
    """Payloads are stored as the exact canonical text that was hashed (including NUL
    characters and non-ASCII text, which jsonb would reject or normalise)."""
    sink, log = _sink_and_log(db)
    before = sink.head().seq
    log.record(AuditKind.STATE_TRANSITION, "s", payload)
    assert sink.head().seq == before + 1
    assert sink.verify() == before + 1


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
def test_writer_cannot_fork_skip_or_rewind_the_chain(db: Db) -> None:
    sink, log = _sink_and_log(db)
    first = log.record(AuditKind.STATE_TRANSITION, "s", {"to": "NO_NEW_TRADES"})
    forged_fork = seal(
        seq=1,
        recorded_at=NOW,
        kind=AuditKind.STATE_TRANSITION,
        subject_id="s",
        payload={"to": "ACTIVE"},
        prev_hash="0" * 64,
    )
    skip = seal(
        seq=3,
        recorded_at=NOW,
        kind=AuditKind.STATE_TRANSITION,
        subject_id="s",
        payload={},
        prev_hash=first.hash,
    )
    wrong_prev = seal(
        seq=2,
        recorded_at=NOW,
        kind=AuditKind.STATE_TRANSITION,
        subject_id="s",
        payload={},
        prev_hash="a" * 64,
    )
    for record in (forged_fork, skip, wrong_prev):
        with pytest.raises(DBAPIError):
            sink.append(record)
    for sql in (
        "UPDATE sentinel.audit_records SET payload = '{}'",
        "DELETE FROM sentinel.audit_records",
        "UPDATE sentinel.audit_head SET seq = 0",
    ):
        with pytest.raises(DBAPIError), db.engine("svc_audit").begin() as conn:
            conn.execute(text(sql))
    assert sink.verify() == 1


@pytest.mark.invariant("INV-AUDIT-IMMUTABLE")
def test_superuser_tampering_with_records_is_detected_against_the_head(db: Db) -> None:
    """ADR 0011: a superuser bypasses database protections. Editing or truncating records
    without also moving the head is still caught by verification."""
    sink, log = _sink_and_log(db)
    for i in range(3):
        log.record(AuditKind.RISK_DECISION, f"dec-{i}", {"outcome": "BLOCKED", "i": i})
    superuser = db.engine()
    with superuser.begin() as conn:
        conn.execute(text("ALTER TABLE sentinel.audit_records DISABLE TRIGGER USER"))
        conn.execute(
            text("UPDATE sentinel.audit_records SET payload = :p WHERE seq = 2"),
            {
                "p": json.dumps(
                    {"i": 1, "outcome": "APPROVED"}, sort_keys=True, separators=(",", ":")
                )
            },
        )
    with pytest.raises(AuditChainError):
        sink.verify()
    with superuser.begin() as conn:
        conn.execute(text("DELETE FROM sentinel.audit_records WHERE seq >= 2"))
    with pytest.raises(AuditChainError, match="anchor"):
        sink.verify()  # the shortened chain is internally valid; only the head catches it


# ----------------------------------------------------------------------------- decisions etc.


def test_decision_approval_and_shadow_stores_round_trip(db: Db) -> None:
    _seed_policy(db)
    decisions = PostgresDecisionStore(db.engine("svc_risk"))
    shadows = PostgresShadowStore(db.engine("svc_risk"))
    i = inputs()
    d = evaluate(i)
    assert i.candidate is not None
    decisions.record(d)
    approvals = issue_approvals(
        d, i.candidate, authorities=REQUIRED_AUTHORITIES, issued_at=NOW, ttl=timedelta(minutes=2)
    )
    decisions.record_approvals(approvals)
    shadows.record(shadow_record_from(i.candidate, d))
    reader = PostgresDecisionStore(db.engine("svc_learning"))
    assert reader.get(d.decision_id) == d
    assert reader.approvals_for(d.decision_id) == tuple(
        sorted(approvals, key=lambda a: a.authority)
    )
    assert list(PostgresShadowStore(db.engine("svc_learning"))) == [
        shadow_record_from(i.candidate, d)
    ]
    assert reader.get("missing") is None


def test_database_rejects_approvals_and_shadows_that_do_not_match_their_decision(db: Db) -> None:
    _seed_policy(db)
    decisions = PostgresDecisionStore(db.engine("svc_risk"))
    i = inputs()
    approved = evaluate(i)
    blocked = replace(evaluate(inputs(day_loss_pct=Decimal(2))), decision_id=DecisionId("dec-b"))
    assert i.candidate is not None
    decisions.record(approved)
    decisions.record(blocked)
    good = issue_approvals(
        approved,
        i.candidate,
        authorities=REQUIRED_AUTHORITIES,
        issued_at=NOW,
        ttl=timedelta(minutes=2),
    )
    with pytest.raises(DBAPIError, match="APPROVED decision"):
        decisions.record_approvals([replace(good[0], decision_id=DecisionId("dec-b"))])
    with pytest.raises(DBAPIError, match="bindings"):
        decisions.record_approvals([replace(good[0], state_version=99)])
    shadow = shadow_record_from(i.candidate, approved)
    with pytest.raises(DBAPIError, match="does not match"):
        PostgresShadowStore(db.engine("svc_risk")).record(
            replace(shadow, decision_outcome=Outcome.BLOCKED)
        )


def test_risk_gate_runs_on_postgres_audit_and_shadow_stores(db: Db) -> None:
    """The M1 gate works unchanged with the Postgres adapters. The decision row is written
    first because shadow trades reference it (gate integration is M8; see report)."""
    _seed_policy(db)
    i = inputs()
    assert i.candidate is not None
    PostgresDecisionStore(db.engine("svc_risk")).record(evaluate(i))
    sink = PostgresAuditSink(db.engine("svc_audit"))
    gate = RiskGate(
        provider=StaticProvider(i),
        audit=AuditLog(sink, clock=FixedClock(NOW)),
        shadow=PostgresShadowStore(db.engine("svc_risk")),
        clock=FixedClock(NOW),
    )
    decision = gate.decide(i.candidate, DecisionId("dec-1"))
    assert decision.approved
    assert sink.verify() == 1
