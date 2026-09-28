"""Deterministic transaction cost calculation.

Pure function(s) of `(CostCalculationRequest, VenueCostSchedule) -> CostBreakdown`.
No I/O, no venue calls, no dependency on `src/pipeline`, `src/execution`, or
`src/risk` -- this module is standalone and independently testable.
"""

from decimal import Decimal

from costs.models import (
    CostBreakdown,
    CostCalculationRequest,
    CostConfidence,
    InstrumentClass,
    LiquidityRole,
    TradeSide,
    VenueCostSchedule,
)

_BPS_DIVISOR = Decimal("10000")
_SECONDS_PER_DAY = Decimal("86400")


def calculate_trade_costs(
    request: CostCalculationRequest,
    schedule: VenueCostSchedule,
) -> CostBreakdown:
    """Price out every cost component of one completed trade/fill.

    All money/price arithmetic is Decimal-only. The result's `net_pnl` is
    `gross_pnl` minus every cost component (a negative `funding_or_swap` is a
    credit and is therefore added back), so a trade with positive
    `gross_pnl` whose costs exceed it correctly produces a negative
    `net_pnl`.

    Raises `ValueError` (fail closed) when `request.instrument_class` does
    not match `schedule.instrument_class` -- a schedule built for one asset
    class (e.g. Sprint 1's crypto-shaped perpetual defaults) must never be
    silently reused to price a different one (e.g. a CFD instrument).
    """
    _require_matching_instrument_class(request, schedule)

    entry_notional = request.quantity * request.entry_price
    exit_notional = request.quantity * request.exit_price

    gross_pnl = _gross_pnl(request)

    exchange_fee = _exchange_fee(request, schedule, entry_notional, exit_notional)
    commission = _commission(schedule, entry_notional, exit_notional)
    fees = exchange_fee + commission

    entry_spread_cost = _spread_cost(
        quantity=request.quantity,
        price=request.entry_price,
        bid=request.entry_bid,
        ask=request.entry_ask,
        fallback_bps=schedule.estimated_spread_bps,
    )
    exit_spread_cost = _spread_cost(
        quantity=request.quantity,
        price=request.exit_price,
        bid=request.exit_bid,
        ask=request.exit_ask,
        fallback_bps=schedule.estimated_spread_bps,
    )
    spread_cost = entry_spread_cost + exit_spread_cost

    entry_slippage_cost = _entry_slippage_cost(request, schedule)
    exit_slippage_cost = _exit_slippage_cost(request, schedule)
    slippage_cost = entry_slippage_cost + exit_slippage_cost

    funding_or_swap = _funding_or_swap(request, schedule, entry_notional)

    net_pnl = gross_pnl - fees - spread_cost - slippage_cost - funding_or_swap

    return CostBreakdown(
        trade_id=request.trade_id,
        cost_confidence=schedule.overall_confidence(),
        gross_pnl=gross_pnl,
        exchange_fee=exchange_fee,
        commission=commission,
        fees=fees,
        entry_spread_cost=entry_spread_cost,
        exit_spread_cost=exit_spread_cost,
        spread_cost=spread_cost,
        entry_slippage_cost=entry_slippage_cost,
        exit_slippage_cost=exit_slippage_cost,
        slippage_cost=slippage_cost,
        funding_or_swap=funding_or_swap,
        net_pnl=net_pnl,
    )


def _require_matching_instrument_class(
    request: CostCalculationRequest,
    schedule: VenueCostSchedule,
) -> None:
    if request.instrument_class is not schedule.instrument_class:
        raise ValueError(
            f"instrument_class mismatch: request {request.trade_id!r} is "
            f"{request.instrument_class} but schedule {schedule.venue!r} is configured for "
            f"{schedule.instrument_class} -- a cost schedule built for one venue/asset class "
            "must never be silently reused to price a different one"
        )


def is_safe_for_risk_decisions(breakdown: CostBreakdown) -> bool:
    """Whether `breakdown` is safe to use for anything that affects real
    risk (sizing, PnL-based halts, live cost attribution), as opposed to
    reporting-only display.

    `UNKNOWN` confidence means at least one contributing cost component had
    no data at all and must not be silently treated as a verified zero.
    """
    return breakdown.cost_confidence is not CostConfidence.UNKNOWN


def _gross_pnl(request: CostCalculationRequest) -> Decimal:
    if request.side is TradeSide.LONG:
        return (request.exit_price - request.entry_price) * request.quantity
    return (request.entry_price - request.exit_price) * request.quantity


