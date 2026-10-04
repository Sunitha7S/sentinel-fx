"""Numerical invariants of position sizing (fxmath.sizing.size_position).

These are properties over the whole input space, not examples. Worked examples live in
tests/unit/fxmath/test_sizing_examples.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from sentinel.fxmath.sizing import (
    CODE_MAX_RISK_FRACTION,
    PositionSize,
    SizingError,
    size_position,
)


@dataclass(frozen=True)
class Case:
    equity: Decimal
    risk_fraction: Decimal
    entry: Decimal
    stop: Decimal
    cost_buffer: Decimal
    quote_to_account: Decimal
    min_units: Decimal
    unit_step: Decimal
    max_units: Decimal

    def run(self, **override: Decimal) -> PositionSize:
        args = {**self.__dict__, **override}
        return size_position(**args)


def decimals(lo: str, hi: str, places: int) -> st.SearchStrategy[Decimal]:
    return st.decimals(min_value=Decimal(lo), max_value=Decimal(hi), places=places, allow_nan=False)


@st.composite
def cases(draw: st.DrawFn) -> Case:
    entry = draw(decimals("0.5", "200", 5))
    distance = draw(decimals("0.00001", "0.5", 5).filter(lambda d: d < entry / 2))
    side = draw(st.sampled_from([1, -1]))
    step = draw(st.sampled_from([Decimal("0.1"), Decimal(1), Decimal(10), Decimal(1000)]))
    min_units = step * draw(st.integers(1, 10))
    max_units = min_units * draw(st.integers(10, 1_000_000))
    return Case(
        equity=draw(decimals("100", "10000000", 2)),
        risk_fraction=draw(decimals("0.0001", str(CODE_MAX_RISK_FRACTION), 6)),
        entry=entry,
        stop=entry - side * distance,
        cost_buffer=draw(decimals("0", "0.001", 6)),
        quote_to_account=draw(decimals("0.005", "2", 6)),
        min_units=min_units,
        unit_step=step,
        max_units=max_units,
    )


# ----------------------------------------------------------------------------- INV-SIZE-01


@pytest.mark.invariant("INV-SIZE-01")
@given(cases())
def test_risk_amount_never_exceeds_equity_times_risk_fraction(c: Case) -> None:
    r = c.run()
    assert r.risk_amount <= c.equity * c.risk_fraction
    assert r.risk_amount == r.units * r.loss_per_unit


@pytest.mark.invariant("INV-SIZE-01")
@given(cases())
def test_loss_per_unit_is_never_underestimated(c: Case) -> None:
    r = c.run()
    assume(r.ok)
    true_loss_per_unit = (abs(c.entry - c.stop) + c.cost_buffer) * c.quote_to_account
    assert r.loss_per_unit >= true_loss_per_unit
    assert r.units * true_loss_per_unit <= c.equity * c.risk_fraction


# ----------------------------------------------------------------------------- INV-SIZE-02


@pytest.mark.invariant("INV-SIZE-02")
@given(cases())
def test_size_is_non_negative_and_respects_broker_precision(c: Case) -> None:
    r = c.run()
    assert r.units >= 0
    assert r.units % c.unit_step == 0
    if r.units != 0:
        assert c.min_units <= r.units <= c.max_units


@pytest.mark.invariant("INV-SIZE-02")
@given(cases())
def test_size_rounds_down_tightly(c: Case) -> None:
    """Rounding is down, but not wastefully: one more step would break a limit."""
    r = c.run()
    assume(r.ok)
    next_units = r.units + c.unit_step
    assert next_units * r.loss_per_unit > c.equity * c.risk_fraction or next_units > c.max_units


@pytest.mark.invariant("INV-SIZE-02")
@given(cases())
def test_below_minimum_is_blocked_never_rounded_up(c: Case) -> None:
    r = c.run(min_units=c.max_units, max_units=c.max_units)
    if r.units == 0:
        assert r.error is SizingError.BELOW_MIN_UNITS
    else:
        assert r.units == c.max_units


# ----------------------------------------------------------------------------- INV-SIZE-03


@pytest.mark.invariant("INV-SIZE-03")
@given(cases(), decimals("-1", "0", 5))
def test_zero_or_negative_stop_distance_is_rejected(c: Case, offset: Decimal) -> None:
    # stop at entry (zero distance)
    zero = c.run(stop=c.entry)
    assert zero.units == 0
    assert zero.error is SizingError.NONPOSITIVE_STOP_DISTANCE
    # non-positive stop price is invalid input as well
    bad = c.run(stop=offset)
    assert bad.units == 0
    assert bad.error is not None


@pytest.mark.invariant("INV-SIZE-03")
@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"equity": Decimal(0)}, SizingError.NONPOSITIVE_EQUITY),
        ({"equity": Decimal(-5)}, SizingError.NONPOSITIVE_EQUITY),
        ({"risk_fraction": Decimal(0)}, SizingError.INVALID_RISK_FRACTION),
        ({"risk_fraction": Decimal("-0.01")}, SizingError.INVALID_RISK_FRACTION),
        ({"risk_fraction": Decimal("0.0101")}, SizingError.RISK_ABOVE_CODE_CAP),
        ({"risk_fraction": Decimal("0.5")}, SizingError.RISK_ABOVE_CODE_CAP),
        ({"entry": Decimal(0)}, SizingError.INVALID_PRICE),
        ({"cost_buffer": Decimal("-0.0001")}, SizingError.NEGATIVE_COST),
        ({"quote_to_account": Decimal(0)}, SizingError.INVALID_CONVERSION),
        ({"unit_step": Decimal(0)}, SizingError.INVALID_INSTRUMENT),
        ({"max_units": Decimal("0.5")}, SizingError.INVALID_INSTRUMENT),
    ],
)
def test_invalid_inputs_give_zero_size_and_an_error(
    override: dict[str, Decimal], error: SizingError
) -> None:
    base = Case(
        equity=Decimal(10_000),
        risk_fraction=Decimal("0.005"),
        entry=Decimal("1.08510"),
        stop=Decimal("1.08210"),
        cost_buffer=Decimal("0.00012"),
        quote_to_account=Decimal(1),
        min_units=Decimal(1),
        unit_step=Decimal(1),
        max_units=Decimal(10_000_000),
    )
    r = base.run(**override)
    assert r.units == 0
    assert r.risk_amount == 0
    assert r.error is error
    assert not r.ok


@pytest.mark.invariant("INV-SIZE-03")
@pytest.mark.parametrize("field", ["equity", "risk_fraction", "entry", "stop"])
def test_floats_are_refused(field: str) -> None:
    kwargs: dict[str, object] = {
        "equity": Decimal(10_000),
        "risk_fraction": Decimal("0.005"),
        "entry": Decimal("1.1"),
        "stop": Decimal("1.09"),
        "cost_buffer": Decimal(0),
        "quote_to_account": Decimal(1),
        "min_units": Decimal(1),
        "unit_step": Decimal(1),
        "max_units": Decimal(1_000_000),
        field: 1.05,
    }
    with pytest.raises(TypeError):
        size_position(**kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- INV-SIZE-04


@pytest.mark.invariant("INV-SIZE-04")
@given(cases(), decimals("1", "5", 3))
def test_wider_stop_never_produces_a_larger_position(c: Case, widen: Decimal) -> None:
    distance = c.entry - c.stop
    wider_stop = c.entry - distance * widen
    assume(wider_stop > 0)
    assert c.run(stop=wider_stop).units <= c.run().units


@pytest.mark.invariant("INV-SIZE-04")
@given(cases(), decimals("0", "1", 4))
def test_lower_permitted_risk_never_produces_a_larger_position(c: Case, shrink: Decimal) -> None:
    lower = c.risk_fraction * shrink
    assert c.run(risk_fraction=lower).units <= c.run().units


@pytest.mark.invariant("INV-SIZE-04")
@given(cases(), decimals("0", "0.001", 6))
def test_higher_costs_never_produce_a_larger_position(c: Case, extra: Decimal) -> None:
    assert c.run(cost_buffer=c.cost_buffer + extra).units <= c.run().units


@pytest.mark.invariant("INV-SIZE-04")
@given(cases(), decimals("0", "1", 4))
def test_lower_equity_never_produces_a_larger_position(c: Case, shrink: Decimal) -> None:
    lower = c.equity * shrink
    assume(lower > 0)
    assert c.run(equity=lower).units <= c.run().units
