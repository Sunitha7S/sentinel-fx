"""Schema for LLM news classifications, and a parser that never raises.

LLM output is untrusted text. It is accepted only if it validates against this schema
exactly: no extra fields, no coercion of strings to numbers, no floats for severity.
Anything else parses to ``None`` and is treated by the caller as a failed classification.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, ValidationError

from sentinel.domain.types import Currency
from sentinel.schemas.base import Schema

__all__ = ["MAX_RAW_LENGTH", "NewsClassification", "parse_news_classification"]

MAX_RAW_LENGTH = 20_000


class NewsClassification(Schema):
    category: Literal[
        "monetary_policy",
        "macro_data",
        "geopolitical",
        "fiscal",
        "market_structure",
        "corporate",
        "other",
    ]
    currencies: tuple[Currency, ...] = Field(min_length=1, max_length=8)
    severity: Literal[0, 1, 2, 3]
    surprise: StrictBool
    confidence: StrictFloat | StrictInt = Field(ge=0, le=1)
    rationale: str = Field(max_length=400)


def parse_news_classification(raw: object) -> NewsClassification | None:
    """Validate raw LLM output (JSON text, bytes or a mapping). Returns ``None`` on any problem."""
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            if len(raw) > MAX_RAW_LENGTH:
                return None
            data = json.loads(raw)
        elif isinstance(raw, Mapping):
            data = dict(raw)
        else:
            return None
        if not isinstance(data, dict):
            return None
        severity = data.get("severity")
        if not isinstance(severity, int) or isinstance(severity, bool):
            return None
        # Re-validate as JSON in strict mode: enums from their values, arrays as tuples,
        # but no string-to-number or float-to-int coercion.
        return NewsClassification.model_validate_json(json.dumps(data), strict=True)
    except (ValueError, TypeError, ValidationError, RecursionError):
        return None
