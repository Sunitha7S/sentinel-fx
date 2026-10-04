"""Account-tier rules: loss limits, drawdown, losing streaks, trade frequency."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from sentinel.domain.decision import Stage
from sentinel.domain.snapshots import AccountSnapshot
from sentinel.domain.types import Money, Percent
from sentinel.risk.context import Context, MissingInput
from sentinel.risk.policy import RiskPolicy
from sentinel.risk.rules.base import Check, Rule

__all__ = ["RULES"]


def _period_loss(
    label: str,
    start_of: Callable[[AccountSnapshot], Money],
    limit_of: Callable[[RiskPolicy], Percent],
) -> Callable[[Context], Check]:
    def check(ctx: Context) -> Check:
        start = start_of(ctx.account).amount
        if start <= 0:
            raise MissingInput(f"positive {label} start equity")
        realised = max(Decimal(0), start - ctx.equity)
        after = (realised + ctx.prospective_risk) / start * 100
        limit = limit_of(ctx.policy).value
        return Check(
            after <= limit,
            f"{label} loss incl. this trade's risk is {after:.3f}% (limit {limit}%)",
            observed=f"{after:.4f}%",
            threshold=f"<= {limit}%",
        )

    return check


def drawdown_below_halt(ctx: Context) -> Check:
    dd, limit = ctx.drawdown, ctx.policy.max_drawdown
    return Check(
        dd < limit,
        f"drawdown from peak is {dd.value:.3f}%",
        observed=f"{dd.value:.4f}%",
        threshold=f"< {limit}",
    )


def not_in_cooldown(ctx: Context) -> Check:
    p, a = ctx.policy, ctx.account
    if a.consecutive_losses < p.cooldown_after_consecutive_losses:
        return Check(
            True, f"{a.consecutive_losses} consecutive losses", observed=str(a.consecutive_losses)
        )
    if a.last_loss_at is None:
        return Check(False, "loss streak without a last-loss timestamp", observed="unknown")
    until = a.last_loss_at + p.cooldown
    return Check(
        ctx.as_of >= until,
        f"cooldown after {a.consecutive_losses} losses until {until.isoformat()}",
        observed=f"{a.consecutive_losses} losses, last {a.last_loss_at.isoformat()}",
        threshold=f"wait {p.cooldown}",
    )


def below_streak_halt(ctx: Context) -> Check:
    n, limit = ctx.account.consecutive_losses, ctx.policy.halt_after_consecutive_losses
    return Check(n < limit, f"{n} consecutive losses", observed=str(n), threshold=f"< {limit}")


def daily_trade_count(ctx: Context) -> Check:
    n, limit = ctx.account.trades_today, ctx.policy.max_new_trades_per_day
    return Check(n < limit, f"{n} trades today", observed=str(n), threshold=f"< {limit}")


RULES: tuple[Rule, ...] = (
    Rule(
        "R-ACC-01",
        Stage.ACCOUNT,
        "Daily loss incl. new risk within limit",
        _period_loss("daily", lambda a: a.day_start_equity, lambda p: p.daily_loss),
    ),
    Rule(
        "R-ACC-02",
        Stage.ACCOUNT,
        "Weekly loss incl. new risk within limit",
        _period_loss("weekly", lambda a: a.week_start_equity, lambda p: p.weekly_loss),
    ),
    Rule(
        "R-ACC-03",
        Stage.ACCOUNT,
        "Monthly loss incl. new risk within limit",
        _period_loss("monthly", lambda a: a.month_start_equity, lambda p: p.monthly_loss),
    ),
    Rule("R-ACC-04", Stage.ACCOUNT, "Drawdown below halt level", drawdown_below_halt),
    Rule("R-ACC-05", Stage.ACCOUNT, "Not in consecutive-loss cooldown", not_in_cooldown),
    Rule("R-ACC-06", Stage.ACCOUNT, "Below consecutive-loss halt", below_streak_halt),
    Rule("R-ACC-07", Stage.ACCOUNT, "Daily trade count below limit", daily_trade_count),
)
