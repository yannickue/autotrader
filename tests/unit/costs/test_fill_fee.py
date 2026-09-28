"""Unit tests for `costs.engine.calculate_fill_fee` (per-fill fee pricing)."""

from decimal import Decimal

import pytest

from costs.engine import calculate_fill_fee
from costs.models import LiquidityRole, VenueCostSchedule


def _schedule(**changes: object) -> VenueCostSchedule:
    defaults: dict[str, object] = {
        "venue": "test-venue",
        "maker_fee_rate": Decimal("0.0002"),
        "taker_fee_rate": Decimal("0.0005"),
    }
    defaults.update(changes)
    return VenueCostSchedule(**defaults)  # type: ignore[arg-type]


def test_taker_fill_uses_taker_fee_rate() -> None:
    schedule = _schedule(maker_fee_rate=Decimal("0.0001"), taker_fee_rate=Decimal("0.001"))

    fee = calculate_fill_fee(
        notional=Decimal("1000"), liquidity_role=LiquidityRole.TAKER, schedule=schedule
    )

    assert fee == Decimal("1.0")  # 0.001 * 1000


def test_maker_fill_uses_maker_fee_rate() -> None:
    schedule = _schedule(maker_fee_rate=Decimal("0.0001"), taker_fee_rate=Decimal("0.001"))

    fee = calculate_fill_fee(
        notional=Decimal("1000"), liquidity_role=LiquidityRole.MAKER, schedule=schedule
    )

    assert fee == Decimal("0.1")  # 0.0001 * 1000


def test_fill_fee_includes_commission_rate_and_fixed() -> None:
    schedule = _schedule(
        maker_fee_rate=Decimal("0"),
        taker_fee_rate=Decimal("0"),
        commission_rate=Decimal("0.0001"),
        commission_fixed=Decimal("0.5"),
    )

    fee = calculate_fill_fee(
        notional=Decimal("1000"), liquidity_role=LiquidityRole.TAKER, schedule=schedule
    )

    # commission_rate * notional + commission_fixed = 0.1 + 0.5
    assert fee == Decimal("0.6")


def test_fill_fee_floored_at_commission_minimum() -> None:
    schedule = _schedule(
        maker_fee_rate=Decimal("0"),
        taker_fee_rate=Decimal("0"),
        commission_rate=Decimal("0.0001"),
        commission_fixed=Decimal("0"),
        commission_minimum=Decimal("5"),
    )

    fee = calculate_fill_fee(
        notional=Decimal("1000"), liquidity_role=LiquidityRole.TAKER, schedule=schedule
    )

    # raw commission (0.1) is below the configured minimum, floored to 5
    assert fee == Decimal("5")


def test_fill_fee_not_floored_when_above_commission_minimum() -> None:
    schedule = _schedule(
        maker_fee_rate=Decimal("0"),
        taker_fee_rate=Decimal("0"),
        commission_rate=Decimal("0.01"),
        commission_fixed=Decimal("0"),
        commission_minimum=Decimal("5"),
    )

    fee = calculate_fill_fee(
        notional=Decimal("1000"), liquidity_role=LiquidityRole.TAKER, schedule=schedule
    )

    # raw commission (10) exceeds the minimum, so the raw value is used
    assert fee == Decimal("10")


def test_fill_fee_combines_exchange_fee_and_commission() -> None:
    schedule = _schedule(
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        commission_rate=Decimal("0.0001"),
        commission_fixed=Decimal("0.25"),
    )

    fee = calculate_fill_fee(
        notional=Decimal("2000"), liquidity_role=LiquidityRole.MAKER, schedule=schedule
    )

    # exchange fee: 0.0002 * 2000 = 0.4; commission: 0.0001 * 2000 + 0.25 = 0.45
    assert fee == Decimal("0.85")


def test_fill_fee_rejects_negative_notional() -> None:
    schedule = _schedule()
    with pytest.raises(ValueError):
        calculate_fill_fee(
            notional=Decimal("-1"), liquidity_role=LiquidityRole.TAKER, schedule=schedule
        )


def test_fill_fee_zero_notional_is_zero_fee_when_no_fixed_or_minimum() -> None:
    schedule = _schedule(maker_fee_rate=Decimal("0.001"), taker_fee_rate=Decimal("0.001"))
    fee = calculate_fill_fee(
        notional=Decimal("0"), liquidity_role=LiquidityRole.TAKER, schedule=schedule
    )
    assert fee == Decimal("0")
