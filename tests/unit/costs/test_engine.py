from datetime import timedelta
from decimal import Decimal

import pytest

from costs.engine import calculate_trade_costs
from costs.models import (
    CostCalculationRequest,
    CostConfidence,
    InstrumentClass,
    LiquidityRole,
    TradeSide,
    VenueCostSchedule,
)


def _schedule(**changes: object) -> VenueCostSchedule:
    defaults: dict[str, object] = {
        "venue": "test-venue",
        "instrument_class": InstrumentClass.SPOT,
        "cost_confidence": CostConfidence.ESTIMATED,
        "maker_fee_rate": Decimal("0.0002"),
        "taker_fee_rate": Decimal("0.0005"),
    }
    defaults.update(changes)
    return VenueCostSchedule(**defaults)  # type: ignore[arg-type]


def _request(**changes: object) -> CostCalculationRequest:
    defaults: dict[str, object] = {
        "trade_id": "trade-1",
        "instrument": "BTCUSDT-PERP",
        "instrument_class": InstrumentClass.SPOT,
        "side": TradeSide.LONG,
        "liquidity_role": LiquidityRole.TAKER,
        "quantity": Decimal("1"),
        "entry_price": Decimal("100"),
        "exit_price": Decimal("110"),
    }
    defaults.update(changes)
    return CostCalculationRequest(**defaults)  # type: ignore[arg-type]


# --- maker vs taker fee selection -------------------------------------------------


def test_taker_role_uses_taker_fee_rate() -> None:
    schedule = _schedule(maker_fee_rate=Decimal("0.0001"), taker_fee_rate=Decimal("0.001"))
    request = _request(liquidity_role=LiquidityRole.TAKER, quantity=Decimal("1"),
                        entry_price=Decimal("100"), exit_price=Decimal("100"))

    result = calculate_trade_costs(request, schedule)

    # taker fee applied to entry + exit notional: 0.001 * (100 + 100) = 0.2
    assert result.exchange_fee == Decimal("0.2")


def test_maker_role_uses_maker_fee_rate() -> None:
    schedule = _schedule(maker_fee_rate=Decimal("0.0001"), taker_fee_rate=Decimal("0.001"))
    request = _request(liquidity_role=LiquidityRole.MAKER, quantity=Decimal("1"),
                        entry_price=Decimal("100"), exit_price=Decimal("100"))

    result = calculate_trade_costs(request, schedule)

    # maker fee applied to entry + exit notional: 0.0001 * (100 + 100) = 0.02
    assert result.exchange_fee == Decimal("0.02")


# --- spread cost --------------------------------------------------------------------


def test_spread_cost_computed_from_observed_bid_ask() -> None:
    schedule = _schedule()
    request = _request(
        quantity=Decimal("2"),
        entry_price=Decimal("100"),
        entry_bid=Decimal("99.9"),
        entry_ask=Decimal("100.1"),
        exit_price=Decimal("110"),
        exit_bid=Decimal("109.9"),
        exit_ask=Decimal("110.1"),
    )

    result = calculate_trade_costs(request, schedule)

    # half-spread * quantity for each leg: (0.2/2)*2 = 0.2, twice = 0.4
    assert result.entry_spread_cost == Decimal("0.2")
    assert result.exit_spread_cost == Decimal("0.2")
    assert result.spread_cost == Decimal("0.4")


def test_spread_cost_falls_back_to_schedule_estimate_when_no_quotes_given() -> None:
    schedule = _schedule(estimated_spread_bps=Decimal("10"))  # 10 bps
    request = _request(quantity=Decimal("1"), entry_price=Decimal("100"), exit_price=Decimal("100"))

    result = calculate_trade_costs(request, schedule)

    # 100 * (10/10000) * 1 = 0.1 per leg
    assert result.entry_spread_cost == Decimal("0.1")
    assert result.exit_spread_cost == Decimal("0.1")


def test_spread_cost_is_zero_without_quotes_or_schedule_estimate() -> None:
    schedule = _schedule()
    request = _request()

    result = calculate_trade_costs(request, schedule)

    assert result.spread_cost == Decimal("0")


# --- slippage on entry and exit ------------------------------------------------------


def test_entry_slippage_cost_from_expected_price_long() -> None:
    schedule = _schedule()
    request = _request(
        side=TradeSide.LONG,
        quantity=Decimal("1"),
        expected_entry_price=Decimal("100"),
        entry_price=Decimal("100.5"),
        exit_price=Decimal("100.5"),
    )

    result = calculate_trade_costs(request, schedule)

    assert result.entry_slippage_cost == Decimal("0.5")


def test_exit_slippage_cost_from_expected_price_long() -> None:
    schedule = _schedule()
    request = _request(
        side=TradeSide.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        expected_exit_price=Decimal("110"),
        exit_price=Decimal("109.4"),
    )

    result = calculate_trade_costs(request, schedule)

    assert result.exit_slippage_cost == Decimal("0.6")


