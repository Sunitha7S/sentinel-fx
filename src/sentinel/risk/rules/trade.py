"""Trade-tier rules: stop validity, reward/risk net of costs, timing, size, leverage."""

from __future__ import annotations

from decimal import Decimal

from sentinel.domain.decision import Stage
from sentinel.risk.context import Context
from sentinel.risk.rules.base import Check, Rule

__all__ = ["RULES"]


def _stop_distance(ctx: Context) -> Decimal:
    c = ctx.candidate
    return (c.entry - c.stop_loss) * c.side.sign


def stop_valid(ctx: Context) -> Check:
    p, m = ctx.policy, ctx.market
    distance = _stop_distance(ctx)
    atr = m.atr_h1
    problems = []
    if distance <= 0:
        problems.append("stop is not on the loss side of entry")
    else:
        if distance < p.min_stop_atr_h1 * atr:
            problems.append(f"stop {distance / atr:.2f} ATR < {p.min_stop_atr_h1}")
        if distance > p.max_stop_atr_h1 * atr:
            problems.append(f"stop {distance / atr:.2f} ATR > {p.max_stop_atr_h1}")
        if distance < p.min_stop_spread_multiple * m.spread:
            problems.append(f"stop closer than {p.min_stop_spread_multiple}x spread")
    return Check(
        not problems,
        "; ".join(problems) or "stop placement valid",
        observed=f"distance {distance} ({(distance / atr):.2f} ATR)" if atr else str(distance),
        threshold=(
            f"{p.min_stop_atr_h1}-{p.max_stop_atr_h1} ATR(H1), "
            f">= {p.min_stop_spread_multiple}x spread"
        ),
    )


def reward_to_risk(ctx: Context) -> Check:
    c, cost = ctx.candidate, ctx.cost_buffer
    reward = (c.take_profit - c.entry) * c.side.sign - cost
    risk = _stop_distance(ctx) + cost
    if reward <= 0 or risk <= 0:
        return Check(False, "target or stop on the wrong side after costs", observed="n/a")
    rr = reward / risk
    return Check(
        rr >= ctx.policy.min_rr_net,
        f"reward/risk net of costs {rr:.2f}",
        observed=f"{rr:.3f}",
        threshold=f">= {ctx.policy.min_rr_net}",
    )


def timing_valid(ctx: Context) -> Check:
    c, p = ctx.candidate, ctx.policy
    problems = []
    if c.created_at > ctx.as_of + p.max_clock_skew:
        problems.append("created in the future")
    if ctx.as_of >= c.expires_at:
        problems.append("expired")
    if c.expires_at - c.created_at > p.signal_ttl:
        problems.append(f"ttl {c.expires_at - c.created_at} exceeds {p.signal_ttl}")
    return Check(
        not problems,
        "; ".join(problems) or "signal within its validity window",
        observed=f"created {c.created_at.isoformat()}, expires {c.expires_at.isoformat()}",
        threshold=f"ttl <= {p.signal_ttl}",
    )


def size_valid(ctx: Context) -> Check:
    s = ctx.sizing
    return Check(
        s.ok and s.units > 0,
        f"size {s.units} units, risk {s.risk_amount:.2f} {ctx.account_ccy}"
        if s.ok
        else f"no valid size: {s.error}",
        observed=f"{s.units} units" if s.ok else str(s.error),
        threshold=f">= {ctx.instrument.min_units} units",
    )


def leverage_and_margin(ctx: Context) -> Check:
    acct, p = ctx.account_ccy, ctx.policy
    legs = [(pos.symbol, pos.units) for pos in ctx.positions]
    legs.append((ctx.candidate.symbol, ctx.new_units))
    notional = Decimal(0)
    margin = Decimal(0)
    for symbol, units in legs:
        inst = ctx.instrument_for(symbol)
        value = units * ctx.fx.rate(inst.base, acct)
        notional += value
        margin += value * inst.margin_rate
    leverage = notional / ctx.equity if ctx.equity > 0 else Decimal("Infinity")
    margin_limit = ctx.equity * p.max_margin_utilisation.fraction
    ok = leverage <= p.max_effective_leverage and margin <= margin_limit
    return Check(
        ok,
        f"effective leverage {leverage:.2f}x, margin {margin:.2f} {acct}",
        observed=f"{leverage:.3f}x, margin {margin:.2f}",
        threshold=f"<= {p.max_effective_leverage}x, margin <= {margin_limit:.2f}",
    )


RULES: tuple[Rule, ...] = (
    Rule(
        "R-TRD-01", Stage.TRADE, "Stop on the loss side, within ATR and spread bounds", stop_valid
    ),
    Rule("R-TRD-02", Stage.TRADE, "Reward/risk net of costs above minimum", reward_to_risk),
    Rule("R-TRD-03", Stage.TRADE, "Signal within its validity window", timing_valid),
    Rule("R-TRD-04", Stage.TRADE, "A valid broker-precision size exists", size_valid),
    Rule(
        "R-TRD-05",
        Stage.TRADE,
        "Leverage and margin after trade within limits",
        leverage_and_margin,
    ),
)
