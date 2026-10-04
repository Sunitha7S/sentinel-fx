"""The execution guard: the single check every order path must pass.

During M0-M1 execution is hard-disabled in code. ``EXECUTION_HARD_DISABLED`` is a constant,
not configuration: no file, environment variable or API call can enable order submission
until the constant is changed in a reviewed commit (planned for M10).

Independently of that switch, the guard enforces environment consistency, so these rules
are already tested before the switch is ever flipped:

* execution must be explicitly enabled;
* BACKTEST and SHADOW never send broker orders;
* PAPER requires a PRACTICE broker account;
* LIVE_MICRO and LIVE require a LIVE broker account *and* the live-trading flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sentinel.audit.chain import AuditKind, AuditLog
from sentinel.domain.system import BrokerEnvironment, SystemMode

__all__ = [
    "EXECUTION_HARD_DISABLED",
    "ExecutionEnvironment",
    "ExecutionRefused",
    "assert_execution_permitted",
    "execution_refusals",
]

EXECUTION_HARD_DISABLED: Final[bool] = True


class ExecutionRefused(RuntimeError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True, slots=True)
class ExecutionEnvironment:
    mode: SystemMode
    execution_enabled: bool
    live_trading_enabled: bool
    broker_environment: BrokerEnvironment


def execution_refusals(env: ExecutionEnvironment) -> tuple[str, ...]:
    """Every reason order submission is refused. Empty means permitted."""
    reasons: list[str] = []
    if EXECUTION_HARD_DISABLED:
        reasons.append("execution is hard-disabled in this build (enabled no earlier than M10)")
    if not env.execution_enabled:
        reasons.append("execution is not enabled in configuration")
    if not env.mode.sends_broker_orders:
        reasons.append(f"{env.mode} mode does not send broker orders")
    if env.mode is SystemMode.PAPER and env.broker_environment is not BrokerEnvironment.PRACTICE:
        reasons.append("PAPER mode requires a PRACTICE broker account")
    if env.mode.is_live:
        if env.broker_environment is not BrokerEnvironment.LIVE:
            reasons.append(f"{env.mode} mode requires a LIVE broker account")
        if not env.live_trading_enabled:
            reasons.append("live trading is not enabled")
    return tuple(reasons)


def assert_execution_permitted(env: ExecutionEnvironment, *, audit: AuditLog | None = None) -> None:
    reasons = execution_refusals(env)
    if reasons:
        if audit is not None:
            audit.record(
                AuditKind.EXECUTION_REFUSED,
                "execution_guard",
                {"environment": env, "reasons": list(reasons)},
            )
        raise ExecutionRefused(reasons)
