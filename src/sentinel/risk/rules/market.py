"""Market-tier rules: event blackouts, spread, rollover, market hours, volatility, news."""

from __future__ import annotations

from datetime import datetime

from sentinel.domain.decision import Stage
from sentinel.domain.snapshots import EconomicEvent, EventTier, VolRegime
from sentinel.risk.context import Context
from sentinel.risk.policy import RiskPolicy
from sentinel.risk.rules.base import Check, Rule
from sentinel.risk.sessions import after_friday_cutoff, in_local_window, market_closed

__all__ = ["RULES", "blackout_window"]


def blackout_window(event: EconomicEvent, policy: RiskPolicy) -> tuple[datetime, datetime] | None:
    """The no-new-risk window around an event, or ``None`` for tier-3 events."""
    end = event.ends_at or event.scheduled_at
    if event.is_central_bank:
        before, after = policy.blackout_central_bank_before, policy.blackout_central_bank_after
    elif event.tier is EventTier.TIER1:
        before, after = policy.blackout_tier1_before, policy.blackout_tier1_after
    elif event.tier is EventTier.TIER2:
        before, after = policy.blackout_tier2_before, policy.blackout_tier2_after
    else:
        return None
    return event.scheduled_at - before, end + after


def no_blackout(ctx: Context) -> Check:
    currencies = set(ctx.instrument.currencies)
    start, end = ctx.as_of, ctx.hold_end()
    hits = []
    for e in ctx.calendar.events:
        window = blackout_window(e, ctx.policy)
        if window is None or e.currency not in currencies:
            continue
        if window[0] <= end and window[1] >= start:
            hits.append(
                f"{e.currency} {e.title} at {e.scheduled_at.isoformat()} (tier {int(e.tier)})"
            )
    return Check(
        not hits,
        "no event blackout in the hold window"
        if not hits
        else "event blackout: " + "; ".join(hits),
        observed="; ".join(hits) or "none",
        threshold=f"hold window {start.isoformat()}..{end.isoformat()}",
    )


def spread_ok(ctx: Context) -> Check:
    m, inst, p = ctx.market, ctx.instrument, ctx.policy
    ratio = m.spread / m.session_median_spread
    spread_pips = m.spread / inst.pip_size
    ok = ratio <= p.max_spread_ratio and spread_pips <= inst.max_spread_pips
    return Check(
        ok,
        f"spread {spread_pips:.2f} pips, {ratio:.2f}x session median",
        observed=f"{ratio:.3f}x, {spread_pips:.2f} pips",
        threshold=f"<= {p.max_spread_ratio}x, <= {inst.max_spread_pips} pips",
    )


def outside_rollover(ctx: Context) -> Check:
    p = ctx.policy
    inside = in_local_window(ctx.as_of, p.rollover_start, p.rollover_end, p.rollover_tz)
    return Check(
        not inside,
        "inside the rollover window" if inside else "outside the rollover window",
        threshold=f"not in {p.rollover_start}-{p.rollover_end} {p.rollover_tz}",
    )


def market_open(ctx: Context) -> Check:
    reasons = []
    if market_closed(ctx.as_of):
        reasons.append("market closed for the weekend")
    if after_friday_cutoff(ctx.as_of, ctx.policy.friday_cutoff_utc):
        reasons.append("after the Friday cutoff")
    if ctx.as_of.date() in ctx.inputs.holidays:
        reasons.append("thin-liquidity or closed holiday")
    return Check(not reasons, "; ".join(reasons) or "market open for new entries")


def volatility_ok(ctx: Context) -> Check:
    regime = ctx.market.regime
    return Check(
        regime is not VolRegime.EXTREME,
        f"volatility regime {regime}",
        observed=str(regime),
        threshold=f"not {VolRegime.EXTREME}",
    )


def news_ok(ctx: Context) -> Check:
    limit = ctx.policy.max_news_risk_score
    problems = []
    for ccy in ctx.instrument.currencies:
        risk = ctx.news.for_currency(ccy)
        if risk is None:
            problems.append(f"{ccy}: no news assessment")
        elif risk.tripwire_active:
            problems.append(f"{ccy}: tripwire ({', '.join(risk.reasons) or 'unspecified'})")
        elif risk.score >= limit:
            problems.append(f"{ccy}: score {risk.score}")
    return Check(
        not problems,
        "news risk acceptable" if not problems else "news risk: " + "; ".join(problems),
        observed="; ".join(problems) or "ok",
        threshold=f"no tripwire, score < {limit}",
    )


RULES: tuple[Rule, ...] = (
    Rule("R-MKT-01", Stage.MARKET, "No event blackout over the hold window", no_blackout),
    Rule("R-MKT-02", Stage.MARKET, "Spread within limits", spread_ok),
    Rule("R-MKT-03", Stage.MARKET, "Outside the rollover window", outside_rollover),
    Rule("R-MKT-04", Stage.MARKET, "Market open; before Friday cutoff; not a holiday", market_open),
    Rule("R-MKT-05", Stage.MARKET, "Volatility regime not extreme", volatility_ok),
    Rule("R-MKT-06", Stage.MARKET, "No news tripwire; news score below limit", news_ok),
)
