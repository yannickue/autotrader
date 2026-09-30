# ruff: noqa: E501
"""Contract between the DEMO runner loop (src/demo/runner.py) and the MT5/Nautilus stack.

The runner is written and tested against ``StackPort`` (with ``FakeStack``); the real
implementation (attach-only ActivTrades DEMO, Nautilus kernel, one MT5 IPC thread) is
``Mt5DemoStack`` in ``src/demo/execution/live.py``. Both MUST satisfy this Protocol exactly.

Fail-closed rule: any method may raise ``StackFailClosed``; the runner then stops taking new
exposure, keeps managing protection/exits if it still can, records the error and exits non-zero.
The stack never retries an exposure-changing request on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from demo.contracts import TradeIntent
from demo.execution.events import ExecutionEvent
from demo.opportunity.bar_source import BarSource


class StackFailClosed(RuntimeError):
    """Non-demo / unknown account / stale feed / unreconciled / disconnect / unprotected exposure."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountSnapshot:
    is_demo: bool
    account_id_hash: str  # never the raw login
    equity: float
    balance: float
    profit: float  # floating P/L
    reconciliation: str  # NOT_RECONCILED | RECONCILING | RECONCILED | MISMATCH
    connected: bool
    open_positions: int
    open_orders: int
    all_positions_protected: bool
    kill_switch: bool
    server_time_utc: datetime | None = None
    extra: dict[str, object] = field(default_factory=dict)


class StackPort(Protocol):
    bar_source: BarSource

    def start(self) -> AccountSnapshot:
        """Attach (never log in), verify DEMO, reconcile from the venue snapshot -> RECONCILED."""

    def account_snapshot(self) -> AccountSnapshot: ...

    def has_position(self, market: str) -> bool:
        """True if the venue (broker truth) holds or is opening a position on ``market``."""

    def submit(self, intent: TradeIntent) -> list[ExecutionEvent]:
        """Risk-size and send one ACCEPTED intent (entry + mandatory broker stop [+TP]).
        Exactly-once per ``intent_id``. In shadow mode it must never reach order_send."""

    def poll_events(self) -> list[ExecutionEvent]:
        """Broker-side events since the last call (stop/target exits, late fills, external closes)."""

    def on_clock(self, now: datetime) -> list[ExecutionEvent]:
        """Forced-flat handling at ``intent.forced_flat_utc``; returns close events."""

    def open_intents(self) -> Sequence[str]:
        """intent_ids the stack believes are open (for restart adoption / reconciliation)."""

    def halt_new_exposure(self, reason: str) -> None: ...

    def stop(self) -> None: ...