def test_slippage_cost_short_side_sign_convention() -> None:
    schedule = _schedule()
    # Selling to open a short below the expected price is a cost.
    request = _request(
        side=TradeSide.SHORT,
        quantity=Decimal("1"),
        expected_entry_price=Decimal("100"),
        entry_price=Decimal("99.5"),
        expected_exit_price=Decimal("90"),
        exit_price=Decimal("90.4"),
    )

    result = calculate_trade_costs(request, schedule)

    assert result.entry_slippage_cost == Decimal("0.5")
    assert result.exit_slippage_cost == Decimal("0.4")


def test_slippage_cost_falls_back_to_schedule_bps_when_no_expected_price() -> None:
    schedule = _schedule(entry_slippage_bps=Decimal("5"), exit_slippage_bps=Decimal("5"))
    request = _request(quantity=Decimal("1"), entry_price=Decimal("100"), exit_price=Decimal("200"))

    result = calculate_trade_costs(request, schedule)

    assert result.entry_slippage_cost == Decimal("0.05")
    assert result.exit_slippage_cost == Decimal("0.10")


# --- funding cost accrual over a holding period ---------------------------------------


def test_funding_cost_accrues_per_completed_interval_for_perpetual_long() -> None:
    schedule = _schedule(
        instrument_class=InstrumentClass.PERPETUAL,
        funding_rate_long_per_interval=Decimal("0.0001"),
        funding_interval=timedelta(hours=8),
    )
    request = _request(
        instrument_class=InstrumentClass.PERPETUAL,
        side=TradeSide.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("1000"),
        exit_price=Decimal("1000"),
        holding_period=timedelta(hours=24),
    )

    result = calculate_trade_costs(request, schedule)

    # 3 completed 8h intervals * 0.0001 * 1000 notional = 0.3
    assert result.funding_or_swap == Decimal("0.3")


def test_funding_cost_does_not_accrue_for_partial_interval() -> None:
    schedule = _schedule(
        instrument_class=InstrumentClass.PERPETUAL,
        funding_rate_long_per_interval=Decimal("0.0001"),
        funding_interval=timedelta(hours=8),
    )
    request = _request(
        instrument_class=InstrumentClass.PERPETUAL,
        side=TradeSide.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("1000"),
        exit_price=Decimal("1000"),
        holding_period=timedelta(hours=7, minutes=59),
    )

    result = calculate_trade_costs(request, schedule)

    assert result.funding_or_swap == Decimal("0")


def test_swap_cost_prorates_over_days_for_cfd_short() -> None:
    schedule = _schedule(
        instrument_class=InstrumentClass.CFD, swap_rate_short_per_day=Decimal("0.0002")
    )
    request = _request(
        instrument_class=InstrumentClass.CFD,
        side=TradeSide.SHORT,
        quantity=Decimal("1"),
        entry_price=Decimal("1000"),
        exit_price=Decimal("1000"),
        holding_period=timedelta(hours=12),
    )

    result = calculate_trade_costs(request, schedule)

    # half a day * 0.0002 * 1000 notional = 0.1
    assert result.funding_or_swap == Decimal("0.1")


def test_spot_instrument_has_no_funding_or_swap_cost() -> None:
    schedule = _schedule(
        funding_rate_long_per_interval=Decimal("0.01"),
        swap_rate_long_per_day=Decimal("0.01"),
    )
    request = _request(
        instrument_class=InstrumentClass.SPOT,
        holding_period=timedelta(days=5),
    )

    result = calculate_trade_costs(request, schedule)

    assert result.funding_or_swap == Decimal("0")


# --- commission with and without a minimum floor ---------------------------------------


def test_commission_rate_and_fixed_combine_without_floor() -> None:
    schedule = _schedule(commission_rate=Decimal("0.001"), commission_fixed=Decimal("0.5"))
    request = _request(quantity=Decimal("1"), entry_price=Decimal("100"), exit_price=Decimal("100"))

    result = calculate_trade_costs(request, schedule)

    # 0.001 * (100 + 100) + 0.5 = 0.7
    assert result.commission == Decimal("0.7")


def test_commission_minimum_floor_applied_when_computed_commission_is_lower() -> None:
    schedule = _schedule(commission_rate=Decimal("0.0001"), commission_minimum=Decimal("5"))
    request = _request(quantity=Decimal("1"), entry_price=Decimal("100"), exit_price=Decimal("100"))

    result = calculate_trade_costs(request, schedule)

    # raw commission = 0.0001 * 200 = 0.02, below the 5 floor
    assert result.commission == Decimal("5")


