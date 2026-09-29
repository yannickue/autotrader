"""ONE simple deterministic proof strategy -- lifecycle proof, NOT alpha.

EMA(fast)/EMA(slow) crossover on closed M5 BID bars, stop = k * ATR.

    signal -> RiskPolicy/PositionSizer (via NautilusRiskBridge)
           -> Nautilus market order -> Nautilus fill -> Nautilus position
           -> protective Nautilus STOP_MARKET (reduce-only)
           -> exit (opposite signal / time stop / session end / stop hit)
           -> Nautilus position close -> realized PnL (Nautilus)

V1 rules: max one position per instrument; same-direction signal while
positioned is ignored (no pyramiding); an opposite signal flattens first and
only on a LATER bar may open the reversed entry (no same-tick flip).

State ownership: this class keeps only decision helpers (`_prev_diff`,
`_pending_reversal`, `_stop_for_next_fill`); orders/positions/PnL are always
read from the Nautilus cache. The `Evidence` log is an append-only audit
trail, never read back to make decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any

from nautilus_trader.common.enums import LogColor
from nautilus_trader.indicators import AverageTrueRange, ExponentialMovingAverage
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide, TimeInForce
from nautilus_trader.model.events import OrderFilled, PositionClosed, PositionOpened
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy, StrategyConfig

from nautilus_kernel.risk_bridge import MarketInputs, NautilusRiskBridge
from risk.models import RiskSide
from risk.policy import PolicyRejection


@dataclass(slots=True)
class Evidence:
    """Append-only audit trail of lifecycle checkpoints (never used for decisions)."""

    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: str, ts_ns: int, **fields: Any) -> None:
        self.events.append({"event": event, "ts_ns": ts_ns, **fields})

    def of(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


class ProofStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bid_bar_type: BarType
    spread_points: dict[int, int]  # bar ts_event(ns) -> broker spread points
    point: float = 0.01
    fast_period: int = 8
    slow_period: int = 21
    atr_period: int = 14
    atr_stop_multiple: float = 2.0
    hold_bars: int = 24
    entry_from_utc: time = time(1, 0)
    entry_until_utc: time = time(19, 0)
    flatten_from_utc: time = time(19, 30)


class ProofStrategy(Strategy):
    def __init__(
        self, config: ProofStrategyConfig, *, bridge: NautilusRiskBridge, evidence: Evidence
    ) -> None:
        super().__init__(config)
        self._cfg = config
        self._bridge = bridge
        self._evidence = evidence
        self._fast = ExponentialMovingAverage(config.fast_period)
        self._slow = ExponentialMovingAverage(config.slow_period)
        self._atr = AverageTrueRange(config.atr_period)
        self._instrument: Any = None
        self._prev_diff: float | None = None
        self._pending_reversal: RiskSide | None = None
        self._stop_for_next_fill: Decimal | None = None
        self._bar_ns = int(config.bid_bar_type.spec.timedelta.total_seconds() * 1e9)

    # -- lifecycle ---------------------------------------------------------

    def on_start(self) -> None:
        self._instrument = self.cache.instrument(self._cfg.instrument_id)
        if self._instrument is None:
            self.log.error("instrument not in cache; stopping")
            self.stop()
            return
        self.register_indicator_for_bars(self._cfg.bid_bar_type, self._fast)
        self.register_indicator_for_bars(self._cfg.bid_bar_type, self._slow)
        self.register_indicator_for_bars(self._cfg.bid_bar_type, self._atr)
        self.subscribe_bars(self._cfg.bid_bar_type)
        self._evidence.record(
            "STRATEGY_INITIALIZED",
            self.clock.timestamp_ns(),
            strategy_id=str(self.id),
            instrument=str(self._cfg.instrument_id),
            bars_in_cache=self.cache.bar_count(self._cfg.bid_bar_type),
        )

    def on_stop(self) -> None:
        self.cancel_all_orders(self._cfg.instrument_id)
        self.close_all_positions(self._cfg.instrument_id)

    # -- decisions ---------------------------------------------------------

    def on_bar(self, bar: Bar) -> None:
        if bar.bar_type != self._cfg.bid_bar_type:
            return
        if not (self._fast.initialized and self._slow.initialized and self._atr.initialized):
            return
        ts_ns = bar.ts_event
        now = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC)
        t = now.time()
        diff = self._fast.value - self._slow.value
        cross_up = self._prev_diff is not None and self._prev_diff <= 0 < diff
        cross_dn = self._prev_diff is not None and self._prev_diff >= 0 > diff
        self._prev_diff = diff
        signal_side: RiskSide | None = (
            RiskSide.BUY if cross_up else RiskSide.SELL if cross_dn else None
        )

        open_positions = self.cache.positions_open(instrument_id=self._cfg.instrument_id)
        if open_positions:
            self._manage_open_position(open_positions[0], signal_side, now, t, ts_ns)
            return

        if self.cache.orders_inflight(instrument_id=self._cfg.instrument_id) or (
            self.cache.orders_open(instrument_id=self._cfg.instrument_id)
        ):
            return  # an order is still working; v1 allows one at a time

        side = signal_side
        origin = "SIGNAL"
        if side is None and self._pending_reversal is not None:
            still_valid = (self._pending_reversal == RiskSide.BUY and diff > 0) or (
                self._pending_reversal == RiskSide.SELL and diff < 0
            )
            if still_valid:
                side, origin = self._pending_reversal, "REVERSAL_AFTER_FLATTEN"
        self._pending_reversal = None
        if side is None:
            return
        if not (self._cfg.entry_from_utc <= t < self._cfg.entry_until_utc):
            self._evidence.record("SIGNAL_OUTSIDE_ENTRY_WINDOW", ts_ns, side=side.value)
            return
        self._try_entry(bar, side, now, ts_ns, origin)

    def _manage_open_position(
        self, position: Any, signal_side: RiskSide | None, now: datetime, t: time, ts_ns: int
    ) -> None:
        held_bars = (ts_ns - position.ts_opened) // self._bar_ns
        position_side = RiskSide.BUY if position.side == PositionSide.LONG else RiskSide.SELL
        reason: str | None = None
        if t >= self._cfg.flatten_from_utc:
            reason = "SESSION_END"
        elif signal_side is not None and signal_side != position_side:
            reason = "OPPOSITE_SIGNAL"
            self._pending_reversal = signal_side
        elif held_bars >= self._cfg.hold_bars:
            reason = "TIME_EXIT"
        elif signal_side == position_side:
            self._evidence.record("SAME_DIRECTION_SIGNAL_IGNORED", ts_ns, side=signal_side.value)
        if reason is None:
            return
        self._evidence.record("EXIT_REQUESTED", ts_ns, reason=reason, position_id=str(position.id))
        self.cancel_all_orders(self._cfg.instrument_id)
        self.close_position(position, tags=[f"exit:{reason}"])

    def _try_entry(self, bar: Bar, side: RiskSide, now: datetime, ts_ns: int, origin: str) -> None:
        signal_id = f"{self.id}-{ts_ns}"
        self._evidence.record("SIGNAL", ts_ns, signal_id=signal_id, side=side.value, origin=origin)
        spread_points = self._cfg.spread_points[ts_ns]
        bid = Decimal(str(bar.close.as_double()))
        ask = bid + Decimal(spread_points) * Decimal(str(self._cfg.point))
        stop_distance = Decimal(str(self._atr.value * self._cfg.atr_stop_multiple))
        stop_price = bid - stop_distance if side == RiskSide.BUY else ask + stop_distance
        stop_price = stop_price.quantize(Decimal(str(self._cfg.point)))
        decision = self._bridge.evaluate_entry(
            portfolio=self.portfolio,
            cache=self.cache,
            market=MarketInputs(
                now=now, bid=bid, ask=ask, volume=Decimal(str(bar.volume.as_double()))
            ),
            side=side,
            stop_price=stop_price,
            signal_id=signal_id,
        )
        if isinstance(decision, PolicyRejection):
            self._evidence.record(
                "RISK_REJECTED", ts_ns, signal_id=signal_id, reason=str(decision.reason_code)
            )
            return
        self._evidence.record(
            "RISK_APPROVED",
            ts_ns,
            signal_id=signal_id,
            quantity=str(decision.quantity),
            notional=str(decision.notional),
            reference_price=str(decision.reference_price),
            stop_price=str(decision.stop_price),
            risk_budget=str(decision.risk_budget),
            binding_constraint=decision.metadata["binding_constraint"],
        )
        order = self.order_factory.market(
            instrument_id=self._cfg.instrument_id,
            order_side=OrderSide.BUY if side == RiskSide.BUY else OrderSide.SELL,
            quantity=self._instrument.make_qty(float(decision.quantity)),
            time_in_force=TimeInForce.IOC,
            tags=[f"risk:{signal_id}"],
        )
        self._stop_for_next_fill = decision.stop_price
        self.submit_order(order)
        self._evidence.record(
            "ORDER_SUBMITTED",
            ts_ns,
            signal_id=signal_id,
            client_order_id=str(order.client_order_id),
        )

    # -- Nautilus events ---------------------------------------------------

    def on_order_filled(self, event: OrderFilled) -> None:
        self._evidence.record(
            "ORDER_FILLED",
            event.ts_event,
            client_order_id=str(event.client_order_id),
            side=event.order_side.name,
            qty=str(event.last_qty),
            px=str(event.last_px),
            position_id=str(event.position_id),
        )

    def on_position_opened(self, event: PositionOpened) -> None:
        self._evidence.record(
            "POSITION_OPENED",
            event.ts_event,
            position_id=str(event.position_id),
            side=event.side.name,
            qty=str(event.quantity),
            avg_px_open=str(event.avg_px_open),
        )
        if self._stop_for_next_fill is None:
            return
        stop_side = OrderSide.SELL if event.side == PositionSide.LONG else OrderSide.BUY
        stop = self.order_factory.stop_market(
            instrument_id=self._cfg.instrument_id,
            order_side=stop_side,
            quantity=event.quantity,
            trigger_price=self._instrument.make_price(float(self._stop_for_next_fill)),
            time_in_force=TimeInForce.GTC,
            reduce_only=True,
            tags=["protective_stop"],
        )
        self._stop_for_next_fill = None
        self.submit_order(stop)
        self._evidence.record(
            "STOP_SUBMITTED", event.ts_event, client_order_id=str(stop.client_order_id)
        )

    def on_position_closed(self, event: PositionClosed) -> None:
        self.cancel_all_orders(self._cfg.instrument_id)
        self._evidence.record(
            "POSITION_CLOSED",
            event.ts_event,
            position_id=str(event.position_id),
            realized_pnl=str(event.realized_pnl),
        )
        self.log.info(f"position closed pnl={event.realized_pnl}", color=LogColor.BLUE)
