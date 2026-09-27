from decimal import Decimal

import pytest

from features.market import absolute_movement, realized_volatility


def test_absolute_movement_uses_first_and_last_price_without_float_conversion() -> None:
    # Catches direction-sensitive or float-based movement calculations.
    assert absolute_movement([Decimal("100.00"), Decimal("102.50")]) == Decimal("0.025")
    assert absolute_movement([Decimal("100.00"), Decimal("97.50")]) == Decimal("0.025")


def test_realized_volatility_is_zero_for_flat_prices_and_positive_for_returns() -> None:
    # Catches using price levels instead of log returns.
    assert realized_volatility([Decimal("10"), Decimal("10"), Decimal("10")]) == Decimal(0)
    assert realized_volatility([Decimal("10"), Decimal("11"), Decimal("10")]) > Decimal(0)


@pytest.mark.parametrize("prices", [[], [Decimal("1")], [Decimal("1"), Decimal("0")]])
def test_price_features_reject_insufficient_or_non_positive_prices(
    prices: list[Decimal],
) -> None:
    # Catches silently publishing meaningless or undefined features.
    with pytest.raises(ValueError):
        realized_volatility(prices)