def test_commission_minimum_floor_not_applied_when_computed_commission_is_higher() -> None:
    schedule = _schedule(commission_rate=Decimal("0.05"), commission_minimum=Decimal("1"))
    request = _request(quantity=Decimal("1"), entry_price=Decimal("100"), exit_price=Decimal("100"))

    result = calculate_trade_costs(request, schedule)

    # raw commission = 0.05 * 200 = 10, above the 1 floor
    assert result.commission == Decimal("10")


# --- gross/net pnl composition -----------------------------------------------------


def test_net_pnl_equals_gross_minus_all_cost_components() -> None:
    schedule = _schedule(
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        commission_rate=Decimal("0.0001"),
    )
    request = _request(
        liquidity_role=LiquidityRole.TAKER,
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
    )

    result = calculate_trade_costs(request, schedule)

    assert result.net_pnl == (
        result.gross_pnl
        - result.fees
        - result.spread_cost
        - result.slippage_cost
        - result.funding_or_swap
    )


# --- regression: positive gross expectancy, negative net expectancy -----------------


def test_positive_gross_pnl_with_costs_exceeding_it_is_reported_as_net_negative() -> None:
    """A strategy can look profitable gross and still be a net loser.

    This is the critical correctness case: entry/exit prices produce a small
    positive gross_pnl, but fees + spread + slippage + funding/swap exceed
    it, so net_pnl must be negative even though gross_pnl is positive.
    """
    schedule = _schedule(
        instrument_class=InstrumentClass.PERPETUAL,
        maker_fee_rate=Decimal("0.001"),
        taker_fee_rate=Decimal("0.001"),
        commission_rate=Decimal("0.0005"),
        estimated_spread_bps=Decimal("15"),
        entry_slippage_bps=Decimal("10"),
        exit_slippage_bps=Decimal("10"),
        funding_rate_long_per_interval=Decimal("0.0004"),
        funding_interval=timedelta(hours=8),
    )
    request = _request(
        instrument_class=InstrumentClass.PERPETUAL,
        side=TradeSide.LONG,
        liquidity_role=LiquidityRole.TAKER,
        quantity=Decimal("10"),
        entry_price=Decimal("100"),
        exit_price=Decimal("100.50"),  # small positive move -> positive gross_pnl
        holding_period=timedelta(hours=24),  # 3 funding intervals
    )

    result = calculate_trade_costs(request, schedule)

    assert result.gross_pnl == Decimal("5.00")
    assert result.gross_pnl > 0
    total_costs = result.fees + result.spread_cost + result.slippage_cost + result.funding_or_swap
    assert total_costs > result.gross_pnl
    assert result.net_pnl < 0


# --- validation ----------------------------------------------------------------------


def test_schedule_rejects_negative_fee_rate() -> None:
    with pytest.raises(ValueError, match="maker_fee_rate must be non-negative"):
        _schedule(maker_fee_rate=Decimal("-0.0001"))


def test_schedule_rejects_non_positive_funding_interval() -> None:
    with pytest.raises(ValueError, match="funding_interval must be positive"):
        _schedule(funding_interval=timedelta(0))


def test_request_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError, match="quantity must be positive"):
        _request(quantity=Decimal("0"))


def test_request_rejects_crossed_entry_quote() -> None:
    with pytest.raises(ValueError, match="entry_bid cannot exceed entry_ask"):
        _request(entry_bid=Decimal("101"), entry_ask=Decimal("100"))


def test_request_rejects_negative_holding_period() -> None:
    with pytest.raises(ValueError, match="holding_period cannot be negative"):
        _request(holding_period=timedelta(hours=-1))


# --- instrument_class cross-check (request vs. schedule) ----------------------------


def test_mismatched_instrument_class_raises_value_error() -> None:
    schedule = _schedule(instrument_class=InstrumentClass.PERPETUAL)
    request = _request(instrument_class=InstrumentClass.CFD)

    with pytest.raises(ValueError, match="instrument_class mismatch"):
        calculate_trade_costs(request, schedule)


def test_matched_cfd_request_against_cfd_schedule_computes_normally() -> None:
    schedule = _schedule(
        instrument_class=InstrumentClass.CFD,
        maker_fee_rate=Decimal("0"),
        taker_fee_rate=Decimal("0"),
        commission_rate=Decimal("0.0001"),
        swap_rate_long_per_day=Decimal("0.00002"),
    )
    request = _request(
        instrument_class=InstrumentClass.CFD,
        side=TradeSide.LONG,
        quantity=Decimal("1"),
        entry_price=Decimal("1000"),
        exit_price=Decimal("1010"),
        holding_period=timedelta(days=1),
    )

    result = calculate_trade_costs(request, schedule)

    assert result.gross_pnl == Decimal("10")
    assert result.commission == Decimal("0.201")  # 0.0001 * (1000 + 1010)
    assert result.funding_or_swap == Decimal("0.02")  # 0.00002 * 1000 * 1 day
    assert result.net_pnl == (
        result.gross_pnl - result.fees - result.spread_cost - result.slippage_cost
        - result.funding_or_swap
    )
