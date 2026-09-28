"""Standalone margin requirement and liquidation-safety module.

See `margin.engine.MarginEngine` for the entry points and `margin.models`
for the data contracts. Independent of `risk.engine`, `execution`, and
`pipeline` by design (see the module docstrings) so this module can be
developed and tested in isolation; a later integration step wires
`MarginEngine.evaluate_stop_safety` into `RiskEngine._evaluate_inner` as an
additional pre-approval check.
"""

from margin.engine import MarginEngine
from margin.models import (
    LiquidationEstimate,
    MaintenanceMarginPolicy,
    MaintenanceMarginRequirement,
    MarginPositionSide,
    MarginRequirement,
    MarginSafetyReason,
    MarginSafetyResult,
    VenueUncertaintyBuffer,
    VolatilityLeverageCapPolicy,
)

__all__ = [
    "LiquidationEstimate",
    "MaintenanceMarginPolicy",
    "MaintenanceMarginRequirement",
    "MarginEngine",
    "MarginPositionSide",
    "MarginRequirement",
    "MarginSafetyReason",
    "MarginSafetyResult",
    "VenueUncertaintyBuffer",
    "VolatilityLeverageCapPolicy",
]
