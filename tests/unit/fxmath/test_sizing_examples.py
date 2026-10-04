"""Hand-computed sizing examples (docs/04 section 4) and conversion behaviour."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.domain.types import Currency
from sentinel.fxmath.conversion import ConversionUnavailable, FxRates
from sentinel.fxmath.pips import pips_to_price, price_to_pips
from sentinel.fxmath.sizing import size_position

USD, EUR, JPY, CAD, GBP, CHF = (
    Currency.USD,
    Currency.EUR,
    Currency.JPY,
    Currency.CAD,
    Currency.GBP,
    Currency.CHF,
)


def _size(entry: str, stop: str, cost: str, conversion: Decimal) -> Decimal:
    return size_position(
        equity=Decimal(10_000),
        risk_fraction=Decimal("0.005"),
        entry=Decimal(entry),
        stop=Decimal(stop),
        cost_buffer=Decimal(cost),
        quote_to_account=conversion,
        min_units=Decimal(1),
        unit_step=Decimal(1),
        max_units=Decimal(10_000_000),
    ).units


def test_eurusd_long_worked_example() -> None:
    # 30 pip stop + 1.2 pips cost = 0.00312 USD per unit; $50 / 0.00312 = 16025.6
    assert _size("1.0850", "1.0820", "0.00012", Decimal(1)) == Decimal(16025)


def test_usdjpy_short_worked_example() -> None:
    # 60 pip stop + 1.5 pips = 0.615 JPY per unit = 0.0041137 USD; $50 / that = 12154.4
    rates = FxRates({("USD", "JPY"): Decimal("149.50")})
    assert _size("149.50", "150.10", "0.015", rates.rate(JPY, USD)) == Decimal(12154)


def test_usdcad_long_converts_quote_currency() -> None:
    rates = FxRates({("USD", "CAD"): Decimal("1.36")})
    units = _size("1.3600", "1.3570", "0.00012", rates.rate(CAD, USD))
    # 0.00312 CAD per unit / 1.36 = 0.0022941 USD; $50 / that = 21794.8
    assert units == Decimal(21794)


def test_fx_rates_direct_inverse_cross_and_identity() -> None:
    rates = FxRates(
        {
            ("EUR", "USD"): Decimal("1.08"),
            ("USD", "JPY"): Decimal("150"),
            ("GBP", "USD"): Decimal("1.25"),
        }
    )
    assert rates.rate(USD, USD) == 1
    assert rates.rate(EUR, USD) == Decimal("1.08")
    assert rates.rate(USD, EUR) == Decimal(1) / Decimal("1.08")
    assert rates.rate(EUR, JPY) == Decimal("1.08") * Decimal(150)
    assert rates.rate(GBP, EUR) == Decimal("1.25") / Decimal("1.08")
    with pytest.raises(ConversionUnavailable):
        rates.rate(CHF, USD)


def test_fx_rates_reject_non_positive_quotes() -> None:
    with pytest.raises(ValueError, match="positive"):
        FxRates({("EUR", "USD"): Decimal(0)})


def test_pip_conversion_round_trip() -> None:
    assert price_to_pips(Decimal("0.0030"), Decimal("0.0001")) == Decimal(30)
    assert pips_to_price(Decimal(30), Decimal("0.01")) == Decimal("0.30")
    with pytest.raises(ValueError, match="pip_size"):
        price_to_pips(Decimal(1), Decimal(0))
