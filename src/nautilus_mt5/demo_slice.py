"""C7 first ActivTrades DEMO vertical slice (execution / reconciliation proof, NOT alpha).

    real MT5 quotes -> Nautilus Strategy -> RiskPolicy/PositionSizer (via NautilusRiskBridge)
    -> Nautilus bracket order (market entry + broker-side stop) -> MT5 ExecutionClient
    -> demo fill -> Nautilus position -> controlled reduce-only close -> Nautilus PnL
    -> fresh VENUE_SNAPSHOT reconciliation

The Strategy is a one-shot state machine (WAIT_QUOTE -> ENTRY -> HOLD -> EXIT -> DONE); it has no
signal logic. Everything is assembled from REAL Nautilus components (MessageBus, Cache, Portfolio,
DataEngine, RiskEngine, ExecutionEngine, Trader); only the MT5 client object is injected, so the
same code runs against `FakeMT5Broker` in pytest and against the real terminal via
`scripts/c7_demo_vertical_slice.py` (which alone may obtain the real client).

STOP conditions (any => no entry / abort, never a retry of an exposure-changing request):
non-demo account, wrong/mismatching account, non-netting mode, non-flat book, stale quote, wide
spread, unreconciled adapter, risk rejection or a non-minimum quantity, lane/lock failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.engine import DataEngine
from nautilus_trader.execution.engine import ExecutionEngine
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled, PositionClosed
from nautilus_trader.model.identifiers import InstrumentId, StrategyId, TraderId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.orders import OrderList
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.risk.engine import RiskEngine
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.trading.trader import Trader

from nautilus_kernel.risk_bridge import MarketInputs, NautilusRiskBridge
from nautilus_mt5.data_client import Mt5DataClientConfig
from nautilus_mt5.execution_client import Mt5ExecClientConfig
from nautilus_mt5.factory import Mt5Adapter, build_mt5_adapter
from nautilus_mt5.instruments import InstrumentAssumptions
from nautilus_mt5.symbols import GER40
from risk.models import ReconciliationState, RiskPolicy, RiskSide
from risk.policy import PolicyRejection

ZERO = Decimal(0)
IID = GER40.instrument_id


class SliceAbort(RuntimeError):
    """A STOP condition fired. Broker execution must not continue."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DemoSliceConfig:
    stop_distance: Decimal = Decimal("40")  # index points below the ask; 0.25 lot => ~10 EUR risk
    max_spread: Decimal = Decimal("6")  # index points (observed 1.7-1.9)
    max_quote_age_s: float = 15.0
    hold_seconds: float = 8.0
    fill_timeout_s: float = 25.0
    close_timeout_s: float = 25.0
    min_quantity: Decimal = Decimal("0.25")


def demo_risk_policy() -> RiskPolicy:
    """Explicit DEMO-PROOF limits (not a trading policy). Sized so the risk-approved quantity is
    the broker minimum lot on a small demo balance: the exposure caps bind, not the 30x ceiling."""
    return RiskPolicy(
        policy_id="c7-demo-proof-v1",
        risk_fraction=Decimal("0.03"),
        max_leverage=Decimal("30"),
        max_gross_notional=Decimal("8000"),
        max_net_notional=Decimal("8000"),
        max_daily_loss=Decimal("100"),
        max_drawdown=Decimal("150"),
        max_consecutive_losses=2,
        liquidity_fraction=Decimal("0.5"),
        max_data_age=timedelta(seconds=30),
        max_signal_age=timedelta(seconds=30),
        estimated_cost_bps=Decimal("3"),
    )


@dataclass(slots=True)
class SliceMeasurements:
    events: list[dict[str, Any]] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    state: str = "WAIT_QUOTE"
    failure: str | None = None

    def note(self, event: str, **fields: Any) -> None:
        self.events.append({"t_ns": time.time_ns(), "event": event, **fields})


class DemoStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    stop_distance: float = 40.0
    max_spread: float = 6.0
    max_quote_age_s: float = 15.0
    hold_seconds: float = 8.0


