"""RiskPolicy.to_mapping is the exact inverse of from_mapping (needed to persist versions)."""

from __future__ import annotations

import json
from datetime import time, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.domain.types import Percent
from sentinel.risk.policy import PolicyViolation, RiskPolicy

from support.builders import policy


def test_shipped_policy_round_trips_through_json() -> None:
    p = policy()
    mapping = json.loads(json.dumps(p.to_mapping()))
    again = RiskPolicy.from_mapping(mapping)
    assert again == p
    assert again.sha256 == p.sha256


@given(
    risk=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1.0"), places=2),
    rr=st.decimals(min_value=Decimal("1.0"), max_value=Decimal(5), places=2),
    ttl_min=st.integers(1, 240),
    cooldown_h=st.integers(1, 72),
    skew_s=st.integers(0, 10),
)
def test_valid_variants_round_trip(
    risk: Decimal, rr: Decimal, ttl_min: int, cooldown_h: int, skew_s: int
) -> None:
    p = policy().with_changes(
        risk_per_trade=Percent(risk),
        min_rr_net=rr,
        signal_ttl=timedelta(minutes=ttl_min),
        cooldown=timedelta(hours=cooldown_h),
        max_clock_skew=timedelta(seconds=skew_s),
    )
    assert RiskPolicy.from_mapping(json.loads(json.dumps(p.to_mapping()))) == p


@pytest.mark.parametrize(
    "changes",
    [
        {"cooldown": timedelta(hours=1, minutes=30)},
        {"signal_ttl": timedelta(minutes=1, seconds=5)},
        {"rollover_start": time(16, 45, 30)},
    ],
)
def test_values_without_an_exact_file_representation_are_rejected(
    changes: dict[str, object],
) -> None:
    with pytest.raises(PolicyViolation):
        policy().with_changes(**changes).to_mapping()
