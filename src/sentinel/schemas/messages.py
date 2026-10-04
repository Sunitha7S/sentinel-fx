"""Boundary schemas for the domain objects services exchange.

Each schema converts to and from its domain counterpart. Conversion *into* the domain
re-runs the domain's own validation, so a message that passes schema checks but violates
a domain invariant is still rejected.
"""

from __future__ import annotations

from datetime import timedelta

from sentinel.domain.decision import (
    Approval,
    Authority,
    Outcome,
    RiskDecision,
    RuleResult,
    Severity,
    ShadowTradeRecord,
    SignalCandidate,
    Stage,
)
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
from sentinel.domain.types import (
    ApprovalId,
    CandidateId,
    Currency,
    DecisionId,
    Money,
    Percent,
    PositionId,
    Side,
    SnapshotId,
)
from sentinel.schemas.base import Dec, Schema, UtcDatetime

__all__ = [
    "AccountSnapshotMsg",
    "ApprovalMsg",
    "CalendarSnapshotMsg",
    "MarketSnapshotMsg",
    "MoneyMsg",
    "NewsRiskSnapshotMsg",
    "RiskDecisionMsg",
    "ShadowTradeMsg",
    "SignalCandidateMsg",
]


class MoneyMsg(Schema):
    amount: Dec
    currency: Currency

    @classmethod
    def from_domain(cls, m: Money) -> MoneyMsg:
        return cls(amount=m.amount, currency=m.currency)

    def to_domain(self) -> Money:
        return Money(self.amount, self.currency)


class SignalCandidateMsg(Schema):
    candidate_id: str
    symbol: str
    side: Side
    detector: str
    entry: Dec
    stop_loss: Dec
    take_profit: Dec
    created_at: UtcDatetime
    expires_at: UtcDatetime
    expected_hold_seconds: int

    @classmethod
    def from_domain(cls, c: SignalCandidate) -> SignalCandidateMsg:
        return cls(
            candidate_id=c.candidate_id,
            symbol=c.symbol,
            side=c.side,
            detector=c.detector,
            entry=c.entry,
            stop_loss=c.stop_loss,
            take_profit=c.take_profit,
            created_at=c.created_at,
            expires_at=c.expires_at,
            expected_hold_seconds=int(c.expected_hold.total_seconds()),
        )

    def to_domain(self) -> SignalCandidate:
        return SignalCandidate(
            candidate_id=CandidateId(self.candidate_id),
            symbol=self.symbol,
            side=self.side,
            detector=self.detector,
            entry=self.entry,
            stop_loss=self.stop_loss,
            take_profit=self.take_profit,
            created_at=self.created_at,
            expires_at=self.expires_at,
            expected_hold=timedelta(seconds=self.expected_hold_seconds),
        )


class MarketSnapshotMsg(Schema):
    snapshot_id: str
    symbol: str
    as_of: UtcDatetime
    last_tick_at: UtcDatetime
    bid: Dec
    ask: Dec
    atr_h1: Dec
    atr_d1: Dec
    session_median_spread: Dec
    regime: VolRegime
    circuit_breaker_active: bool

    @classmethod
    def from_domain(cls, m: MarketSnapshot) -> MarketSnapshotMsg:
        return cls(**{f: getattr(m, f) for f in cls.model_fields})

    def to_domain(self) -> MarketSnapshot:
        data = self.model_dump()
        data["snapshot_id"] = SnapshotId(self.snapshot_id)
        return MarketSnapshot(**data)


class OpenPositionMsg(Schema):
    position_id: str
    symbol: str
    side: Side
    units: Dec
    entry_price: Dec
    stop_loss: Dec | None
    risk_amount: MoneyMsg

    @classmethod
    def from_domain(cls, p: OpenPosition) -> OpenPositionMsg:
        return cls(
            position_id=p.position_id,
            symbol=p.symbol,
            side=p.side,
            units=p.units,
            entry_price=p.entry_price,
            stop_loss=p.stop_loss,
            risk_amount=MoneyMsg.from_domain(p.risk_amount),
        )

    def to_domain(self) -> OpenPosition:
        return OpenPosition(
            position_id=PositionId(self.position_id),
            symbol=self.symbol,
            side=self.side,
            units=self.units,
            entry_price=self.entry_price,
            stop_loss=self.stop_loss,
            risk_amount=self.risk_amount.to_domain(),
        )