class DemoProofStrategy(Strategy):
    """One-shot execution proof. Decisions go through the RiskPolicy/PositionSizer bridge."""

    def __init__(
        self,
        config: DemoStrategyConfig,
        *,
        bridge: NautilusRiskBridge,
        measurements: SliceMeasurements,
        done: asyncio.Event,
        now_utc: Any,
    ) -> None:
        super().__init__(config)
        self._cfg = config
        self._bridge = bridge
        self._m = measurements
        self._done = done
        self._now_utc = now_utc
        self._instrument: Any = None
        self._entry_id: Any = None

    def on_start(self) -> None:
        self._instrument = self.cache.instrument(self._cfg.instrument_id)
        if self._instrument is None:
            self._fail("INSTRUMENT_NOT_IN_CACHE")
            return
        self.subscribe_quote_ticks(self._cfg.instrument_id)
        self._m.note("strategy_started")

    def _fail(self, reason: str) -> None:
        self._m.state, self._m.failure = "FAILED", reason
        self._m.note("failed", reason=reason)
        self._done.set()

    # -- entry -----------------------------------------------------------------------------

    def on_quote_tick(self, tick: QuoteTick) -> None:
        if self._m.state != "WAIT_QUOTE":
            return
        now = self._now_utc()
        age = (now.timestamp() * 1e9 - tick.ts_event) / 1e9
        bid, ask = Decimal(str(tick.bid_price)), Decimal(str(tick.ask_price))
        if age > self._cfg.max_quote_age_s:
            return  # keep waiting for a fresh quote; the runner enforces the overall timeout
        if ask - bid > Decimal(str(self._cfg.max_spread)):
            return
        self._m.state = "DECIDING"
        self._m.values["decision_bid"], self._m.values["decision_ask"] = str(bid), str(ask)
        self._m.values["quote_age_s"] = round(age, 2)
        stop = (bid - Decimal(str(self._cfg.stop_distance))).quantize(Decimal("0.01"))
        decision = self._bridge.evaluate_entry(
            portfolio=self.portfolio,
            cache=self.cache,
            market=MarketInputs(now=now, bid=bid, ask=ask, volume=ZERO),
            side=RiskSide.BUY,
            stop_price=stop,
            signal_id=f"c7-{time.time_ns()}",
        )
        if isinstance(decision, PolicyRejection):
            return self._fail(f"RISK_REJECTED:{decision.reason_code}")
        self._m.values["risk"] = {
            "quantity": str(decision.quantity),
            "notional": str(decision.notional),
            "leverage": str(decision.leverage),
            "max_leverage": str(decision.max_leverage),
            "reference_price": str(decision.reference_price),
            "binding_constraint": decision.metadata["binding_constraint"],
            "risk_budget": str(decision.risk_budget),
        }
        if decision.quantity != Decimal(str(self._cfg_min())):
            return self._fail(f"NOT_MINIMUM_LOT:{decision.quantity}")
        quantity = self._instrument.make_qty(float(decision.quantity))
        entry = self.order_factory.market(
            instrument_id=self._cfg.instrument_id,
            order_side=OrderSide.BUY,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
        )
        protective = self.order_factory.stop_market(
            instrument_id=self._cfg.instrument_id,
            order_side=OrderSide.SELL,
            quantity=quantity,
            trigger_price=Price(float(stop), self._instrument.price_precision),
            reduce_only=True,
        )
        self._entry_id = entry.client_order_id
        self._m.values["expected"] = {
            "entry_ask": str(ask),
            "stop": str(stop),
            "qty": str(quantity),
        }
        self._m.state = "ENTRY_SUBMITTED"
        self._m.values["submit_ns"] = time.time_ns()
        self._m.note("entry_submitted", client_order_id=str(entry.client_order_id))
        self.submit_order_list(
            OrderList(self.order_factory.generate_order_list_id(), [entry, protective])
        )

    def _cfg_min(self) -> Decimal:
        return Decimal("0.25")

    # -- lifecycle events --------------------------------------------------------------------

    def on_order_filled(self, event: OrderFilled) -> None:
        self._m.note(
            "order_filled",
            client_order_id=str(event.client_order_id),
            side=event.order_side.name,
            qty=str(event.last_qty),
            px=str(event.last_px),
            trade_id=str(event.trade_id),
            commission=str(event.commission),
        )
        if event.client_order_id == self._entry_id and self._m.state == "ENTRY_SUBMITTED":
            self._m.state = "HOLD"
            self._m.values["entry_fill_ns"] = time.time_ns()
            self._m.values["entry_fill_px"] = str(event.last_px)
            self._m.values["entry_fill_qty"] = str(event.last_qty)
            self._m.values["entry_trade_id"] = str(event.trade_id)
            self.clock.set_time_alert(
                name="c7-hold-done",
                alert_time=self.clock.utc_now() + timedelta(seconds=self._cfg.hold_seconds),
                callback=self._on_hold_done,
            )

    def _on_hold_done(self, _event: Any) -> None:
        positions = self.cache.positions_open(instrument_id=self._cfg.instrument_id)
        if not positions:
            return self._fail("NO_POSITION_AT_EXIT")
        self._m.state = "EXIT_SUBMITTED"
        self._m.values["exit_submit_ns"] = time.time_ns()
        self._m.note("exit_submitted", position_id=str(positions[0].id))
        self.close_position(positions[0], tags=["c7:controlled-close"])

    def on_position_closed(self, event: PositionClosed) -> None:
        self._m.values["nautilus_realized_pnl"] = str(event.realized_pnl)
        self._m.values["exit_fill_ns"] = time.time_ns()
        self._m.state = "DONE"
        self._m.note("position_closed", realized_pnl=str(event.realized_pnl))
        self.log.info(f"C7 position closed pnl={event.realized_pnl}", color=LogColor.BLUE)
        self._done.set()

    def on_order_denied(self, event: Any) -> None:
        self._fail(f"ORDER_DENIED:{event.reason}")

    def on_order_rejected(self, event: Any) -> None:
        self._fail(f"ORDER_REJECTED:{event.reason}")

    def on_stop(self) -> None:
        self.unsubscribe_quote_ticks(self._cfg.instrument_id)


