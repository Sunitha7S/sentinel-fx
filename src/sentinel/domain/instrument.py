"""Tradeable instruments and their broker precision rules."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sentinel.domain.types import (
    Currency,
    DomainError,
    require_positive,
    to_decimal,
)

__all__ = ["DEFAULT_INSTRUMENTS", "Instrument"]


@dataclass(frozen=True, slots=True)
class Instrument:
    """A currency pair with the broker's size precision and margin requirement.

    ``min_units``, ``unit_step`` and ``max_units`` are in base-currency units. ``margin_rate``
    is a fraction of notional (``0.0333`` is 30:1). ``max_spread_pips`` is an absolute ceiling
    applied on top of the policy's relative spread limit.
    """

    symbol: str
    base: Currency
    quote: Currency
    pip_size: Decimal
    min_units: Decimal
    unit_step: Decimal
    max_units: Decimal
    margin_rate: Decimal
    max_spread_pips: Decimal

    def __post_init__(self) -> None:
        for name in (
            "pip_size",
            "min_units",
            "unit_step",
            "max_units",
            "margin_rate",
            "max_spread_pips",
        ):
            value = to_decimal(getattr(self, name), field=f"Instrument.{name}")
            object.__setattr__(self, name, require_positive(value, field=f"Instrument.{name}"))
        if self.base is self.quote:
            raise DomainError("Instrument: base and quote must differ")
        if self.symbol != f"{self.base}_{self.quote}":
            raise DomainError(f"Instrument: symbol {self.symbol!r} must be BASE_QUOTE")
        if self.max_units < self.min_units:
            raise DomainError("Instrument: max_units must be >= min_units")
        if self.min_units % self.unit_step != 0:
            raise DomainError("Instrument: min_units must be a multiple of unit_step")
        if self.margin_rate > 1:
            raise DomainError("Instrument: margin_rate is a fraction and must be <= 1")

    @property
    def currencies(self) -> tuple[Currency, Currency]:
        return (self.base, self.quote)


def _major(base: Currency, quote: Currency, pip: str) -> Instrument:
    return Instrument(
        symbol=f"{base}_{quote}",
        base=base,
        quote=quote,
        pip_size=Decimal(pip),
        min_units=Decimal(1),
        unit_step=Decimal(1),
        max_units=Decimal(10_000_000),
        margin_rate=Decimal("0.0333"),
        max_spread_pips=Decimal(3),
    )


DEFAULT_INSTRUMENTS: dict[str, Instrument] = {
    i.symbol: i
    for i in (
        _major(Currency.EUR, Currency.USD, "0.0001"),
        _major(Currency.GBP, Currency.USD, "0.0001"),
        _major(Currency.USD, Currency.JPY, "0.01"),
        _major(Currency.AUD, Currency.USD, "0.0001"),
        _major(Currency.USD, Currency.CAD, "0.0001"),
    )
}
"""The five pairs in scope. Precision values are typical for a retail broker and must be
confirmed against the broker's instrument endpoint before PAPER mode (M10)."""
