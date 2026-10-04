"""The risk policy: a frozen, validated, content-addressed set of limits.

A ``RiskPolicy`` is built from a plain mapping (normally parsed from YAML at the boundary
by ``sentinel.config.policy_loader``). Construction rejects unknown keys, missing keys,
floats, and any value beyond the code-level ceilings below. The ceilings are deliberately
in code rather than configuration: no file, environment variable or approval can exceed
them without a reviewed code change.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sentinel.domain.canonical import sha256_hex
from sentinel.domain.types import Currency, DomainError, Percent, to_decimal

__all__ = ["CEILINGS", "DrawdownStep", "PolicyReader", "PolicyViolation", "RiskPolicy"]


class PolicyViolation(DomainError):
    """The policy is malformed or exceeds a code-level ceiling."""


@dataclass(frozen=True, slots=True)
class DrawdownStep:
    drawdown: Percent
    risk_multiplier: Decimal


def _pct(v: str) -> Percent:
    return Percent(Decimal(v))


def _s(seconds: int) -> timedelta:
    return timedelta(seconds=seconds)


# field -> (bound, "max"|"min"): the value must be <= (max) or >= (min) the bound.
CEILINGS: dict[str, tuple[Any, str]] = {
    "risk_per_trade": (_pct("1.0"), "max"),
    "max_open_risk": (_pct("5.0"), "max"),
    "max_open_positions": (10, "max"),
    "max_positions_per_pair": (1, "max"),
    "max_new_trades_per_day": (10, "max"),
    "max_effective_leverage": (Decimal(10), "max"),
    "max_margin_utilisation": (_pct("50"), "max"),
    "daily_loss": (_pct("3.0"), "max"),
    "weekly_loss": (_pct("6.0"), "max"),
    "monthly_loss": (_pct("10.0"), "max"),
    "max_drawdown": (_pct("20.0"), "max"),
    "halt_after_consecutive_losses": (10, "max"),
    "cooldown": (_s(3600), "min"),
    "max_currency_net_risk": (_pct("3.0"), "max"),
    "max_gap_loss": (_pct("10.0"), "max"),
    "gap_atr_multiple_normal": (Decimal("0.25"), "min"),
    "gap_atr_multiple_event": (Decimal("0.5"), "min"),
    "min_rr_net": (Decimal("1.0"), "min"),
    "min_stop_atr_h1": (Decimal("0.5"), "min"),
    "min_stop_spread_multiple": (Decimal(3), "min"),
    "max_entry_drift_atr": (Decimal("1.0"), "max"),
    "signal_ttl": (_s(4 * 3600), "max"),
    "max_spread_ratio": (Decimal(3), "max"),
    "max_news_risk_score": (Decimal(70), "max"),
    "min_calendar_sources": (1, "min"),
    "max_price_age": (_s(60), "max"),
    "max_market_snapshot_age": (_s(900), "max"),
    "max_account_age": (_s(300), "max"),
    "max_calendar_age": (_s(12 * 3600), "max"),
    "max_news_age": (_s(3600), "max"),
    "max_reconciliation_age": (_s(600), "max"),
    "max_clock_skew": (_s(10), "max"),
    "approval_ttl": (_s(600), "max"),
    "live_micro_risk_multiplier": (Decimal("0.5"), "max"),
}


class PolicyReader(Protocol):
    """Read-only access to the active policy. This is all a non-governance component gets."""

    def active(self) -> RiskPolicy: ...

    def active_sha256(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    policy_id: str
    account_currency: Currency
    # limits
    risk_per_trade: Percent
    max_open_risk: Percent
    max_open_positions: int
    max_positions_per_pair: int
    max_new_trades_per_day: int
    max_effective_leverage: Decimal
    max_margin_utilisation: Percent
    # loss limits
    daily_loss: Percent
    weekly_loss: Percent
    monthly_loss: Percent
    max_drawdown: Percent
    drawdown_throttle: tuple[DrawdownStep, ...]
    # streaks
    cooldown_after_consecutive_losses: int
    cooldown: timedelta
    halt_after_consecutive_losses: int
    # exposure
    max_currency_net_risk: Percent
    max_gap_loss: Percent
    gap_atr_multiple_normal: Decimal
    gap_atr_multiple_event: Decimal
    # trade quality
    min_rr_net: Decimal
    min_stop_atr_h1: Decimal
    max_stop_atr_h1: Decimal
    min_stop_spread_multiple: Decimal
    expected_slippage_pips: Decimal
    max_entry_drift_atr: Decimal
    signal_ttl: timedelta
    max_hold_horizon: timedelta
    # market
    max_spread_ratio: Decimal
    max_news_risk_score: Decimal
    min_calendar_sources: int
    blackout_tier1_before: timedelta
    blackout_tier1_after: timedelta
    blackout_central_bank_before: timedelta
    blackout_central_bank_after: timedelta
    blackout_tier2_before: timedelta
    blackout_tier2_after: timedelta
    rollover_start: time
    rollover_end: time
    rollover_tz: str
    friday_cutoff_utc: time
    # data freshness
    max_price_age: timedelta
    max_market_snapshot_age: timedelta
    max_account_age: timedelta
    max_calendar_age: timedelta
    max_news_age: timedelta
    max_reconciliation_age: timedelta
    max_clock_skew: timedelta
    # execution
    approval_ttl: timedelta
    require_broker_side_stop: bool
    live_micro_risk_multiplier: Decimal

    # ------------------------------------------------------------------ validation

    def __post_init__(self) -> None:  # noqa: PLR0912 - a flat list of explicit checks
        problems: list[str] = []
        if not self.policy_id.strip():
            problems.append("policy_id must not be empty")
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if isinstance(value, float):
                problems.append(f"{f.name}: float not allowed")
            if (
                isinstance(value, Percent | Decimal)
                and _num(value) <= 0
                and f.name not in {"expected_slippage_pips"}
            ):
                problems.append(f"{f.name} must be > 0")
            if isinstance(value, int) and not isinstance(value, bool) and value < 1:
                problems.append(f"{f.name} must be >= 1")
            if isinstance(value, timedelta) and value < timedelta(0):
                problems.append(f"{f.name} must not be negative")
        if self.expected_slippage_pips < 0:
            problems.append("expected_slippage_pips must be >= 0")
        for name, (bound, kind) in CEILINGS.items():
            value = getattr(self, name)
            if (kind == "max" and value > bound) or (kind == "min" and value < bound):
                problems.append(f"{name}={value} exceeds code ceiling ({kind} {bound})")
        if self.require_broker_side_stop is not True:
            problems.append("require_broker_side_stop must be true")
        if self.max_stop_atr_h1 <= self.min_stop_atr_h1:
            problems.append("max_stop_atr_h1 must exceed min_stop_atr_h1")
        if self.halt_after_consecutive_losses < self.cooldown_after_consecutive_losses:
            problems.append("halt_after_consecutive_losses must be >= cooldown threshold")
        if not self.daily_loss <= self.weekly_loss <= self.monthly_loss:
            problems.append("loss limits must satisfy daily <= weekly <= monthly")
        if self.risk_per_trade > self.daily_loss:
            problems.append("risk_per_trade must not exceed the daily loss limit")
        if not timedelta(0) < self.max_hold_horizon <= timedelta(hours=72):
            problems.append("max_hold_horizon must be within (0, 72h]")
        if self.signal_ttl <= timedelta(0) or self.approval_ttl <= timedelta(0):
            problems.append("signal_ttl and approval_ttl must be positive")
        problems.extend(self._throttle_problems())
        try:
            ZoneInfo(self.rollover_tz)
        except (ZoneInfoNotFoundError, ValueError):
            problems.append(f"unknown timezone {self.rollover_tz!r}")
        if problems:
            raise PolicyViolation("; ".join(problems))

    def _throttle_problems(self) -> list[str]:
        problems: list[str] = []
        previous_dd = Percent(Decimal(0))
        previous_mult = Decimal(1)
        for step in self.drawdown_throttle:
            if not Decimal(0) < step.risk_multiplier <= Decimal(1):
                problems.append("drawdown_throttle multipliers must be within (0, 1]")
            if step.risk_multiplier > previous_mult:
                problems.append("drawdown_throttle multipliers must not increase with drawdown")
            if step.drawdown <= previous_dd:
                problems.append("drawdown_throttle thresholds must be strictly increasing")
            if step.drawdown >= self.max_drawdown:
                problems.append("drawdown_throttle thresholds must be below max_drawdown")
            previous_dd, previous_mult = step.drawdown, step.risk_multiplier
        return problems

    # ------------------------------------------------------------------ helpers

    @property
    def sha256(self) -> str:
        return sha256_hex(self)

    def with_changes(self, **changes: Any) -> RiskPolicy:
        """A validated copy with ``changes`` applied. The original is untouched."""
        return dataclasses.replace(self, **changes)

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in dataclasses.fields(cls))

    def throttle_multiplier(self, drawdown: Percent) -> Decimal:
        multiplier = Decimal(1)
        for step in self.drawdown_throttle:
            if drawdown >= step.drawdown:
                multiplier = step.risk_multiplier
        return multiplier

    # ------------------------------------------------------------------ construction

    def to_mapping(self) -> dict[str, Any]:
        """The exact inverse of ``from_mapping``: ``from_mapping(p.to_mapping()) == p``.

        Decimals are written as strings, so the mapping is JSON-safe without floats. Used to
        persist a policy version so it can be re-parsed and its hash re-verified on load.
        """

        def d(value: Decimal | Percent) -> str:
            return str(value.value if isinstance(value, Percent) else value)

        def whole(delta: timedelta, unit: timedelta, name: str) -> int:
            if delta % unit:
                raise PolicyViolation(f"{name} must be a whole number of {unit}")
            return delta // unit

        def hhmm(value: time, name: str) -> str:
            if value.second or value.microsecond:
                raise PolicyViolation(f"{name} must be a whole minute")
            return value.strftime("%H:%M")

        minute, hour, second = timedelta(minutes=1), timedelta(hours=1), timedelta(seconds=1)
        return {
            "id": self.policy_id,
            "account_currency": self.account_currency.value,
            "limits": {
                "risk_per_trade_pct": d(self.risk_per_trade),
                "max_open_risk_pct": d(self.max_open_risk),
                "max_open_positions": self.max_open_positions,
                "max_positions_per_pair": self.max_positions_per_pair,
                "max_new_trades_per_day": self.max_new_trades_per_day,
                "max_effective_leverage": d(self.max_effective_leverage),
                "max_margin_utilisation_pct": d(self.max_margin_utilisation),
            },
            "loss_limits": {
                "daily_pct": d(self.daily_loss),
                "weekly_pct": d(self.weekly_loss),
                "monthly_pct": d(self.monthly_loss),
                "max_drawdown_pct": d(self.max_drawdown),
            },
            "drawdown_throttle": [
                {"drawdown_pct": d(s.drawdown), "risk_multiplier": d(s.risk_multiplier)}
                for s in self.drawdown_throttle
            ],
            "streaks": {
                "cooldown_after_consecutive_losses": self.cooldown_after_consecutive_losses,
                "cooldown_hours": whole(self.cooldown, hour, "cooldown"),
                "halt_after_consecutive_losses": self.halt_after_consecutive_losses,
            },
            "exposure": {
                "max_currency_net_risk_pct": d(self.max_currency_net_risk),
                "max_gap_loss_pct": d(self.max_gap_loss),
                "gap_atr_multiple_normal": d(self.gap_atr_multiple_normal),
                "gap_atr_multiple_event": d(self.gap_atr_multiple_event),
            },
            "trade_quality": {
                "min_rr_net": d(self.min_rr_net),
                "min_stop_atr_h1": d(self.min_stop_atr_h1),
                "max_stop_atr_h1": d(self.max_stop_atr_h1),
                "min_stop_spread_multiple": d(self.min_stop_spread_multiple),
                "expected_slippage_pips": d(self.expected_slippage_pips),
                "max_entry_drift_atr": d(self.max_entry_drift_atr),
                "signal_ttl_minutes": whole(self.signal_ttl, minute, "signal_ttl"),
                "max_hold_horizon_hours": whole(self.max_hold_horizon, hour, "max_hold_horizon"),
            },
            "market": {
                "max_spread_ratio": d(self.max_spread_ratio),
                "max_news_risk_score": d(self.max_news_risk_score),
                "min_calendar_sources": self.min_calendar_sources,
                "blackout_minutes": {
                    "tier1_before": whole(self.blackout_tier1_before, minute, "blackout"),
                    "tier1_after": whole(self.blackout_tier1_after, minute, "blackout"),
                    "central_bank_before": whole(
                        self.blackout_central_bank_before, minute, "blackout"
                    ),
                    "central_bank_after": whole(
                        self.blackout_central_bank_after, minute, "blackout"
                    ),
                    "tier2_before": whole(self.blackout_tier2_before, minute, "blackout"),
                    "tier2_after": whole(self.blackout_tier2_after, minute, "blackout"),
                },
                "rollover": {
                    "start": hhmm(self.rollover_start, "rollover_start"),
                    "end": hhmm(self.rollover_end, "rollover_end"),
                    "tz": self.rollover_tz,
                },
                "friday_no_new_entries_after_utc": hhmm(self.friday_cutoff_utc, "friday_cutoff"),
            },
            "data_freshness_seconds": {
                "price": whole(self.max_price_age, second, "max_price_age"),
                "market_snapshot": whole(self.max_market_snapshot_age, second, "freshness"),
                "account": whole(self.max_account_age, second, "freshness"),
                "calendar": whole(self.max_calendar_age, second, "freshness"),
                "news": whole(self.max_news_age, second, "freshness"),
                "reconciliation": whole(self.max_reconciliation_age, second, "freshness"),
                "max_clock_skew": whole(self.max_clock_skew, second, "freshness"),
            },
            "execution": {
                "approval_ttl_seconds": whole(self.approval_ttl, second, "approval_ttl"),
                "require_broker_side_stop": self.require_broker_side_stop,
                "live_micro_risk_multiplier": d(self.live_micro_risk_multiplier),
            },
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> RiskPolicy:
        reader = _Reader(data, "")
        limits = reader.section("limits")
        loss = reader.section("loss_limits")
        streaks = reader.section("streaks")
        exposure = reader.section("exposure")
        quality = reader.section("trade_quality")
        market = reader.section("market")
        blackout = market.section("blackout_minutes")
        rollover = market.section("rollover")
        fresh = reader.section("data_freshness_seconds")
        execution = reader.section("execution")
        throttle_raw = reader.raw("drawdown_throttle")
        if not isinstance(throttle_raw, list):
            raise PolicyViolation("drawdown_throttle must be a list")
        throttle = []
        for i, item in enumerate(throttle_raw):
            if not isinstance(item, Mapping):
                raise PolicyViolation(f"drawdown_throttle[{i}] must be a mapping")
            step = _Reader(item, f"drawdown_throttle[{i}].")
            throttle.append(DrawdownStep(step.pct("drawdown_pct"), step.dec("risk_multiplier")))
            step.finish()
        try:
            currency = Currency(reader.text("account_currency"))
        except ValueError as exc:
            raise PolicyViolation(str(exc)) from exc

        policy = cls(
            policy_id=reader.text("id"),
            account_currency=currency,
            risk_per_trade=limits.pct("risk_per_trade_pct"),
            max_open_risk=limits.pct("max_open_risk_pct"),
            max_open_positions=limits.int_("max_open_positions"),
            max_positions_per_pair=limits.int_("max_positions_per_pair"),
            max_new_trades_per_day=limits.int_("max_new_trades_per_day"),
            max_effective_leverage=limits.dec("max_effective_leverage"),
            max_margin_utilisation=limits.pct("max_margin_utilisation_pct"),
            daily_loss=loss.pct("daily_pct"),
            weekly_loss=loss.pct("weekly_pct"),
            monthly_loss=loss.pct("monthly_pct"),
            max_drawdown=loss.pct("max_drawdown_pct"),
            drawdown_throttle=tuple(throttle),
            cooldown_after_consecutive_losses=streaks.int_("cooldown_after_consecutive_losses"),
            cooldown=timedelta(hours=streaks.int_("cooldown_hours")),
            halt_after_consecutive_losses=streaks.int_("halt_after_consecutive_losses"),
            max_currency_net_risk=exposure.pct("max_currency_net_risk_pct"),
            max_gap_loss=exposure.pct("max_gap_loss_pct"),
            gap_atr_multiple_normal=exposure.dec("gap_atr_multiple_normal"),
            gap_atr_multiple_event=exposure.dec("gap_atr_multiple_event"),
            min_rr_net=quality.dec("min_rr_net"),
            min_stop_atr_h1=quality.dec("min_stop_atr_h1"),
            max_stop_atr_h1=quality.dec("max_stop_atr_h1"),
            min_stop_spread_multiple=quality.dec("min_stop_spread_multiple"),
            expected_slippage_pips=quality.dec("expected_slippage_pips"),
            max_entry_drift_atr=quality.dec("max_entry_drift_atr"),
            signal_ttl=timedelta(minutes=quality.int_("signal_ttl_minutes")),
            max_hold_horizon=timedelta(hours=quality.int_("max_hold_horizon_hours")),
            max_spread_ratio=market.dec("max_spread_ratio"),
            max_news_risk_score=market.dec("max_news_risk_score"),
            min_calendar_sources=market.int_("min_calendar_sources"),
            blackout_tier1_before=blackout.minutes("tier1_before"),
            blackout_tier1_after=blackout.minutes("tier1_after"),
            blackout_central_bank_before=blackout.minutes("central_bank_before"),
            blackout_central_bank_after=blackout.minutes("central_bank_after"),
            blackout_tier2_before=blackout.minutes("tier2_before"),
            blackout_tier2_after=blackout.minutes("tier2_after"),
            rollover_start=rollover.clock("start"),
            rollover_end=rollover.clock("end"),
            rollover_tz=rollover.text("tz"),
            friday_cutoff_utc=market.clock("friday_no_new_entries_after_utc"),
            max_price_age=fresh.seconds("price"),
            max_market_snapshot_age=fresh.seconds("market_snapshot"),
            max_account_age=fresh.seconds("account"),
            max_calendar_age=fresh.seconds("calendar"),
            max_news_age=fresh.seconds("news"),
            max_reconciliation_age=fresh.seconds("reconciliation"),
            max_clock_skew=fresh.seconds("max_clock_skew"),
            approval_ttl=execution.seconds("approval_ttl_seconds"),
            require_broker_side_stop=execution.bool_("require_broker_side_stop"),
            live_micro_risk_multiplier=execution.dec("live_micro_risk_multiplier"),
        )
        for r in (
            reader,
            limits,
            loss,
            streaks,
            exposure,
            quality,
            market,
            blackout,
            rollover,
            fresh,
            execution,
        ):
            r.finish()
        return policy


def _num(value: Percent | Decimal) -> Decimal:
    return value.value if isinstance(value, Percent) else value


class _Reader:
    """Strict accessor over a nested mapping: tracks which keys were consumed."""

    def __init__(self, data: Mapping[str, Any], prefix: str) -> None:
        if not isinstance(data, Mapping):
            raise PolicyViolation(f"{prefix or 'policy'} must be a mapping")
        self._data = data
        self._prefix = prefix
        self._used: set[str] = set()

    def raw(self, key: str) -> Any:
        if key not in self._data:
            raise PolicyViolation(f"missing key {self._prefix}{key}")
        self._used.add(key)
        return self._data[key]

    def section(self, key: str) -> _Reader:
        return _Reader(self.raw(key), f"{self._prefix}{key}.")

    def text(self, key: str) -> str:
        value = self.raw(key)
        if not isinstance(value, str):
            raise PolicyViolation(f"{self._prefix}{key} must be a string")
        return value

    def dec(self, key: str) -> Decimal:
        value = self.raw(key)
        try:
            return to_decimal(value, field=f"{self._prefix}{key}")
        except (TypeError, DomainError) as exc:
            raise PolicyViolation(str(exc)) from exc

    def pct(self, key: str) -> Percent:
        return Percent(self.dec(key))

    def int_(self, key: str) -> int:
        value = self.raw(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise PolicyViolation(f"{self._prefix}{key} must be an integer")
        return value

    def bool_(self, key: str) -> bool:
        value = self.raw(key)
        if not isinstance(value, bool):
            raise PolicyViolation(f"{self._prefix}{key} must be true or false")
        return value

    def seconds(self, key: str) -> timedelta:
        return timedelta(seconds=self.int_(key))

    def minutes(self, key: str) -> timedelta:
        return timedelta(minutes=self.int_(key))

    def clock(self, key: str) -> time:
        text = self.text(key)
        try:
            return time.fromisoformat(text)
        except ValueError as exc:
            raise PolicyViolation(f"{self._prefix}{key}: {text!r} is not HH:MM") from exc

    def finish(self) -> None:
        unknown = set(self._data) - self._used
        if unknown:
            names = ", ".join(sorted(f"{self._prefix}{k}" for k in unknown))
            raise PolicyViolation(f"unknown key(s): {names}")
