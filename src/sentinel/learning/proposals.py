"""Learning outputs are research artefacts: proposals with evidence, never changes.

The production policy is versioned and frozen. A proposal can only become a change by a
human filing a change request through governance, after the candidate policy has passed
backtesting, out-of-sample evaluation and paper trading (ADR 0005). This module has no
access to the governance or persistence layers; it can only read the active policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sentinel.domain.types import require_utc
from sentinel.risk.policy import PolicyReader

__all__ = ["Proposal", "ProposalKind", "draft_proposal"]


class ProposalKind(StrEnum):
    STRATEGY_PARAM = "STRATEGY_PARAM"
    FILTER = "FILTER"
    DETECTOR_DISABLE = "DETECTOR_DISABLE"
    RISK_RULE = "RISK_RULE"


@dataclass(frozen=True, slots=True)
class Proposal:
    kind: ProposalKind
    target: str
    summary: str
    evidence: dict[str, str]
    based_on_policy_sha256: str
    created_at: datetime


def draft_proposal(
    reader: PolicyReader,
    *,
    kind: ProposalKind,
    target: str,
    summary: str,
    evidence: dict[str, str],
    created_at: datetime,
) -> Proposal:
    """Record a candidate change against the currently active policy, for human review."""
    return Proposal(
        kind=kind,
        target=target,
        summary=summary,
        evidence=dict(evidence),
        based_on_policy_sha256=reader.active_sha256(),
        created_at=require_utc(created_at, field="created_at"),
    )
