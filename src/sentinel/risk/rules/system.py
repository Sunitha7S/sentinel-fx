"""System-tier rules: inputs present, trading state, policy integrity, data freshness."""

from __future__ import annotations

from sentinel.domain.decision import Stage
from sentinel.domain.system import TradingState
from sentinel.risk.context import Context, MissingInput
from sentinel.risk.rules.base import Check, Rule, age_text, fresh

__all__ = ["RULES"]


def inputs_present(ctx: Context) -> Check:
    i = ctx.inputs
    missing = [
        name
        for name, value in (
            ("candidate", i.candidate),
            ("policy", i.policy),
            ("active policy hash", i.active_policy_sha256),
            ("system state", i.system),
            ("account", i.account),
            ("calendar", i.calendar),
            ("news", i.news),
        )
        if value is None
    ]
    if i.candidate is not None and i.account is not None:
        try:
            for symbol in ctx.required_symbols:
                if symbol not in i.markets:
                    missing.append(f"market {symbol}")
                if symbol not in i.instruments:
                    missing.append(f"instrument {symbol}")
        except MissingInput as exc:
            missing.append(str(exc))
    if missing:
        return Check(False, "missing inputs: " + ", ".join(missing), observed=", ".join(missing))
    return Check(True, "all inputs present")


def trading_active(ctx: Context) -> Check:
    state = ctx.system.trading_state
    return Check(
        state is TradingState.ACTIVE,
        f"trading state is {state}",
        observed=str(state),
        threshold=str(TradingState.ACTIVE),
    )


def policy_integrity(ctx: Context) -> Check:
    actual = ctx.policy.sha256
    expected = ctx.inputs.active_policy_sha256
    return Check(
        expected is not None and actual == expected,
        "policy matches the approved active policy"
        if actual == expected
        else "policy hash does not match the approved active policy",
        observed=actual,
        threshold=expected,
    )


def account_fresh(ctx: Context) -> Check:
    p, a = ctx.policy, ctx.account
    ok = fresh(a.as_of, ctx.as_of, p.max_account_age, p.max_clock_skew)
    return Check(
        ok,
        "account snapshot is fresh" if ok else "account snapshot is stale or future-dated",
        observed=age_text(a.as_of, ctx.as_of),
        threshold=f"<= {p.max_account_age.total_seconds():.0f}s",
    )


def markets_fresh(ctx: Context) -> Check:
    p = ctx.policy
    stale = []
    for symbol in ctx.required_symbols:
        m = ctx.market_for(symbol)
        if not fresh(m.as_of, ctx.as_of, p.max_market_snapshot_age, p.max_clock_skew):
            stale.append(f"{symbol} snapshot {age_text(m.as_of, ctx.as_of)}")
        if not fresh(m.last_tick_at, ctx.as_of, p.max_price_age, p.max_clock_skew):
            stale.append(f"{symbol} tick {age_text(m.last_tick_at, ctx.as_of)}")
    return Check(
        not stale,
        "market data is fresh" if not stale else "stale market data: " + "; ".join(stale),
        observed="; ".join(stale) or "fresh",
        threshold=(
            f"snapshot <= {p.max_market_snapshot_age.total_seconds():.0f}s, "
            f"tick <= {p.max_price_age.total_seconds():.0f}s"
        ),
    )


def calendar_fresh(ctx: Context) -> Check:
    p, c = ctx.policy, ctx.calendar
    ok = fresh(c.as_of, ctx.as_of, p.max_calendar_age, p.max_clock_skew)
    return Check(
        ok,
        "calendar is fresh" if ok else "calendar is stale or future-dated",
        observed=age_text(c.as_of, ctx.as_of),
        threshold=f"<= {p.max_calendar_age.total_seconds():.0f}s",
    )


def calendar_agreement(ctx: Context) -> Check:
    p, c = ctx.policy, ctx.calendar
    sources = len(set(c.sources))
    ok = c.source_agreement and sources >= p.min_calendar_sources
    detail = "; ".join(c.discrepancies)
    return Check(
        ok,
        "calendar sources agree"
        if ok
        else f"calendar sources disagree or are insufficient {detail}".strip(),
        observed=f"agreement={c.source_agreement}, sources={sources}",
        threshold=f"agreement=True, sources>={p.min_calendar_sources}",
    )


def news_fresh(ctx: Context) -> Check:
    p, n = ctx.policy, ctx.news
    ok = fresh(n.as_of, ctx.as_of, p.max_news_age, p.max_clock_skew)
    return Check(
        ok,
        "news risk is fresh" if ok else "news risk is stale or future-dated",
        observed=age_text(n.as_of, ctx.as_of),
        threshold=f"<= {p.max_news_age.total_seconds():.0f}s",
    )


def reconciled(ctx: Context) -> Check:
    p, a = ctx.policy, ctx.account
    at = a.reconciled_at
    ok = (
        a.reconciliation_ok
        and at is not None
        and fresh(at, ctx.as_of, p.max_reconciliation_age, p.max_clock_skew)
    )
    return Check(
        ok,
        "broker reconciliation is current and matched"
        if ok
        else "broker reconciliation failed or is stale",
        observed=f"ok={a.reconciliation_ok}, age={age_text(at, ctx.as_of) if at else 'never'}",
        threshold=f"ok=True, age <= {p.max_reconciliation_age.total_seconds():.0f}s",
    )


def no_circuit_breaker(ctx: Context) -> Check:
    traded = set(ctx.instrument.currencies)
    tripped = sorted(
        symbol
        for symbol, m in ctx.inputs.markets.items()
        if m.circuit_breaker_active
        and symbol in ctx.inputs.instruments
        and traded & set(ctx.inputs.instruments[symbol].currencies)
    )
    return Check(
        not tripped,
        "no circuit breaker active" if not tripped else f"circuit breaker active: {tripped}",
        observed=", ".join(tripped) or "none",
    )


def positions_protected(ctx: Context) -> Check:
    unprotected = sorted(p.position_id for p in ctx.positions if p.stop_loss is None)
    return Check(
        not unprotected,
        "all open positions have broker-side stops"
        if not unprotected
        else f"open positions without a stop: {unprotected}",
        observed=", ".join(unprotected) or "none",
    )


RULES: tuple[Rule, ...] = (
    Rule("R-SYS-00", Stage.SYSTEM, "All required inputs are present", inputs_present),
    Rule("R-SYS-01", Stage.SYSTEM, "Trading state is ACTIVE", trading_active),
    Rule("R-SYS-02", Stage.SYSTEM, "Policy is the approved active policy", policy_integrity),
    Rule("R-SYS-03", Stage.SYSTEM, "Account snapshot is fresh", account_fresh),
    Rule("R-SYS-04", Stage.SYSTEM, "Market data in use is fresh", markets_fresh),
    Rule("R-SYS-05", Stage.SYSTEM, "Calendar is fresh", calendar_fresh),
    Rule("R-SYS-06", Stage.SYSTEM, "Calendar sources agree", calendar_agreement),
    Rule("R-SYS-07", Stage.SYSTEM, "News risk is fresh", news_fresh),
    Rule("R-SYS-08", Stage.SYSTEM, "Broker reconciliation is current", reconciled),
    Rule("R-SYS-09", Stage.SYSTEM, "No circuit breaker on traded currencies", no_circuit_breaker),
    Rule("R-SYS-10", Stage.SYSTEM, "Every open position has a stop", positions_protected),
)
