"""Minimal real-Nautilus wiring for adapter tests (no TradingNode, no network, no MT5).

`Harness` owns a real MessageBus + Cache + LiveClock and an asyncio loop that tests pump
explicitly (`pump()`), so nothing depends on wall-clock sleeps. Endpoints the clients talk
to (`DataEngine.process`, `ExecEngine.process`, ...) are captured so tests can assert on the
exact Nautilus events/reports the adapter produced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import numpy as np
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId

RATES_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<u8"),
        ("spread", "<i4"),
        ("real_volume", "<u8"),
    ]
)


def make_rates(rows: list[tuple]) -> np.ndarray:
    arr = np.zeros(len(rows), dtype=RATES_DTYPE)
    for i, row in enumerate(rows):
        arr[i] = row
    return arr


class Harness:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.clock = LiveClock()
        self.msgbus = MessageBus(trader_id=TraderId("TESTER-001"), clock=self.clock)
        self.cache = Cache()
        self.captured: dict[str, list[Any]] = {}

    def capture(self, endpoint: str) -> list[Any]:
        bucket: list[Any] = self.captured.setdefault(endpoint, [])
        self.msgbus.register(endpoint=endpoint, handler=bucket.append)
        return bucket

    def run(self, coro) -> Any:
        return self.loop.run_until_complete(coro)

    def pump(self, seconds: float = 0.02) -> None:
        self.loop.run_until_complete(asyncio.sleep(seconds))

    def until(self, predicate: Callable[[], bool], timeout: float = 2.0) -> None:
        async def waiter() -> None:
            end = self.loop.time() + timeout
            while not predicate():
                if self.loop.time() > end:
                    raise AssertionError("condition not reached")
                await asyncio.sleep(0.005)

        self.loop.run_until_complete(waiter())

    def close(self) -> None:
        pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()
