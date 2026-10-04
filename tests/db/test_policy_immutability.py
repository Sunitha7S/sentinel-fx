"""INV-POLICY-IMMUTABLE.

* Policy versions are immutable.
* Policy activation is append-only and linear.
* Historical decisions always reference the exact policy hash used when they were made.
* Replacing policy history is impossible through normal service roles.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from psycopg import errors as pg
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from db.conftest import Db
from sentinel.domain.system import TradingState
from sentinel.domain.types import DecisionId, Percent
from sentinel.risk.governance import (
    Activation,
    GovernanceError,
    approve_change,
    file_change_request,
    issue_human_risk_admin,
)
from sentinel.risk.kernel import evaluate
from sentinel.risk.policy import RiskPolicy
from sentinel.store.postgres.decision_store import PostgresDecisionStore
from sentinel.store.postgres.engine import OWNER_ROLE, SERVICE_ROLES
from sentinel.store.postgres.policy_store import PolicyIntegrityError, PostgresPolicyStore

from support.builders import inputs, policy

ADMIN = issue_human_risk_admin("operator", step_up_verified=True)
T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # in the past relative to the database clock


def installed(db: Db) -> PostgresPolicyStore:
    store = PostgresPolicyStore(db.engine("human_admin"))
    store.install_initial(policy(), installed_by=ADMIN, request_id="init", reason="M2", at=T0)
    return store


def _activation(current: RiskPolicy, proposed: RiskPolicy, approved_at: datetime) -> Activation:
    request = file_change_request(
        current,
        proposed,
        request_id=f"cr-{proposed.policy_id}",
        reason="review",
        requested_by="operator",
        requested_at=approved_at,
    )
    return approve_change(
        request,
        approver=ADMIN,
        approved_at=approved_at,
        trading_state=TradingState.ACTIVE,
        drawdown=Percent(Decimal(0)),
    )


def _fails(engine: Engine, sql: str, params: dict[str, object] | None = None) -> str:
    with pytest.raises(DBAPIError) as exc, engine.begin() as conn:
        conn.execute(text(sql), params or {})
    return str(exc.value.orig)


# ----------------------------------------------------------------------------- store behaviour


@pytest.mark.invariant("INV-POLICY-IMMUTABLE")
def test_install_and_load_round_trip_verifies_the_hash(db: Db) -> None:
    store = installed(db)
    reader = PostgresPolicyStore(db.engine("svc_learning")).reader()
    assert reader.active() == policy()
    assert reader.active_sha256() == policy().sha256
    assert [h.direction for h in store.history()] == ["INITIAL"]


@pytest.mark.invariant("INV-POLICY-IMMUTABLE")
def test_tightening_and_cooled_off_loosening_extend_history_linearly(db: Db) -> None:
    store = installed(db)
    tighter = policy().with_changes(policy_id="rs_v2", risk_per_trade=Percent(Decimal("0.4")))
    store.activate(_activation(policy(), tighter, T0), approver=ADMIN, now=T0)
    looser = tighter.with_changes(policy_id="rs_v3", risk_per_trade=Percent(Decimal("0.5")))
    act = _activation(tighter, looser, T0 + timedelta(days=1))
    with pytest.raises(GovernanceError, match="cooling"):
        store.activate(act, approver=ADMIN, now=T0 + timedelta(days=1, hours=1))
    store.activate(act, approver=ADMIN, now=act.effective_at)
    history = store.history()
    assert [h.seq for h in history] == [1, 2, 3]
    assert [h.direction for h in history] == ["INITIAL", "TIGHTEN", "LOOSEN"]
    assert history[2].based_on_sha256 == tighter.sha256
    assert store.active() == looser
    # Every version stays loadable and verifiable: history is never replaced.
    assert store.version(policy().sha256) == policy()


@pytest.mark.invariant("INV-POLICY-IMMUTABLE")
def test_stale_request_is_refused_by_application_and_database(db: Db) -> None:
    store = installed(db)
    a = policy().with_changes(policy_id="rs_a", min_rr_net=Decimal(2))
    b = policy().with_changes(policy_id="rs_b", min_rr_net=Decimal("2.5"))
    act_a, act_b = _activation(policy(), a, T0), _activation(policy(), b, T0)
    store.activate(act_a, approver=ADMIN, now=T0)
    with pytest.raises(GovernanceError, match="no longer active"):
        store.activate(act_b, approver=ADMIN, now=T0)
    # Bypassing the application: the database refuses a non-linear activation too.
    message = _fails(
        db.engine("human_admin"),
        "INSERT INTO sentinel.policy_activations (seq, policy_sha256, based_on_sha256, "
        "direction, request_id, reason, approved_by, approved_at, effective_at) "
        "VALUES (3, :sha, :based, 'TIGHTEN', 'x', 'r', 'op', :t, :t)",
        {"sha": policy().sha256, "based": policy().sha256, "t": T0},
    )
    assert "based on" in message


# (seq, based on the active policy?, direction, effective_at - approved_at in hours, error)
ACTIVATION_CASES = [
    (5, True, "TIGHTEN", 0, "does not follow"),
    (2, True, "LOOSEN", 1, "check constraint"),  # loosening without the 24 h gap
    (2, True, "MIXED", 23, "check constraint"),
    (2, True, "TIGHTEN", 24 * 400, "once it is effective"),  # future-dated
    (2, True, "INITIAL", 0, "check constraint"),
    (2, False, "TIGHTEN", 0, "based on"),
    (2, True, "TIGHTEN", -1, "check constraint"),  # effective before approval
]


@pytest.mark.invariant("INV-POLICY-IMMUTABLE")
@pytest.mark.parametrize("case", ACTIVATION_CASES, ids=range(len(ACTIVATION_CASES)))
def test_database_enforces_activation_rules(db: Db, case: tuple[int, bool, str, int, str]) -> None:
    seq, based_on, direction, effective, expected = case
    installed(db)
    tighter = policy().with_changes(policy_id="rs_t", risk_per_trade=Percent(Decimal("0.3")))
    with db.engine("human_admin").begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sentinel.policy_versions (sha256, policy_id, content, created_by) "
                "VALUES (:s, 'rs_t', '{}', 'op')"
            ),
            {"s": tighter.sha256},
        )
    message = _fails(
        db.engine("human_admin"),
        "INSERT INTO sentinel.policy_activations (seq, policy_sha256, based_on_sha256, "
        "direction, request_id, reason, approved_by, approved_at, effective_at) "
        "VALUES (:seq, :sha, :based, :dir, 'r2', 'reason', 'op', :a, :e)",
        {
            "seq": seq,
            "sha": tighter.sha256,
            "based": policy().sha256 if based_on else None,
            "dir": direction,
            "a": T0,
            "e": T0 + timedelta(hours=effective),
        },
    )
    assert expected in message


@pytest.mark.invariant("INV-POLICY-IMMUTABLE")
@pytest.mark.parametrize("role", [*SERVICE_ROLES, OWNER_ROLE])
def test_no_role_can_rewrite_or_erase_policy_history(db: Db, role: str) -> None:
    installed(db)
    for sql in (
        "UPDATE sentinel.policy_versions SET content = '{}'::jsonb",
        "UPDATE sentinel.policy_activations SET policy_sha256 = policy_sha256",
        "DELETE FROM sentinel.policy_activations",
        "DELETE FROM sentinel.policy_versions",
        "TRUNCATE sentinel.policy_versions, sentinel.policy_activations CASCADE",
    ):
        message = _fails(db.engine(role), sql)
        assert "permission denied" in message or "append-only" in message, (role, sql, message)


@pytest.mark.invariant("INV-POLICY-IMMUTABLE")
def test_tampered_policy_content_is_detected_on_load(db: Db) -> None:
    """A superuser can bypass every database protection (ADR 0011). The application still
    refuses a stored policy whose content no longer hashes to its key."""
    store = installed(db)
    looser = policy().to_mapping()
    looser["limits"]["risk_per_trade_pct"] = "1.0"
    with db.engine().begin() as conn:  # superuser
        conn.execute(text("ALTER TABLE sentinel.policy_versions DISABLE TRIGGER USER"))
        conn.execute(
            text("UPDATE sentinel.policy_versions SET content = CAST(:c AS jsonb)"),
            {"c": json.dumps(looser)},
        )
        conn.execute(text("ALTER TABLE sentinel.policy_versions ENABLE TRIGGER USER"))
    with pytest.raises(PolicyIntegrityError, match="altered"):
        store.active()


@pytest.mark.invariant("INV-POLICY-IMMUTABLE")
def test_capability_and_database_role_are_both_required(db: Db) -> None:
    """A valid human capability over a service connection is still refused by the database,
    and a human_admin connection without the capability is refused by the application."""
    learning_store = PostgresPolicyStore(db.engine("svc_learning"))
    with pytest.raises(DBAPIError) as exc:
        learning_store.install_initial(
            policy(), installed_by=ADMIN, request_id="x", reason="r", at=T0
        )
    assert isinstance(exc.value.orig, pg.InsufficientPrivilege)
    admin_store = PostgresPolicyStore(db.engine("human_admin"))
    with pytest.raises(GovernanceError):
        admin_store.install_initial(
            policy(),
            installed_by="learning-service",  # type: ignore[arg-type]
            request_id="x",
            reason="r",
            at=T0,
        )


# ----------------------------------------------------------------------------- decisions


@pytest.mark.invariant("INV-POLICY-IMMUTABLE")
def test_decisions_reference_the_exact_policy_hash_they_were_made_under(db: Db) -> None:
    store = installed(db)
    decisions = PostgresDecisionStore(db.engine("svc_risk"))
    approved = evaluate(inputs())
    assert approved.approved
    decisions.record(approved)
    # Policy moves on; the historical decision still points at the old version.
    tighter = policy().with_changes(policy_id="rs_v2", min_rr_net=Decimal(2))
    store.activate(_activation(policy(), tighter, T0), approver=ADMIN, now=T0)
    loaded = decisions.get(approved.decision_id)
    assert loaded == approved
    assert loaded is not None
    assert store.version(loaded.policy_sha256 or "") == policy()
    # An APPROVED decision under a policy that is no longer active is refused...
    stale = replace(approved, decision_id=DecisionId("dec-2"))
    with pytest.raises(DBAPIError, match="active policy"):
        decisions.record(stale)
    # ...as is any decision naming a policy that was never stored.
    unknown = replace(
        evaluate(inputs(day_loss_pct=Decimal(2))),
        decision_id=DecisionId("dec-3"),
        policy_sha256="f" * 64,
    )
    with pytest.raises(DBAPIError, match="foreign key"):
        decisions.record(unknown)
    # A BLOCKED decision under the old policy is history, not authority: allowed.
    blocked = replace(evaluate(inputs(day_loss_pct=Decimal(2))), decision_id=DecisionId("dec-4"))
    decisions.record(blocked)
    assert decisions.get("dec-4") == blocked
