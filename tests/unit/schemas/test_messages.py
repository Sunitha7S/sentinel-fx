"""Boundary schemas round-trip the domain exactly and reject floats, extras and bad times."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from sentinel.domain.decision import shadow_record_from
from sentinel.domain.types import Currency, Side
from sentinel.risk.approvals import REQUIRED_AUTHORITIES, issue_approvals
from sentinel.risk.kernel import RiskInputs, evaluate
from sentinel.schemas.base import Envelope
from sentinel.schemas.messages import (
    AccountSnapshotMsg,
    ApprovalMsg,
    CalendarSnapshotMsg,
    MarketSnapshotMsg,
    NewsRiskSnapshotMsg,
    RiskDecisionMsg,
    ShadowTradeMsg,
    SignalCandidateMsg,
)

from support.builders import NOW, ExistingPosition, event, inputs


def _full_inputs() -> RiskInputs:
    return inputs(
        positions=(ExistingPosition("USD_JPY", Side.LONG, Decimal("0.2")),),
        events=(
            event(
                Currency.JPY,
                NOW + timedelta(hours=9),
                central_bank=True,
                ends_at=NOW + timedelta(hours=10),
            ),
        ),
        last_loss_ago=timedelta(hours=1),
        consecutive_losses=1,
    )


def test_snapshot_messages_round_trip_through_json() -> None:
    i = _full_inputs()
    assert i.account is not None
    assert i.calendar is not None
    assert i.news is not None
    assert i.candidate is not None
    pairs: list[tuple[Any, Any]] = [
        (AccountSnapshotMsg, i.account),
        (CalendarSnapshotMsg, i.calendar),
        (NewsRiskSnapshotMsg, i.news),
        (MarketSnapshotMsg, i.markets["EUR_USD"]),
        (SignalCandidateMsg, i.candidate),
    ]
    for schema, obj in pairs:
        msg = schema.from_domain(obj)
        again = schema.model_validate_json(msg.model_dump_json())
        assert again.to_domain() == obj, schema.__name__


def test_decision_approval_and_shadow_messages_round_trip() -> None:
    i = inputs()
    d = evaluate(i)
    assert i.candidate is not None
    assert (
        RiskDecisionMsg.model_validate_json(
            RiskDecisionMsg.from_domain(d).model_dump_json()
        ).to_domain()
        == d
    )
    blocked = evaluate(inputs(day_loss_pct=Decimal(2)))
    assert RiskDecisionMsg.from_domain(blocked).to_domain() == blocked
    for a in issue_approvals(
        d, i.candidate, authorities=REQUIRED_AUTHORITIES, issued_at=NOW, ttl=timedelta(minutes=2)
    ):
        assert (
            ApprovalMsg.model_validate_json(
                ApprovalMsg.from_domain(a).model_dump_json()
            ).to_domain()
            == a
        )
    shadow = shadow_record_from(i.candidate, blocked)
    assert (
        ShadowTradeMsg.model_validate_json(
            ShadowTradeMsg.from_domain(shadow).model_dump_json()
        ).to_domain()
        == shadow
    )


def test_decimals_serialise_as_strings_and_floats_are_rejected() -> None:
    c = inputs().candidate
    assert c is not None
    doc = SignalCandidateMsg.from_domain(c).model_dump(mode="json")
    assert doc["entry"] == "1.08510"
    doc["entry"] = 1.0851
    with pytest.raises(ValidationError, match="float"):
        SignalCandidateMsg.model_validate(doc)


def test_extra_fields_and_naive_times_are_rejected() -> None:
    c = inputs().candidate
    assert c is not None
    doc = SignalCandidateMsg.from_domain(c).model_dump(mode="json")
    with pytest.raises(ValidationError):
        SignalCandidateMsg.model_validate({**doc, "override": True})
    with pytest.raises(ValidationError):
        SignalCandidateMsg.model_validate({**doc, "created_at": "2026-10-06T12:00:00"})
    with pytest.raises(ValidationError):
        SignalCandidateMsg.model_validate({**doc, "created_at": "2026-10-06T12:00:00+02:00"})


def test_domain_invariants_still_apply_after_schema_validation() -> None:
    c = inputs().candidate
    assert c is not None
    doc = SignalCandidateMsg.from_domain(c).model_dump(mode="json")
    msg = SignalCandidateMsg.model_validate({**doc, "entry": "0"})
    with pytest.raises(ValueError, match="entry"):
        msg.to_domain()


def test_envelope_carries_schema_version() -> None:
    c = inputs().candidate
    assert c is not None
    env = Envelope[SignalCandidateMsg](
        message_type="signal.candidate",
        message_id="m-1",
        produced_at=NOW,
        producer="test",
        payload=SignalCandidateMsg.from_domain(c),
    )
    back = Envelope[SignalCandidateMsg].model_validate_json(env.model_dump_json())
    assert back.schema_version == 1
    assert back.payload.to_domain() == c
    with pytest.raises(ValidationError):
        Envelope[SignalCandidateMsg].model_validate({**env.model_dump(), "schema_version": 2})