_MONEY_FIELDS = (
    "balance",
    "equity",
    "peak_equity",
    "day_start_equity",
    "week_start_equity",
    "month_start_equity",
    "margin_used",
)


class AccountSnapshotMsg(Schema):
    snapshot_id: str
    as_of: UtcDatetime
    state_version: int
    currency: Currency
    balance: MoneyMsg
    equity: MoneyMsg
    peak_equity: MoneyMsg
    day_start_equity: MoneyMsg
    week_start_equity: MoneyMsg
    month_start_equity: MoneyMsg
    margin_used: MoneyMsg
    open_positions: tuple[OpenPositionMsg, ...]
    consecutive_losses: int
    last_loss_at: UtcDatetime | None
    trades_today: int
    reconciled_at: UtcDatetime | None
    reconciliation_ok: bool

    @classmethod
    def from_domain(cls, a: AccountSnapshot) -> AccountSnapshotMsg:
        return cls(
            snapshot_id=a.snapshot_id,
            as_of=a.as_of,
            state_version=a.state_version,
            currency=a.currency,
            open_positions=tuple(OpenPositionMsg.from_domain(p) for p in a.open_positions),
            consecutive_losses=a.consecutive_losses,
            last_loss_at=a.last_loss_at,
            trades_today=a.trades_today,
            reconciled_at=a.reconciled_at,
            reconciliation_ok=a.reconciliation_ok,
            **{f: MoneyMsg.from_domain(getattr(a, f)) for f in _MONEY_FIELDS},
        )

    def to_domain(self) -> AccountSnapshot:
        return AccountSnapshot(
            snapshot_id=SnapshotId(self.snapshot_id),
            as_of=self.as_of,
            state_version=self.state_version,
            currency=self.currency,
            open_positions=tuple(p.to_domain() for p in self.open_positions),
            consecutive_losses=self.consecutive_losses,
            last_loss_at=self.last_loss_at,
            trades_today=self.trades_today,
            reconciled_at=self.reconciled_at,
            reconciliation_ok=self.reconciliation_ok,
            **{f: getattr(self, f).to_domain() for f in _MONEY_FIELDS},
        )


class EconomicEventMsg(Schema):
    event_id: str
    currency: Currency
    title: str
    tier: EventTier
    scheduled_at: UtcDatetime
    is_central_bank: bool = False
    ends_at: UtcDatetime | None = None

    def to_domain(self) -> EconomicEvent:
        return EconomicEvent(**self.model_dump())


class CalendarSnapshotMsg(Schema):
    snapshot_id: str
    as_of: UtcDatetime
    events: tuple[EconomicEventMsg, ...]
    sources: tuple[str, ...]
    source_agreement: bool
    discrepancies: tuple[str, ...] = ()

    @classmethod
    def from_domain(cls, c: CalendarSnapshot) -> CalendarSnapshotMsg:
        return cls(
            snapshot_id=c.snapshot_id,
            as_of=c.as_of,
            events=tuple(
                EconomicEventMsg(**{f: getattr(e, f) for f in EconomicEventMsg.model_fields})
                for e in c.events
            ),
            sources=c.sources,
            source_agreement=c.source_agreement,
            discrepancies=c.discrepancies,
        )

    def to_domain(self) -> CalendarSnapshot:
        return CalendarSnapshot(
            snapshot_id=SnapshotId(self.snapshot_id),
            as_of=self.as_of,
            events=tuple(e.to_domain() for e in self.events),
            sources=self.sources,
            source_agreement=self.source_agreement,
            discrepancies=self.discrepancies,
        )


class CurrencyNewsRiskMsg(Schema):
    currency: Currency
    score: Dec
    tripwire_active: bool
    reasons: tuple[str, ...] = ()


class NewsRiskSnapshotMsg(Schema):
    snapshot_id: str
    as_of: UtcDatetime
    per_currency: tuple[CurrencyNewsRiskMsg, ...]

    @classmethod
    def from_domain(cls, n: NewsRiskSnapshot) -> NewsRiskSnapshotMsg:
        return cls(
            snapshot_id=n.snapshot_id,
            as_of=n.as_of,
            per_currency=tuple(
                CurrencyNewsRiskMsg(
                    currency=r.currency,
                    score=r.score,
                    tripwire_active=r.tripwire_active,
                    reasons=r.reasons,
                )
                for r in n.per_currency
            ),
        )

    def to_domain(self) -> NewsRiskSnapshot:
        return NewsRiskSnapshot(
            snapshot_id=SnapshotId(self.snapshot_id),
            as_of=self.as_of,
            per_currency=tuple(CurrencyNewsRisk(**r.model_dump()) for r in self.per_currency),
        )


