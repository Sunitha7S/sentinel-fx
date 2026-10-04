"""Portfolio-tier rules: currency exposure, total open risk, position counts, gap stress."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sentinel.domain.decision import Stage
from sentinel.domain.snapshots import EventTier
from sentinel.domain.types import Currency, Side
from sentinel.risk.context import Context
from sentinel.risk.rules.base import Check, Rule
from sentinel.risk.sessions import crosses_weekly_close

__all__ = ["RULES", "currency_exposure"]


def currency_exposure(ctx: Context) -> dict[Currency, Decimal]:
    """Signed risk per currency (% of equity) after adding the candidate.

    A long EURUSD position puts its stop-risk on EUR (+) and against USD (-). Summing per
    currency exposes concentration that pairwise correlation hides: long EURUSD, GBPUSD
    and AUDUSD are three positions but one short-USD bet.
    """
    net: dict[Currency, Decimal] = defaultdict(Decimal)
    legs: list[tuple[str, Side, Decimal]] = [
        (p.symbol, p.side, p.risk_amount.amount) for p in ctx.positions
    ]
    legs.append((ctx.candidate.symbol, ctx.candidate.side, ctx.prospective_risk))
    for symbol, side, risk in legs:
        inst = ctx.instrument_for(symbol)
        pct = ctx.pct_of_equity(risk)
        net[inst.base] += side.sign * pct
        net[inst.quote] -= side.sign * pct
    return dict(net)


def currency_net_risk(ctx: Context) -> Check:
    limit = ctx.policy.max_currency_net_risk.value
    net = currency_exposure(ctx)
    touched = {c: net.get(c, Decimal(0)) for c in ctx.instrument.currencies}
    worst = max(touched.items(), key=lambda kv: abs(kv[1]))
    return Check(
        all(abs(v) <= limit for v in touched.values()),
        f"net risk after trade: {', '.join(f'{c} {v:+.3f}%' for c, v in touched.items())}",
        observed=f"{worst[0]} {worst[1]:+.4f}%",
        threshold=f"|net| <= {limit}%",
    )


def total_open_risk(ctx: Context) -> Check:
    existing = sum((p.risk_amount.amount for p in ctx.positions), Decimal(0))
    total = ctx.pct_of_equity(existing + ctx.prospective_risk)
    limit = ctx.policy.max_open_risk.value
    return Check(
        total <= limit,
        f"total open risk after trade {total:.3f}%",
        observed=f"{total:.4f}%",
        threshold=f"<= {limit}%",
    )


def position_counts(ctx: Context) -> Check:
    p = ctx.policy
    total = len(ctx.positions) + 1
    same = sum(1 for pos in ctx.positions if pos.symbol == ctx.candidate.symbol) + 1
    ok = total <= p.max_open_positions and same <= p.max_positions_per_pair
    return Check(
        ok,
        f"{total} positions after trade, {same} on {ctx.candidate.symbol}",
        observed=f"total {total}, pair {same}",
        threshold=f"total <= {p.max_open_positions}, pair <= {p.max_positions_per_pair}",
    )


def _event_risk(ctx: Context, symbol: str, until: datetime) -> bool:
    currencies = set(ctx.instrument_for(symbol).currencies)
    if crosses_weekly_close(ctx.as_of, until):
        return True
    return any(
        e.tier is EventTier.TIER1
        and e.currency in currencies
        and ctx.as_of <= e.scheduled_at <= until
        for e in ctx.calendar.events
    )


def gap_scenario(ctx: Context) -> Check:
    """Loss if every stop filled ``k x ATR(D1)`` beyond its level. Stops are not guarantees."""
    p, acct = ctx.policy, ctx.account_ccy
    legs = [
        (pos.symbol, pos.units, pos.risk_amount.amount, ctx.horizon_end()) for pos in ctx.positions
    ]
    legs.append((ctx.candidate.symbol, ctx.new_units, ctx.prospective_risk, ctx.hold_end()))
    total = Decimal(0)
    for symbol, units, risk, until in legs:
        inst = ctx.instrument_for(symbol)
        k = (
            p.gap_atr_multiple_event
            if _event_risk(ctx, symbol, until)
            else p.gap_atr_multiple_normal
        )
        gap = k * ctx.market_for(symbol).atr_d1 * units * ctx.fx.rate(inst.quote, acct)
        total += risk + gap
    pct = ctx.pct_of_equity(total)
    limit = p.max_gap_loss.value
    return Check(
        pct <= limit,
        f"gap-scenario loss {pct:.3f}% of equity",
        observed=f"{pct:.4f}%",
        threshold=f"<= {limit}%",
    )


RULES: tuple[Rule, ...] = (
    Rule("R-PTF-01", Stage.PORTFOLIO, "Per-currency net risk within limit", currency_net_risk),
    Rule("R-PTF-02", Stage.PORTFOLIO, "Total open risk within limit", total_open_risk),
    Rule("R-PTF-03", Stage.PORTFOLIO, "Position counts within limits", position_counts),
    Rule("R-PTF-04", Stage.PORTFOLIO, "Gap-scenario loss within limit", gap_scenario),
)
