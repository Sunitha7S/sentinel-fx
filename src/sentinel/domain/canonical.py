"""Canonical, deterministic serialisation used for hashing and audit records.

The same value always produces the same bytes: keys are sorted, decimals are written in
normalised fixed-point form, datetimes in UTC ISO-8601, enums by value. Hashes produced
here identify snapshots, approvals, policies and audit records.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

__all__ = ["JsonValue", "canonical", "canonical_json", "sha256_hex"]

type JsonValue = bool | int | str | list[JsonValue] | dict[str, JsonValue] | None


def _decimal(value: Decimal) -> str:
    normalised = value.normalize()
    if normalised == 0:
        return "0"
    return format(normalised, "f")


def canonical(value: Any) -> JsonValue:  # noqa: PLR0911, PLR0912 - one branch per type
    """Convert ``value`` into a JSON-compatible structure with a single representation."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, Enum):
        return canonical(value.value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise TypeError("floats are not allowed in canonical data")
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError("naive datetime cannot be canonicalised")
        return value.isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        seconds = Decimal(value.days * 86400 + value.seconds) + Decimal(value.microseconds).scaleb(
            -6
        )
        return f"PT{_decimal(seconds)}S"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: canonical(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(canonical(k)): canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [canonical(v) for v in value]
    if isinstance(value, frozenset | set):
        items = [canonical(v) for v in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    raise TypeError(f"cannot canonicalise {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()