@dataclass(slots=True)
class Assembled:
    adapter: Mt5Adapter
    loop: asyncio.AbstractEventLoop
    msgbus: MessageBus
    cache: Cache
    clock: LiveClock
    portfolio: Portfolio
    data_engine: DataEngine
    risk_engine: RiskEngine
    exec_engine: ExecutionEngine
    trader: Trader | None = None


def assemble(
    *,
    client: Any,
    connection: Any,
    loop: asyncio.AbstractEventLoop,
    state_path: Any,
    lock_path: Any = None,
    exec_config: Mt5ExecClientConfig | None = None,
    data_config: Mt5DataClientConfig | None = None,
    now: Any = None,
) -> Assembled:
    clock = LiveClock()
    trader_id = TraderId("C7-DEMO-001")
    msgbus = MessageBus(trader_id=trader_id, clock=clock)
    cache = Cache()
    portfolio = Portfolio(msgbus, cache, clock)
    data_engine = DataEngine(msgbus, cache, clock)
    risk_engine = RiskEngine(portfolio, msgbus, cache, clock)
    exec_engine = ExecutionEngine(msgbus, cache, clock)
    adapter = build_mt5_adapter(
        client=client,
        connection=connection,
        assumptions=InstrumentAssumptions(  # broker does not report margin rates: labelled ASSUMED
            margin_init=Decimal("0.05"), margin_maint=Decimal("0.05")
        ),
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        state_path=state_path,
        lock_path=lock_path,
        exec_config=exec_config,
        data_config=data_config,
    )
    if now is not None:
        adapter.exec_client._now_fn = now
    data_engine.register_client(adapter.data_client)
    exec_engine.register_client(adapter.exec_client)
    return Assembled(
        adapter=adapter,
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        portfolio=portfolio,
        data_engine=data_engine,
        risk_engine=risk_engine,
        exec_engine=exec_engine,
    )


async def connect_and_verify(asm: Assembled, *, require_demo: bool = True) -> dict[str, Any]:
    """Connect both clients and evaluate every pre-entry STOP condition."""
    adapter = asm.adapter
    await adapter.exec_client._connect()
    adapter.exec_client._set_connected(True)
    await adapter.data_client._connect()
    adapter.data_client._set_connected(True)
    return await adapter.exec_client._lane_run(_preconditions, adapter, require_demo)


