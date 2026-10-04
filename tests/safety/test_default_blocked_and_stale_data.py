"""Missing or stale data blocks trading; the system starts blocked."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from sentinel.domain.decision import Outcome
from sentinel.domain.system import TradingState, initial_system_state
from sentinel.domain.types import Currency, Side
from sentinel.risk.kernel import evaluate

from support.builders import NOW, ExistingPosition, Knobs, inputs

# ----------------------------------------------------------------------------- default state


@pytest.mark.invariant("INV-DEFAULT-01")
def test_initial_system_state_is_halted() -> None:
    state = initial_system_state(NOW)
    assert state.trading_state is TradingState.HALTED


@pytest.mark.invariant("INV-DEFAULT-01")
def test_a_fresh_system_blocks_an_otherwise_perfect_trade() -> None:
    d = evaluate(replace(inputs(), system=initial_system_state(NOW)))
    assert d.outcome is Outcome.BLOCKED
    assert "R-SYS-01" in d.blocking_rules


@pytest.mark.invariant("INV-DEFAULT-01")
@pytest.mark.parametrize(
    "field",
    ["candidate", "policy", "active_policy_sha256", "system", "account", "calendar", "news"],
)
def test_missing_input_blocks(field: str) -> None:
    changes: dict[str, Any] = {field: None}
    d = evaluate(replace(inputs(), **changes))
    assert d.outcome is Outcome.BLOCKED
    assert "R-SYS-00" in d.blocking_rules
    assert d.units == 0


@pytest.mark.invariant("INV-DEFAULT-01")
def test_missing_market_for_candidate_blocks() -> None:
    base = inputs()
    markets = {k: v for k, v in base.markets.items() if k != "EUR_USD"}
    d = evaluate(replace(base, markets=markets))
    assert d.outcome is Outcome.BLOCKED
    assert "R-SYS-00" in d.blocking_rules


@pytest.mark.invariant("INV-DEFAULT-01")
def test_missing_conversion_market_blocks() -> None:
    base = inputs(symbol="USD_JPY")
    markets = {k: v for k, v in base.markets.items() if k != "USD_JPY"}
    d = evaluate(replace(base, markets=markets))
    assert d.outcome is Outcome.BLOCKED


@pytest.mark.invariant("INV-DEFAULT-01")
def test_missing_instrument_blocks() -> None:
    d = evaluate(replace(inputs(), instruments={}))
    assert d.outcome is Outcome.BLOCKED
    assert "R-SYS-00" in d.blocking_rules


@pytest.mark.invariant("INV-DEFAULT-01")
def test_missing_news_entry_for_a_traded_currency_blocks() -> None:
    base = inputs()
    assert base.news is not None
    news = replace(
        base.news,
        per_currency=tuple(r for r in base.news.per_currency if r.currency is not Currency.EUR),
    )
    d = evaluate(replace(base, news=news))
    assert d.outcome is Outcome.BLOCKED
    assert "R-MKT-06" in d.blocking_rules


@pytest.mark.invariant("INV-DEFAULT-01")
def test_everything_missing_still_returns_a_blocked_decision() -> None:
    base = inputs()
    empty = replace(
        base,
        candidate=None,
        policy=None,
        active_policy_sha256=None,
        system=None,
        account=None,
        markets={},
        calendar=None,
        news=None,
        instruments={},
    )
    d = evaluate(empty)
    assert d.outcome is Outcome.BLOCKED
    assert d.units == 0
    assert d.rule_results


# ----------------------------------------------------------------------------- staleness

STALE = [
    ("account", "account_age", timedelta(seconds=120), "R-SYS-03"),
    ("market snapshot", "market_age", timedelta(seconds=360), "R-SYS-04"),
    ("price tick", "tick_age", timedelta(seconds=10), "R-SYS-04"),
    ("calendar", "calendar_age", timedelta(hours=6), "R-SYS-05"),
    ("news", "news_age", timedelta(seconds=900), "R-SYS-07"),
    ("reconciliation", "recon_age", timedelta(seconds=120), "R-SYS-08"),
]


@pytest.mark.invariant("INV-DATA-01")
@pytest.mark.parametrize(("label", "knob", "limit", "rule"), STALE, ids=[s[0] for s in STALE])
def test_data_at_the_age_limit_is_accepted_and_one_second_older_blocks(
    label: str, knob: str, limit: timedelta, rule: str
) -> None:
    at_limit = evaluate(inputs(**{knob: limit}))
    assert rule not in at_limit.blocking_rules, label
    stale = evaluate(inputs(**{knob: limit + timedelta(seconds=1)}))
    assert stale.outcome is Outcome.BLOCKED, label
    assert rule in stale.blocking_rules, label


@pytest.mark.invariant("INV-DATA-01")
@pytest.mark.parametrize(
    ("knob", "rule"),
    [
        ("account_age", "R-SYS-03"),
        ("market_age", "R-SYS-04"),
        ("calendar_age", "R-SYS-05"),
        ("news_age", "R-SYS-07"),
    ],
)
def test_snapshots_from_the_future_beyond_clock_skew_block(knob: str, rule: str) -> None:
    d = evaluate(inputs(**{knob: timedelta(seconds=-3)}))
    assert rule in d.blocking_rules


@pytest.mark.invariant("INV-DATA-01")
def test_stale_market_of_an_open_position_blocks_new_risk() -> None:
    # The candidate (EURUSD) is fresh, but the open USDJPY position is valued, converted
    # and gap-stressed with a stale USDJPY quote.
    base = inputs(positions=(ExistingPosition("USD_JPY", Side.LONG, Decimal("0.2")),))
    stale_jpy = replace(base.markets["USD_JPY"], last_tick_at=NOW - timedelta(minutes=5))
    d = evaluate(replace(base, markets={**base.markets, "USD_JPY": stale_jpy}))
    assert "R-SYS-04" in d.blocking_rules


@pytest.mark.invariant("INV-DATA-01")
def test_unrelated_stale_market_does_not_block() -> None:
    base = inputs()
    stale_cad = replace(base.markets["USD_CAD"], last_tick_at=NOW - timedelta(minutes=5))
    d = evaluate(replace(base, markets={**base.markets, "USD_CAD": stale_cad}))
    assert d.outcome is Outcome.APPROVED, d.blocking_rules


@pytest.mark.invariant("INV-DATA-02")
def test_disagreeing_calendar_sources_block() -> None:
    d = evaluate(inputs(calendar_agreement=False))
    assert d.outcome is Outcome.BLOCKED
    assert "R-SYS-06" in d.blocking_rules


@pytest.mark.invariant("INV-DATA-02")
def test_calendar_with_too_few_sources_blocks() -> None:
    d = evaluate(inputs(Knobs(calendar_sources=("primary_api",))))
    assert "R-SYS-06" in d.blocking_rules
