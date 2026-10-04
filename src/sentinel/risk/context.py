"""Kernel inputs and the lazily computed evaluation context shared by all rules.

Every derived quantity (size, drawdown, conversion rates, required markets) is computed in
one place, once, so rules cannot disagree about it. Accessing anything that is missing
raises ``MissingInput``; the kernel turns that into a failed rule, never an exception.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import cached_property

from sentinel.domain.decision import SignalCandidate
from sentinel.domain.instrument import Instrument
from sentinel.domain.snapshots import (
    AccountSnapshot,
    CalendarSnapshot,
    MarketSnapshot,
    NewsRiskSnapshot,
    OpenPosition,
    SnapshotRef,
    snapshot_digest,
)
from sentinel.domain.system import SystemMode, SystemState
from sentinel.domain.types import Currency, DecisionId, Percent, require_utc
from sentinel.fxmath.conversion import FxRates
from sentinel.fxmath.sizing import CODE_MAX_RISK_FRACTION, PositionSize, size_position
from sentinel.risk.policy import RiskPolicy

__all__ = ["Context", "MissingInput", "RiskInputs"]


@dataclass(frozen=True, slots=True)
class RiskInputs:
    """Everything the kernel may look at. Nothing else is reachable from a rule."""

    decision_id: DecisionId
    as_of: datetime
    candidate: SignalCandidate | None
    policy: RiskPolicy | None
    active_policy_sha256: str | None
    system: SystemState | None
    account: AccountSnapshot | None
    markets: Mapping[str, MarketSnapshot]
    calendar: CalendarSnapshot | None
    news: NewsRiskSnapshot | None
    instruments: Mapping[str, Instrument]
    holidays: frozenset[date] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", require_utc(self.as_of, field="RiskInputs.as_of"))


class MissingInput(LookupError):
    """A rule needed an input that is absent."""


def _need[T](value: T | None, name: str) -> T:
    if value is None:
        raise MissingInput(name)
    return value


class Context:
    def __init__(self, inputs: RiskInputs) -> None:
        self.inputs = inputs
        self.as_of = inputs.as_of

    # ------------------------------------------------------------------ raw inputs

    @cached_property
    def candidate(self) -> SignalCandidate:
        return _need(self.inputs.candidate, "candidate")

    @cached_property
    def policy(self) -> RiskPolicy:
        return _need(self.inputs.policy, "policy")

    @cached_property
    def system(self) -> SystemState:
        return _need(self.inputs.system, "system state")

    @cached_property
    def account(self) -> AccountSnapshot:
        return _need(self.inputs.account, "account snapshot")

    @cached_property
    def calendar(self) -> CalendarSnapshot:
        return _need(self.inputs.calendar, "calendar snapshot")

    @cached_property
    def news(self) -> NewsRiskSnapshot:
        return _need(self.inputs.news, "news snapshot")

    def instrument_for(self, symbol: str) -> Instrument:
        return _need(self.inputs.instruments.get(symbol), f"instrument {symbol}")

    def market_for(self, symbol: str) -> MarketSnapshot:
        return _need(self.inputs.markets.get(symbol), f"market {symbol}")

    @cached_property
    def instrument(self) -> Instrument:
        return self.instrument_for(self.candidate.symbol)

    @cached_property
    def market(self) -> MarketSnapshot:
        return self.market_for(self.candidate.symbol)

    @property
    def positions(self) -> tuple[OpenPosition, ...]:
        return self.account.open_positions

    @property
    def account_ccy(self) -> Currency:
        return self.account.currency

    # ------------------------------------------------------------------ markets in use

    def _conversion_symbol(self, ccy: Currency) -> str | None:
        acct = self.account_ccy
        if ccy is acct:
            return None
        for symbol, inst in self.inputs.instruments.items():
            if {inst.base, inst.quote} == {ccy, acct}:
                return symbol
        raise MissingInput(f"conversion instrument {ccy}/{acct}")

    @cached_property
    def required_symbols(self) -> tuple[str, ...]:
        """Markets the decision depends on: the candidate, open positions, conversions."""
        traded = [self.candidate.symbol, *(p.symbol for p in self.positions)]
        needed = set(traded)
        for symbol in traded:
            for ccy in self.instrument_for(symbol).currencies:
                conversion = self._conversion_symbol(ccy)
                if conversion is not None:
                    needed.add(conversion)
        return tuple(sorted(needed))

    @cached_property
    def fx(self) -> FxRates:
        quotes: dict[tuple[str, str], Decimal] = {}
        for symbol in self.required_symbols:
            inst = self.instrument_for(symbol)
            quotes[(inst.base, inst.quote)] = self.market_for(symbol).mid
        return FxRates(quotes)

    # ------------------------------------------------------------------ account metrics

    @cached_property
    def equity(self) -> Decimal:
        return self.account.equity.amount

    @cached_property
    def drawdown(self) -> Percent:
        peak = self.account.peak_equity.amount
        if peak <= 0:
            raise MissingInput("positive peak equity")
        return Percent(max(Decimal(0), (peak - self.equity) / peak * 100))

    def pct_of_equity(self, amount: Decimal) -> Decimal:
        if self.equity <= 0:
            raise MissingInput("positive equity")
        return amount / self.equity * 100

    # ------------------------------------------------------------------ sizing

    @cached_property
    def effective_risk(self) -> Percent:
        cap = Percent.from_fraction(CODE_MAX_RISK_FRACTION)
        base = min(self.policy.risk_per_trade, cap)
        multiplier = self.policy.throttle_multiplier(self.drawdown)
        if self.system.mode is SystemMode.LIVE_MICRO:
            multiplier *= self.policy.live_micro_risk_multiplier
        return Percent(base.value * multiplier)

    @cached_property
    def cost_buffer(self) -> Decimal:
        slippage = self.policy.expected_slippage_pips * self.instrument.pip_size
        return self.market.spread + slippage

    @cached_property
    def sizing(self) -> PositionSize:
        inst = self.instrument
        return size_position(
            equity=self.equity,
            risk_fraction=self.effective_risk.fraction,
            entry=self.candidate.entry,
            stop=self.candidate.stop_loss,
            cost_buffer=self.cost_buffer,
            quote_to_account=self.fx.rate(inst.quote, self.account_ccy),
            min_units=inst.min_units,
            unit_step=inst.unit_step,
            max_units=inst.max_units,
        )

    @cached_property
    def prospective_risk(self) -> Decimal:
        """Risk the new trade adds, in account currency. When sizing failed, the full risk
        budget is assumed, so loss and exposure checks stay conservative."""
        if self.sizing.ok:
            return self.sizing.risk_amount
        return self.equity * self.effective_risk.fraction

    @cached_property
    def new_units(self) -> Decimal:
        return self.sizing.units if self.sizing.ok else Decimal(0)

    def hold_end(self) -> datetime:
        return self.as_of + min(self.candidate.expected_hold, self.policy.max_hold_horizon)

    def horizon_end(self) -> datetime:
        return self.as_of + self.policy.max_hold_horizon

    # ------------------------------------------------------------------ binding

    @cached_property
    def snapshot_refs(self) -> tuple[SnapshotRef, ...]:
        refs = [self.account.ref(), self.calendar.ref(), self.news.ref()]
        refs.extend(self.market_for(s).ref() for s in self.required_symbols)
        return tuple(refs)

    @cached_property
    def snapshot_digest(self) -> str:
        return snapshot_digest(self.snapshot_refs)
