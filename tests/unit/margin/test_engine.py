from decimal import Decimal

import pytest

from margin.engine import MarginEngine
from margin.models import (
    MaintenanceMarginPolicy,
    MarginPositionSide,
    MarginSafetyReason,
    VenueUncertaintyBuffer,
    VolatilityLeverageCapPolicy,
)
from risk.models import MAX_SYSTEM_LEVERAGE


def _maintenance_policy(**changes: object) -> MaintenanceMarginPolicy:
    values: dict[str, object] = {"maintenance_margin_rate": Decimal("0.005")}
    values.update(changes)
    return MaintenanceMarginPolicy(**values)


def _buffer(**changes: object) -> VenueUncertaintyBuffer:
    values: dict[str, object] = {"buffer_bps": Decimal("50")}
    values.update(changes)
    return VenueUncertaintyBuffer(**values)


def _cap_policy(**changes: object) -> VolatilityLeverageCapPolicy:
    values: dict[str, object] = {
        "base_cap": Decimal("10"),
        "volatility_sensitivity": Decimal("20"),
        "min_cap": Decimal("1"),
    }
    values.update(changes)
    return VolatilityLeverageCapPolicy(**values)


@pytest.fixture
def engine() -> MarginEngine:
    return MarginEngine()


# -- margin requirement ------------------------------------------------------


def test_initial_margin_requirement_scales_inversely_with_leverage(engine: MarginEngine) -> None:
    req_5x = engine.initial_margin_requirement(notional=Decimal("1000"), leverage=Decimal("5"))
    req_10x = engine.initial_margin_requirement(notional=Decimal("1000"), leverage=Decimal("10"))
    req_20x = engine.initial_margin_requirement(notional=Decimal("1000"), leverage=Decimal("20"))

    assert req_5x.initial_margin == Decimal("200")
    assert req_10x.initial_margin == Decimal("100")
    assert req_20x.initial_margin == Decimal("50")
    assert req_20x.initial_margin < req_10x.initial_margin < req_5x.initial_margin


def test_initial_margin_requirement_rejects_non_positive_inputs(engine: MarginEngine) -> None:
    with pytest.raises(ValueError, match="notional"):
        engine.initial_margin_requirement(notional=Decimal("0"), leverage=Decimal("5"))
    with pytest.raises(ValueError, match="leverage"):
        engine.initial_margin_requirement(notional=Decimal("1000"), leverage=Decimal("0"))


# -- maintenance margin -------------------------------------------------------


def test_maintenance_margin_requirement_is_notional_times_rate(engine: MarginEngine) -> None:
    policy = _maintenance_policy(maintenance_margin_rate=Decimal("0.005"))
    result = engine.maintenance_margin_requirement(notional=Decimal("1000"), policy=policy)
    assert result.maintenance_margin == Decimal("5.000")


def test_maintenance_margin_requirement_scales_with_notional(engine: MarginEngine) -> None:
    policy = _maintenance_policy()
    small = engine.maintenance_margin_requirement(notional=Decimal("100"), policy=policy)
    large = engine.maintenance_margin_requirement(notional=Decimal("10000"), policy=policy)
    assert large.maintenance_margin > small.maintenance_margin


# -- liquidation price estimate ----------------------------------------------


def test_liquidation_price_estimate_long_below_entry(engine: MarginEngine) -> None:
    estimate = engine.estimate_liquidation_price(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(maintenance_margin_rate=Decimal("0.005")),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("0")),
    )
    # loss_fraction = 1/10 - 0.005 = 0.095 -> raw = 100 * 0.905 = 90.5
    assert estimate.raw_liquidation_price == Decimal("90.5")
    assert estimate.buffered_liquidation_price == estimate.raw_liquidation_price
    assert estimate.raw_liquidation_price < estimate.entry_price
    assert estimate.is_estimate is True


