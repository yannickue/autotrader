"""C6 parity runners: the SAME controlled scenario driven through two independent stacks.

- `LegacyRunner`  : PaperExecutionEngine + Portfolio ledger (REGRESSION ORACLE only).
- `NautilusRunner`: real Nautilus ExecutionEngine + Cache + Portfolio behind the C5 MT5
                    execution client, talking to the stateful fake broker.

Both expose the same tiny surface so scenarios are written once. Fills are fed as BROKER TRUTH
(inbound), with explicit prices/quantities, so the comparison is about economic and safety
semantics -- not about how each stack simulates matching.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from adapters.activtrades_mt5.fake_broker import FakeBrokerConfig, FakeMT5Broker
from costs.models import CostConfidence, InstrumentClass, VenueCostSchedule
from data.models import DataQuality, MarketSnapshot
from execution.models import ExecutionRequest, OrderSide, OrderType, TimeInForce
from execution.paper import EngineMode, ExecutionConfig, PaperExecutionEngine
from portfolio.ledger import Portfolio
from risk.models import ReconciliationSource, ReconciliationState, RiskDecision, RuntimeMode
from tests.unit.nautilus_mt5.conftest import real_symbol_info

NOW = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
INSTRUMENT = "GER40"
BID, ASK = Decimal("25000.00"), Decimal("25001.50")
D = Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class Obs:
    """Economic / safety observation, comparable across stacks."""

    position_qty: Decimal  # signed
    avg_price: Decimal | None
    realized_net: Decimal  # realized PnL after fees
    fees: Decimal
    reconciled: bool
    halted: bool
    entry_status: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------
class LegacyRunner:
    name = "legacy"

    def __init__(self, *, fee_rate: Decimal = D(0), tmp_path: pathlib.Path | None = None) -> None:
        self.fee_rate = fee_rate
        self._config = ExecutionConfig(
            max_request_age=timedelta(seconds=5),
            max_decision_age=timedelta(seconds=5),
            max_quote_age=timedelta(seconds=5),
            max_order_age=timedelta(minutes=30),
            max_spread_bps=D("50"),
            max_slippage_bps=D("50"),
            slippage_bps=D("2"),
        )
        self._schedule = VenueCostSchedule(
            venue="parity",
            instrument_class=InstrumentClass.PERPETUAL,
            cost_confidence=CostConfidence.ESTIMATED,
            maker_fee_rate=fee_rate,
            taker_fee_rate=fee_rate,
        )
        self.portfolio = Portfolio(starting_balance=D("100000"))
        self.engine = PaperExecutionEngine(self._config, self.portfolio, self._schedule)
        self.engine.reconcile({"orders": {}, "positions": {}}, NOW)
        self._n = 0
        self.current: str | None = None
        self._planned: list[tuple[str, Decimal, Decimal]] = []
        self._delivered = 0

    # -- helpers
    def _quote(self) -> MarketSnapshot:
        return MarketSnapshot(
            instrument=INSTRUMENT,
            timestamp=NOW,
            bid=BID,
            ask=ASK,
            last=BID,
            volume=D("1000"),
            volatility=None,
            liquidity=None,
            source="parity",
            quality=DataQuality.LIVE,
        )

    def _submit(self, side: str, qty: str, stop: str | None, reduce_only: bool) -> str:
        self._n += 1
        n = self._n
        decision = RiskDecision(
            decision_id=f"d{n}",
            signal_id=f"s{n}",
            instrument=INSTRUMENT,
            timestamp=NOW,
            approved=True,
            reason="ok",
            quantity=D(qty),
            notional=D(qty) * D("100"),
            leverage=D("1"),
            max_leverage=D("5"),
            risk_budget=D("1000"),
            stop_price=D(stop) if stop else None,
            metadata={"side": side, "reduce_only": reduce_only},
        )
        away = BID - 5 if side == "buy" else ASK + 5  # rests on the book: no simulated fill
        request = ExecutionRequest(
            request_id=f"r{n}",
            risk_decision_id=f"d{n}",
            instrument=INSTRUMENT,
            timestamp=NOW,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            quantity=D(qty),
            order_type=OrderType.LIMIT,
            limit_price=away,
            reduce_only=reduce_only,
            client_order_id=f"c{n}",
            time_in_force=TimeInForce.GTC,
            metadata={},
        )
        result = self.engine.submit(request, decision, self._quote(), NOW)
        self.current = f"c{n}"
        return "ACCEPTED" if result.accepted else f"BLOCKED:{result.reject_code}"

    # -- runner surface
    def place_entry(self, side: str, qty: str, stop: str | None, fills: list) -> str:
        self._planned, self._delivered = [(t, D(p), D(q)) for t, p, q in fills], 0
        return self._submit(side, qty, stop, reduce_only=False)

    def place_reduce(self, side: str, qty: str, fills: list) -> str:
        self._planned, self._delivered = [(t, D(p), D(q)) for t, p, q in fills], 0
        return self._submit(side, qty, None, reduce_only=True)

    def deliver(self, count: int | None = None, repeats: int = 1) -> None:
        end = len(self._planned) if count is None else self._delivered + count
        for trade_id, price, qty in self._planned[self._delivered : end]:
            for _ in range(repeats):
                self.engine.report_fill(self.current, trade_id, price, qty, NOW)
        self._delivered = end

    def deliver_unknown(self, trade_id: str, price: str, qty: str) -> None:
        self.engine.report_fill("c-unknown", trade_id, D(price), D(qty), NOW)

    def set_state(self, runtime: RuntimeMode, recon: ReconciliationState) -> None:
        self.engine.mode = {
            RuntimeMode.READY: EngineMode.READY,
            RuntimeMode.HALTED: EngineMode.HALTED,
        }.get(runtime, EngineMode.RECONCILING)
        self.engine.reconciliation_state = recon
        self.engine.reconciliation_source = (
            ReconciliationSource.VENUE_SNAPSHOT if recon is ReconciliationState.RECONCILED else None
        )

    def venue_state(self, position: Decimal | None = None) -> dict[str, Any]:
        local = self.portfolio.positions
        qty = (
            position
            if position is not None
            else (local[INSTRUMENT].quantity if INSTRUMENT in local else D(0))
        )
        orders = {cid: {} for cid, o in self.engine.orders.items() if not o.is_terminal()}
        return {"orders": orders, "positions": {INSTRUMENT: qty} if qty else {}}

    def reconcile(self, broker_position: Decimal | None = None) -> bool:
        return self.engine.reconcile(self.venue_state(broker_position), NOW)

    def restart(self) -> None:
        checkpoint = self.engine.export_checkpoint()
        state = self.portfolio.export_state()
        self.portfolio = Portfolio(starting_balance=D("100000"))
        self.portfolio.import_state(state)
        self.engine = PaperExecutionEngine(self._config, self.portfolio, self._schedule)
        self.engine.import_checkpoint(checkpoint)

    def obs(self) -> Obs:
        position = self.portfolio.positions.get(INSTRUMENT)
        qty = position.quantity if position else D(0)
        return Obs(
            position_qty=qty,
            avg_price=position.avg_entry_price if position and qty else None,
            realized_net=self.portfolio.realized_pnl - self.portfolio.fees,
            fees=self.portfolio.fees,
            reconciled=self.engine.reconciliation_state is ReconciliationState.RECONCILED,
            halted=self.engine.mode is EngineMode.HALTED,
        )

    def state(self) -> tuple[str, str]:
        return self.engine.mode.value, self.engine.reconciliation_state.value


# ---------------------------------------------------------------------------------------------
class NautilusRunner:
    name = "nautilus"

    def __init__(
        self, tmp_path: pathlib.Path, *, auto_reconcile: bool = True, live_engine: bool = False
    ) -> None:
        from nautilus_mt5.execution_client import Mt5ExecClientConfig
        from tests.unit.nautilus_mt5.harness import ExecHarness

        self._harness_cls = ExecHarness
        self.tmp_path = tmp_path
        self.broker = FakeMT5Broker(real_symbol_info(), FakeBrokerConfig())
        self.broker.bid, self.broker.ask = float(BID), float(ASK)
        self._cfg = Mt5ExecClientConfig(
            autostart_sync=False, auto_reconcile_on_connect=auto_reconcile
        )
        self.h = ExecHarness(self.broker, tmp_path, cfg=self._cfg, live_engine=live_engine)
        self.current: Any = None
        self._planned: list[tuple[str, Decimal, Decimal]] = []
        self._tickets: list[int] = []
        self._delivered = 0

    def close(self) -> None:
        self.h.shutdown()

    def _plan(self, fills: list) -> None:
        self._planned = [(t, D(p), D(q)) for t, p, q in fills]
        self._delivered = 0
        self.broker.progressive = True
        self.broker.fill_plan.append([(float(q), float(p)) for _t, p, q in self._planned])

    def _status(self, order: Any) -> str:
        status = self.h.order(order).status.name
        if status in ("DENIED", "REJECTED"):
            return f"BLOCKED:{self.h.denial(order) or status}"
        return "ACCEPTED"

    def place_entry(self, side: str, qty: str, stop: str | None, fills: list) -> str:
        from nautilus_trader.model.enums import OrderSide

        before = {d.ticket for d in self.broker.deals}
        self._plan(fills)
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
        if stop is None:  # bare market entry (no protective stop)
            order = self.h.market(order_side, qty)
            self.h.submit(order)
        else:
            order, _ = self.h.submit_bracket(order_side, qty, float(stop))
        self.current = order
        self._tickets = sorted(d.ticket for d in self.broker.deals if d.ticket not in before)
        return self._status(order)

    def place_reduce(self, side: str, qty: str, fills: list) -> str:
        from nautilus_trader.model.enums import OrderSide

        before = {d.ticket for d in self.broker.deals}
        self._plan(fills)
        order = self.h.market(
            OrderSide.BUY if side == "buy" else OrderSide.SELL, qty, reduce_only=True
        )
        self.h.submit(order)
        self.current = order
        self._tickets = sorted(d.ticket for d in self.broker.deals if d.ticket not in before)
        return self._status(order)

    def deliver(self, count: int | None = None, repeats: int = 1) -> None:
        end = len(self._planned) if count is None else self._delivered + count
        batch = self._tickets[self._delivered : end]
        self.broker.release_deals(*batch)
        for _ in range(repeats):
            self.h.client.sync_once()
        self.h.pump(0.03)
        self._delivered = end

    def deliver_unknown(self, trade_id: str, price: str, qty: str) -> None:
        self.broker.external_market_fill(is_buy=False, volume=float(qty), price=float(price))
        self.h.client.sync_once()
        self.h.pump(0.05)

    def set_state(self, runtime: RuntimeMode, recon: ReconciliationState) -> None:
        self.h.client.recon.runtime = runtime
        self.h.client.recon.state = recon
        self.h.client.recon.source = (
            ReconciliationSource.VENUE_SNAPSHOT if recon is ReconciliationState.RECONCILED else None
        )

    def reconcile(self, broker_position: Decimal | None = None) -> bool:
        return self.h.client.reconcile().state is ReconciliationState.RECONCILED

    def restart(self) -> None:
        self.h.session.disconnect()
        self.h.store.close()
        self.h.close()
        self.h = self._harness_cls(self.broker, self.tmp_path, cfg=self._cfg)

    def obs(self) -> Obs:
        from nautilus_kernel.queries import closed_position_lifecycles
        from tests.unit.nautilus_mt5.harness import IID

        cache = self.h.cache
        opened = list(cache.positions_open(instrument_id=IID))
        every = closed_position_lifecycles(cache, IID) + opened
        realized = sum((D(str(p.realized_pnl.as_decimal())) for p in every), D(0))
        fees = D(0)
        for p in every:
            for money in p.commissions():
                fees += D(str(money.as_decimal()))
        qty = sum((D(str(p.signed_qty)) for p in opened), D(0))
        avg = D(str(opened[0].avg_px_open)) if opened else None
        recon = self.h.client.recon
        return Obs(
            position_qty=qty,
            avg_price=avg,
            realized_net=realized,
            fees=fees,
            reconciled=recon.state is ReconciliationState.RECONCILED,
            halted=recon.runtime is RuntimeMode.HALTED,
        )

    def state(self) -> tuple[str, str]:
        return self.h.client.recon.runtime.value, self.h.client.recon.state.value
