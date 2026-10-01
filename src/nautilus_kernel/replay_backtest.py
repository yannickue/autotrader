"""Candidate-REPLAY Nautilus backtest (research workbench, OFFLINE ONLY).

NOT imported by anything production-reachable. Reuses the catalog / instrument / venue
conventions of :mod:`nautilus_kernel.backtest` (same BacktestEngine venue, same bid/ask-synthesized
bars, same Cfd instrument) but replaces the EMA/ATR `ProofStrategy` with a strategy that REPLAYS a
fixed list of candidates:

    decision bar CLOSE  -> market entry (IOC) with the externally supplied quantity
    position opened     -> reduce-only STOP_MARKET (stop) + reduce-only LIMIT (target) pair,
                           each cancelling the other on fill
    forced exits        -> `close_position` market order at the bar close listed in ``forced_exits``

Nautilus owns orders, fills, positions and PnL; this module only submits orders at predetermined
times and READS the results back. No sizing, risk or alpha logic lives here (quantity = input).

Matching semantics (measured, see docs in tests/unit/research_workbench/test_differential_e2e.py):
bar-based quote-tick matching, fill price = book (bid/ask close for market orders at bar close),
FillModel slips exactly ONE tick when ``prob_slippage=1`` else zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.backtest.models import FillModel, LatencyModel, StandardMarginModel
from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
from nautilus_trader.model.currencies import EUR
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide, TimeInForce
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId, TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from nautilus_kernel.backtest import STARTING_BALANCE_EUR
from nautilus_kernel.catalog import bar_type_strings, read_catalog
from nautilus_kernel.instrument import VENUE
from nautilus_kernel.queries import closed_position_lifecycles

LABELS = ("TECHNICAL_BACKTEST", "NOT_YET_BROKER_CALIBRATED", "CANDIDATE_REPLAY")


@dataclass(frozen=True, slots=True)
class ReplayCandidate:
    """One decision to replay. ``decision_ts_ns`` is the decision bar's CLOSE timestamp (ns)."""

    decision_ts_ns: int
    side: int  # +1 long / -1 short
    stop: float
    target: float | None
    qty: float


@dataclass(frozen=True, slots=True)
class ReplayFill:
    ts_ns: int
    side: str  # "BUY" | "SELL"
    price: float
    qty: float
    tag: str  # "entry" | "exit:STOP" | "exit:TARGET" | "exit:<FORCED REASON>"


@dataclass(slots=True)
class ReplayResult:
    labels: tuple[str, ...]
    fills: list[ReplayFill] = field(default_factory=list)
    realized_pnl: list[Decimal] = field(
        default_factory=list
    )  # per closed lifecycle (ts_closed order)
    commissions: list[Decimal] = field(default_factory=list)
    ignored: list[dict[str, Any]] = field(default_factory=list)  # candidates Nautilus did not take
    # (candidate, bar ts_event at which the entry order was submitted), in submission order
    decisions: list[tuple[ReplayCandidate, int]] = field(default_factory=list)
    position_snapshots: int = 0


class ReplayConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bid_bar_type: BarType