def _preconditions(adapter: Mt5Adapter, require_demo: bool) -> dict[str, Any]:
    session = adapter.session
    client = session.client
    account = session.call("account_info", client.account_info)
    facts: dict[str, Any] = {
        "trade_mode": int(account.trade_mode),
        "currency": str(account.currency),
        "leverage": int(account.leverage),
        "margin_mode": int(account.margin_mode),
        "balance": float(account.balance),
    }
    if require_demo and int(account.trade_mode) != 0:
        raise SliceAbort("STOP: account is not a DEMO account (trade_mode != 0)")
    session.account_mode()  # raises unless RETAIL_NETTING
    positions = session.call("positions_get", client.positions_get)
    orders = session.call("orders_get", client.orders_get)
    facts["positions"], facts["working_orders"] = len(positions), len(orders)
    if positions or orders:
        raise SliceAbort(f"STOP: book not flat ({len(positions)} positions, {len(orders)} orders)")
    recon = adapter.exec_client.recon
    facts["reconciliation"] = f"{recon.state.value}/{recon.source.value if recon.source else None}"
    if recon.state is not ReconciliationState.RECONCILED:
        raise SliceAbort(f"STOP: adapter not RECONCILED at start: {recon.state.value}")
    return facts


async def run_slice(
    asm: Assembled,
    cfg: DemoSliceConfig,
    *,
    now_utc: Any = None,
) -> SliceMeasurements:
    """Run the one-shot strategy inside the assembled Nautilus kernel. Caller has already run
    `connect_and_verify`. Always ends flat-or-loud: on any abnormal end a still-open position is
    reported for MANUAL action (no automatic retry of exposure-changing requests)."""
    m = SliceMeasurements()
    done = asyncio.Event()
    now_utc = now_utc or (lambda: datetime.now(UTC))
    bridge = NautilusRiskBridge(instrument_id=IID, policy=demo_risk_policy(), limits=_limits())
    strategy = DemoProofStrategy(
        DemoStrategyConfig(
            instrument_id=IID,
            stop_distance=float(cfg.stop_distance),
            max_spread=float(cfg.max_spread),
            max_quote_age_s=cfg.max_quote_age_s,
            hold_seconds=cfg.hold_seconds,
        ),
        bridge=bridge,
        measurements=m,
        done=done,
        now_utc=now_utc,
    )
    trader = Trader(
        trader_id=TraderId("C7-DEMO-001"),
        instance_id=UUID4(),
        msgbus=asm.msgbus,
        cache=asm.cache,
        portfolio=asm.portfolio,
        data_engine=asm.data_engine,
        risk_engine=asm.risk_engine,
        exec_engine=asm.exec_engine,
        clock=asm.clock,
        environment=_live_env(),
    )
    asm.trader = trader
    for engine in (asm.data_engine, asm.risk_engine, asm.exec_engine):
        engine.start()
    trader.add_strategy(strategy)
    started = time.perf_counter()
    stop_sampling = asyncio.Event()
    sampler_task = asyncio.create_task(sampler(asm, m, stop_sampling))
    trader.start()
    try:
        budget = cfg.fill_timeout_s + cfg.hold_seconds + cfg.close_timeout_s + 30
        await asyncio.wait_for(done.wait(), timeout=budget)
    except TimeoutError:
        m.failure = m.failure or "TIMEOUT"
        m.state = "TIMEOUT"
    finally:
        trader.stop()
        await asyncio.sleep(0.2)
        stop_sampling.set()
        await sampler_task
    m.values["elapsed_s"] = round(time.perf_counter() - started, 2)
    return m


def _live_env() -> Any:
    from nautilus_trader.common import Environment

    return Environment.LIVE


def _limits() -> Any:
    from risk.models import InstrumentRiskLimits

    return InstrumentRiskLimits(
        instrument="GER40",
        max_leverage=Decimal("30"),
        quantity_step=Decimal("0.25"),
        min_quantity=Decimal("0.25"),
        min_notional=ZERO,
        max_notional=Decimal("100000"),
        max_spread_bps=Decimal("10"),
        maintenance_margin_rate=Decimal("0.025"),
    )


