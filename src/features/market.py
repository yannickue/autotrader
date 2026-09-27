"""Deterministic point-in-time market features."""

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise


def _validate_prices(prices: Sequence[Decimal]) -> None:
    if len(prices) < 2:
        raise ValueError("at least two prices are required")
    if any(not price.is_finite() or price <= 0 for price in prices):
        raise ValueError("prices must be finite and positive")


def absolute_movement(prices: Sequence[Decimal]) -> Decimal:
    """Absolute point-to-point return over an ordered price window."""
    _validate_prices(prices)
    return abs(prices[-1] / prices[0] - Decimal(1))


def realized_volatility(prices: Sequence[Decimal]) -> Decimal:
    """Unannualized square-root sum of consecutive logarithmic returns."""
    _validate_prices(prices)
    log_returns = [(current / previous).ln() for previous, current in pairwise(prices)]
    return sum((value * value for value in log_returns), start=Decimal(0)).sqrt()