class ReplayStrategy(Strategy):
    def __init__(
        self,
        config: ReplayConfig,
        *,
        candidates: list[ReplayCandidate],
        forced_exits: dict[int, str],
        result: ReplayResult,
    ) -> None:
        super().__init__(config)
        self._cfg = config
        self._by_ts = {c.decision_ts_ns: c for c in candidates}
        self._forced = dict(forced_exits)
        self._res = result
        self._instrument: Any = None
        self._pending: ReplayCandidate | None = None

    def on_start(self) -> None:
        self._instrument = self.cache.instrument(self._cfg.instrument_id)
        if self._instrument is None:
            self.log.error("instrument not in cache; stopping")
            self.stop()
            return
        self.subscribe_bars(self._cfg.bid_bar_type)

    def on_stop(self) -> None:
        self.cancel_all_orders(self._cfg.instrument_id)
        self.close_all_positions(self._cfg.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        if bar.bar_type != self._cfg.bid_bar_type:
            return
        ts = bar.ts_event
        iid = self._cfg.instrument_id
        open_positions = self.cache.positions_open(instrument_id=iid)
        reason = self._forced.get(ts)
        if open_positions and reason is not None:
            self.cancel_all_orders(iid)
            self.close_position(open_positions[0], tags=[f"exit:{reason}"])
            return
        cand = self._by_ts.get(ts)
        if cand is None:
            return
        if open_positions or self.cache.orders_inflight(instrument_id=iid):
            self._res.ignored.append({"ts_ns": ts, "why": "position_or_order_active"})
            return
        self._pending = cand
        self._res.decisions.append((cand, int(ts)))
        order = self.order_factory.market(
            instrument_id=iid,
            order_side=OrderSide.BUY if cand.side > 0 else OrderSide.SELL,
            quantity=self._instrument.make_qty(cand.qty),
            time_in_force=TimeInForce.IOC,
            tags=["entry"],
        )
        self.submit_order(order)

    def on_order_filled(self, event: OrderFilled) -> None:
        order = self.cache.order(event.client_order_id)
        tags = list(order.tags or []) if order is not None else []
        tag = tags[0] if tags else "unknown"
        self._res.fills.append(
            ReplayFill(
                ts_ns=int(event.ts_event),
                side=event.order_side.name,
                price=float(event.last_px),
                qty=float(event.last_qty),
                tag=str(tag),
            )
        )
        # Protective legs are placed from the entry fill (position is open from here on).
        if tag == "entry" and self._pending is not None:
            cand, self._pending = self._pending, None
            exit_side = OrderSide.SELL if cand.side > 0 else OrderSide.BUY
            qty = self._instrument.make_qty(float(event.last_qty))
            self.submit_order(
                self.order_factory.stop_market(
                    instrument_id=self._cfg.instrument_id,
                    order_side=exit_side,
                    quantity=qty,
                    trigger_price=self._instrument.make_price(cand.stop),
                    time_in_force=TimeInForce.GTC,
                    reduce_only=True,
                    tags=["exit:STOP"],
                )
            )
            if cand.target is not None:
                self.submit_order(
                    self.order_factory.limit(
                        instrument_id=self._cfg.instrument_id,
                        order_side=exit_side,
                        quantity=qty,
                        price=self._instrument.make_price(cand.target),
                        time_in_force=TimeInForce.GTC,
                        reduce_only=True,
                        tags=["exit:TARGET"],
                    )
                )

    def on_position_closed(self, event: Any) -> None:
        self.cancel_all_orders(self._cfg.instrument_id)


def run_candidate_replay_backtest(
    *,
    catalog_path: Path | str,
    instrument: Any,
    timeframe: str,
    candidates: list[ReplayCandidate],
    forced_exits: dict[int, str] | None = None,
    slippage_one_tick: bool = False,
    latency_nanos: int = 0,
) -> ReplayResult:
    """Replay ``candidates`` through the REAL Nautilus BacktestEngine.

    ``forced_exits`` maps a bar-CLOSE ts (ns) -> exit reason tag; the open position is flattened
    with a market order at that bar's close. ``slippage_one_tick`` => ``FillModel(prob_slippage=1)``
    (Nautilus can slip exactly one tick or none). ``latency_nanos=0`` => orders submitted in
    ``on_bar`` fill against the decision bar's closing book (see module docstring).
    """
    inst, bid_bars, ask_bars = read_catalog(catalog_path, instrument, timeframe)
    bid_type_s, _ = bar_type_strings(inst, timeframe)
    result = ReplayResult(labels=LABELS)
    engine = BacktestEngine(
        BacktestEngineConfig(
            trader_id=TraderId("WB-REPLAY-001"),
            logging=LoggingConfig(log_level="ERROR", bypass_logging=True),
        )
    )
    try:
        engine.add_venue(
            venue=VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=EUR,
            starting_balances=[Money(STARTING_BALANCE_EUR, EUR)],
            default_leverage=Decimal(20),
            margin_model=StandardMarginModel(),
            fill_model=FillModel(
                prob_fill_on_limit=1.0,
                prob_slippage=1.0 if slippage_one_tick else 0.0,
                random_seed=42,
            ),
            latency_model=LatencyModel(base_latency_nanos=latency_nanos) if latency_nanos else None,
            bar_execution=True,
        )
        engine.add_instrument(inst)
        engine.add_data(bid_bars)
        engine.add_data(ask_bars)
        engine.add_strategy(
            ReplayStrategy(
                ReplayConfig(instrument_id=inst.id, bid_bar_type=BarType.from_str(bid_type_s)),
                candidates=candidates,
                forced_exits=forced_exits or {},
                result=result,
            )
        )
        engine.run()
        closed = closed_position_lifecycles(engine.cache, inst.id)
        result.position_snapshots = len(closed)
        for p in closed:
            result.realized_pnl.append(Decimal(str(p.realized_pnl.as_decimal())))
            commission = Decimal(0)
            for money in p.commissions():
                commission += Decimal(str(money.as_decimal()))
            result.commissions.append(commission)
    finally:
        engine.dispose()
    return result


__all__ = [
    "ReplayCandidate",
    "ReplayFill",
    "ReplayResult",
    "run_candidate_replay_backtest",
]
