"""Explicit value types: money, prices, quantities, percentages, timestamps and identifiers.

Rules enforced here:
* No binary floating point anywhere a monetary or risk quantity is stored. ``to_decimal``
  rejects ``float`` (and ``bool``) outright instead of converting it.
* Timestamps are timezone-aware and normalised to UTC. Naive datetimes are rejected.
* Percentages carry their unit: ``Percent(Decimal("0.5"))`` is half a percent, and the
  conversion to a fraction is explicit (``.fraction``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import NewType

__all__ = [
    "ApprovalId",
    "CandidateId",
    "Currency",
    "DecisionId",
    "DomainError",
    "Money",
    "Percent",
    "PositionId",
    "Price",
    "Side",
    "SnapshotId",
    "Units",
    "require_non_negative",
    "require_positive",
    "require_utc",
    "to_decimal",
]


class DomainError(ValueError):
    """Raised when a domain value would be constructed in an invalid state."""


# ----------------------------------------------------------------------------- identifiers

DecisionId = NewType("DecisionId", str)
CandidateId = NewType("CandidateId", str)
SnapshotId = NewType("SnapshotId", str)
ApprovalId = NewType("ApprovalId", str)
PositionId = NewType("PositionId", str)

# ----------------------------------------------------------------------------- quantities

Price = NewType("Price", Decimal)
"""A quoted price in the instrument's quote currency per unit of base currency."""

Units = NewType("Units", Decimal)
"""A position size in units of the base currency."""


def to_decimal(value: object, *, field: str) -> Decimal:
    """Convert ``value`` to a finite ``Decimal``, refusing floats and booleans.

    Floats are refused rather than converted: ``Decimal(0.1)`` silently carries binary
    rounding error, and a float reaching a money calculation is a bug at the boundary
    that produced it.
    """
    if isinstance(value, bool | float):
        raise TypeError(f"{field}: {type(value).__name__} is not allowed; use Decimal or str")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int | str):
        try:
            result = Decimal(value)
        except InvalidOperation as exc:
            raise DomainError(f"{field}: {value!r} is not a decimal number") from exc
    else:
        raise TypeError(f"{field}: unsupported type {type(value).__name__}")
    if not result.is_finite():
        raise DomainError(f"{field}: must be finite, got {result}")
    return result


def require_positive(value: Decimal, *, field: str) -> Decimal:
    if value <= 0:
        raise DomainError(f"{field}: must be > 0, got {value}")
    return value


def require_non_negative(value: Decimal, *, field: str) -> Decimal:
    if value < 0:
        raise DomainError(f"{field}: must be >= 0, got {value}")
    return value


def require_utc(value: datetime, *, field: str) -> datetime:
    """Return ``value`` normalised to UTC; reject naive datetimes."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field}: expected datetime, got {type(value).__name__}")
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None:
        raise DomainError(f"{field}: naive datetime is not allowed")
    if offset != timedelta(0):
        raise DomainError(f"{field}: must be UTC, got offset {offset}")
    return value.astimezone(UTC)


# ----------------------------------------------------------------------------- enums


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    JPY = "JPY"
    AUD = "AUD"
    CAD = "CAD"
    CHF = "CHF"
    NZD = "NZD"


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


# ----------------------------------------------------------------------------- money


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in a specific currency. Arithmetic across currencies is an error."""

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", to_decimal(self.amount, field="Money.amount"))
        if not isinstance(self.currency, Currency):
            raise TypeError("Money.currency must be a Currency")

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        return cls(Decimal(0), currency)

    def _same(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError(f"cannot combine Money with {type(other).__name__}")
        if other.currency is not self.currency:
            raise DomainError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._same(other)
        return self.amount >= other.amount

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"


# ----------------------------------------------------------------------------- percentages

_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True, order=True)
class Percent:
    """A percentage expressed in percent units: ``Percent(Decimal("0.5"))`` == 0.5 %."""

    value: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", to_decimal(self.value, field="Percent.value"))

    @classmethod
    def from_fraction(cls, fraction: Decimal) -> Percent:
        return cls(to_decimal(fraction, field="fraction") * _HUNDRED)

    @property
    def fraction(self) -> Decimal:
        return self.value / _HUNDRED

    def of(self, money: Money) -> Money:
        return Money(money.amount * self.fraction, money.currency)

    def __str__(self) -> str:
        return f"{self.value}%"
