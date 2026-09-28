"""Standalone transaction cost model.

Public API:
    `costs.models.VenueCostSchedule` -- venue-specific fee/commission/spread/
        funding/swap configuration (the only place fee values may live).
    `costs.models.CostCalculationRequest` -- one completed trade/fill to price.
    `costs.models.CostBreakdown` -- the priced result (gross_pnl, fees,
        spread_cost, slippage_cost, funding_or_swap, net_pnl, plus granular
        detail fields).
    `costs.models.LiquidityRole`, `TradeSide`, `InstrumentClass` -- enums.
    `costs.engine.calculate_trade_costs(request, schedule) -> CostBreakdown`.

This module has no dependency on `src/pipeline`, `src/execution`, or
`src/risk` and performs no venue I/O; it is independently testable and is
wired into the pipeline by a later integration step.
"""

from costs.engine import calculate_trade_costs
from costs.models import (
    CostBreakdown,
    CostCalculationRequest,
    InstrumentClass,
    LiquidityRole,
    TradeSide,
    VenueCostSchedule,
)

__all__ = [
    "CostBreakdown",
    "CostCalculationRequest",
    "InstrumentClass",
    "LiquidityRole",
    "TradeSide",
    "VenueCostSchedule",
    "calculate_trade_costs",
]