def strategy_id_for_reports() -> StrategyId:
    return StrategyId("DemoProofStrategy-000")


# ---------------------------------------------------------------------------------------------
# Measurements: broker-side facts, expected-vs-actual, verdict (no credentials anywhere)
# ---------------------------------------------------------------------------------------------


def _iso_from_server_epoch(adapter: Mt5Adapter, epoch: float) -> str:
    try:
        return adapter.session.time_policy.server_epoch_to_utc(epoch).isoformat()
    except Exception:  # ambiguous DST hour: keep the raw server-clock value, flagged
        return f"server-clock:{int(epoch)}"


def collect_broker_facts(adapter: Mt5Adapter, since_utc: datetime) -> dict[str, Any]:
    """Runs ON THE MT5 LANE. Read-only: account, book, tick, deals since `since_utc`."""
    session = adapter.session
    client = session.client
    policy = session.time_policy
    account = session.call("account_info", client.account_info)
    tick = session.call("symbol_info_tick", client.symbol_info_tick, "Ger40")
    positions = session.call("positions_get", client.positions_get)
    orders = session.call("orders_get", client.orders_get)
    start = policy.utc_to_request_datetime(since_utc - timedelta(minutes=1))
    end = policy.utc_to_request_datetime(datetime.now(UTC) + timedelta(days=1))
    deals = session.call("history_deals_get", client.history_deals_get, start, end)
    return {
        "account": {
            "balance": float(account.balance),
            "equity": float(account.equity),
            "margin": float(account.margin),
            "margin_free": float(account.margin_free),
            "profit": float(account.profit),
        },
        "tick": {
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "spread": round(float(tick.ask) - float(tick.bid), 2),
        },
        "positions": [
            {
                "ticket": int(x.ticket),
                "volume": float(x.volume),
                "type": int(x.type),
                "price_open": float(x.price_open),
                "sl": float(x.sl),
                "tp": float(x.tp),
                "swap": float(x.swap),
                "profit": float(x.profit),
            }
            for x in positions
        ],
        "working_orders": len(orders),
        "deals": [
            {
                "ticket": int(d.ticket),
                "order": int(d.order),
                "position_id": int(d.position_id),
                "type": int(d.type),
                "entry": int(d.entry),
                "volume": float(d.volume),
                "price": float(d.price),
                "commission": float(d.commission),
                "swap": float(d.swap),
                "profit": float(d.profit),
                "fee": float(getattr(d, "fee", 0.0)),
                "reason": int(getattr(d, "reason", 0)),
                "symbol": str(d.symbol),
                "time_utc": _iso_from_server_epoch(adapter, d.time),
            }
            for d in deals
            if str(d.symbol) == "Ger40"
        ],
    }


def expected_margin(adapter: Mt5Adapter, price: float, volume: float) -> float | None:
    """order_calc_margin for a BUY (read-only estimate from the terminal itself)."""
    client = adapter.session.client
    return adapter.session.call(
        "order_calc_margin", client.order_calc_margin, 0, "Ger40", volume, price, none_ok=True
    )


async def sampler(asm: Assembled, m: SliceMeasurements, stop: asyncio.Event) -> None:
    """While the slice runs, sample broker account/positions (peak margin, attached SL, tickets)."""
    samples: list[dict[str, Any]] = []
    m.values["samples"] = samples
    started = datetime.now(UTC)
    while not stop.is_set():
        try:
            facts = await asm.adapter.exec_client._lane_run(
                collect_broker_facts, asm.adapter, started
            )
            samples.append(
                {
                    "t_ns": time.time_ns(),
                    "margin": facts["account"]["margin"],
                    "positions": facts["positions"],
                    "tick": facts["tick"],
                }
            )
            del samples[:-40]
        except Exception as exc:  # sampling is best-effort; never affects execution
            samples.append({"t_ns": time.time_ns(), "error": repr(exc)})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=0.15)


