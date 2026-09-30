"""Nautilus ``DemoTraderStrategy``: the order-lifecycle shell of the DEMO stack.

It lives INSIDE the assembled Nautilus kernel (LIVE environment, our MT5 execution client) and has
no signal logic and no sizing logic. The stack hands it fully validated, risk-sized jobs through a
thread-safe queue; the strategy

* re-checks the invariants it can see on the loop thread (stale intent, forced-flat passed,
  duplicate ``client_order_id``, existing position, halt flag) - a second gate, not the first;
* submits ONE ``OrderList`` per entry job: market entry + MANDATORY broker-side stop
  (+ broker-side take-profit when the intent carries a target), exactly once per client order id;
* resolves the job's future when protection is confirmed (stop child ACCEPTED, which the adapter
  only does after the entry exists at the broker) or when the entry is denied/rejected;
* closes positions with reduce-only market orders on request (forced flat, protection failure);
* Lane E (exit engine): "ReduceJob" = reduce-only MARKET order of a given (partial) quantity,
  "ModifyStopJob" = tighten-only move of the broker-side stop through the adapter's modify path.

The strategy never touches MT5. Everything money-related that needs broker truth (fills, costs,
exit reasons) is read by the stack from the broker's deals.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, OrderType, PositionSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Price
from nautilus_trader.model.orders import OrderList
from nautilus_trader.trading.strategy import Strategy

from demo.contracts import TradeIntent, stable_hash
from demo.execution.parity import parse_utc

DRY_RUN_MARK = "DRY_RUN_ORDER_CHECK_OK"


def client_order_id_for(intent_id: str) -> str:
    """Deterministic client order id of an intent.

    Byte-identical to ``demo.store.client_order_id_for`` (``dt-`` + 24 hex chars); replicated here
    on purpose so the execution package does not import the recorder's store.
    """
    return "dt-" + stable_hash("client-order", intent_id, n=24)


@dataclass(slots=True)
class EntryJob:
    intent: TradeIntent
    instrument_id: InstrumentId
    quantity: Decimal  # lots, on the broker step (sized by the risk gate)
    stop: Decimal
    target: Decimal | None
    client_order_id: str
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    entry_fills: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # (qty, price)


@dataclass(slots=True)
class FlattenJob:
    instrument_id: InstrumentId
    tag: str
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)


@dataclass(slots=True)
class ReduceJob:
    """Reduce-only MARKET close of ``quantity`` lots (a PARTIAL exit). ``tag`` = decision id."""

    instrument_id: InstrumentId
    quantity: Decimal
    tag: str
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    fills: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # (qty, price)


@dataclass(slots=True)
class ModifyStopJob:
    """Tighten-only stop move to ``new_stop`` (never loosens; the adapter re-validates)."""

    instrument_id: InstrumentId
    new_stop: Decimal
    tag: str
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    client_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """``status``: filled | denied | rejected | dry_run_ok | protection_denied | flat | failed |
    reduced | modified | unchanged | modify_rejected."""

    status: str
    reason: str = ""
    fills: tuple[tuple[Decimal, Decimal], ...] = ()


class DemoTraderStrategyConfig(StrategyConfig, frozen=True):
    pass


class DemoTraderStrategy(Strategy):
    def __init__(
        self,
        config: DemoTraderStrategyConfig | None = None,
        *,
        loop: asyncio.AbstractEventLoop,
        now: Callable[[], datetime] | None = None,
        halt_reason: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(config or DemoTraderStrategyConfig())
        self._loop = loop
        self._now = now or (lambda: datetime.now(UTC))
        self._halt_reason = halt_reason or (lambda: None)
        self._inbox: queue.SimpleQueue[
            EntryJob | FlattenJob | ReduceJob | ModifyStopJob
        ] = queue.SimpleQueue()
        self._entries: dict[str, EntryJob] = {}  # by entry client order id
        self._children: dict[str, EntryJob] = {}  # SL/TP client order id -> job
        self._flattens: dict[InstrumentId, FlattenJob] = {}
        self._reduces: dict[str, ReduceJob] = {}  # reduce-only client order id -> job
        self._modifies: dict[str, ModifyStopJob] = {}  # stop client order id -> job
        self._seen: set[str] = set()
        self.submitted: list[str] = []  # client order ids handed to the exec engine (audit)

    # -- thread-safe entry point --------------------------------------------------------------

    def enqueue(
        self, job: EntryJob | FlattenJob | ReduceJob | ModifyStopJob
    ) -> concurrent.futures.Future:
        """Callable from ANY thread; the job runs on the Nautilus loop thread."""
        self._inbox.put(job)
        self._loop.call_soon_threadsafe(self._drain)
        return job.future

    def _drain(self) -> None:
        while True:
            try:
                job = self._inbox.get_nowait()
            except queue.Empty:
                return
            try:
                if isinstance(job, EntryJob):
                    self._start_entry(job)
                elif isinstance(job, ReduceJob):
                    self._start_reduce(job)
                elif isinstance(job, ModifyStopJob):
                    self._start_modify_stop(job)
                else:
                    self._start_flatten(job)
            except Exception as exc:  # never leave a caller waiting on a swallowed error
                self._resolve(job, JobOutcome("failed", f"strategy_error:{type(exc).__name__}"))

    @staticmethod
    def _resolve(
        job: EntryJob | FlattenJob | ReduceJob | ModifyStopJob, outcome: JobOutcome
    ) -> None:
        if not job.future.done():
            job.future.set_result(outcome)

    # -- entry ----------------------------------------------------------------------------------

    def _start_entry(self, job: EntryJob) -> None:
        intent = job.intent
        if self._halt_reason():
            return self._resolve(job, JobOutcome("denied", "halted"))
        if job.client_order_id in self._seen:
            return self._resolve(job, JobOutcome("denied", "duplicate_intent"))
        now = self._now().astimezone(UTC)
        if now > parse_utc(intent.valid_until_utc):
            return self._resolve(job, JobOutcome("denied", "stale_signal"))
        if intent.forced_flat_utc is not None and now >= parse_utc(intent.forced_flat_utc):
            return self._resolve(job, JobOutcome("denied", "past_forced_flat"))
        if self.cache.positions_open(instrument_id=job.instrument_id):
            return self._resolve(job, JobOutcome("denied", "position_exists"))
        if job.stop <= 0:
            return self._resolve(job, JobOutcome("denied", "no_stop"))  # protection is mandatory
        instrument = self.cache.instrument(job.instrument_id)
        if instrument is None:
            return self._resolve(job, JobOutcome("denied", "instrument_not_in_cache"))
        quantity = instrument.make_qty(float(job.quantity))
        if Decimal(str(quantity)) != job.quantity:
            return self._resolve(job, JobOutcome("denied", "quantity_precision"))

        self._seen.add(job.client_order_id)
        is_long = intent.direction == 1
        entry_side = OrderSide.BUY if is_long else OrderSide.SELL
        exit_side = OrderSide.SELL if is_long else OrderSide.BUY
        tags = [f"intent:{intent.intent_id}"]
        entry = self.order_factory.market(
            instrument_id=job.instrument_id,
            order_side=entry_side,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
            client_order_id=ClientOrderId(job.client_order_id),
            tags=tags,
        )
        stop_id = ClientOrderId(job.client_order_id + "-SL")
        stop = self.order_factory.stop_market(
            instrument_id=job.instrument_id,
            order_side=exit_side,
            quantity=quantity,
            trigger_price=Price(float(job.stop), instrument.price_precision),
            reduce_only=True,
            client_order_id=stop_id,
            tags=tags,
        )
        orders: list[Any] = [entry, stop]
        self._children[str(stop_id)] = job
        if job.target is not None:
            target_id = ClientOrderId(job.client_order_id + "-TP")
            take = self.order_factory.limit(
                instrument_id=job.instrument_id,
                order_side=exit_side,
                quantity=quantity,
                price=Price(float(job.target), instrument.price_precision),
                time_in_force=TimeInForce.GTC,
                reduce_only=True,
                client_order_id=target_id,
                tags=tags,
            )
            orders.append(take)
            self._children[str(target_id)] = job
        self._entries[job.client_order_id] = job
        self.submitted.append(job.client_order_id)
        self.submit_order_list(OrderList(self.order_factory.generate_order_list_id(), orders))

    # -- flatten (reduce-only) ------------------------------------------------------------------

    def _start_flatten(self, job: FlattenJob) -> None:
        positions = self.cache.positions_open(instrument_id=job.instrument_id)
        if not positions:
            return self._resolve(job, JobOutcome("flat", "no_open_position"))
        self._flattens[job.instrument_id] = job
        for position in positions:
            self.close_position(position, tags=[job.tag])

    # -- partial reduce (reduce-only market) ------------------------------------------------------

    def _start_reduce(self, job: ReduceJob) -> None:
        positions = self.cache.positions_open(instrument_id=job.instrument_id)
        if not positions:
            return self._resolve(job, JobOutcome("flat", "no_open_position"))
        if len(positions) != 1:
            return self._resolve(job, JobOutcome("denied", f"positions_{len(positions)}"))
        position = positions[0]
        if job.quantity <= 0 or Decimal(str(position.quantity)) < job.quantity:
            return self._resolve(job, JobOutcome("denied", "reduce_exceeds_position"))
        instrument = self.cache.instrument(job.instrument_id)
        if instrument is None:
            return self._resolve(job, JobOutcome("denied", "instrument_not_in_cache"))
        quantity = instrument.make_qty(float(job.quantity))
        if Decimal(str(quantity)) != job.quantity:
            return self._resolve(job, JobOutcome("denied", "quantity_precision"))
        side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY
        order = self.order_factory.market(
            instrument_id=job.instrument_id,
            order_side=side,
            quantity=quantity,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            tags=[job.tag],
        )
        self._reduces[str(order.client_order_id)] = job
        self.submit_order(order)

    # -- stop modification (tighten only) -----------------------------------------------------

    def _start_modify_stop(self, job: ModifyStopJob) -> None:
        positions = self.cache.positions_open(instrument_id=job.instrument_id)
        if len(positions) != 1:
            return self._resolve(job, JobOutcome("denied", f"positions_{len(positions)}"))
        is_long = positions[0].side == PositionSide.LONG
        stops = [
            o
            for o in self.cache.orders_open(instrument_id=job.instrument_id)
            if o.order_type == OrderType.STOP_MARKET and o.is_reduce_only
        ]
        if len(stops) != 1:
            return self._resolve(job, JobOutcome("denied", f"stop_orders_{len(stops)}"))
        order = stops[0]
        current = Decimal(str(order.trigger_price))
        if job.new_stop == current:
            return self._resolve(job, JobOutcome("unchanged", "already_at_level"))
        if (job.new_stop < current) if is_long else (job.new_stop > current):
            return self._resolve(job, JobOutcome("denied", "stop_not_tighter"))
        instrument = self.cache.instrument(job.instrument_id)
        if instrument is None:
            return self._resolve(job, JobOutcome("denied", "instrument_not_in_cache"))
        job.client_order_id = str(order.client_order_id)
        self._modifies[job.client_order_id] = job
        trigger = Price(float(job.new_stop), instrument.price_precision)
        self.modify_order(order, trigger_price=trigger)

    def on_order_updated(self, event: Any) -> None:
        job = self._modifies.get(str(event.client_order_id))
        if job is None or job.future.done():
            return
        trigger = getattr(event, "trigger_price", None)
        if trigger is not None and Decimal(str(trigger)) == job.new_stop:
            self._modifies.pop(str(event.client_order_id), None)
            self._resolve(job, JobOutcome("modified", "stop_updated"))

    def on_order_modify_rejected(self, event: Any) -> None:
        job = self._modifies.pop(str(event.client_order_id), None)
        if job is not None:
            self._resolve(job, JobOutcome("modify_rejected", str(getattr(event, "reason", ""))))

    # -- lifecycle events ------------------------------------------------------------------------

    def on_order_accepted(self, event: Any) -> None:
        job = self._children.get(str(event.client_order_id))
        if job is None or job.future.done():
            return
        if str(event.client_order_id).endswith("-SL"):
            self._resolve(job, JobOutcome("filled", "protection_accepted", tuple(job.entry_fills)))

    def on_order_filled(self, event: Any) -> None:
        job = self._entries.get(str(event.client_order_id))
        if job is not None:
            job.entry_fills.append((Decimal(str(event.last_qty)), Decimal(str(event.last_px))))
        reduce = self._reduces.get(str(event.client_order_id))
        if reduce is not None:
            reduce.fills.append((Decimal(str(event.last_qty)), Decimal(str(event.last_px))))
            if sum((q for q, _ in reduce.fills), Decimal(0)) >= reduce.quantity:
                self._reduces.pop(str(event.client_order_id), None)
                self._resolve(reduce, JobOutcome("reduced", "filled", tuple(reduce.fills)))

    def on_order_canceled(self, event: Any) -> None:
        """An IOC reduce order ended without filling its whole quantity (partial / none)."""
        reduce = self._reduces.pop(str(event.client_order_id), None)
        if reduce is not None and not reduce.future.done():
            status = "reduced" if reduce.fills else "failed"
            self._resolve(reduce, JobOutcome(status, "canceled_remainder", tuple(reduce.fills)))

    def on_order_denied(self, event: Any) -> None:
        self._on_refused("denied", event)

    def on_order_rejected(self, event: Any) -> None:
        self._on_refused("rejected", event)

    def _on_refused(self, kind: str, event: Any) -> None:
        reason = str(getattr(event, "reason", ""))
        cid = str(event.client_order_id)
        job = self._entries.get(cid)
        if job is not None:
            if DRY_RUN_MARK in reason:
                return self._resolve(job, JobOutcome("dry_run_ok", reason))
            return self._resolve(job, JobOutcome(kind, reason))
        reduce = self._reduces.pop(cid, None)
        if reduce is not None:
            return self._resolve(reduce, JobOutcome(kind, reason, tuple(reduce.fills)))
        child = self._children.get(cid)
        if child is not None:
            if child.future.done():
                return
            if child.entry_fills:  # entry filled but the broker-side stop could not be confirmed
                fills = tuple(child.entry_fills)
                self._resolve(child, JobOutcome("protection_denied", reason, fills))
            else:
                self._resolve(child, JobOutcome(kind, reason))
            return
        order = self.cache.order(event.client_order_id)
        tags = list(getattr(order, "tags", None) or ())
        for instrument_id, flat in list(self._flattens.items()):
            if flat.tag in tags:
                self._flattens.pop(instrument_id, None)
                self._resolve(flat, JobOutcome("failed", f"{kind}:{reason}"))

    def on_position_closed(self, event: Any) -> None:
        flat = self._flattens.pop(event.instrument_id, None)
        if flat is not None:
            self._resolve(flat, JobOutcome("flat", "closed"))

    def forget(self, client_order_id: str) -> None:
        """Drop bookkeeping of a finished job (bounded memory in a long-running process)."""
        self._entries.pop(client_order_id, None)
        self._children.pop(client_order_id + "-SL", None)
        self._children.pop(client_order_id + "-TP", None)
