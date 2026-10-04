"""Table tests: one failing case per rule, each from the approving baseline.

Each row changes one knob and asserts the decision is BLOCKED *and* that the expected
rule is among the blocking rules, so a row cannot pass for an unrelated reason.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from sentinel.domain.decision import Outcome, Stage
from sentinel.domain.snapshots import VolRegime
from sentinel.domain.system import SystemMode, TradingState
from sentinel.domain.types import Currency, Percent, Side
from sentinel.risk.kernel import ALL_RULE_IDS, evaluate

from support.builders import NOW, ExistingPosition, Knobs, event, inputs, policy

USD, EUR, GBP, JPY, AUD = Currency.USD, Currency.EUR, Currency.GBP, Currency.JPY, Currency.AUD


def test_baseline_is_approved_with_every_rule_reported() -> None:
    d = evaluate(inputs())
    assert d.outcome is Outcome.APPROVED, d.blocking_rules
    assert d.units == Decimal(16025)  # docs/04 worked example, via the full kernel
    assert d.risk_amount is not None
    assert d.risk_amount.amount <= Decimal(50)
    assert tuple(r.rule_id for r in d.rule_results) == ALL_RULE_IDS
    assert all(r.passed for r in d.rule_results)


def test_rule_ids_are_unique_and_namespaced() -> None:
    assert len(ALL_RULE_IDS) == len(set(ALL_RULE_IDS))
    stages = {"SYS", "ACC", "MKT", "TRD", "PTF"}
    assert all(rid.startswith("R-") and rid.split("-")[1] in stages for rid in ALL_RULE_IDS)


TUESDAY = NOW
FRIDAY_LATE = NOW.replace(day=9, hour=19)  # Fri 19:00 UTC, after the 18:00 cutoff
SATURDAY = NOW.replace(day=10, hour=12)
ROLLOVER = NOW.replace(hour=21, minute=0)  # 17:00 New York (EDT)

CASES: list[tuple[str, dict[str, Any], str]] = [
    # --- system
    ("trading state not ACTIVE", {"trading_state": TradingState.NO_NEW_TRADES}, "R-SYS-01"),
    ("halted", {"trading_state": TradingState.HALTED}, "R-SYS-01"),
    ("policy hash mismatch", {"active_sha_override": "0" * 64}, "R-SYS-02"),
    ("stale account", {"account_age": timedelta(seconds=121)}, "R-SYS-03"),
    ("account from the future", {"account_age": timedelta(seconds=-5)}, "R-SYS-03"),
    ("stale market snapshot", {"market_age": timedelta(seconds=361)}, "R-SYS-04"),
    ("stale last tick", {"tick_age": timedelta(seconds=11)}, "R-SYS-04"),
    ("stale calendar", {"calendar_age": timedelta(hours=6, seconds=1)}, "R-SYS-05"),
    ("calendar sources disagree", {"calendar_agreement": False}, "R-SYS-06"),
    ("single calendar source", {"calendar_sources": ("primary_api",)}, "R-SYS-06"),
    ("stale news", {"news_age": timedelta(seconds=901)}, "R-SYS-07"),
    ("reconciliation mismatch", {"reconciliation_ok": False}, "R-SYS-08"),
    ("reconciliation stale", {"recon_age": timedelta(seconds=121)}, "R-SYS-08"),
    ("circuit breaker", {"circuit_breaker": True}, "R-SYS-09"),
    (
        "open position without stop",
        {"positions": (ExistingPosition("USD_JPY", Side.LONG, Decimal("0.2"), stop=False),)},
        "R-SYS-10",
    ),
    # --- account
    ("daily loss at limit", {"day_loss_pct": Decimal("1.5")}, "R-ACC-01"),
    ("daily loss + new risk over limit", {"day_loss_pct": Decimal("1.1")}, "R-ACC-01"),
    ("weekly loss", {"week_loss_pct": Decimal("2.6")}, "R-ACC-02"),
    ("monthly loss", {"month_loss_pct": Decimal("4.6")}, "R-ACC-03"),
    ("drawdown halt", {"drawdown_pct": Decimal(10)}, "R-ACC-04"),
    (
        "cooldown active",
        {"consecutive_losses": 3, "last_loss_ago": timedelta(hours=2)},
        "R-ACC-05",
    ),
    ("cooldown without loss timestamp", {"consecutive_losses": 3}, "R-ACC-05"),
    (
        "consecutive-loss halt",
        {"consecutive_losses": 5, "last_loss_ago": timedelta(days=3)},
        "R-ACC-06",
    ),
    ("daily trade count", {"trades_today": 2}, "R-ACC-07"),
    # --- market
    (
        "tier-1 USD event inside hold window",
        {"events": (event(USD, NOW + timedelta(hours=2)),)},
        "R-MKT-01",
    ),
    (
        "tier-1 event just happened",
        {"events": (event(EUR, NOW - timedelta(minutes=30)),)},
        "R-MKT-01",
    ),
    (
        "central bank press conference",
        {
            "events": (
                event(
                    EUR,
                    NOW - timedelta(minutes=30),
                    central_bank=True,
                    ends_at=NOW + timedelta(minutes=10),
                ),
            )
        },
        "R-MKT-01",
    ),
    (
        "tier-2 event soon",
        {"events": (event(USD, NOW + timedelta(minutes=20), tier=2),)},
        "R-MKT-01",
    ),
    ("wide spread", {"spread_multiple": Decimal(2)}, "R-MKT-02"),
    ("rollover window", {"now": ROLLOVER}, "R-MKT-03"),
    ("friday after cutoff", {"now": FRIDAY_LATE}, "R-MKT-04"),
    ("weekend", {"now": SATURDAY}, "R-MKT-04"),
    ("thin-liquidity holiday", {"holidays": frozenset({NOW.date()})}, "R-MKT-04"),
    ("extreme volatility", {"regime": VolRegime.EXTREME}, "R-MKT-05"),
    ("news tripwire", {"tripwires": frozenset({EUR})}, "R-MKT-06"),
    ("news score high", {"news_scores": ((USD, Decimal(40)),)}, "R-MKT-06"),
    # --- trade
    ("stop too tight vs ATR", {"stop_pips": Decimal(15)}, "R-TRD-01"),
    ("stop too wide vs ATR", {"stop_pips": Decimal(85), "target_pips": Decimal(200)}, "R-TRD-01"),
    ("zero stop distance", {"stop_pips": Decimal(0)}, "R-TRD-01"),
    ("stop on wrong side", {"stop_pips": Decimal(-30)}, "R-TRD-01"),
    ("reward too small", {"target_pips": Decimal(50)}, "R-TRD-02"),
    ("expired candidate", {"candidate_age": timedelta(minutes=16)}, "R-TRD-03"),
    ("ttl longer than policy", {"candidate_ttl": timedelta(hours=1)}, "R-TRD-03"),
    ("candidate from the future", {"candidate_age": timedelta(minutes=-5)}, "R-TRD-03"),
    ("size below broker minimum", {"equity": Decimal("0.02")}, "R-TRD-04"),
    # --- portfolio
    (
        "USD exposure via correlated longs",
        {
            "positions": (
                ExistingPosition("GBP_USD", Side.LONG, Decimal("0.5")),
                ExistingPosition("AUD_USD", Side.LONG, Decimal("0.25")),
            )
        },
        "R-PTF-01",
    ),
    (
        "total open risk",
        {
            "positions": (
                ExistingPosition("GBP_USD", Side.LONG, Decimal("0.5")),
                ExistingPosition("USD_JPY", Side.LONG, Decimal("0.6")),
            )
        },
        "R-PTF-02",
    ),
    (
        "position count",
        {
            "positions": (
                ExistingPosition("GBP_USD", Side.LONG, Decimal(0)),
                ExistingPosition("USD_JPY", Side.LONG, Decimal(0)),
                ExistingPosition("USD_CAD", Side.SHORT, Decimal(0)),
            )
        },
        "R-PTF-03",
    ),
    (
        "second position on same pair",
        {"positions": (ExistingPosition("EUR_USD", Side.SHORT, Decimal(0)),)},
        "R-PTF-03",
    ),
]


@pytest.mark.parametrize(("label", "changes", "rule_id"), CASES, ids=[c[0] for c in CASES])
def test_rule_blocks(label: str, changes: dict[str, Any], rule_id: str) -> None:
    d = evaluate(inputs(**changes))
    assert d.outcome is Outcome.BLOCKED, label
    assert rule_id in d.blocking_rules, (label, d.blocking_rules)
    assert d.units == 0


def test_every_rule_has_a_blocking_case() -> None:
    covered = {rule_id for _, _, rule_id in CASES} | {"R-TRD-05", "R-PTF-04", "R-SYS-00"}
    assert covered == set(ALL_RULE_IDS)


def test_leverage_and_margin_limit() -> None:
    tight = policy().with_changes(max_effective_leverage=Decimal("1.5"))
    d = evaluate(inputs(policy_override=tight))
    assert "R-TRD-05" in d.blocking_rules


def test_gap_scenario_limit() -> None:
    tight = policy().with_changes(max_gap_loss=Percent(Decimal("0.4")))
    d = evaluate(inputs(policy_override=tight))
    assert "R-PTF-04" in d.blocking_rules


def test_rule_results_report_observed_and_threshold() -> None:
    d = evaluate(inputs(day_loss_pct=Decimal("1.5")))
    r = next(r for r in d.rule_results if r.rule_id == "R-ACC-01")
    assert r.stage is Stage.ACCOUNT
    assert r.observed is not None
    assert r.threshold is not None


def test_cooldown_expires_after_configured_hours() -> None:
    d = evaluate(inputs(consecutive_losses=3, last_loss_ago=timedelta(hours=25)))
    assert "R-ACC-05" not in d.blocking_rules
    assert d.outcome is Outcome.APPROVED


def test_events_outside_window_or_tier3_do_not_block() -> None:
    far = event(USD, NOW + timedelta(hours=8))
    tier3 = event(USD, NOW + timedelta(minutes=10), tier=3)
    unrelated = event(JPY, NOW + timedelta(minutes=10))
    d = evaluate(inputs(events=(far, tier3, unrelated)))
    assert d.outcome is Outcome.APPROVED, d.blocking_rules


def test_risk_reducing_exposure_is_not_double_counted() -> None:
    # A short GBPUSD (+USD) offsets the new long EURUSD (-USD).
    d = evaluate(inputs(positions=(ExistingPosition("GBP_USD", Side.SHORT, Decimal("0.5")),)))
    assert "R-PTF-01" not in d.blocking_rules


def test_short_candidate_is_sized_and_approved() -> None:
    d = evaluate(inputs(side=Side.SHORT))
    assert d.outcome is Outcome.APPROVED, d.blocking_rules


@pytest.mark.parametrize("symbol", ["USD_JPY", "USD_CAD", "GBP_USD", "AUD_USD"])
def test_other_pairs_approve_under_baseline_conditions(symbol: str) -> None:
    d = evaluate(inputs(symbol=symbol))
    assert d.outcome is Outcome.APPROVED, (symbol, d.blocking_rules)
    assert d.risk_amount is not None
    assert d.risk_amount.amount <= Decimal(50)


def test_drawdown_throttle_reduces_size() -> None:
    full = evaluate(inputs())
    throttled = evaluate(inputs(drawdown_pct=Decimal(6)))
    assert throttled.outcome is Outcome.APPROVED
    assert throttled.effective_risk is not None
    assert throttled.effective_risk.value == Decimal("0.25")
    assert throttled.units < full.units


def test_live_micro_mode_reduces_size() -> None:
    d = evaluate(inputs(mode=SystemMode.LIVE_MICRO))
    assert d.effective_risk is not None
    assert d.effective_risk.value == Decimal("0.1")


def test_decision_binds_policy_state_and_snapshots() -> None:
    d = evaluate(inputs())
    assert d.policy_id == "rs_v1"
    assert d.state_version == 7
    assert d.snapshot_digest is not None
    assert len(d.snapshot_digest) == 64
    assert evaluate(inputs(news_age=timedelta(seconds=30))).snapshot_digest != d.snapshot_digest


def test_knobs_default_is_reusable() -> None:
    assert inputs(Knobs()).candidate == inputs().candidate
