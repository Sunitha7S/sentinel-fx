"""Value types, canonical encoding and domain-object validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.domain.canonical import canonical, canonical_json, sha256_hex
from sentinel.domain.decision import (
    Approval,
    Authority,
    Outcome,
    RiskDecision,
    RuleResult,
    Stage,
    shadow_record_from,
)
from sentinel.domain.instrument import DEFAULT_INSTRUMENTS
from sentinel.domain.snapshots import (
    CurrencyNewsRisk,
    EconomicEvent,
    EventTier,
    NewsRiskSnapshot,
)
from sentinel.domain.system import Actor, ActorKind, SystemMode
from sentinel.domain.types import (
    ApprovalId,
    CandidateId,
    Currency,
    DecisionId,
    DomainError,
    Money,
    Percent,
    SnapshotId,
    require_utc,
    to_decimal,
)
from sentinel.risk.kernel import evaluate

from support.builders import NOW, inputs

USD, EUR = Currency.USD, Currency.EUR


# ----------------------------------------------------------------------------- to_decimal


@pytest.mark.parametrize("bad", [1.5, True, None, [1]])
def test_to_decimal_rejects_floats_bools_and_other_types(bad: object) -> None:
    with pytest.raises(TypeError):
        to_decimal(bad, field="x")


@pytest.mark.parametrize("bad", ["abc", "NaN", "Infinity", Decimal("NaN")])
def test_to_decimal_rejects_non_finite_and_garbage(bad: object) -> None:
    with pytest.raises(DomainError):
        to_decimal(bad, field="x")


def test_to_decimal_accepts_int_str_decimal() -> None:
    assert to_decimal(3, field="x") == Decimal(3)
    assert to_decimal("1.25", field="x") == Decimal("1.25")


def test_require_utc() -> None:
    with pytest.raises(DomainError, match="naive"):
        require_utc(datetime(2026, 1, 1), field="t")  # noqa: DTZ001
    with pytest.raises(DomainError, match="UTC"):
        require_utc(datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2))), field="t")
    with pytest.raises(TypeError):
        require_utc("2026-01-01", field="t")  # type: ignore[arg-type]
    assert require_utc(datetime(2026, 1, 1, tzinfo=UTC), field="t").tzinfo is UTC


# ----------------------------------------------------------------------------- money / percent


def test_money_arithmetic_and_currency_safety() -> None:
    a, b = Money(Decimal(10), USD), Money(Decimal(3), USD)
    assert a + b == Money(Decimal(13), USD)
    assert a - b == Money(Decimal(7), USD)
    assert -b == Money(Decimal(-3), USD)
    assert b < a
    assert b <= a
    assert a > b
    assert a >= b
    assert str(a) == "10 USD"
    assert Money.zero(EUR).amount == 0
    with pytest.raises(DomainError):
        _ = a + Money(Decimal(1), EUR)
    with pytest.raises(TypeError):
        _ = a + 1  # type: ignore[operator]
    with pytest.raises(TypeError):
        Money(Decimal(1), "USD")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(1.0, USD)  # type: ignore[arg-type]


def test_percent_units_are_explicit() -> None:
    half = Percent(Decimal("0.5"))
    assert half.fraction == Decimal("0.005")
    assert Percent.from_fraction(Decimal("0.005")) == half
    assert half.of(Money(Decimal(10_000), USD)) == Money(Decimal(50), USD)
    assert str(half) == "0.5%"
    assert Percent(Decimal(1)) > half


@given(st.decimals(min_value=Decimal(-1000), max_value=Decimal(1000), places=4))
def test_percent_fraction_round_trip(value: Decimal) -> None:
    assert Percent.from_fraction(Percent(value).fraction) == Percent(value)


# ----------------------------------------------------------------------------- canonical


def test_canonical_is_deterministic_and_typed() -> None:
    assert canonical(Decimal("1.500")) == canonical(Decimal("1.5")) == "1.5"
    assert canonical(Decimal("0E-8")) == "0"
    assert canonical(timedelta(minutes=1, microseconds=5)) == "PT60.000005S"
    assert canonical(date(2026, 1, 2)) == "2026-01-02"
    assert canonical(time(16, 45)) == "16:45:00"
    assert canonical(frozenset({2, 1})) == [1, 2]
    assert canonical({"b": 1, "a": USD}) == {"b": 1, "a": "USD"}
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert sha256_hex({"a": 1}) == sha256_hex({"a": 1})
    with pytest.raises(TypeError):
        canonical(1.0)
    with pytest.raises(TypeError):
        canonical(datetime(2026, 1, 1))  # noqa: DTZ001
    with pytest.raises(TypeError):
        canonical(object())


# ----------------------------------------------------------------------------- instruments


@pytest.mark.parametrize(
    "changes",
    [
        {"symbol": "EURUSD"},
        {"quote": EUR, "symbol": "EUR_EUR"},
        {"min_units": Decimal(0)},
        {"max_units": Decimal("0.5")},
        {"unit_step": Decimal(3), "min_units": Decimal(1)},
        {"margin_rate": Decimal(2)},
    ],
)
def test_instrument_validation(changes: dict[str, Any]) -> None:
    with pytest.raises(DomainError):
        replace(DEFAULT_INSTRUMENTS["EUR_USD"], **changes)


def test_default_instruments_are_the_five_majors() -> None:
    assert set(DEFAULT_INSTRUMENTS) == {"EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD"}
    assert DEFAULT_INSTRUMENTS["USD_JPY"].pip_size == Decimal("0.01")


# ----------------------------------------------------------------------------- snapshots etc.


def test_snapshot_validation() -> None:
    base = inputs()
    assert base.markets["EUR_USD"].mid > 0
    with pytest.raises(DomainError):
        replace(base.markets["EUR_USD"], ask=Decimal("1.0"))
    with pytest.raises(DomainError):
        CurrencyNewsRisk(USD, Decimal(101), tripwire_active=False)
    with pytest.raises(DomainError):
        NewsRiskSnapshot(
            snapshot_id=SnapshotId("n"),
            as_of=NOW,
            per_currency=(
                CurrencyNewsRisk(USD, Decimal(0), False),
                CurrencyNewsRisk(USD, Decimal(1), False),
            ),
        )
    with pytest.raises(DomainError):
        EconomicEvent("e", USD, "x", EventTier.TIER1, NOW, ends_at=NOW - timedelta(minutes=1))
    assert base.account is not None
    with pytest.raises(DomainError):
        replace(base.account, trades_today=-1)
    with pytest.raises(DomainError):
        replace(base.account, balance=Money(Decimal(1), EUR))


def test_candidate_validation() -> None:
    c = inputs().candidate
    assert c is not None
    with pytest.raises(DomainError):
        replace(c, entry=Decimal(0))
    with pytest.raises(DomainError):
        replace(c, expected_hold=timedelta(0))


def _decision(**changes: Any) -> RiskDecision:
    fields: dict[str, Any] = {
        "decision_id": DecisionId("d"),
        "candidate_id": CandidateId("c"),
        "as_of": NOW,
        "outcome": Outcome.APPROVED,
        "units": Decimal(1),
        "risk_amount": Money(Decimal(1), USD),
        "effective_risk": Percent(Decimal("0.5")),
        "rule_results": (RuleResult("R-X", Stage.SYSTEM, True, "ok"),),
        "policy_id": "p",
        "policy_sha256": "s",
        "state_version": 1,
        "snapshot_digest": "d" * 64,
    }
    fields.update(changes)
    return RiskDecision(**fields)


def test_risk_decision_invariants_cannot_be_violated() -> None:
    bad = (RuleResult("R-Y", Stage.SYSTEM, False, "bad"),)
    assert _decision().approved
    with pytest.raises(DomainError, match="hard rules"):
        _decision(rule_results=bad)
    with pytest.raises(DomainError, match="positive units"):
        _decision(units=Decimal(0))
    with pytest.raises(DomainError, match="bindings"):
        _decision(snapshot_digest=None)
    with pytest.raises(DomainError, match="zero units"):
        _decision(outcome=Outcome.BLOCKED, rule_results=bad)
    with pytest.raises(DomainError, match="at least one"):
        _decision(outcome=Outcome.BLOCKED, units=Decimal(0), rule_results=())


def test_approval_and_shadow_record_validation() -> None:
    with pytest.raises(DomainError):
        Approval(
            ApprovalId("a"),
            DecisionId("d"),
            CandidateId("c"),
            Authority.RISK,
            1,
            "x",
            "y",
            NOW,
            NOW,
        )
    i = inputs()
    assert i.candidate is not None
    d = replace(evaluate(i), candidate_id=CandidateId("other"))
    with pytest.raises(DomainError):
        shadow_record_from(i.candidate, d)


def test_system_enums_and_actor() -> None:
    assert SystemMode.LIVE.is_live
    assert not SystemMode.PAPER.is_live
    assert SystemMode.PAPER.sends_broker_orders
    assert not SystemMode.SHADOW.sends_broker_orders
    assert str(Actor(ActorKind.HUMAN, "ops")) == "human:ops"
    with pytest.raises(DomainError):
        Actor(ActorKind.HUMAN, " ")
