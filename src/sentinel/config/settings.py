"""Environment settings: one TOML file per environment, a few strict env-var overrides.

Environments and what they may be wired to:

=========  ======================  ==================
env        modes                   broker
=========  ======================  ==================
shadow     SHADOW, BACKTEST        NONE
paper      PAPER                   PRACTICE
live       LIVE_MICRO, LIVE        LIVE
=========  ======================  ==================

``live_trading_enabled`` is only valid in the live environment, and every shipped file
sets execution and live trading to ``false``. Even when enabled, the execution guard
still refuses while execution is hard-disabled in code.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from sentinel.domain.system import BrokerEnvironment, SystemMode
from sentinel.execution.guard import ExecutionEnvironment

__all__ = ["DEFAULT_CONFIG_DIR", "Environment", "Settings", "SettingsError", "load_settings"]

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


class SettingsError(ValueError):
    pass


class Environment(StrEnum):
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


_ALLOWED: dict[Environment, tuple[frozenset[SystemMode], BrokerEnvironment]] = {
    Environment.SHADOW: (
        frozenset({SystemMode.SHADOW, SystemMode.BACKTEST}),
        BrokerEnvironment.NONE,
    ),
    Environment.PAPER: (frozenset({SystemMode.PAPER}), BrokerEnvironment.PRACTICE),
    Environment.LIVE: (frozenset({SystemMode.LIVE_MICRO, SystemMode.LIVE}), BrokerEnvironment.LIVE),
}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Environment
    mode: SystemMode
    broker_environment: BrokerEnvironment
    execution_enabled: bool = False
    live_trading_enabled: bool = False
    ruleset_path: Path
    audit_log_path: Path
    shadow_log_path: Path
    database_url: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Settings:
        modes, broker = _ALLOWED[self.environment]
        if self.mode not in modes:
            raise ValueError(
                f"mode {self.mode} is not allowed in the {self.environment} environment"
            )
        if self.broker_environment is not broker:
            raise ValueError(f"the {self.environment} environment requires broker {broker}")
        if self.live_trading_enabled and self.environment is not Environment.LIVE:
            raise ValueError("live_trading_enabled is only valid in the live environment")
        return self

    def execution_environment(self) -> ExecutionEnvironment:
        return ExecutionEnvironment(
            mode=self.mode,
            execution_enabled=self.execution_enabled,
            live_trading_enabled=self.live_trading_enabled,
            broker_environment=self.broker_environment,
        )


def _strict_bool(name: str, value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise SettingsError(f"{name} must be exactly 'true' or 'false', got {value!r}")


_OVERRIDES = {
    "SENTINEL_EXECUTION_ENABLED": ("execution_enabled", _strict_bool),
    "SENTINEL_LIVE_TRADING_ENABLED": ("live_trading_enabled", _strict_bool),
    "SENTINEL_DATABASE_URL": ("database_url", lambda _n, v: v),
    "SENTINEL_AUDIT_LOG_PATH": ("audit_log_path", lambda _n, v: v),
    "SENTINEL_SHADOW_LOG_PATH": ("shadow_log_path", lambda _n, v: v),
}


def load_settings(
    environment: str | None = None,
    *,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    env_vars = os.environ if environ is None else environ
    name = environment or env_vars.get("SENTINEL_ENV", Environment.SHADOW.value)
    try:
        env = Environment(name)
    except ValueError as exc:
        raise SettingsError(f"unknown environment {name!r}") from exc
    path = config_dir / "environments" / f"{env.value}.toml"
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"cannot read {path}: {exc}") from exc
    for var, (field, parse) in _OVERRIDES.items():
        if var in env_vars:
            data[field] = parse(var, env_vars[var])
    for field in ("ruleset_path", "audit_log_path", "shadow_log_path"):
        if field in data and not Path(data[field]).is_absolute():
            data[field] = str(config_dir.parent / data[field])
    try:
        return Settings.model_validate(data)
    except ValidationError as exc:
        raise SettingsError(str(exc)) from exc
