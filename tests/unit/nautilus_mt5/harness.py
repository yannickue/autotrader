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


# ----------------------------------------------------------------------
# Full execution wiring: real Nautilus ExecutionEngine + Portfolio + Cache + our client.
# ----------------------------------------------------------------------

import pathlib  # noqa: E402
from decimal import Decimal  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from nautilus_trader.common.factories import OrderFactory  # noqa: E402
from nautilus_trader.core.uuid import UUID4  # noqa: E402
from nautilus_trader.execution.engine import ExecutionEngine  # noqa: E402
from nautilus_trader.execution.messages import SubmitOrder, SubmitOrderList  # noqa: E402
from nautilus_trader.model.enums import OrderSide, TimeInForce  # noqa: E402
from nautilus_trader.model.identifiers import InstrumentId, StrategyId  # noqa: E402
from nautilus_trader.model.objects import Price, Quantity  # noqa: E402
from nautilus_trader.model.orders import OrderList  # noqa: E402
from nautilus_trader.portfolio.portfolio import Portfolio  # noqa: E402

from adapters.config import MT5ConnectionConfig  # noqa: E402
from nautilus_mt5.execution_client import (  # noqa: E402
    Mt5ExecClientConfig,
    Mt5LiveExecutionClient,
)
from nautilus_mt5.executor import Mt5Executor  # noqa: E402
from nautilus_mt5.instruments import InstrumentAssumptions, Mt5InstrumentProvider  # noqa: E402
from nautilus_mt5.session import Mt5Session  # noqa: E402
from nautilus_mt5.state import Mt5StateStore  # noqa: E402

IID = InstrumentId.from_str("GER40.ACTIVTRADES")
TRADER = TraderId("TESTER-001")
STRATEGY = StrategyId("S-001")
SERVER_T0 = 1_790_000_000  # a server-clock epoch inside CEST (no DST ambiguity)


class ExecHarness(Harness):
    def __init__(
        self,
        broker: Any,
        tmp_path: pathlib.Path,
        *,
        cfg: Mt5ExecClientConfig | None = None,
        state_name: str = "state.db",
        connect: bool = True,
        live_engine: bool = False,
        lane: bool = False,
    ) -> None:
        super().__init__()
        self.broker = broker
        broker.server_time = SERVER_T0
        self.portfolio = Portfolio(self.msgbus, self.cache, self.clock)
        if live_engine:  # only for Nautilus' own reconciliation entry points (mass status)
            from nautilus_trader.config import LiveExecEngineConfig
            from nautilus_trader.live.execution_engine import LiveExecutionEngine

            self.engine = LiveExecutionEngine(
                self.loop,
                self.msgbus,
                self.cache,
                self.clock,
                LiveExecEngineConfig(reconciliation=False),
            )
        else:
            self.engine = ExecutionEngine(self.msgbus, self.cache, self.clock)
            self.engine.start()
        self.lane = Mt5Executor() if lane else None
        self.session = Mt5Session(
            broker,
            MT5ConnectionConfig(
                login=broker.cfg.login, password="x", server="s", terminal_path="t"
            ),
            lock_path=tmp_path / "mt5.lock",
            lane=self.lane,
        )
        self.provider = Mt5InstrumentProvider(
            broker,
            assumptions=InstrumentAssumptions(
                margin_init=Decimal("0.05"), margin_maint=Decimal("0.05")
            ),
        )
        self.store = Mt5StateStore(tmp_path / state_name)
        self.client = Mt5LiveExecutionClient(
            self.loop,
            self.msgbus,
            self.cache,
            self.clock,
            self.provider,
            self.session,
            self.store,
            cfg or Mt5ExecClientConfig(autostart_sync=False),
            now=lambda: self.session.time_policy.server_epoch_to_utc(broker.server_time),
        )
        self.engine.register_client(self.client)
        self.factory = OrderFactory(TRADER, STRATEGY, self.clock)
        if connect:
            self.connect()

    def connect(self) -> None:
        self.run(self.client._connect())
        self.client._set_connected(True)

    # -- order helpers ------------------------------------------------------------------

    def _add(self, order: Any) -> Any:
        self.cache.add_order(order, None)
        return order

    def market(
        self,
        side: OrderSide,
        qty: str,
        *,
        reduce_only: bool = False,
        tif: TimeInForce = TimeInForce.IOC,
    ) -> Any:
        return self._add(
            self.factory.market(
                IID, side, Quantity.from_str(qty), time_in_force=tif, reduce_only=reduce_only
            )
        )

    def stop(self, side: OrderSide, qty: str, trigger: float) -> Any:
        return self._add(
            self.factory.stop_market(
                IID, side, Quantity.from_str(qty), Price(trigger, 2), reduce_only=True
            )
        )

    def submit(self, order: Any) -> None:
        cmd = SubmitOrder(
            TRADER, STRATEGY, order, UUID4(), self.clock.timestamp_ns(), client_id=self.client.id
        )
        self.engine.execute(cmd)
        self.pump(0.02)

    def submit_bracket(self, side: OrderSide, qty: str, sl: float) -> tuple[Any, Any]:
        entry = self.factory.market(
            IID, side, Quantity.from_str(qty), time_in_force=TimeInForce.IOC
        )
        stop_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        stop = self.factory.stop_market(
            IID, stop_side, Quantity.from_str(qty), Price(sl, 2), reduce_only=True
        )
        order_list = OrderList(self.factory.generate_order_list_id(), [entry, stop])
        self._add(entry)
        self._add(stop)
        cmd = SubmitOrderList(
            TRADER,
            STRATEGY,
            order_list,
            UUID4(),
            self.clock.timestamp_ns(),
            client_id=self.client.id,
        )
        self.engine.execute(cmd)
        self.pump(0.02)
        return entry, stop

    # -- observations -------------------------------------------------------------------------

    def order(self, order: Any) -> Any:
        return self.cache.order(order.client_order_id)

    def net_qty(self) -> Decimal:
        return sum((Decimal(str(p.signed_qty)) for p in self.cache.positions_open()), Decimal(0))

    def denial(self, order: Any) -> str | None:
        for line in self.client.audit:
            if str(order.client_order_id) in line and ("DENIED" in line or "REJECTED" in line):
                return line
        return None

    def shutdown(self) -> None:
        if self.lane is not None:
            self.run(self.lane.run(self.session.disconnect))  # loop must keep running
            self.lane.shutdown()
        else:
            self.session.disconnect()  # releases the single-owner lock; idempotent
        self.store.close()
        self.close()


def fake_command(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)
