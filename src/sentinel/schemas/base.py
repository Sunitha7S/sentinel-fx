"""Shared schema primitives: float-free decimals, UTC datetimes, and the message envelope."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    PlainSerializer,
)

from sentinel.domain.types import require_utc

__all__ = ["Dec", "Envelope", "Schema", "UtcDatetime"]


def _no_float(value: Any) -> Any:
    if isinstance(value, bool | float):
        raise ValueError("binary floats are not accepted; send decimals as strings")
    return value


def _utc(value: datetime) -> datetime:
    return require_utc(value, field="datetime")


Dec = Annotated[
    Decimal,
    BeforeValidator(_no_float),
    PlainSerializer(lambda d: format(d, "f"), return_type=str, when_used="json"),
]
"""A decimal that refuses floats on input and serialises to a string in JSON."""

UtcDatetime = Annotated[AwareDatetime, AfterValidator(_utc)]


class Schema(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Envelope[T](Schema):
    """Every inter-service message travels in an envelope with a schema version."""

    schema_version: Literal[1] = 1
    message_type: str
    message_id: str
    produced_at: UtcDatetime
    producer: str
    payload: T