def test_liquidation_price_estimate_short_above_entry(engine: MarginEngine) -> None:
    estimate = engine.estimate_liquidation_price(
        side=MarginPositionSide.SHORT,
        entry_price=Decimal("100"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(maintenance_margin_rate=Decimal("0.005")),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("0")),
    )
    # loss_fraction = 0.095 -> raw = 100 * 1.095 = 109.5
    assert estimate.raw_liquidation_price == Decimal("109.5")
    assert estimate.buffered_liquidation_price == estimate.raw_liquidation_price
    assert estimate.raw_liquidation_price > estimate.entry_price
    assert estimate.is_estimate is True


def test_liquidation_price_moves_closer_to_entry_at_higher_leverage(engine: MarginEngine) -> None:
    low_leverage = engine.estimate_liquidation_price(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        leverage=Decimal("5"),
        maintenance_policy=_maintenance_policy(),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("0")),
    )
    high_leverage = engine.estimate_liquidation_price(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        leverage=Decimal("20"),
        maintenance_policy=_maintenance_policy(),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("0")),
    )
    assert high_leverage.raw_liquidation_price > low_leverage.raw_liquidation_price


def test_liquidation_estimate_always_marked_as_estimate(engine: MarginEngine) -> None:
    estimate = engine.estimate_liquidation_price(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(),
        uncertainty_buffer=_buffer(),
    )
    assert estimate.is_estimate is True


# -- venue uncertainty buffer widens the safety margin ------------------------


def test_uncertainty_buffer_pushes_liquidation_estimate_toward_entry_long(
    engine: MarginEngine,
) -> None:
    no_buffer = engine.estimate_liquidation_price(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("0")),
    )
    buffered = engine.estimate_liquidation_price(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("500")),
    )
    # A larger buffer must move the LONG liquidation estimate UP (closer to
    # entry / less room), never down.
    assert buffered.buffered_liquidation_price > no_buffer.buffered_liquidation_price


def test_larger_buffer_flips_a_previously_safe_stop_to_unsafe(engine: MarginEngine) -> None:
    """A larger venue-uncertainty buffer must produce a MORE conservative
    (earlier) unsafe flag, never a less conservative one."""
    small_buffer_result = engine.evaluate_stop_safety(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(maintenance_margin_rate=Decimal("0.005")),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("50")),
    )
    large_buffer_result = engine.evaluate_stop_safety(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        stop_price=Decimal("95"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(maintenance_margin_rate=Decimal("0.005")),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("500")),
    )

    assert small_buffer_result.safe is True
    assert small_buffer_result.reason_code is MarginSafetyReason.SAFE
    assert large_buffer_result.safe is False
    assert large_buffer_result.reason_code in (
        MarginSafetyReason.STOP_TOO_CLOSE_TO_LIQUIDATION,
        MarginSafetyReason.STOP_BEYOND_LIQUIDATION,
    )
    assert large_buffer_result.distance_bps < small_buffer_result.distance_bps


# -- stop-to-liquidation distance ---------------------------------------------


def test_stop_to_liquidation_distance_long(engine: MarginEngine) -> None:
    distance_bps, distance_ratio = engine.stop_to_liquidation_distance(
        side=MarginPositionSide.LONG,
        stop_price=Decimal("95"),
        liquidation_price=Decimal("90"),
        entry_price=Decimal("100"),
    )
    assert distance_bps == Decimal("500")
    assert distance_ratio == Decimal("0.05")


def test_stop_to_liquidation_distance_short(engine: MarginEngine) -> None:
    distance_bps, distance_ratio = engine.stop_to_liquidation_distance(
        side=MarginPositionSide.SHORT,
        stop_price=Decimal("105"),
        liquidation_price=Decimal("110"),
        entry_price=Decimal("100"),
    )
    assert distance_bps == Decimal("500")
    assert distance_ratio == Decimal("0.05")


def test_stop_to_liquidation_distance_is_negative_past_liquidation(engine: MarginEngine) -> None:
    distance_bps, _ = engine.stop_to_liquidation_distance(
        side=MarginPositionSide.LONG,
        stop_price=Decimal("89"),
        liquidation_price=Decimal("90"),
        entry_price=Decimal("100"),
    )
    assert distance_bps < 0


