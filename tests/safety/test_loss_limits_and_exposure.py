"""Loss limits, drawdown halt, consecutive-loss protection and correlated exposure."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.domain.decision import Outcome
from sentinel.domain.instrument import DEFAULT_INSTRUMENTS
from sentinel.domain.types import Currency, Side
from sentinel.risk.kernel import evaluate

from support.builders import ExistingPosition, inputs, policy


@pytest.mark.invariant("INV-LOSS-01")
@pytest.mark.parametrize(
    ("knob", "rule"),
    [("day_loss_pct", "R-ACC-01"), ("week_loss_pct", "R-ACC-02"), ("month_loss_pct", "R-ACC-03")],
)
def test_period_loss_limit_blocks_new_risk(knob: str, rule: str) -> None:
    limit = {
        "day_loss_pct": policy().daily_loss.value,
        "week_loss_pct": policy().weekly_loss.value,
        "month_loss_pct": policy().monthly_loss.value,
    }[knob]
    # At the limit: blocked.
    assert rule in evaluate(inputs(**{knob: limit})).blocking_rules
    # Below the limit, but this trade's risk (~0.48-0.50% of period-start equity) would
    # take the loss past it: blocked.
    assert rule in evaluate(inputs(**{knob: limit - Decimal("0.2")})).blocking_rules
    # Enough room: this rule passes.
    assert rule not in evaluate(inputs(**{knob: limit - Decimal(1)})).blocking_rules


@pytest.mark.invariant("INV-LOSS-01")
@given(st.decimals(min_value=Decimal(0), max_value=Decimal(3), places=3))
def test_approved_trade_never_takes_daily_loss_past_limit(loss: Decimal) -> None:
    d = evaluate(inputs(day_loss_pct=loss))
    if d.outcome is Outcome.APPROVED:
        assert d.risk_amount is not None
        day_start = Decimal(10_000) / (1 - loss / 100)
        realised = day_start - Decimal(10_000)
        assert (realised + d.risk_amount.amount) / day_start * 100 <= policy().daily_loss.value


@pytest.mark.invariant("INV-LOSS-02")
@given(st.decimals(min_value=Decimal(10), max_value=Decimal(60), places=2))
def test_drawdown_at_or_beyond_halt_level_always_blocks(dd: Decimal) -> None:
    d = evaluate(inputs(drawdown_pct=dd))
    assert d.outcome is Outcome.BLOCKED
    assert "R-ACC-04" in d.blocking_rules


@pytest.mark.invariant("INV-LOSS-02")
def test_drawdown_throttle_only_ever_reduces_risk() -> None:
    previous = policy().risk_per_trade.value
    for dd in [Decimal(x) / 2 for x in range(20)]:
        d = evaluate(inputs(drawdown_pct=dd))
        assert d.effective_risk is not None
        assert d.effective_risk.value <= previous
        previous = d.effective_risk.value


@pytest.mark.invariant("INV-LOSS-03")
def test_consecutive_loss_cooldown_blocks_then_releases() -> None:
    n = policy().cooldown_after_consecutive_losses
    hours = policy().cooldown
    during = evaluate(inputs(consecutive_losses=n, last_loss_ago=hours - timedelta(minutes=1)))
    after = evaluate(inputs(consecutive_losses=n, last_loss_ago=hours + timedelta(minutes=1)))
    below = evaluate(inputs(consecutive_losses=n - 1, last_loss_ago=timedelta(minutes=5)))
    assert "R-ACC-05" in during.blocking_rules
    assert "R-ACC-05" not in after.blocking_rules
    assert "R-ACC-05" not in below.blocking_rules
    assert after.outcome is Outcome.APPROVED


@pytest.mark.invariant("INV-LOSS-03")
def test_halt_after_too_many_consecutive_losses_does_not_expire() -> None:
    n = policy().halt_after_consecutive_losses
    d = evaluate(inputs(consecutive_losses=n, last_loss_ago=timedelta(days=30)))
    assert "R-ACC-06" in d.blocking_rules


# ----------------------------------------------------------------------------- exposure

POSITIONS = st.lists(
    st.builds(
        ExistingPosition,
        symbol=st.sampled_from(["GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD"]),
        side=st.sampled_from(list(Side)),
        risk_pct=st.decimals(min_value=Decimal(0), max_value=Decimal("0.8"), places=2),
    ),
    max_size=3,
    unique_by=lambda p: p.symbol,
).map(tuple)


@pytest.mark.invariant("INV-PORT-01")
@given(positions=POSITIONS, side=st.sampled_from(list(Side)))
def test_approved_trade_keeps_currency_and_total_exposure_within_limits(
    positions: tuple[ExistingPosition, ...], side: Side
) -> None:
    d = evaluate(inputs(positions=positions, side=side))
    if d.outcome is not Outcome.APPROVED:
        return
    assert d.risk_amount is not None
    new_pct = d.risk_amount.amount / Decimal(10_000) * 100
    net: dict[Currency, Decimal] = defaultdict(Decimal)
    legs = [(p.symbol, p.side, p.risk_pct) for p in positions] + [("EUR_USD", side, new_pct)]
    for symbol, s, pct in legs:
        inst = DEFAULT_INSTRUMENTS[symbol]
        net[inst.base] += s.sign * pct
        net[inst.quote] -= s.sign * pct
    limit = policy().max_currency_net_risk.value
    for ccy in (Currency.EUR, Currency.USD):
        assert abs(net[ccy]) <= limit, (ccy, net[ccy])
    assert sum(p.risk_pct for p in positions) + new_pct <= policy().max_open_risk.value
    assert len(positions) + 1 <= policy().max_open_positions


@pytest.mark.invariant("INV-PORT-01")
def test_three_correlated_usd_shorts_cannot_all_be_opened() -> None:
    existing = (
        ExistingPosition("GBP_USD", Side.LONG, Decimal("0.5")),
        ExistingPosition("AUD_USD", Side.LONG, Decimal("0.5")),
    )
    d = evaluate(inputs(positions=existing))
    assert d.outcome is Outcome.BLOCKED
    assert "R-PTF-01" in d.blocking_rules