def build_report(
    m: SliceMeasurements,
    facts: dict[str, Any],
    *,
    reconciliation: dict[str, Any],
    nautilus_state: dict[str, Any],
    expected_margin_eur: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    v = m.values
    deals = facts["deals"]
    entry_deals = [d for d in deals if d["entry"] == 0]
    exit_deals = [d for d in deals if d["entry"] in (1, 3)]
    samples = [x for x in v.get("samples", []) if x.get("positions")]
    sample = samples[-1] if samples else None
    peak_margin = max((x["margin"] for x in v.get("samples", []) if "margin" in x), default=None)
    expected = v.get("expected", {})
    entry_px = float(v["entry_fill_px"]) if "entry_fill_px" in v else None
    ask = float(expected["entry_ask"]) if "entry_ask" in expected else None
    broker_profit = sum(d["profit"] for d in exit_deals)
    commissions = sum(d["commission"] + d["fee"] for d in deals)
    swap = sum(d["swap"] for d in deals)
    nautilus_pnl = v.get("nautilus_realized_pnl")
    nautilus_pnl_value = float(str(nautilus_pnl).split()[0]) if nautilus_pnl else None
    report: dict[str, Any] = {
        "mode": "DRY_RUN (order_check only, NO order_send)" if dry_run else "LIVE DEMO",
        "final_state": m.state,
        "failure": m.failure,
        "risk_decision": v.get("risk"),
        "expected_vs_actual": {
            "expected_entry_ask": ask,
            "actual_entry_fill": entry_px,
            "entry_slippage_points": None
            if ask is None or entry_px is None
            else round(entry_px - ask, 2),
            "decision_spread": None
            if "decision_ask" not in v
            else round(float(v["decision_ask"]) - float(v["decision_bid"]), 2),
            "spread_at_end": facts["tick"]["spread"],
            "expected_quantity": expected.get("qty"),
            "broker_quantity": entry_deals[0]["volume"] if entry_deals else None,
            "expected_stop": expected.get("stop"),
            "broker_stop": sample["positions"][0]["sl"] if sample else None,
            "expected_margin_eur": expected_margin_eur,
            "broker_peak_margin_eur": peak_margin,
            "nautilus_realized_pnl": nautilus_pnl,
            "broker_realized_profit": round(broker_profit, 2) if exit_deals else None,
            "submit_to_entry_fill_ms": None
            if "entry_fill_ns" not in v
            else round((v["entry_fill_ns"] - v["submit_ns"]) / 1e6, 1),
            "exit_submit_to_close_ms": None
            if "exit_fill_ns" not in v or "exit_submit_ns" not in v
            else round((v["exit_fill_ns"] - v["exit_submit_ns"]) / 1e6, 1),
            "commissions_eur": round(commissions, 4),
            "swap_eur": round(swap, 4),
        },
        "broker": {
            "position_ticket": sample["positions"][0]["ticket"] if sample else None,
            "order_tickets": sorted({d["order"] for d in deals}),
            "deal_tickets": [d["ticket"] for d in deals],
            "deals": deals,
            "final_positions": facts["positions"],
            "final_working_orders": facts["working_orders"],
            "account_after": facts["account"],
        },
        "nautilus": nautilus_state,
        "reconciliation": reconciliation,
        "events": m.events,
    }
    ok_flat = not facts["positions"] and facts["working_orders"] == 0
    report["verdict"] = {
        "entry_and_close_filled": m.state == "DONE",
        "minimum_lot": bool(entry_deals) and entry_deals[0]["volume"] == 0.25,
        "broker_stop_matches_expectation": bool(sample)
        and expected.get("stop") is not None
        and abs(sample["positions"][0]["sl"] - float(expected["stop"])) < 0.011,
        "broker_flat": ok_flat,
        "nautilus_flat": nautilus_state.get("open_positions") == 0,
        "open_orders_equal": facts["working_orders"] == nautilus_state.get("open_orders"),
        "pnl_matches_within_0.02": nautilus_pnl_value is not None
        and exit_deals
        and abs(nautilus_pnl_value - broker_profit) <= 0.02 + abs(commissions) + abs(swap),
        "venue_snapshot_reconciled": reconciliation.get("state") == "reconciled"
        and reconciliation.get("source") == "venue_snapshot",
    }
    return report
