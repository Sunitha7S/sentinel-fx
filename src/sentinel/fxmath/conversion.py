"""Currency conversion from a set of quoted pair mid prices."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from sentinel.domain.types import Currency, to_decimal

__all__ = ["ConversionUnavailable", "FxRates"]

_PIVOT = Currency.USD


class ConversionUnavailable(LookupError):
    """No direct, inverse or USD-cross quote exists for the requested conversion."""


class FxRates:
    """Converts amounts between currencies using pair quotes ``(BASE, QUOTE) -> price``.

    ``rate(A, B)`` is how many units of B one unit of A is worth. Lookups try the direct
    pair, the inverse pair, then a cross through USD. Nothing is inferred beyond that, and
    a missing quote raises rather than defaulting to 1.
    """

    def __init__(self, quotes: Mapping[tuple[str, str], Decimal]) -> None:
        self._quotes: dict[tuple[str, str], Decimal] = {}
        for (base, quote), price in quotes.items():
            value = to_decimal(price, field=f"{base}_{quote}")
            if value <= 0:
                raise ValueError(f"quote {base}_{quote} must be positive, got {value}")
            self._quotes[(str(base), str(quote))] = value

    def _direct(self, frm: str, to: str) -> Decimal | None:
        if frm == to:
            return Decimal(1)
        if (frm, to) in self._quotes:
            return self._quotes[(frm, to)]
        if (to, frm) in self._quotes:
            return Decimal(1) / self._quotes[(to, frm)]
        return None

    def rate(self, frm: Currency, to: Currency) -> Decimal:
        direct = self._direct(str(frm), str(to))
        if direct is not None:
            return direct
        via_from = self._direct(str(frm), str(_PIVOT))
        via_to = self._direct(str(_PIVOT), str(to))
        if via_from is None or via_to is None:
            raise ConversionUnavailable(f"no quote path {frm} -> {to}")
        return via_from * via_to
