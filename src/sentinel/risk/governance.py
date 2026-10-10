"""Governance of the risk policy: who may change it, how, and when it takes effect.

* Only a ``HumanRiskAdmin`` capability can approve or activate a change. The capability
  is issued after step-up authentication (wired to the API in M14); it cannot be created
  any other way. The learning package is forbidden (by import contracts and tests) from
  importing this module at all; it receives a read-only ``PolicyReader``.
* Each change is classified field by field. Tightening takes effect immediately; anything
  that loosens (or mixes) waits ``COOLING_OFF`` and is refused outright while trading is
  HALTED or drawdown exceeds ``LOOSENING_FORBIDDEN_DRAWDOWN``.
* Fields without a defined "safer" direction count as loosening when they change.
* A request is tied to the policy hash it was based on; if the active policy changed in
  the meantime, activation is refused and the request must be re-filed.

In M1 the store is in memory. M2 moves it to PostgreSQL, where the learning role has no
write grant on policy tables (docs/02 section 9).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from sentinel.domain.system import TradingState
from sentinel.domain.types import DomainError, Percent, require_utc
from sentinel.risk.policy import PolicyReader, RiskPolicy

__all__ = [
    "COOLING_OFF",
    "LOOSENING_FORBIDDEN_DRAWDOWN",
    "UNCLASSIFIED_FIELDS",
    "Activation",
    "ChangeDirection",
    "ChangeRequest",
    "FieldChange",
    "GovernanceError",
    "HumanRiskAdmin",
    "ProtectedPolicyStore",
    "approve_change",
    "classify_change",
    "field_direction",
    "file_change_request",
    "issue_human_risk_admin",
    "require_human_risk_admin",
]

COOLING_OFF: Final = timedelta(hours=24)
LOOSENING_FORBIDDEN_DRAWDOWN: Final = Percent(Decimal(5))


class GovernanceError(DomainError):
    pass


class ChangeDirection(StrEnum):
    TIGHTEN = "TIGHTEN"
    LOOSEN = "LOOSEN"
    MIXED = "MIXED"


# A field is safer when its value goes DOWN ("lower") or UP ("higher").
_SAFER_WHEN: dict[str, str] = {
    **dict.fromkeys(
        [
            "risk_per_trade",
            "max_open_risk",
            "max_open_positions",
            "max_positions_per_pair",
            "max_new_trades_per_day",
            "max_effective_leverage",
            "max_margin_utilisation",
            "daily_loss",
            "weekly_loss",
            "monthly_loss",
            "max_drawdown",
            "cooldown_after_consecutive_losses",
            "halt_after_consecutive_losses",
            "max_currency_net_risk",
            "max_gap_loss",
            "max_stop_atr_h1",
            "max_entry_drift_atr",
            "signal_ttl",
            "max_spread_ratio",
            "max_news_risk_score",
            "max_price_age",
            "max_market_snapshot_age",
            "max_account_age",
            "max_calendar_age",
            "max_news_age",
            "max_reconciliation_age",
            "max_clock_skew",
            "approval_ttl",
            "live_micro_risk_multiplier",
        ],
        "lower",
    ),
    **dict.fromkeys(
        [
            "cooldown",
            "gap_atr_multiple_normal",
            "gap_atr_multiple_event",
            "min_rr_net",
            "min_stop_atr_h1",
            "min_stop_spread_multiple",
            "expected_slippage_pips",
            "max_hold_horizon",
            "min_calendar_sources",
            "blackout_tier1_before",
            "blackout_tier1_after",
            "blackout_central_bank_before",
            "blackout_central_bank_after",
            "blackout_tier2_before",
            "blackout_tier2_after",
        ],
        "higher",
    ),
}

UNCLASSIFIED_FIELDS: Final = frozenset(
    {
        "policy_id",  # identity, not behaviour; ignored
        "account_currency",
        "drawdown_throttle",
        "rollover_start",
        "rollover_end",
        "rollover_tz",
        "friday_cutoff_utc",
        "require_broker_side_stop",
    }
)
"""Fields with no single safer direction. Any change to them is treated as loosening."""


def field_direction(name: str) -> str | None:
    return _SAFER_WHEN.get(name)


@dataclass(frozen=True, slots=True)
class FieldChange:
    field: str
    old: Any
    new: Any
    direction: ChangeDirection


def classify_change(
    current: RiskPolicy, proposed: RiskPolicy
) -> tuple[ChangeDirection, tuple[FieldChange, ...]]:
    changes: list[FieldChange] = []
    for f in dataclasses.fields(RiskPolicy):
        if f.name == "policy_id":
            continue
        old, new = getattr(current, f.name), getattr(proposed, f.name)
        if old == new:
            continue
        safer = _SAFER_WHEN.get(f.name)
        if safer == "lower":
            direction = ChangeDirection.TIGHTEN if new < old else ChangeDirection.LOOSEN
        elif safer == "higher":
            direction = ChangeDirection.TIGHTEN if new > old else ChangeDirection.LOOSEN
        else:
            direction = ChangeDirection.LOOSEN
        changes.append(FieldChange(f.name, old, new, direction))
    if not changes:
        raise GovernanceError("proposed policy does not change any limit")
    kinds = {c.direction for c in changes}
    overall = kinds.pop() if len(kinds) == 1 else ChangeDirection.MIXED
    return overall, tuple(changes)


# ----------------------------------------------------------------------------- capability

_CAPABILITY_KEY: Final = object()


@dataclass(frozen=True, slots=True)
class HumanRiskAdmin:
    """Proof that a step-up-authenticated human is acting. Cannot be constructed directly."""

    identity: str
    _key: object = dataclasses.field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._key is not _CAPABILITY_KEY:
            raise GovernanceError("HumanRiskAdmin can only be issued by the governance module")
        if not self.identity.strip():
            raise GovernanceError("identity required")


def issue_human_risk_admin(identity: str, *, step_up_verified: bool) -> HumanRiskAdmin:
    if step_up_verified is not True:
        raise GovernanceError("step-up authentication required")
    return HumanRiskAdmin(identity, _CAPABILITY_KEY)


def require_human_risk_admin(approver: object) -> HumanRiskAdmin:
    """Return ``approver`` if it is a genuine capability; raise ``GovernanceError`` otherwise."""
    if not isinstance(approver, HumanRiskAdmin) or approver._key is not _CAPABILITY_KEY:
        raise GovernanceError("a human risk-admin capability is required")
    return approver


# ----------------------------------------------------------------------------- requests


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    request_id: str
    based_on_sha256: str
    proposed: RiskPolicy
    direction: ChangeDirection
    changes: tuple[FieldChange, ...]
    reason: str
    requested_by: str
    requested_at: datetime


def file_change_request(
    current: RiskPolicy,
    proposed: RiskPolicy,
    *,
    request_id: str,
    reason: str,
    requested_by: str,
    requested_at: datetime,
) -> ChangeRequest:
    """Anyone (including the learning layer, via a human) may *file* a request."""
    if not reason.strip():
        raise GovernanceError("a change request needs a reason")
    direction, changes = classify_change(current, proposed)
    return ChangeRequest(
        request_id=request_id,
        based_on_sha256=current.sha256,
        proposed=proposed,
        direction=direction,
        changes=changes,
        reason=reason,
        requested_by=requested_by,
        requested_at=require_utc(requested_at, field="requested_at"),
    )


@dataclass(frozen=True, slots=True)
class Activation:
    request: ChangeRequest
    approved_by: str
    approved_at: datetime
    effective_at: datetime

    @property
    def direction(self) -> ChangeDirection:
        return self.request.direction


def approve_change(
    request: ChangeRequest,
    *,
    approver: HumanRiskAdmin,
    approved_at: datetime,
    trading_state: TradingState,
    drawdown: Percent,
) -> Activation:
    admin = require_human_risk_admin(approver)
    approved_at = require_utc(approved_at, field="approved_at")
    loosens = request.direction is not ChangeDirection.TIGHTEN
    if loosens and trading_state is TradingState.HALTED:
        raise GovernanceError("limits cannot be loosened while trading is HALTED")
    if loosens and drawdown > LOOSENING_FORBIDDEN_DRAWDOWN:
        raise GovernanceError(
            f"limits cannot be loosened in a drawdown above {LOOSENING_FORBIDDEN_DRAWDOWN}"
        )
    effective = approved_at + COOLING_OFF if loosens else approved_at
    return Activation(request, admin.identity, approved_at, effective)


# ----------------------------------------------------------------------------- store


@dataclass(frozen=True, slots=True)
class _Reader:
    _store: ProtectedPolicyStore

    def active(self) -> RiskPolicy:
        return self._store.active()

    def active_sha256(self) -> str:
        return self._store.active_sha256()


class ProtectedPolicyStore:
    """In-memory reference implementation of the protected policy store."""

    def __init__(self, initial: RiskPolicy, *, installed_by: HumanRiskAdmin, at: datetime) -> None:
        require_human_risk_admin(installed_by)
        self.__active = initial
        self.__history: list[tuple[datetime, str, RiskPolicy]] = [
            (require_utc(at, field="at"), installed_by.identity, initial)
        ]

    def active(self) -> RiskPolicy:
        return self.__active

    def active_sha256(self) -> str:
        return self.__active.sha256

    def reader(self) -> PolicyReader:
        return _Reader(self)

    @property
    def history(self) -> tuple[tuple[datetime, str, RiskPolicy], ...]:
        return tuple(self.__history)

    def activate(
        self, activation: Activation, *, approver: HumanRiskAdmin, now: datetime
    ) -> RiskPolicy:
        admin = require_human_risk_admin(approver)
        now = require_utc(now, field="now")
        if activation.request.based_on_sha256 != self.active_sha256():
            raise GovernanceError("request was based on a policy that is no longer active")
        if now < activation.effective_at:
            raise GovernanceError(
                f"cooling-off period: effective at {activation.effective_at.isoformat()}"
            )
        self.__active = activation.request.proposed
        self.__history.append((now, admin.identity, self.__active))
        return self.__active
