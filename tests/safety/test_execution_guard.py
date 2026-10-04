"""Execution is disabled in M0-M1, and live trading is impossible unless explicitly enabled."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.audit.chain import AuditKind, AuditLog, InMemoryAuditSink
from sentinel.config.settings import Environment, SettingsError, load_settings
from sentinel.domain.system import BrokerEnvironment, SystemMode
from sentinel.execution import guard
from sentinel.execution.adapters.base import DisabledBrokerAdapter
from sentinel.execution.guard import (
    ExecutionEnvironment,
    ExecutionRefused,
    assert_execution_permitted,
    execution_refusals,
)
from sentinel.risk.kernel import evaluate

from support.builders import NOW, REPO_ROOT, inputs
from support.fakes import FixedClock

CONFIG = REPO_ROOT / "config"

FULLY_ENABLED_LIVE = ExecutionEnvironment(
    mode=SystemMode.LIVE,
    execution_enabled=True,
    live_trading_enabled=True,
    broker_environment=BrokerEnvironment.LIVE,
)


@pytest.mark.invariant("INV-EXEC-01")
def test_execution_is_hard_disabled_in_this_milestone() -> None:
    assert guard.EXECUTION_HARD_DISABLED is True


@pytest.mark.invariant("INV-EXEC-01")
@pytest.mark.parametrize("mode", list(SystemMode))
@pytest.mark.parametrize("broker", list(BrokerEnvironment))
def test_every_environment_is_refused_while_hard_disabled(
    mode: SystemMode, broker: BrokerEnvironment
) -> None:
    env = ExecutionEnvironment(
        mode=mode, execution_enabled=True, live_trading_enabled=True, broker_environment=broker
    )
    with pytest.raises(ExecutionRefused) as exc:
        assert_execution_permitted(env)
    assert any("hard-disabled" in r for r in exc.value.reasons)


@pytest.mark.invariant("INV-EXEC-01")
def test_live_mode_without_live_flag_is_refused_on_its_own_merits() -> None:
    env = ExecutionEnvironment(
        mode=SystemMode.LIVE,
        execution_enabled=True,
        live_trading_enabled=False,
        broker_environment=BrokerEnvironment.LIVE,
    )
    assert any("live trading is not enabled" in r for r in execution_refusals(env))


@pytest.mark.invariant("INV-EXEC-01")
@pytest.mark.parametrize(
    ("mode", "broker", "expected"),
    [
        (SystemMode.PAPER, BrokerEnvironment.LIVE, "PAPER mode requires a PRACTICE"),
        (SystemMode.LIVE, BrokerEnvironment.PRACTICE, "requires a LIVE broker"),
        (SystemMode.SHADOW, BrokerEnvironment.PRACTICE, "does not send broker orders"),
        (SystemMode.BACKTEST, BrokerEnvironment.NONE, "does not send broker orders"),
    ],
)
def test_mode_and_broker_must_match(
    mode: SystemMode, broker: BrokerEnvironment, expected: str
) -> None:
    env = ExecutionEnvironment(
        mode=mode, execution_enabled=True, live_trading_enabled=True, broker_environment=broker
    )
    assert any(expected in r for r in execution_refusals(env))


@pytest.mark.invariant("INV-EXEC-01")
def test_disabled_flag_is_refused() -> None:
    env = ExecutionEnvironment(
        mode=SystemMode.PAPER,
        execution_enabled=False,
        live_trading_enabled=False,
        broker_environment=BrokerEnvironment.PRACTICE,
    )
    assert any("execution is not enabled" in r for r in execution_refusals(env))


@pytest.mark.invariant("INV-EXEC-01")
def test_refusals_are_audited() -> None:
    sink = InMemoryAuditSink()
    with pytest.raises(ExecutionRefused):
        assert_execution_permitted(FULLY_ENABLED_LIVE, audit=AuditLog(sink, clock=FixedClock(NOW)))
    assert [r.kind for r in sink] == [AuditKind.EXECUTION_REFUSED]


@pytest.mark.invariant("INV-EXEC-01")
def test_disabled_adapter_refuses_even_an_approved_decision() -> None:
    i = inputs()
    d = evaluate(i)
    assert d.approved
    assert i.candidate is not None
    adapter = DisabledBrokerAdapter(FULLY_ENABLED_LIVE)
    with pytest.raises(ExecutionRefused):
        adapter.submit_bracket_order(decision=d, candidate=i.candidate, approvals=())


# ----------------------------------------------------------------------------- settings


@pytest.mark.invariant("INV-EXEC-01")
@pytest.mark.parametrize("name", ["shadow", "paper", "live"])
def test_shipped_environments_have_execution_and_live_disabled(name: str) -> None:
    s = load_settings(name, config_dir=CONFIG, environ={})
    assert s.execution_enabled is False
    assert s.live_trading_enabled is False
    with pytest.raises(ExecutionRefused):
        assert_execution_permitted(s.execution_environment())


@pytest.mark.invariant("INV-EXEC-01")
def test_default_environment_is_shadow() -> None:
    s = load_settings(config_dir=CONFIG, environ={})
    assert s.environment is Environment.SHADOW
    assert s.mode is SystemMode.SHADOW
    assert s.broker_environment is BrokerEnvironment.NONE


@pytest.mark.invariant("INV-EXEC-01")
def test_environment_variables_cannot_turn_shadow_into_live() -> None:
    with pytest.raises(SettingsError):
        load_settings(
            "shadow",
            config_dir=CONFIG,
            environ={"SENTINEL_LIVE_TRADING_ENABLED": "true"},
        )


@pytest.mark.invariant("INV-EXEC-01")
@pytest.mark.parametrize("value", ["yes", "1", "TRUE ", "on", ""])
def test_boolean_overrides_are_strict(value: str) -> None:
    with pytest.raises(SettingsError):
        load_settings("paper", config_dir=CONFIG, environ={"SENTINEL_EXECUTION_ENABLED": value})


def test_settings_reject_unknown_environment(tmp_path: Path) -> None:
    with pytest.raises(SettingsError):
        load_settings("production", config_dir=CONFIG, environ={})
    (tmp_path / "environments").mkdir()
    (tmp_path / "environments" / "shadow.toml").write_text('environment = "shadow"\nsurprise = 1\n')
    with pytest.raises(SettingsError):
        load_settings("shadow", config_dir=tmp_path, environ={})


def test_settings_environment_variable_selects_file() -> None:
    s = load_settings(config_dir=CONFIG, environ={"SENTINEL_ENV": "paper"})
    assert s.environment is Environment.PAPER
    assert s.broker_environment is BrokerEnvironment.PRACTICE
    overridden = load_settings(
        config_dir=CONFIG,
        environ={"SENTINEL_ENV": "paper", "SENTINEL_EXECUTION_ENABLED": "true"},
    )
    assert overridden.execution_enabled is True
    with pytest.raises(ExecutionRefused):  # still hard-disabled in M1
        assert_execution_permitted(overridden.execution_environment())