class RuleResultMsg(Schema):
    rule_id: str
    stage: Stage
    passed: bool
    message: str
    observed: str | None = None
    threshold: str | None = None
    severity: Severity = Severity.HARD


class RiskDecisionMsg(Schema):
    decision_id: str
    candidate_id: str | None
    as_of: UtcDatetime
    outcome: Outcome
    units: Dec
    risk_amount: MoneyMsg | None
    effective_risk_pct: Dec | None
    rule_results: tuple[RuleResultMsg, ...]
    policy_id: str | None
    policy_sha256: str | None
    state_version: int | None
    snapshot_digest: str | None

    @classmethod
    def from_domain(cls, d: RiskDecision) -> RiskDecisionMsg:
        return cls(
            decision_id=d.decision_id,
            candidate_id=d.candidate_id,
            as_of=d.as_of,
            outcome=d.outcome,
            units=d.units,
            risk_amount=MoneyMsg.from_domain(d.risk_amount) if d.risk_amount else None,
            effective_risk_pct=d.effective_risk.value if d.effective_risk else None,
            rule_results=tuple(
                RuleResultMsg(**{f: getattr(r, f) for f in RuleResultMsg.model_fields})
                for r in d.rule_results
            ),
            policy_id=d.policy_id,
            policy_sha256=d.policy_sha256,
            state_version=d.state_version,
            snapshot_digest=d.snapshot_digest,
        )

    def to_domain(self) -> RiskDecision:
        return RiskDecision(
            decision_id=DecisionId(self.decision_id),
            candidate_id=CandidateId(self.candidate_id) if self.candidate_id else None,
            as_of=self.as_of,
            outcome=self.outcome,
            units=self.units,
            risk_amount=self.risk_amount.to_domain() if self.risk_amount else None,
            effective_risk=Percent(self.effective_risk_pct)
            if self.effective_risk_pct is not None
            else None,
            rule_results=tuple(RuleResult(**r.model_dump()) for r in self.rule_results),
            policy_id=self.policy_id,
            policy_sha256=self.policy_sha256,
            state_version=self.state_version,
            snapshot_digest=self.snapshot_digest,
        )


class ApprovalMsg(Schema):
    approval_id: str
    decision_id: str
    candidate_id: str
    authority: Authority
    state_version: int
    snapshot_digest: str
    payload_sha256: str
    issued_at: UtcDatetime
    expires_at: UtcDatetime

    @classmethod
    def from_domain(cls, a: Approval) -> ApprovalMsg:
        return cls(**{f: getattr(a, f) for f in cls.model_fields})

    def to_domain(self) -> Approval:
        data = self.model_dump()
        data.update(
            approval_id=ApprovalId(self.approval_id),
            decision_id=DecisionId(self.decision_id),
            candidate_id=CandidateId(self.candidate_id),
        )
        return Approval(**data)


class ShadowTradeMsg(Schema):
    candidate_id: str
    decision_id: str
    symbol: str
    side: Side
    detector: str
    entry: Dec
    stop_loss: Dec
    take_profit: Dec
    created_at: UtcDatetime
    expires_at: UtcDatetime
    decision_outcome: Outcome
    blocking_rules: tuple[str, ...]
    policy_sha256: str | None
    snapshot_digest: str | None
    resolved_at: UtcDatetime | None = None
    filled: bool | None = None
    r_multiple: Dec | None = None
    exit_reason: str | None = None

    @classmethod
    def from_domain(cls, r: ShadowTradeRecord) -> ShadowTradeMsg:
        return cls(**{f: getattr(r, f) for f in cls.model_fields})

    def to_domain(self) -> ShadowTradeRecord:
        data = self.model_dump()
        data.update(
            candidate_id=CandidateId(self.candidate_id), decision_id=DecisionId(self.decision_id)
        )
        return ShadowTradeRecord(**data)
