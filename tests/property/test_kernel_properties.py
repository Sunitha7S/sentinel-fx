"""Properties of the risk kernel over randomised market/account conditions."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.domain.decision import Outcome
from sentinel.domain.types import Currency, Side
from sentinel.risk.kernel import ALL_RULE_IDS, evaluate

from support.builders import ExistingPosition, Knobs, inputs, policy


def dec(lo: str, hi: str, places: int = 2) -> st.SearchStrategy[Decimal]:
    return st.decimals(min_value=Decimal(lo), max_value=Decimal(hi), places=places)


POSITIONS = st.lists(
    st.builds(
        ExistingPosition,
        symbol=st.sampled_from(["GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD"]),
        side=st.sampled_from(list(Side)),
        risk_pct=dec("0", "0.6"),
    ),
    max_size=3,
    unique_by=lambda p: p.symbol,
).map(tuple)


@st.composite
def knobs(draw: st.DrawFn) -> Knobs:
    return Knobs(
        symbol=draw(st.sampled_from(["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD"])),
        side=draw(st.sampled_from(list(Side))),
        equity=draw(dec("500", "1000000")),
        day_loss_pct=draw(dec("0", "2")),
        week_loss_pct=draw(dec("0", "3.5")),
        drawdown_pct=draw(dec("0", "12")),
        consecutive_losses=draw(st.integers(0, 6)),
        last_loss_ago=draw(st.none() | st.integers(0, 72).map(lambda h: timedelta(hours=h))),
        trades_today=draw(st.integers(0, 3)),
        spread_multiple=draw(dec("0.5", "3", 1)),
        account_age=timedelta(seconds=draw(st.integers(0, 200))),
        tick_age=timedelta(seconds=draw(st.integers(0, 20))),
        news_scores=((Currency.USD, draw(dec("0", "60", 0))),),
        stop_pips=draw(dec("10", "100", 0)),
        target_pips=draw(dec("20", "300", 0)),
        positions=draw(POSITIONS),
    )


@pytest.mark.invariant("INV-KERNEL-01")
@given(knobs())
def test_kernel_is_total_exhaustive_and_safe_when_approving(k: Knobs) -> None:
    d = evaluate(inputs(k))
    assert tuple(r.rule_id for r in d.rule_results) == ALL_RULE_IDS
    if d.outcome is Outcome.APPROVED:
        assert d.units > 0
        assert all(r.passed for r in d.rule_results)
        assert d.risk_amount is not None
        assert d.risk_amount.amount <= k.equity * policy().risk_per_trade.fraction
        assert d.snapshot_digest is not None
    else:
        assert d.units == 0
        assert d.blocking_rules


@pytest.mark.invariant("INV-KERNEL-01")
@given(knobs())
def test_kernel_is_deterministic(k: Knobs) -> None:
    assert evaluate(inputs(k)) == evaluate(inputs(k))


# Each entry worsens one dimension of the inputs. If the worse input is approved, the
# original must have been approved too: no amount of extra danger may unlock a trade.
WORSENINGS = {
    "daily loss": lambda k, x: replace(k, day_loss_pct=k.day_loss_pct + x),
    "weekly loss": lambda k, x: replace(k, week_loss_pct=k.week_loss_pct + x),
    "drawdown": lambda k, x: replace(k, drawdown_pct=min(k.drawdown_pct + x, Decimal(99))),
    "spread": lambda k, x: replace(k, spread_multiple=k.spread_multiple + x),
    "account age": lambda k, x: replace(
        k, account_age=k.account_age + timedelta(seconds=int(x * 60))
    ),
    "tick age": lambda k, x: replace(k, tick_age=k.tick_age + timedelta(seconds=int(x * 10))),
    "news score": lambda k, x: replace(
        k, news_scores=((Currency.USD, min(Decimal(100), k.news_scores[0][1] + x * 10)),)
    ),
    "trades today": lambda k, x: replace(k, trades_today=k.trades_today + int(x)),
    "extra position": lambda k, x: (
        replace(k, positions=(*k.positions, ExistingPosition("EUR_USD", Side.LONG, x / 10)))
        if all(p.symbol != "EUR_USD" for p in k.positions) and k.symbol != "EUR_USD"
        else k
    ),
}


@pytest.mark.invariant("INV-KERNEL-02")
@pytest.mark.parametrize("dimension", sorted(WORSENINGS))
@given(k=knobs(), amount=dec("0.01", "5"))
def test_worse_inputs_never_turn_blocked_into_approved(
    dimension: str, k: Knobs, amount: Decimal
) -> None:
    worse = WORSENINGS[dimension](k, amount)
    base_d = evaluate(inputs(k))
    worse_d = evaluate(inputs(worse))
    if worse_d.outcome is Outcome.APPROVED:
        assert base_d.outcome is Outcome.APPROVED
        assert worse_d.units <= base_d.units
