"""Monotonic merge of an LLM news classification into the deterministic news risk.

The deterministic layer (tripwires, M7) produces the baseline. An LLM classification can
only *raise* a currency's score: ``new = max(baseline, severity_score)``. It can never
lower a score or clear a tripwire. When the output is missing or invalid, every currency
gets a fixed penalty instead: a classifier that failed is treated as a reason for more
caution, not less.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from sentinel.domain.snapshots import CurrencyNewsRisk, NewsRiskSnapshot
from sentinel.domain.types import SnapshotId
from sentinel.schemas.llm import NewsClassification, parse_news_classification

__all__ = ["LLM_FAILURE_PENALTY", "SEVERITY_SCORE", "apply_llm_output", "merge_classification"]

SEVERITY_SCORE: dict[int, Decimal] = {
    0: Decimal(0),
    1: Decimal(25),
    2: Decimal(60),
    3: Decimal(100),
}
LLM_FAILURE_PENALTY = Decimal(15)
_MAX = Decimal(100)


def _raise_to(risk: CurrencyNewsRisk, score: Decimal, reason: str) -> CurrencyNewsRisk:
    if score <= risk.score:
        return risk
    return replace(risk, score=min(_MAX, score), reasons=(*risk.reasons, reason))


def merge_classification(
    snapshot: NewsRiskSnapshot,
    classification: NewsClassification | None,
    *,
    snapshot_id: SnapshotId,
    as_of: datetime,
) -> NewsRiskSnapshot:
    merged: list[CurrencyNewsRisk] = []
    for risk in snapshot.per_currency:
        if classification is None:
            merged.append(
                _raise_to(risk, risk.score + LLM_FAILURE_PENALTY, "llm: invalid or missing output")
            )
        elif risk.currency in classification.currencies:
            score = SEVERITY_SCORE[classification.severity]
            merged.append(_raise_to(risk, score, f"llm: severity {classification.severity}"))
        else:
            merged.append(risk)
    return NewsRiskSnapshot(snapshot_id=snapshot_id, as_of=as_of, per_currency=tuple(merged))


def apply_llm_output(
    snapshot: NewsRiskSnapshot, raw: object, *, snapshot_id: SnapshotId, as_of: datetime
) -> NewsRiskSnapshot:
    return merge_classification(
        snapshot, parse_news_classification(raw), snapshot_id=snapshot_id, as_of=as_of
    )
