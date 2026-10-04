"""Rule declaration types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sentinel.domain.decision import Severity, Stage
from sentinel.risk.context import Context

__all__ = ["Check", "Rule", "fresh"]


@dataclass(frozen=True, slots=True)
class Check:
    passed: bool
    message: str
    observed: str | None = None
    threshold: str | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    stage: Stage
    description: str
    check: Callable[[Context], Check]
    severity: Severity = Severity.HARD


def fresh(as_of: datetime, now: datetime, max_age: timedelta, skew: timedelta) -> bool:
    """Age within [-skew, max_age]: neither stale nor from the future beyond clock skew."""
    age = now - as_of
    return -skew <= age <= max_age


def age_text(as_of: datetime, now: datetime) -> str:
    return f"{(now - as_of).total_seconds():.0f}s"