# -- unsafe / safe stop-distance flagging (critical correctness requirement) --


def test_stop_too_close_to_estimated_liquidation_is_flagged_unsafe(engine: MarginEngine) -> None:
    # entry=100, leverage=10, maintenance=0.5% -> raw liquidation = 90.5;
    # buffer=50bps -> buffered liquidation = 91.0. Stop at 91.2 is only
    # 20bps clear of the buffered liquidation price, well inside a 100bps
    # minimum required safety distance.
    result = engine.evaluate_stop_safety(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        stop_price=Decimal("91.2"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(maintenance_margin_rate=Decimal("0.005")),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("50")),
        min_required_distance_bps=Decimal("100"),
    )

    assert result.safe is False
    assert result.reason_code is MarginSafetyReason.STOP_TOO_CLOSE_TO_LIQUIDATION
    assert result.is_estimate is True


def test_stop_with_adequate_distance_is_flagged_safe(engine: MarginEngine) -> None:
    # Same setup, but stop at 96 is far clear of the buffered liquidation
    # price (91.0) -- well beyond the 100bps minimum required distance.
    result = engine.evaluate_stop_safety(
        side=MarginPositionSide.LONG,
        entry_price=Decimal("100"),
        stop_price=Decimal("96"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(maintenance_margin_rate=Decimal("0.005")),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("50")),
        min_required_distance_bps=Decimal("100"),
    )

    assert result.safe is True
    assert result.reason_code is MarginSafetyReason.SAFE
    assert result.is_estimate is True


def test_stop_past_liquidation_is_flagged_unsafe_short(engine: MarginEngine) -> None:
    result = engine.evaluate_stop_safety(
        side=MarginPositionSide.SHORT,
        entry_price=Decimal("100"),
        stop_price=Decimal("112"),
        leverage=Decimal("10"),
        maintenance_policy=_maintenance_policy(maintenance_margin_rate=Decimal("0.005")),
        uncertainty_buffer=_buffer(buffer_bps=Decimal("50")),
    )
    assert result.safe is False
    assert result.reason_code is MarginSafetyReason.STOP_BEYOND_LIQUIDATION


# -- volatility-adjusted leverage cap ------------------------------------------


def test_volatility_adjusted_cap_shrinks_as_volatility_rises(engine: MarginEngine) -> None:
    low_vol_cap = engine.volatility_adjusted_leverage_cap(
        policy=_cap_policy(), volatility=Decimal("0.01")
    )
    high_vol_cap = engine.volatility_adjusted_leverage_cap(
        policy=_cap_policy(), volatility=Decimal("0.10")
    )
    zero_vol_cap = engine.volatility_adjusted_leverage_cap(
        policy=_cap_policy(), volatility=Decimal("0")
    )

    assert zero_vol_cap == Decimal("10")
    assert zero_vol_cap > low_vol_cap > high_vol_cap


def test_volatility_adjusted_cap_never_exceeds_hard_system_ceiling(engine: MarginEngine) -> None:
    cap = engine.volatility_adjusted_leverage_cap(
        policy=_cap_policy(base_cap=MAX_SYSTEM_LEVERAGE, volatility_sensitivity=Decimal("0")),
        volatility=Decimal("0"),
    )
    assert cap <= MAX_SYSTEM_LEVERAGE


def test_volatility_adjusted_cap_never_exceeds_stricter_cap(engine: MarginEngine) -> None:
    cap = engine.volatility_adjusted_leverage_cap(
        policy=_cap_policy(base_cap=Decimal("10")),
        volatility=Decimal("0"),
        configured_cap=Decimal("3"),
    )
    assert cap == Decimal("3")


def test_volatility_adjusted_cap_respects_floor(engine: MarginEngine) -> None:
    cap = engine.volatility_adjusted_leverage_cap(
        policy=_cap_policy(min_cap=Decimal("2")), volatility=Decimal("1000")
    )
    assert cap == Decimal("2")
