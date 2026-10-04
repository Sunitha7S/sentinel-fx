"""Test builders: a baseline that the risk kernel approves, plus knobs to make it worse.

The baseline is deliberately unremarkable: Tuesday 13:00 UTC (London-New York overlap),
1-pip spreads, no events, no news risk, flat account, no open positions, a 30-pip stop
(1.5 x ATR H1) and a 70-pip target. Every test that expects BLOCKED changes one knob
and asserts *which* rule blocked, so a test cannot pass for the wrong reason.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from sentinel.config.policy_loader import load_policy
from sentinel.domain.decision import SignalCandidate
from sentinel.domain.instrument import DEFAULT_INSTRUMENTS
from sentinel.domain.snapshots import (
    AccountSnapshot,
    CalendarSnapshot,
    CurrencyNewsRisk,
    EconomicEvent,
    EventTier,
    MarketSnapshot,
    NewsRiskSnapshot,
    OpenPosition,
    VolRegime,
)
from sentinel.domain.system import Actor, ActorKind, SystemMode, SystemState, TradingState
from sentinel.domain.types import (
    CandidateId,
    Currency,
    DecisionId,
    Money,
    PositionId,
    Side,
    SnapshotId,
)
from sentinel.risk.kernel import RiskInputs
from sentinel.risk.policy import RiskPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "config" / "rulesets" / "rs_v1.yaml"

CENT = Decimal("0.01")
NOW = datetime(2026, 10, 6, 13, 0, tzinfo=UTC)  # Tuesday, 09:00 New York

# symbol -> (bid, atr_h1, pip)
MARKET_BASE: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "EUR_USD": (Decimal("1.08500"), Decimal("0.0020"), Decimal("0.0001")),
    "GBP_USD": (Decimal("1.27000"), Decimal("0.0025"), Decimal("0.0001")),
    "USD_JPY": (Decimal("149.500"), Decimal("0.30"), Decimal("0.01")),
    "AUD_USD": (Decimal("0.66000"), Decimal("0.0015"), Decimal("0.0001")),
    "USD_CAD": (Decimal("1.36000"), Decimal("0.0018"), Decimal("0.0001")),
}

_POLICY_CACHE: dict[Path, RiskPolicy] = {}


def policy() -> RiskPolicy:
    if POLICY_PATH not in _POLICY_CACHE:
        _POLICY_CACHE[POLICY_PATH] = load_policy(POLICY_PATH)
    return _POLICY_CACHE[POLICY_PATH]


@dataclass(frozen=True)
class ExistingPosition:
    symbol: str
    side: Side
    risk_pct: Decimal
    stop: bool = True


@dataclass(frozen=True)
class Knobs:
    # time / freshness (ages relative to NOW)
    now: datetime = NOW
    account_age: timedelta = timedelta(seconds=5)
    market_age: timedelta = timedelta(seconds=5)
    tick_age: timedelta = timedelta(seconds=1)
    calendar_age: timedelta = timedelta(minutes=1)
    news_age: timedelta = timedelta(minutes=1)
    recon_age: timedelta = timedelta(seconds=10)
    reconciliation_ok: bool = True
    # account
    equity: Decimal = Decimal(10_000)
    day_loss_pct: Decimal = Decimal(0)
    week_loss_pct: Decimal = Decimal(0)
    month_loss_pct: Decimal = Decimal(0)
    drawdown_pct: Decimal = Decimal(0)
    consecutive_losses: int = 0
    last_loss_ago: timedelta | None = None
    trades_today: int = 0
    positions: tuple[ExistingPosition, ...] = ()
    state_version: int = 7
    # market
    symbol: str = "EUR_USD"
    spread_multiple: Decimal = Decimal(1)
    regime: VolRegime = VolRegime.NORMAL
    circuit_breaker: bool = False
    # calendar / news
    events: tuple[EconomicEvent, ...] = ()
    calendar_sources: tuple[str, ...] = ("primary_api", "known_schedule")
    calendar_agreement: bool = True
    news_scores: tuple[tuple[Currency, Decimal], ...] = ()
    tripwires: frozenset[Currency] = frozenset()
    holidays: frozenset[date] = frozenset()
    # candidate
    side: Side = Side.LONG
    stop_pips: Decimal = Decimal(30)
    target_pips: Decimal = Decimal(70)
    candidate_age: timedelta = timedelta(minutes=1)
    candidate_ttl: timedelta = timedelta(minutes=15)
    expected_hold: timedelta = timedelta(hours=3)
    # system
    trading_state: TradingState = TradingState.ACTIVE
    mode: SystemMode = SystemMode.SHADOW
    active_sha_override: str | None = None
    policy_override: RiskPolicy | None = None


def market(symbol: str, k: Knobs) -> MarketSnapshot:
    bid, atr_h1, pip = MARKET_BASE[symbol]
    multiple = k.spread_multiple if symbol == k.symbol else Decimal(1)
    return MarketSnapshot(
        snapshot_id=SnapshotId(f"mkt-{symbol}"),
        symbol=symbol,
        as_of=k.now - k.market_age,
        last_tick_at=k.now - k.tick_age,
        bid=bid,
        ask=bid + pip * multiple,
        atr_h1=atr_h1,
        atr_d1=atr_h1 * Decimal("3.5"),
        session_median_spread=pip,
        regime=k.regime if symbol == k.symbol else VolRegime.NORMAL,
        circuit_breaker_active=k.circuit_breaker if symbol == k.symbol else False,
    )


def candidate(k: Knobs) -> SignalCandidate:
    bid, _, pip = MARKET_BASE[k.symbol]
    ask = bid + pip * k.spread_multiple
    entry = ask if k.side is Side.LONG else bid
    sign = k.side.sign
    created = k.now - k.candidate_age
    return SignalCandidate(
        candidate_id=CandidateId("cand-1"),
        symbol=k.symbol,
        side=k.side,
        detector="test_detector@0.0.0",
        entry=entry,
        stop_loss=entry - sign * k.stop_pips * pip,
        take_profit=entry + sign * k.target_pips * pip,
        created_at=created,
        expires_at=created + k.candidate_ttl,
        expected_hold=k.expected_hold,
    )


def _usd_per_quote(symbol: str) -> Decimal:
    quote = symbol.split("_")[1]
    if quote == "USD":
        return Decimal(1)
    bid, _, _ = MARKET_BASE[f"USD_{quote}"]
    return Decimal(1) / bid


def open_position(i: int, p: ExistingPosition, equity: Decimal) -> OpenPosition:
    bid, _, pip = MARKET_BASE[p.symbol]
    risk_amount = equity * p.risk_pct / 100
    stop_distance = 30 * pip
    units = (risk_amount / (stop_distance * _usd_per_quote(p.symbol))).to_integral_value()
    sign = p.side.sign
    return OpenPosition(
        position_id=PositionId(f"pos-{i}"),
        symbol=p.symbol,
        side=p.side,
        units=max(units, Decimal(1)),
        entry_price=bid,
        stop_loss=(bid - sign * stop_distance) if p.stop else None,
        risk_amount=Money(risk_amount, Currency.USD),
    )


def account(k: Knobs) -> AccountSnapshot:
    usd = Currency.USD
    eq = k.equity

    def above(pct: Decimal) -> Money:
        # A reference level that equity is ``pct`` percent below, in whole cents rounded
        # up, so the loss/drawdown seen by the kernel is never smaller than intended.
        return Money((eq / (1 - pct / 100)).quantize(CENT, rounding=ROUND_CEILING), usd)

    return AccountSnapshot(
        snapshot_id=SnapshotId("acct-1"),
        as_of=k.now - k.account_age,
        state_version=k.state_version,
        currency=usd,
        balance=Money(eq, usd),
        equity=Money(eq, usd),
        peak_equity=above(k.drawdown_pct),
        day_start_equity=above(k.day_loss_pct),
        week_start_equity=above(k.week_loss_pct),
        month_start_equity=above(k.month_loss_pct),
        margin_used=Money(Decimal(0), usd),
        open_positions=tuple(open_position(i, p, eq) for i, p in enumerate(k.positions)),
        consecutive_losses=k.consecutive_losses,
        last_loss_at=None if k.last_loss_ago is None else k.now - k.last_loss_ago,
        trades_today=k.trades_today,
        reconciled_at=k.now - k.recon_age,
        reconciliation_ok=k.reconciliation_ok,
    )


def calendar(k: Knobs) -> CalendarSnapshot:
    return CalendarSnapshot(
        snapshot_id=SnapshotId("cal-1"),
        as_of=k.now - k.calendar_age,
        events=k.events,
        sources=k.calendar_sources,
        source_agreement=k.calendar_agreement,
    )


def news(k: Knobs) -> NewsRiskSnapshot:
    scores = dict(k.news_scores)
    return NewsRiskSnapshot(
        snapshot_id=SnapshotId("news-1"),
        as_of=k.now - k.news_age,
        per_currency=tuple(
            CurrencyNewsRisk(
                currency=c,
                score=scores.get(c, Decimal(0)),
                tripwire_active=c in k.tripwires,
                reasons=("tripwire: test",) if c in k.tripwires else (),
            )
            for c in Currency
        ),
    )


def system_state(k: Knobs) -> SystemState:
    return SystemState(
        mode=k.mode,
        trading_state=k.trading_state,
        since=k.now - timedelta(hours=1),
        reason="test",
        actor=Actor(ActorKind.HUMAN, "operator"),
    )


def inputs(k: Knobs | None = None, /, **changes: Any) -> RiskInputs:
    """Build kernel inputs from ``k`` (default baseline) with ``changes`` applied."""
    k = replace(k or Knobs(), **changes)
    pol = k.policy_override or policy()
    return RiskInputs(
        decision_id=DecisionId("dec-1"),
        as_of=k.now,
        candidate=candidate(k),
        policy=pol,
        active_policy_sha256=k.active_sha_override or pol.sha256,
        system=system_state(k),
        account=account(k),
        markets={s: market(s, k) for s in MARKET_BASE},
        calendar=calendar(k),
        news=news(k),
        instruments=DEFAULT_INSTRUMENTS,
        holidays=k.holidays,
    )


def event(
    currency: Currency,
    at: datetime,
    *,
    tier: int = 1,
    central_bank: bool = False,
    ends_at: datetime | None = None,
) -> EconomicEvent:
    return EconomicEvent(
        event_id=f"evt-{currency}-{at.isoformat()}",
        currency=currency,
        title="test event",
        tier=EventTier(tier),
        scheduled_at=at,
        is_central_bank=central_bank,
        ends_at=ends_at,
    )
