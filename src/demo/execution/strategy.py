"""Thin Nautilus strategy shell for the DEMO executor."""

from __future__ import annotations

from typing import Any

from nautilus_trader.trading.strategy import Strategy

from demo.contracts import TradeIntent
from demo.execution.events import ExecutionEvent
from demo.execution.executor import DemoExecutor


class DemoTraderStrategy(Strategy):
    def __init__(self, *, executor: DemoExecutor) -> None:
        super().__init__()
        self._demo_executor = executor

    def submit_intent(self, intent: TradeIntent) -> list[ExecutionEvent]:
        """Public seam used by the runner after OpportunitySource polling."""
        return self._demo_executor.submit(intent)

    def on_timer(self, event: Any) -> None:
        self._demo_executor.on_clock()
