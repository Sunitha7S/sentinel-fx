"""PostgreSQL stores for decisions, approvals and shadow trades (all append-only).

Each row keeps the full boundary-schema payload, and loading converts it back through the
domain constructors, so a stored APPROVED decision that violates a ``RiskDecision``
invariant cannot be loaded as one.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from sqlalchemy import Engine, text

from sentinel.domain.decision import Approval, RiskDecision, ShadowTradeRecord
from sentinel.schemas.messages import ApprovalMsg, RiskDecisionMsg, ShadowTradeMsg

__all__ = ["PostgresDecisionStore", "PostgresShadowStore"]

_INSERT_DECISION = text(
    """
    INSERT INTO sentinel.decisions
      (decision_id, candidate_id, as_of, outcome, units, risk_amount, risk_currency,
       effective_risk_pct, policy_id, policy_sha256, state_version, snapshot_digest,
       blocking_rules, payload)
    VALUES (:decision_id, :candidate_id, :as_of, :outcome, :units, :risk_amount, :risk_currency,
            :effective_risk_pct, :policy_id, :policy_sha256, :state_version, :snapshot_digest,
            :blocking_rules, CAST(:payload AS jsonb))
    """
)
_INSERT_APPROVAL = text(
    """
    INSERT INTO sentinel.approvals
      (approval_id, decision_id, candidate_id, authority, state_version, snapshot_digest,
       payload_sha256, issued_at, expires_at)
    VALUES (:approval_id, :decision_id, :candidate_id, :authority, :state_version,
            :snapshot_digest, :payload_sha256, :issued_at, :expires_at)
    """
)
_INSERT_SHADOW = text(
    """
    INSERT INTO sentinel.shadow_trades
      (decision_id, candidate_id, symbol, side, detector, decision_outcome, blocking_rules,
       policy_sha256, snapshot_digest, payload)
    VALUES (:decision_id, :candidate_id, :symbol, :side, :detector, :decision_outcome,
            :blocking_rules, :policy_sha256, :snapshot_digest, CAST(:payload AS jsonb))
    """
)


class PostgresDecisionStore:
    """Writes need ``svc_risk``; any reader role can load."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, decision: RiskDecision) -> None:
        msg = RiskDecisionMsg.from_domain(decision)
        with self._engine.begin() as conn:
            conn.execute(
                _INSERT_DECISION,
                {
                    "decision_id": decision.decision_id,
                    "candidate_id": decision.candidate_id,
                    "as_of": decision.as_of,
                    "outcome": decision.outcome.value,
                    "units": decision.units,
                    "risk_amount": decision.risk_amount.amount if decision.risk_amount else None,
                    "risk_currency": decision.risk_amount.currency.value
                    if decision.risk_amount
                    else None,
                    "effective_risk_pct": decision.effective_risk.value
                    if decision.effective_risk
                    else None,
                    "policy_id": decision.policy_id,
                    "policy_sha256": decision.policy_sha256,
                    "state_version": decision.state_version,
                    "snapshot_digest": decision.snapshot_digest,
                    "blocking_rules": list(decision.blocking_rules),
                    "payload": msg.model_dump_json(),
                },
            )

    def record_approvals(self, approvals: Sequence[Approval]) -> None:
        with self._engine.begin() as conn:
            for a in approvals:
                conn.execute(
                    _INSERT_APPROVAL,
                    {
                        "approval_id": a.approval_id,
                        "decision_id": a.decision_id,
                        "candidate_id": a.candidate_id,
                        "authority": a.authority.value,
                        "state_version": a.state_version,
                        "snapshot_digest": a.snapshot_digest,
                        "payload_sha256": a.payload_sha256,
                        "issued_at": a.issued_at,
                        "expires_at": a.expires_at,
                    },
                )

    def get(self, decision_id: str) -> RiskDecision | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT payload FROM sentinel.decisions WHERE decision_id = :d"),
                {"d": decision_id},
            ).one_or_none()
        return None if row is None else RiskDecisionMsg.model_validate(row.payload).to_domain()

    def approvals_for(self, decision_id: str) -> tuple[Approval, ...]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT approval_id, decision_id, candidate_id, authority, state_version, "
                        "snapshot_digest, payload_sha256, issued_at, expires_at "
                        "FROM sentinel.approvals WHERE decision_id = :d ORDER BY authority"
                    ),
                    {"d": decision_id},
                )
                .mappings()
                .all()
            )
        return tuple(ApprovalMsg.model_validate(dict(r)).to_domain() for r in rows)


class PostgresShadowStore:
    """``ShadowSink`` implementation. The decision must already be recorded (foreign key)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, record: ShadowTradeRecord) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                _INSERT_SHADOW,
                {
                    "decision_id": record.decision_id,
                    "candidate_id": record.candidate_id,
                    "symbol": record.symbol,
                    "side": record.side.value,
                    "detector": record.detector,
                    "decision_outcome": record.decision_outcome.value,
                    "blocking_rules": list(record.blocking_rules),
                    "policy_sha256": record.policy_sha256,
                    "snapshot_digest": record.snapshot_digest,
                    "payload": ShadowTradeMsg.from_domain(record).model_dump_json(),
                },
            )

    def __iter__(self) -> Iterator[ShadowTradeRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT payload FROM sentinel.shadow_trades ORDER BY recorded_at, decision_id")
            ).all()
        return iter([ShadowTradeMsg.model_validate(r.payload).to_domain() for r in rows])
