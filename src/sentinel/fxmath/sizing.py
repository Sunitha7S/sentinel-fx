"""Deterministic position sizing.

Rounding is always in the conservative direction:

* the loss per unit is rounded **up** (``ROUND_CEILING``), so risk is never under-estimated;
* the raw size is rounded **down** (``ROUND_FLOOR``) to the broker's unit step;
* a final guard removes one step at a time while ``units * loss_per_unit`` exceeds the budget.

Invalid input never raises (except a float, which is a programming error at the boundary):
it returns a zero size with a ``SizingError`` so the caller can block with a reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum

from sentinel.domain.types import to_decimal

__all__ = ["CODE_MAX_RISK_FRACTION", "PositionSize", "SizingError", "size_position"]

CODE_MAX_RISK_FRACTION = Decimal("0.01")
"""Hard ceiling on risk per trade (1 % of equity). No configuration can exceed it."""

_PRECISION = 50


class SizingError(StrEnum):
    NONPOSITIVE_EQUITY = "NONPOSITIVE_EQUITY"
    INVALID_RISK_FRACTION = "INVALID_RISK_FRACTION"
    RISK_ABOVE_CODE_CAP = "RISK_ABOVE_CODE_CAP"
    INVALID_PRICE = "INVALID_PRICE"
    NONPOSITIVE_STOP_DISTANCE = "NONPOSITIVE_STOP_DISTANCE"
    NEGATIVE_COST = "NEGATIVE_COST"
    INVALID_CONVERSION = "INVALID_CONVERSION"
    INVALID_INSTRUMENT = "INVALID_INSTRUMENT"
    BELOW_MIN_UNITS = "BELOW_MIN_UNITS"


@dataclass(frozen=True, slots=True)
class PositionSize:
    units: Decimal
    risk_amount: Decimal
    """Loss in account currency if the stop fills: ``units * loss_per_unit``."""
    loss_per_unit: Decimal
    risk_budget: Decimal
    error: SizingError | None

    @property
    def ok(self) -> bool:
        return self.error is None


def _reject(error: SizingError, budget: Decimal = Decimal(0)) -> PositionSize:
    return PositionSize(Decimal(0), Decimal(0), Decimal(0), budget, error)


def size_position(  # noqa: PLR0911 - one early return per rejected input
    *,
    equity: Decimal,
    risk_fraction: Decimal,
    entry: Decimal,
    stop: Decimal,
    cost_buffer: Decimal,
    quote_to_account: Decimal,
    min_units: Decimal,
    unit_step: Decimal,
    max_units: Decimal,
) -> PositionSize:
    """Largest size whose loss at the stop (plus costs) fits ``equity * risk_fraction``.

    ``cost_buffer`` (spread + expected slippage, in price units) is added to the stop
    distance. ``quote_to_account`` converts one unit of the quote currency to the account
    currency.
    """
    equity = to_decimal(equity, field="equity")
    risk_fraction = to_decimal(risk_fraction, field="risk_fraction")
    entry = to_decimal(entry, field="entry")
    stop = to_decimal(stop, field="stop")
    cost_buffer = to_decimal(cost_buffer, field="cost_buffer")
    quote_to_account = to_decimal(quote_to_account, field="quote_to_account")
    min_units = to_decimal(min_units, field="min_units")
    unit_step = to_decimal(unit_step, field="unit_step")
    max_units = to_decimal(max_units, field="max_units")

    if equity <= 0:
        return _reject(SizingError.NONPOSITIVE_EQUITY)
    if risk_fraction <= 0:
        return _reject(SizingError.INVALID_RISK_FRACTION)
    if risk_fraction > CODE_MAX_RISK_FRACTION:
        return _reject(SizingError.RISK_ABOVE_CODE_CAP)
    if entry <= 0 or stop <= 0:
        return _reject(SizingError.INVALID_PRICE)
    if cost_buffer < 0:
        return _reject(SizingError.NEGATIVE_COST)
    if quote_to_account <= 0:
        return _reject(SizingError.INVALID_CONVERSION)
    if unit_step <= 0 or min_units <= 0 or max_units < min_units:
        return _reject(SizingError.INVALID_INSTRUMENT)
    distance = abs(entry - stop)
    if distance == 0:
        return _reject(SizingError.NONPOSITIVE_STOP_DISTANCE)

    with localcontext() as ctx:
        ctx.prec = _PRECISION
        ctx.rounding = ROUND_FLOOR
        budget = equity * risk_fraction
        ctx.rounding = ROUND_CEILING
        loss_per_unit = (distance + cost_buffer) * quote_to_account
        ctx.rounding = ROUND_FLOOR
        raw_units = budget / loss_per_unit
        steps = (raw_units / unit_step).to_integral_value(rounding=ROUND_FLOOR)
        max_steps = (max_units / unit_step).to_integral_value(rounding=ROUND_FLOOR)
        steps = min(steps, max_steps)
        while steps > 0 and steps * unit_step * loss_per_unit > budget:
            steps -= 1
        units = steps * unit_step
        if units < min_units:
            return PositionSize(
                Decimal(0), Decimal(0), loss_per_unit, budget, SizingError.BELOW_MIN_UNITS
            )
        return PositionSize(units, units * loss_per_unit, loss_per_unit, budget, None)
