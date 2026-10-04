"""Protected risk policy, governance of changes, and the trading-state machine."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.audit.chain import AuditKind, AuditLog, InMemoryAuditSink
from sentinel.config.policy_loader import parse_policy_yaml
from sentinel.decision.state_service import AuditedStateMachine
from sentinel.domain.system import Actor, ActorKind, SystemMode, TradingState, initial_system_state
from sentinel.domain.types import Percent
from sentinel.risk.governance import (
    COOLING_OFF,
    ChangeDirection,
    ChangeRequest,
    GovernanceError,
    HumanRiskAdmin,
    ProtectedPolicyStore,
    approve_change,
    classify_change,
    file_change_request,
    issue_human_risk_admin,
)
from sentinel.risk.policy import PolicyViolation
from sentinel.risk.state_machine import TransitionRefused, transition

from support.builders import NOW, POLICY_PATH, policy
from support.fakes import FixedClock

SRC = Path(__file__).resolve().parents[2] / "src" / "sentinel"
HUMAN = Actor(ActorKind.HUMAN, "operator")
AUTO = Actor(ActorKind.AUTO, "R-ACC-04")


def admin() -> HumanRiskAdmin:
    return issue_human_risk_admin("operator", step_up_verified=True)


def store() -> ProtectedPolicyStore:
    return ProtectedPolicyStore(policy(), installed_by=admin(), at=NOW)


# ----------------------------------------------------------------------------- learning access


def _imported_modules(package: Path) -> set[str]:
    found: set[str] = set()
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names)
    return found


@pytest.mark.invariant("INV-POLICY-01")
def test_learning_package_cannot_import_policy_writers() -> None:
    imported = _imported_modules(SRC / "learning")
    forbidden = ("sentinel.risk.governance", "sentinel.store", "sentinel.execution")
    assert sorted(m for m in imported if m.startswith(forbidden)) == []


@pytest.mark.invariant("INV-POLICY-01")
def test_read_only_view_exposes_no_way_to_write() -> None:
    reader = store().reader()
    assert reader.active() == policy()
    assert reader.active_sha256() == policy().sha256
    public = {n for n in dir(reader) if not n.startswith("_")}
    assert public == {"active", "active_sha256"}
    with pytest.raises(AttributeError):
        reader.activate  # type: ignore[attr-defined]  # noqa: B018


@pytest.mark.invariant("INV-POLICY-01")
def test_human_capability_cannot_be_forged() -> None:
    with pytest.raises(GovernanceError):
        HumanRiskAdmin("learning-service", object())
    with pytest.raises(GovernanceError):
        issue_human_risk_admin("learning-service", step_up_verified=False)


@pytest.mark.invariant("INV-POLICY-01")
def test_activation_requires_the_human_capability() -> None:
    s = store()
    proposed = policy().with_changes(policy_id="rs_v2", risk_per_trade=Percent(Decimal("0.4")))
    request = file_change_request(
        s.active(),
        proposed,
        request_id="cr-1",
        reason="tighten",
        requested_by="learning",
        requested_at=NOW,
    )
    activation = approve_change(
        request,
        approver=admin(),
        approved_at=NOW,
        trading_state=TradingState.ACTIVE,
        drawdown=Percent(Decimal(0)),
    )
    with pytest.raises(GovernanceError):
        s.activate(activation, approver="learning-service", now=NOW)  # type: ignore[arg-type]
    assert s.active() == policy()


@pytest.mark.invariant("INV-POLICY-01")
def test_policy_objects_are_immutable() -> None:
    p = policy()
    with pytest.raises(AttributeError):
        p.risk_per_trade = Percent(Decimal(5))  # type: ignore[misc]


# ----------------------------------------------------------------------------- governance


def _request(proposed_changes: dict[str, object]) -> ChangeRequest:
    proposed = policy().with_changes(policy_id="rs_v2", **proposed_changes)
    return file_change_request(
        policy(),
        proposed,
        request_id="cr-1",
        reason="review",
        requested_by="operator",
        requested_at=NOW,
    )


@pytest.mark.invariant("INV-POLICY-02")
def test_tightening_activates_immediately() -> None:
    s = store()
    req = _request({"risk_per_trade": Percent(Decimal("0.25"))})
    act = approve_change(
        req,
        approver=admin(),
        approved_at=NOW,
        trading_state=TradingState.ACTIVE,
        drawdown=Percent(Decimal(0)),
    )
    assert act.direction is ChangeDirection.TIGHTEN
    assert act.effective_at == NOW
    assert s.activate(act, approver=admin(), now=NOW).risk_per_trade == Percent(Decimal("0.25"))


@pytest.mark.invariant("INV-POLICY-02")
def test_loosening_waits_for_cooling_off_period() -> None:
    s = store()
    req = _request({"risk_per_trade": Percent(Decimal("0.75"))})
    act = approve_change(
        req,
        approver=admin(),
        approved_at=NOW,
        trading_state=TradingState.ACTIVE,
        drawdown=Percent(Decimal(0)),
    )
    assert act.direction is ChangeDirection.LOOSEN
    assert act.effective_at >= NOW + COOLING_OFF
    with pytest.raises(GovernanceError, match="cooling"):
        s.activate(act, approver=admin(), now=NOW + COOLING_OFF - timedelta(seconds=1))
    assert s.activate(act, approver=admin(), now=act.effective_at).risk_per_trade.value == Decimal(
        "0.75"
    )


@pytest.mark.invariant("INV-POLICY-02")
def test_mixed_changes_are_treated_as_loosening() -> None:
    req = _request({"risk_per_trade": Percent(Decimal("0.25")), "daily_loss": Percent(Decimal(2))})
    act = approve_change(
        req,
        approver=admin(),
        approved_at=NOW,
        trading_state=TradingState.ACTIVE,
        drawdown=Percent(Decimal(0)),
    )
    assert act.direction is ChangeDirection.MIXED
    assert act.effective_at >= NOW + COOLING_OFF


@pytest.mark.invariant("INV-POLICY-02")
@pytest.mark.parametrize(
    ("state", "dd"),
    [(TradingState.HALTED, Decimal(0)), (TradingState.ACTIVE, Decimal("5.01"))],
)
def test_loosening_is_refused_while_halted_or_in_drawdown(state: TradingState, dd: Decimal) -> None:
    req = _request({"max_drawdown": Percent(Decimal(12))})
    with pytest.raises(GovernanceError):
        approve_change(
            req, approver=admin(), approved_at=NOW, trading_state=state, drawdown=Percent(dd)
        )


@pytest.mark.invariant("INV-POLICY-02")
def test_activation_of_a_stale_request_is_refused() -> None:
    s = store()
    first = approve_change(
        _request({"risk_per_trade": Percent(Decimal("0.25"))}),
        approver=admin(),
        approved_at=NOW,
        trading_state=TradingState.ACTIVE,
        drawdown=Percent(Decimal(0)),
    )
    second = approve_change(
        _request({"min_rr_net": Decimal(2)}),
        approver=admin(),
        approved_at=NOW,
        trading_state=TradingState.ACTIVE,
        drawdown=Percent(Decimal(0)),
    )
    s.activate(first, approver=admin(), now=NOW)
    with pytest.raises(GovernanceError, match="based on"):
        s.activate(second, approver=admin(), now=NOW)


@pytest.mark.invariant("INV-POLICY-02")
@pytest.mark.parametrize(
    ("change", "direction"),
    [
        ({"risk_per_trade": Percent(Decimal("0.4"))}, ChangeDirection.TIGHTEN),
        ({"min_rr_net": Decimal(2)}, ChangeDirection.TIGHTEN),
        ({"blackout_tier1_before": timedelta(minutes=90)}, ChangeDirection.TIGHTEN),
        ({"max_price_age": timedelta(seconds=5)}, ChangeDirection.TIGHTEN),
        ({"max_spread_ratio": Decimal(2)}, ChangeDirection.LOOSEN),
        ({"min_calendar_sources": 1}, ChangeDirection.LOOSEN),
        ({"rollover_tz": "Europe/London"}, ChangeDirection.LOOSEN),
    ],
)
def test_change_direction_classification(
    change: dict[str, object], direction: ChangeDirection
) -> None:
    proposed = policy().with_changes(**change)
    assert classify_change(policy(), proposed)[0] is direction


@pytest.mark.invariant("INV-POLICY-02")
def test_every_policy_field_has_a_classification() -> None:
    from sentinel.risk.governance import UNCLASSIFIED_FIELDS, field_direction

    for name in policy().field_names():
        if name in UNCLASSIFIED_FIELDS:
            continue
        assert field_direction(name) is not None, name


@pytest.mark.invariant("INV-POLICY-02")
@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("limits.risk_per_trade_pct", "1.5"),
        ("loss_limits.daily_pct", "4"),
        ("loss_limits.max_drawdown_pct", "25"),
        ("limits.max_effective_leverage", "20"),
        ("execution.require_broker_side_stop", "false"),
        ("drawdown_throttle", "[{drawdown_pct: 4.0, risk_multiplier: 1.5}]"),
        (
            "drawdown_throttle",
            "[{drawdown_pct: 4.0, risk_multiplier: 0.5},"
            " {drawdown_pct: 6.0, risk_multiplier: 0.75}]",
        ),
        ("data_freshness_seconds.price", "600"),
        ("trade_quality.min_rr_net", "0.5"),
    ],
)
def test_code_ceilings_reject_dangerous_policy_files(path: str, value: str) -> None:
    text = POLICY_PATH.read_text(encoding="utf-8")
    section, _, key = path.rpartition(".")
    if not section:
        start = text.index(f"\n{key}:")
        end = text.index("\n\n", start + 1)
        mutated = text[:start] + f"\n{key}: {value}" + text[end:]
    else:
        lines = text.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith(f"{key}:"))
        indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip())]
        lines[idx] = f"{indent}{key}: {value}"
        mutated = "\n".join(lines)
    with pytest.raises(PolicyViolation):
        parse_policy_yaml(mutated)


@pytest.mark.invariant("INV-POLICY-02")
def test_unknown_or_missing_policy_keys_are_rejected() -> None:
    text = POLICY_PATH.read_text(encoding="utf-8")
    with pytest.raises(PolicyViolation, match="unknown"):
        parse_policy_yaml(text + "\nsurprise_section: {a: 1}\n")
    with pytest.raises(PolicyViolation, match="missing"):
        parse_policy_yaml(text.replace("  daily_pct: 1.5", ""))


@pytest.mark.invariant("INV-POLICY-02")
def test_policy_hash_is_stable_and_content_addressed() -> None:
    a = parse_policy_yaml(POLICY_PATH.read_text(encoding="utf-8"))
    b = parse_policy_yaml(POLICY_PATH.read_text(encoding="utf-8").replace("0.50", "0.5"))
    assert a.sha256 == b.sha256 == policy().sha256
    assert a.with_changes(min_rr_net=Decimal(2)).sha256 != a.sha256


# ----------------------------------------------------------------------------- state machine

STATES = list(TradingState)


@pytest.mark.invariant("INV-STATE-01")
@given(st.sampled_from(STATES), st.sampled_from(STATES))
def test_automatic_transitions_only_tighten(current: TradingState, target: TradingState) -> None:
    state = replace(initial_system_state(NOW), trading_state=current)
    if target.restrictiveness > current.restrictiveness:
        new, record = transition(state, target, actor=AUTO, reason="limit breached", at=NOW)
        assert new.trading_state is target
        assert record.from_state is current
    else:
        with pytest.raises(TransitionRefused):
            transition(state, target, actor=AUTO, reason="auto", at=NOW)


@pytest.mark.invariant("INV-STATE-01")
def test_humans_resume_one_step_at_a_time() -> None:
    halted = initial_system_state(NOW)
    with pytest.raises(TransitionRefused):
        transition(halted, TradingState.ACTIVE, actor=HUMAN, reason="go", at=NOW)
    nnt, _ = transition(halted, TradingState.NO_NEW_TRADES, actor=HUMAN, reason="reviewed", at=NOW)
    active, _ = transition(nnt, TradingState.ACTIVE, actor=HUMAN, reason="ready", at=NOW)
    assert active.trading_state is TradingState.ACTIVE
    flatten, _ = transition(active, TradingState.FLATTEN, actor=HUMAN, reason="kill", at=NOW)
    with pytest.raises(TransitionRefused):
        transition(flatten, TradingState.ACTIVE, actor=HUMAN, reason="oops", at=NOW)


@pytest.mark.invariant("INV-STATE-01")
def test_transition_needs_a_reason_and_monotonic_time() -> None:
    halted = initial_system_state(NOW)
    with pytest.raises(TransitionRefused):
        transition(halted, TradingState.NO_NEW_TRADES, actor=HUMAN, reason="  ", at=NOW)
    with pytest.raises(TransitionRefused):
        transition(
            halted,
            TradingState.NO_NEW_TRADES,
            actor=HUMAN,
            reason="x",
            at=NOW - timedelta(seconds=1),
        )


@pytest.mark.invariant("INV-STATE-01")
def test_every_transition_and_refusal_is_audited() -> None:
    sink = InMemoryAuditSink()
    machine = AuditedStateMachine(
        initial_system_state(NOW, SystemMode.SHADOW), AuditLog(sink, clock=FixedClock(NOW))
    )
    machine.request(TradingState.NO_NEW_TRADES, actor=HUMAN, reason="reviewed", at=NOW)
    with pytest.raises(TransitionRefused):
        machine.request(TradingState.ACTIVE, actor=AUTO, reason="auto-resume", at=NOW)
    kinds = [r.kind for r in sink]
    assert kinds == [AuditKind.STATE_TRANSITION, AuditKind.STATE_TRANSITION_REFUSED]
    assert machine.state.trading_state is TradingState.NO_NEW_TRADES


@pytest.mark.invariant("INV-STATE-01")
def test_state_is_unchanged_when_audit_fails() -> None:
    class Broken(InMemoryAuditSink):
        def append(self, record: object) -> None:
            raise OSError("disk full")

    machine = AuditedStateMachine(
        initial_system_state(NOW), AuditLog(Broken(), clock=FixedClock(NOW))
    )
    with pytest.raises(OSError, match="disk full"):
        machine.request(TradingState.NO_NEW_TRADES, actor=HUMAN, reason="reviewed", at=NOW)
    assert machine.state.trading_state is TradingState.HALTED
