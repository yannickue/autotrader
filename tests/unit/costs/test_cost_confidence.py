"""Unit tests for CFD cost-confidence safety: `CostConfidence`,
`VenueCostSchedule.overall_confidence`, `make_cfd_cost_schedule`, and
`costs.engine.is_safe_for_risk_decisions`.

These guard the directive-mandated requirement that a Binance-derived/
default cost schedule must never silently apply to a CFD instrument, and
that unsupported/placeholder CFD cost configuration is explicitly
identifiable rather than silently treated as a verified zero-cost trade.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from costs.engine import calculate_trade_costs, is_safe_for_risk_decisions
from costs.models import (
    CostCalculationRequest,
    CostConfidence,
    InstrumentClass,
    LiquidityRole,
    TradeSide,
    VenueCostSchedule,
    make_cfd_cost_schedule,
)


def _cfd_request(**changes: object) -> CostCalculationRequest:
    defaults: dict[str, object] = {
        "trade_id": "cfd-trade-1",
        "instrument": "DE40.cash",
        "instrument_class": InstrumentClass.CFD,
        "side": TradeSide.LONG,
        "liquidity_role": LiquidityRole.TAKER,
        "quantity": Decimal("1"),
        "entry_price": Decimal("18000"),
        "exit_price": Decimal("18050"),
        "holding_period": timedelta(hours=12),
    }
    defaults.update(changes)
    return CostCalculationRequest(**defaults)  # type: ignore[arg-type]


# --- make_cfd_cost_schedule shape ----------------------------------------------------


def test_make_cfd_cost_schedule_has_cfd_instrument_class_and_zero_funding() -> None:
    schedule = make_cfd_cost_schedule(
        venue="activtrades-demo",
        commission_rate=Decimal("0.0001"),
        swap_rate_long_per_day=Decimal("0.00002"),
    )

    assert schedule.instrument_class is InstrumentClass.CFD
    assert schedule.maker_fee_rate == Decimal("0")
    assert schedule.taker_fee_rate == Decimal("0")
    assert schedule.funding_rate_long_per_interval == Decimal("0")
    assert schedule.funding_rate_short_per_interval == Decimal("0")


def test_make_cfd_cost_schedule_defaults_to_estimated_confidence_never_verified() -> None:
    schedule = make_cfd_cost_schedule(venue="activtrades-demo")

    assert schedule.cost_confidence is CostConfidence.ESTIMATED
    assert schedule.cost_confidence is not CostConfidence.VERIFIED


def test_make_cfd_cost_schedule_accepts_unknown_confidence() -> None:
    schedule = make_cfd_cost_schedule(
        venue="activtrades-demo", cost_confidence=CostConfidence.UNKNOWN
    )

    assert schedule.cost_confidence is CostConfidence.UNKNOWN


def test_make_cfd_cost_schedule_rejects_verified_confidence() -> None:
    with pytest.raises(ValueError, match="VERIFIED"):
        make_cfd_cost_schedule(venue="activtrades-demo", cost_confidence=CostConfidence.VERIFIED)


def test_make_cfd_cost_schedule_rejects_verified_component_override() -> None:
    with pytest.raises(ValueError, match="VERIFIED"):
        make_cfd_cost_schedule(
            venue="activtrades-demo",
            component_confidence={"commission_rate": CostConfidence.VERIFIED},
        )


# --- overall_confidence resolution (fail closed on worst component) -----------------


def test_overall_confidence_defaults_to_schedule_level_value() -> None:
    schedule = make_cfd_cost_schedule(
        venue="activtrades-demo", cost_confidence=CostConfidence.ESTIMATED
    )

    assert schedule.overall_confidence() is CostConfidence.ESTIMATED


def test_overall_confidence_is_worst_of_schedule_and_component_overrides() -> None:
    schedule = make_cfd_cost_schedule(
        venue="activtrades-demo",
        cost_confidence=CostConfidence.ESTIMATED,
        component_confidence={"swap_rate_long_per_day": CostConfidence.UNKNOWN},
    )

    # One UNKNOWN component drags the whole schedule's overall confidence to
    # UNKNOWN, even though the schedule-level default is ESTIMATED.
    assert schedule.overall_confidence() is CostConfidence.UNKNOWN


def test_component_confidence_rejects_unknown_field_name() -> None:
    with pytest.raises(ValueError, match="unknown field name"):
        VenueCostSchedule(
            venue="activtrades-demo",
            instrument_class=InstrumentClass.CFD,
            cost_confidence=CostConfidence.ESTIMATED,
            component_confidence={"not_a_real_field": CostConfidence.ESTIMATED},
            maker_fee_rate=Decimal("0"),
            taker_fee_rate=Decimal("0"),
        )


# --- CostBreakdown carries confidence forward; is_safe_for_risk_decisions -----------


def test_cfd_breakdown_carries_unknown_confidence_forward_and_flags_unsafe() -> None:
    schedule = make_cfd_cost_schedule(
        venue="activtrades-demo",
        commission_rate=Decimal("0.0001"),
        swap_rate_long_per_day=Decimal("0.00002"),
        cost_confidence=CostConfidence.UNKNOWN,
    )
    request = _cfd_request()

    result = calculate_trade_costs(request, schedule)

    assert result.cost_confidence is CostConfidence.UNKNOWN
    assert is_safe_for_risk_decisions(result) is False


def test_cfd_breakdown_with_estimated_confidence_is_flagged_safe_for_risk_decisions() -> None:
    schedule = make_cfd_cost_schedule(
        venue="activtrades-demo",
        commission_rate=Decimal("0.0001"),
        swap_rate_long_per_day=Decimal("0.00002"),
        cost_confidence=CostConfidence.ESTIMATED,
    )
    request = _cfd_request()

    result = calculate_trade_costs(request, schedule)

    assert result.cost_confidence is CostConfidence.ESTIMATED
    assert is_safe_for_risk_decisions(result) is True


# --- instrument_class cross-check, CFD-specific -------------------------------------


def test_cfd_request_against_perpetual_schedule_raises_value_error() -> None:
    perpetual_schedule = VenueCostSchedule(
        venue="binance-usdm",
        instrument_class=InstrumentClass.PERPETUAL,
        cost_confidence=CostConfidence.ESTIMATED,
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0004"),
    )
    request = _cfd_request()

    with pytest.raises(ValueError, match="instrument_class mismatch"):
        calculate_trade_costs(request, perpetual_schedule)


def test_matched_cfd_request_against_cfd_schedule_computes_normally() -> None:
    schedule = make_cfd_cost_schedule(
        venue="activtrades-demo",
        commission_rate=Decimal("0.0001"),
        swap_rate_long_per_day=Decimal("0.00002"),
    )
    request = _cfd_request()

    result = calculate_trade_costs(request, schedule)

    assert result.gross_pnl == Decimal("50")
    # commission: 0.0001 * (18000 + 18050) = 3.605
    assert result.commission == Decimal("3.605")
    # exchange_fee is zero: maker/taker rates are forced to zero for CFDs
    assert result.exchange_fee == Decimal("0")
    # swap: 0.00002 * 18000 entry notional * 0.5 day = 0.18
    assert result.funding_or_swap == Decimal("0.18")
