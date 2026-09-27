"""Risk decisions that gate every exposure-increasing execution request."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

MAX_SYSTEM_LEVERAGE = Decimal("20")


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskDecision:
    """Immutable approval or rejection emitted by the risk engine."""

    decision_id: str
    signal_id: str
    instrument: str
    timestamp: datetime
    approved: bool
    reason: str
    quantity: Decimal
    notional: Decimal
    leverage: Decimal
    max_leverage: Decimal
    risk_budget: Decimal
    stop_price: Decimal | None
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.max_leverage > MAX_SYSTEM_LEVERAGE:
            raise ValueError("max_leverage cannot exceed system maximum of 20")
        if self.leverage > self.max_leverage:
            raise ValueError("leverage cannot exceed max_leverage")