def _fee_rate(liquidity_role: LiquidityRole, schedule: VenueCostSchedule) -> Decimal:
    if liquidity_role is LiquidityRole.MAKER:
        return schedule.maker_fee_rate
    return schedule.taker_fee_rate


def _exchange_fee(
    request: CostCalculationRequest,
    schedule: VenueCostSchedule,
    entry_notional: Decimal,
    exit_notional: Decimal,
) -> Decimal:
    rate = _fee_rate(request.liquidity_role, schedule)
    return rate * (entry_notional + exit_notional)


def _commission_for_notional(schedule: VenueCostSchedule, notional: Decimal) -> Decimal:
    raw = schedule.commission_rate * notional + schedule.commission_fixed
    if schedule.commission_minimum is not None and raw < schedule.commission_minimum:
        return schedule.commission_minimum
    return raw


def _commission(
    schedule: VenueCostSchedule,
    entry_notional: Decimal,
    exit_notional: Decimal,
) -> Decimal:
    return _commission_for_notional(schedule, entry_notional + exit_notional)


def calculate_fill_fee(
    *,
    notional: Decimal,
    liquidity_role: LiquidityRole,
    schedule: VenueCostSchedule,
) -> Decimal:
    """Price the exchange fee + commission for a single fill.

    Unlike `calculate_trade_costs` (round-trip: entry + exit), this prices
    exactly one fill's notional -- used by `PaperExecutionEngine` to debit a
    real per-fill fee into the portfolio ledger at the moment each fill is
    applied. Shares `_fee_rate`/`_commission_for_notional` with
    `calculate_trade_costs` so the two never diverge on how a fee/commission
    is computed.
    """
    if not notional.is_finite() or notional < 0:
        raise ValueError("notional must be finite and non-negative")
    exchange_fee = _fee_rate(liquidity_role, schedule) * notional
    commission = _commission_for_notional(schedule, notional)
    return exchange_fee + commission


def _spread_cost(
    *,
    quantity: Decimal,
    price: Decimal,
    bid: Decimal | None,
    ask: Decimal | None,
    fallback_bps: Decimal | None,
) -> Decimal:
    if bid is not None and ask is not None:
        half_spread = (ask - bid) / Decimal("2")
        return half_spread * quantity
    if fallback_bps is not None:
        return price * (fallback_bps / _BPS_DIVISOR) * quantity
    return Decimal("0")


def _entry_slippage_cost(
    request: CostCalculationRequest,
    schedule: VenueCostSchedule,
) -> Decimal:
    expected = request.expected_entry_price
    if expected is not None:
        if request.side is TradeSide.LONG:
            # Buying to open: paying more than expected is a cost.
            return (request.entry_price - expected) * request.quantity
        # Selling to open a short: receiving less than expected is a cost.
        return (expected - request.entry_price) * request.quantity
    if schedule.entry_slippage_bps is not None:
        return request.entry_price * (schedule.entry_slippage_bps / _BPS_DIVISOR) * request.quantity
    return Decimal("0")


def _exit_slippage_cost(
    request: CostCalculationRequest,
    schedule: VenueCostSchedule,
) -> Decimal:
    expected = request.expected_exit_price
    if expected is not None:
        if request.side is TradeSide.LONG:
            # Selling to close: receiving less than expected is a cost.
            return (expected - request.exit_price) * request.quantity
        # Buying to close a short: paying more than expected is a cost.
        return (request.exit_price - expected) * request.quantity
    if schedule.exit_slippage_bps is not None:
        return request.exit_price * (schedule.exit_slippage_bps / _BPS_DIVISOR) * request.quantity
    return Decimal("0")


def _funding_or_swap(
    request: CostCalculationRequest,
    schedule: VenueCostSchedule,
    entry_notional: Decimal,
) -> Decimal:
    if request.instrument_class is InstrumentClass.PERPETUAL:
        interval_seconds = Decimal(str(schedule.funding_interval.total_seconds()))
        holding_seconds = Decimal(str(request.holding_period.total_seconds()))
        intervals = int(holding_seconds // interval_seconds)
        rate = (
            schedule.funding_rate_long_per_interval
            if request.side is TradeSide.LONG
            else schedule.funding_rate_short_per_interval
        )
        return rate * entry_notional * intervals

    if request.instrument_class is InstrumentClass.CFD:
        days_held = Decimal(str(request.holding_period.total_seconds())) / _SECONDS_PER_DAY
        rate = (
            schedule.swap_rate_long_per_day
            if request.side is TradeSide.LONG
            else schedule.swap_rate_short_per_day
        )
        return rate * entry_notional * days_held

    return Decimal("0")
