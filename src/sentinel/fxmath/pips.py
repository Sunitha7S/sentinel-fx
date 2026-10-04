"""Pip conversions."""

from __future__ import annotations

from decimal import Decimal

from sentinel.domain.types import to_decimal

__all__ = ["pips_to_price", "price_to_pips"]


def _pip(pip_size: Decimal) -> Decimal:
    pip = to_decimal(pip_size, field="pip_size")
    if pip <= 0:
        raise ValueError(f"pip_size must be > 0, got {pip}")
    return pip


def price_to_pips(distance: Decimal, pip_size: Decimal) -> Decimal:
    return to_decimal(distance, field="distance") / _pip(pip_size)


def pips_to_price(pips: Decimal, pip_size: Decimal) -> Decimal:
    return to_decimal(pips, field="pips") * _pip(pip_size)
