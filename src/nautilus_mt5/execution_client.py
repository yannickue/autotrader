"""Nautilus `LiveExecutionClient` for ActivTrades MT5 (NETTING accounts, v1 rules).

Nautilus remains authoritative for orders, fills, positions, portfolio and PnL. This
client TRANSLATES commands into MT5 requests and broker truth into Nautilus events /
reports; the only state it keeps is `Mt5StateStore` (ClientOrderId <-> ticket map and the
set of ingested deals) and the adapter `ReconciliationTracker`.

OUTBOUND (submit / modify / cancel / protect) passes `gates.admit` and a second,
independent admission at the very last step (fresh broker evidence + local checks).
INBOUND broker truth (deals, SL/TP executions, external activity) is ALWAYS ingested,
in every reconciliation/runtime state; ingestion never consults the gates. A deal is
marked "seen" only AFTER the Nautilus event/report was produced successfully.

Order-send outcomes: REJECTED (definite, nothing happened) vs IN_DOUBT (timeout / lost
response / unknown retcode: the order may exist). IN_DOUBT never emits a rejection; it
invalidates reconciliation and is resolved from broker history by `reconcile()`.

Protective orders map to MT5 position SL/TP (broker-side, dies with the position; a pending
stop order could open a reverse position in a netting account). New entries carry their
SL/TP in the SAME order_send request (atomic) whenever `require_attached_protection`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.reports import (
    ExecutionMassStatus,
    FillReport,
    OrderStatusReport,
    PositionStatusReport,
)
from nautilus_trader.live.execution_client import LiveExecutionClient
from nautilus_trader.model.currencies import EUR
from nautilus_trader.model.enums import (
    AccountType,
    LiquiditySide,
    OmsType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from nautilus_trader.model.identifiers import (
    AccountId,
    ClientId,
    ClientOrderId,
    InstrumentId,
    TradeId,
    VenueOrderId,
)
from nautilus_trader.model.objects import AccountBalance, Currency, MarginBalance, Money, Price

from adapters.activtrades_mt5.history import AmbiguousServerTime
from adapters.activtrades_mt5.models import (
    DealRecord,
    OrderRecord,
    PositionRecord,
    deal_to_deal_record,
    order_to_order_record,
    position_to_position_record,
)
from nautilus_mt5.constants import (
    ORDER_CHECK_OK,
    DealReason,
    SendOutcome,
    classify_send_retcode,
)
from nautilus_mt5.executor import LaneTimeout, LoopBridge
from nautilus_mt5.gates import AdapterStatus, OutboundKind, admit
from nautilus_mt5.instruments import Mt5InstrumentProvider
from nautilus_mt5.reconciliation import (
    Discrepancy,
    DiscrepancyKind,
    LocalView,
    ReconciliationTracker,
    compare,
    snapshot_from_records,
)
from nautilus_mt5.session import Mt5CallError, Mt5Session
from nautilus_mt5.state import Mt5StateStore, OrderRow
from nautilus_mt5.symbols import VENUE
from nautilus_mt5.translate import (
    Quote,
    RequestRejected,
    close_request,
    is_tightening,
    market_entry_request,
    sltp_request,
    validate_stops,
)
from risk.models import ReconciliationSource, ReconciliationState, RuntimeMode

NS = 1_000_000_000
ZERO = Decimal(0)
KIND_MARKET, KIND_EXIT, KIND_SL, KIND_TP = "MARKET", "EXIT", "PROTECT_SL", "PROTECT_TP"


NT_EMITTERS = (
    "generate_order_submitted",
    "generate_order_accepted",
    "generate_order_rejected",
    "generate_order_denied",
    "generate_order_filled",
    "generate_order_canceled",
    "generate_order_updated",
    "generate_order_triggered",
    "generate_order_modify_rejected",
    "generate_order_cancel_rejected",
    "generate_order_expired",
    "generate_account_state",
    "_send_order_status_report",
    "_send_fill_report",
    "_send_position_status_report",
    "_send_mass_status_report",
    "_set_account_id",
)


class _MarshalledCache:
    """Cache facade for code running in the MT5 lane: every call executes on the loop thread."""

    def __init__(self, cache: Any, bridge: LoopBridge) -> None:
        self._cache = cache
        self._bridge = bridge

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._cache, name)
        if not callable(attr):
            return attr
        return lambda *args, **kwargs: self._bridge.call(attr, *args, **kwargs)


def _run_coroutine_inline(coroutine: Any) -> Any:
    """Drive a coroutine that has no real suspension points to completion (inside the lane)."""
    try:
        coroutine.send(None)
    except StopIteration as stop:
        return stop.value
    coroutine.close()
    raise RuntimeError("coroutine suspended inside the MT5 lane")


def _command_instrument(command: Any) -> Any:
    iid = getattr(command, "instrument_id", None)
    if iid is None and getattr(command, "order", None) is not None:
        iid = command.order.instrument_id
    if iid is None and getattr(command, "order_list", None) is not None:
        iid = command.order_list.orders[0].instrument_id
    return iid


def _in_section(client: Any, command: Any, coroutine: Any) -> Any:
    """Run the coroutine inside the per-symbol critical section of the command's instrument."""
    iid = _command_instrument(command)
    if iid is None:
        return _run_coroutine_inline(coroutine)
    with client._section(iid):
        return _run_coroutine_inline(coroutine)


def lane_op(method: Any) -> Any:
    """Run an async handler's (synchronous) body on the MT5 lane; inline when no lane exists."""

    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        lane = self._session.lane
        if lane is None or lane.in_lane:
            return await method(self, *args, **kwargs)
        command = args[0] if args else None
        return await lane.run(_in_section, self, command, method(self, *args, **kwargs))

    wrapper.__name__ = method.__name__
    return wrapper


def exposure_op(method: Any) -> Any:
    """Like `lane_op`, plus: a lane TIMEOUT means OUTCOME UNKNOWN -- never 'rejected', never
    resent. The involved orders become IN_DOUBT and reconciliation authority is dropped."""

    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        lane = self._session.lane
        if lane is None or lane.in_lane:
            return await method(self, *args, **kwargs)
        try:
            return await lane.run(
                _in_section,
                self,
                args[0] if args else None,
                method(self, *args, **kwargs),
                timeout=self._cfg.exposure_timeout_secs,
            )
        except LaneTimeout:
            self._on_exposure_timeout(method.__name__, args[0] if args else None)
            return None

    wrapper.__name__ = method.__name__
    return wrapper


@dataclass(frozen=True, slots=True, kw_only=True)
class Mt5ExecClientConfig:
    magic: int = 730001
    deviation_points: int = 20
    require_attached_protection: bool = True
    deal_lookback: timedelta = timedelta(days=2)
    in_doubt_grace: timedelta = timedelta(minutes=2)
    sync_interval_secs: float = 1.0
    reconnect_backoff_secs: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)
    autostart_sync: bool = True
    auto_reconcile_on_connect: bool = True
    # Caller-side wait for an exposure-changing operation on the MT5 lane. Expiry is NOT a
    # rejection: the operation may still complete at the broker (outcome unknown -> reconcile).
    exposure_timeout_secs: float = 30.0
    # DRY RUN: run the whole admission path and the REAL order_check, then STOP before
    # order_send (the order is rejected locally with DRY_RUN_ORDER_CHECK_OK).
    dry_run: bool = False
    # Re-verify the attached account (expected login) and, when True, DEMO trade mode right
    # before EVERY exposure-changing order_send / SL-TP change.
    require_demo_account: bool = False
    # When set, the attached account's server must equal it right before every send (L1).
    expected_server: str | None = None


@dataclass(slots=True)
class IngestStats:
    ingested: int = 0
    duplicates: int = 0
    failed: int = 0
    non_trade: int = 0
    external: int = 0
    foreign_symbol: int = 0


class Mt5LiveExecutionClient(LiveExecutionClient):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Any,
        clock: LiveClock,
        instrument_provider: Mt5InstrumentProvider,
        session: Mt5Session,
        store: Mt5StateStore,
        config: Mt5ExecClientConfig | None = None,
        base_currency: Currency = EUR,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId("ACTIVTRADES"),
            venue=VENUE,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            base_currency=base_currency,
            instrument_provider=instrument_provider,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
        )
        self._provider = instrument_provider
        self._session = session
        self._store = store
        self._cfg = config or Mt5ExecClientConfig()
        self._ccy = base_currency
        self._bridge = LoopBridge(loop)  # Nautilus objects are touched on the loop thread only
        self._bridge.bind_current_thread()
        self._nt_cache = _MarshalledCache(self._cache, self._bridge)
        for name in NT_EMITTERS:
            setattr(self, name, self._marshalled(getattr(self, name)))
        self._now_fn = now  # injectable time source (deterministic tests); default: clock
        self.recon = ReconciliationTracker()
        self.ingest_stats = IngestStats()
        self._protective_resize_pending = False  # Lane E: retry a failed SL/TP resize
        self._failed_deals: set[int] = set()  # deals that failed and have not booked since
        self.last_sync_error: str | None = None
        self._sync_task: asyncio.Task | None = None
        self._backoff_index = 0
        self._last_generation = -1
        self.audit: list[str] = []  # small append-only trail for tests/operators (no decisions)

    # ------------------------------------------------------------------ status --

    @contextlib.contextmanager
    def _section(self, instrument_id: InstrumentId) -> Any:
        """Per-symbol critical section for execution-changing work (no-op without a lane)."""
        lane = self._session.lane
        if lane is None:
            yield
            return
        symbol = self._provider.registry.by_instrument_id(instrument_id).broker_symbol
        with lane.symbol_section(symbol):
            yield

    def _marshalled(self, fn: Any) -> Any:
        return lambda *args, **kwargs: self._bridge.call(fn, *args, **kwargs)

    async def _lane_run(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        lane = self._session.lane
        if lane is None:
            return fn(*args, **kwargs)
        return await lane.run(fn, *args, **kwargs)

    def _on_exposure_timeout(self, operation: str, command: Any) -> None:
        """The caller stopped waiting: the outcome is UNKNOWN. Mark, invalidate, do NOT resend."""
        ids: list[str] = []
        if command is not None:
            if hasattr(command, "order"):
                ids.append(str(command.order.client_order_id))
            elif hasattr(command, "order_list"):
                ids.extend(str(o.client_order_id) for o in command.order_list.orders)
            elif hasattr(command, "client_order_id"):
                ids.append(str(command.client_order_id))
        for cid in ids:
            row = self._store.by_client_order_id(cid)
            if (
                row is not None
                and row.parent_client_order_id is None  # children follow their parent
                and row.status in ("INTENT", "SENT")
            ):
                self._store.update_order(cid, status="IN_DOUBT")
        self.audit.append(f"LANE_TIMEOUT {operation}: outcome unknown for {ids}")
        self.recon.invalidate(f"LANE_TIMEOUT {operation}")
        self._session.mark_degraded(f"lane timeout during {operation}")

    @property
    def status(self) -> AdapterStatus:
        return AdapterStatus(
            runtime=self.recon.runtime,
            reconciliation=self.recon.state,
            source=self.recon.source,
            unprotected_positions=self.recon.unprotected_positions,
        )

    def _now_ns(self) -> int:
        if self._now_fn is not None:
            return int(self._now_fn().timestamp() * NS)
        return int(self._clock.timestamp_ns())

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._now_ns() / NS, tz=UTC)

    # ------------------------------------------------------------- broker reads --

    def _call(self, what: str, function: Any, *args: Any, **kwargs: Any) -> Any:
        return self._session.call(what, function, *args, **kwargs)

    def _broker_positions(self, symbol: str | None = None) -> list[PositionRecord]:
        if symbol is None:
            raw = self._call("positions_get", self._session.client.positions_get)
        else:  # real MT5: filters are keyword-only
            raw = self._call("positions_get", self._session.client.positions_get, symbol=symbol)
        return [position_to_position_record(p) for p in raw]

    def _broker_open_orders(self) -> list[OrderRecord]:
        raw = self._call("orders_get", self._session.client.orders_get)
        return [order_to_order_record(o) for o in raw]

    def _history_window(self) -> tuple[datetime, datetime]:
        policy = self._session.time_policy
        start = policy.utc_to_request_datetime(self._now() - self._cfg.deal_lookback)
        end = policy.utc_to_request_datetime(self._now() + timedelta(days=1))
        return start, end

    def _broker_deals(self, since: datetime) -> list[DealRecord]:
        policy = self._session.time_policy
        start = policy.utc_to_request_datetime(since)
        end = policy.utc_to_request_datetime(self._now() + timedelta(days=1))
        raw = self._call("history_deals_get", self._session.client.history_deals_get, start, end)
        return [deal_to_deal_record(d) for d in raw]

    def _quote(self, broker_symbol: str) -> Quote:
        tick = self._call("symbol_info_tick", self._session.client.symbol_info_tick, broker_symbol)
        return Quote(bid=Decimal(str(tick.bid)), ask=Decimal(str(tick.ask)))

    def _ts_ns_from_server_epoch(self, epoch_seconds: float) -> tuple[int, bool]:
        """(ts_ns, exact). Inbound truth is never dropped: an ambiguous DST-hour timestamp
        falls back to 'now' and is flagged."""
        try:
            return int(
                self._session.time_policy.server_epoch_to_utc(epoch_seconds).timestamp() * NS
            ), True
        except AmbiguousServerTime:
            return self._now_ns(), False

    # ---------------------------------------------------------------- connection --

    def _connect_core(self) -> None:
        result = self._session.acquire()
        if result is not None and not result.success:
            raise ConnectionError(f"MT5 connect failed: {result.reason}")
        self._session.account_mode()  # fail closed unless RETAIL_NETTING
        self._provider.load_all_sync()
        for instrument in self._provider.list_all():
            self._nt_cache.add_instrument(instrument)
        account = self._call("account_info", self._session.client.account_info)
        self._set_account_id(AccountId(f"ACTIVTRADES-{int(account.login)}"))
        self._push_account_state()
        self.recon.on_connect(self._session.generation)
        self._last_generation = self._session.generation
        if self._cfg.auto_reconcile_on_connect:
            self.reconcile()

    async def _connect(self) -> None:
        await self._lane_run(self._connect_core)
        self._ensure_sync_task()

    def _disconnect_core(self) -> None:
        self.recon.invalidate("disconnect")
        self._session.release()

    async def _disconnect(self) -> None:
        await self._stop_sync_task()
        await self._lane_run(self._disconnect_core)

    def _check_generation(self) -> None:
        """A changed session generation means a reconnect happened: authority is gone."""
        if self._session.generation != self._last_generation:
            self.recon.on_connect(self._session.generation)
            self._last_generation = self._session.generation

    # ------------------------------------------------------------- account state --

    def _push_account_state(self) -> None:
        info = self._call("account_info", self._session.client.account_info)
        # Nautilus requires total == locked + free. MT5 `margin_free` is equity-based, so it is
        # reported raw in `info`; here free = balance - margin keeps the invariant.
        total = Money(Decimal(str(info.balance)), self._ccy)
        margin = Money(Decimal(str(info.margin)), self._ccy)
        free = Money(total.as_decimal() - margin.as_decimal(), self._ccy)
        self.generate_account_state(
            balances=[AccountBalance(total, margin, free)],
            margins=[MarginBalance(margin, margin, None)] if margin.as_decimal() > 0 else [],
            reported=True,
            ts_event=self._now_ns(),
            info={
                "equity": str(info.equity),
                "profit": str(info.profit),
                "broker_margin_free": str(info.margin_free),
                "margin_level": str(info.margin_level),
                "leverage": str(info.leverage),
                "source": "MT5_ACCOUNT_INFO",
            },
        )

    # -------------------------------------------------------------- deal ingestion --

    def sync_once(self) -> IngestStats:
        """INBOUND, never gated: pull broker deals and turn each new one into Nautilus truth."""
        self._check_generation()
        since = self._now() - self._cfg.deal_lookback
        deals = self._broker_deals(since)
        for deal in sorted(
            deals, key=lambda d: (d.time_msc or d.executed_at.timestamp(), d.ticket)
        ):
            self._ingest_deal(deal)
        if self._protective_resize_pending:
            self._retry_protective_resize()
        return self.ingest_stats

    def _ingest_deal(self, deal: DealRecord) -> bool:
        """Book one broker deal exactly once. Returns True if newly booked."""
        stats = self.ingest_stats
        if self._store.is_ingested(deal.ticket):
            stats.duplicates += 1
            return False
        if str(deal.kind) not in ("BUY", "SELL"):
            self._store.mark_ingested(deal.ticket, deal.order or None)
            stats.non_trade += 1
            return False
        mapping = self._provider.registry.by_broker_symbol(deal.symbol)
        if mapping is None:
            stats.foreign_symbol += 1  # not marked: no instrument to book it against
            return False
        row = self._row_for_deal(deal)
        try:
            if row is not None:
                self._book_known_fill(deal, row, mapping.instrument_id)
            else:
                self._book_external_fill(deal, mapping.instrument_id)
                stats.external += 1
        except Exception as exc:  # booking failed midway: leave UN-marked so it is retried
            stats.failed += 1
            self._failed_deals.add(deal.ticket)
            self.audit.append(f"deal {deal.ticket} booking failed: {exc!r}")
            return False
        # The fill IS booked in Nautilus now; only then is the deal "seen".
        self._failed_deals.discard(deal.ticket)
        self._store.mark_ingested(deal.ticket, deal.order or None)
        stats.ingested += 1
        try:  # best-effort follow-up; idempotent and re-run by reconcile()
            self._after_booking(deal, mapping.instrument_id)
        except Exception as exc:
            self.audit.append(f"deal {deal.ticket} follow-up failed: {exc!r}")
            self._protective_resize_pending = True  # the SL/TP resize is re-run on the next sync
        return True

    def _row_for_deal(self, deal: DealRecord) -> OrderRow | None:
        row = self._store.by_order_ticket(deal.order) if deal.order else None
        if row is not None:
            return row
        token = deal.comment.split("[")[0].strip() if deal.comment else ""
        if token.startswith("NT"):
            row = self._store.by_token(token)
            if row is not None:
                self._store.update_order(
                    row.client_order_id,
                    order_ticket=deal.order or None,
                    position_ticket=deal.position_id or None,
                )
                return row
        if int(deal.reason) in (int(DealReason.SL), int(DealReason.TP)):
            want = KIND_SL if int(deal.reason) == int(DealReason.SL) else KIND_TP
            for candidate in self._store.all_orders():
                if (
                    candidate.kind == want
                    and candidate.position_ticket == deal.position_id
                    and candidate.status in ("ACCEPTED", "DONE")
                ):
                    return candidate
        return None

    def _deal_common(self, deal: DealRecord, instrument_id: InstrumentId) -> dict[str, Any]:
        instrument = self._provider.find(instrument_id)
        ts_event, exact = self._ts_ns_from_server_epoch(
            (deal.time_msc / 1000.0) if deal.time_msc else deal.executed_at.timestamp()
        )
        # MT5 books costs as negatives; Nautilus commission is a positive cost.
        commission = -(deal.commission + deal.fee)
        return {
            "instrument": instrument,
            "ts_event": ts_event,
            "info": {
                "source": "MT5_DEAL",
                "deal_ticket": deal.ticket,
                "broker_profit": str(deal.profit),  # broker truth, audit only (Nautilus books PnL)
                "broker_swap": str(deal.swap),
                "broker_commission": str(deal.commission),
                "reason": int(deal.reason),
                "ts_event_exact": exact,
            },
            "commission": Money(commission, self._ccy),
            "side": OrderSide.BUY if str(deal.kind) == "BUY" else OrderSide.SELL,
            "qty": instrument.make_qty(float(deal.volume)),
            "px": instrument.make_price(float(deal.price)),
        }

    def _book_known_fill(
        self, deal: DealRecord, row: OrderRow, instrument_id: InstrumentId
    ) -> None:
        common = self._deal_common(deal, instrument_id)
        client_order_id = ClientOrderId(row.client_order_id)
        order = self._nt_cache.order(client_order_id)
        if order is None:
            # Cold cache (restart): hand Nautilus the fill as reports; its own reconciliation
            # de-duplicates by trade id. Not an external trade -> no mismatch flag.
            return self._book_external_fill(deal, instrument_id, row=row, flag=False)
        if TradeId(str(deal.ticket)) in order.trade_ids:
            return  # crash window: Nautilus already has this trade; just let the caller mark it
        strategy_id = order.strategy_id
        # A protective order keeps the venue id it was accepted with ("SL:<ticket>"); Nautilus
        # rejects a fill whose venue_order_id differs from the accepted one.
        venue_order_id = order.venue_order_id or VenueOrderId(
            str(deal.order) if deal.order else row.venue_order_id or "0"
        )
        if row.position_ticket is None and deal.position_id:
            self._store.update_order(row.client_order_id, position_ticket=deal.position_id)
        if row.venue_order_id is None or row.status in ("INTENT", "SENT", "IN_DOUBT"):
            self._store.update_order(
                row.client_order_id,
                status="ACCEPTED",
                order_ticket=deal.order or None,
                position_ticket=deal.position_id or None,
                venue_order_id=str(venue_order_id),
            )
            if order.status in (OrderStatus.SUBMITTED, OrderStatus.INITIALIZED):
                self.generate_order_accepted(
                    strategy_id, instrument_id, client_order_id, venue_order_id, common["ts_event"]
                )
        self._promote_children(row.client_order_id)  # a real fill proves the parent exists
        if order.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT) and (
            order.status == OrderStatus.ACCEPTED
        ):
            # A protective stop that the broker executed passes through TRIGGERED in Nautilus.
            self.generate_order_triggered(
                strategy_id, instrument_id, client_order_id, venue_order_id, common["ts_event"]
            )
        self.generate_order_filled(
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            venue_position_id=None,  # NETTING: Nautilus assigns the position id
            trade_id=TradeId(str(deal.ticket)),  # broker deal ticket == fill identity
            order_side=common["side"],
            order_type=order.order_type,
            last_qty=common["qty"],
            last_px=common["px"],
            quote_currency=common["instrument"].quote_currency,
            commission=common["commission"],
            liquidity_side=LiquiditySide.TAKER,
            ts_event=common["ts_event"],
            info=common["info"],
        )
        self._finalize_if_broker_terminal(row.client_order_id)

    def _book_external_fill(
        self,
        deal: DealRecord,
        instrument_id: InstrumentId,
        *,
        row: OrderRow | None = None,
        flag: bool = True,
    ) -> None:
        """A deal no Nautilus order accounts for: report it (order + fill) so Nautilus holds the
        truth, and flag reconciliation (handled in `_after_booking`)."""
        common = self._deal_common(deal, instrument_id)
        venue_order_id = VenueOrderId(
            (row.venue_order_id if row is not None and row.venue_order_id else None)
            or str(deal.order or deal.ticket)
        )
        client_order_id = ClientOrderId(row.client_order_id) if row is not None else None
        now = self._now_ns()
        order_report = OrderStatusReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            venue_order_id=venue_order_id,
            order_side=common["side"],
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            order_status=OrderStatus.FILLED,
            quantity=common["qty"],
            filled_qty=common["qty"],
            report_id=UUID4(),
            ts_accepted=common["ts_event"],
            ts_last=common["ts_event"],
            ts_init=now,
            avg_px=Decimal(str(deal.price)),
            client_order_id=client_order_id,
        )
        fill_report = FillReport(
            account_id=self.account_id,
            instrument_id=instrument_id,
            venue_order_id=venue_order_id,
            trade_id=TradeId(str(deal.ticket)),
            order_side=common["side"],
            last_qty=common["qty"],
            last_px=common["px"],
            commission=common["commission"],
            liquidity_side=LiquiditySide.TAKER,
            report_id=UUID4(),
            ts_event=common["ts_event"],
            ts_init=now,
            client_order_id=client_order_id,
        )
        self._send_order_status_report(order_report)
        self._send_fill_report(fill_report)
        if not flag:
            return
        self.recon.complete_mismatch(
            (
                *self.recon.discrepancies,
                Discrepancy(
                    kind=DiscrepancyKind.EXTERNAL_ACTIVITY,
                    detail=f"deal {deal.ticket} ({deal.comment!r}) matches no Nautilus order",
                ),
            )
        )

    def _after_booking(self, deal: DealRecord, instrument_id: InstrumentId) -> None:
        """Keep protective (SL/TP) order state consistent with what the broker did."""
        remaining = self._call(
            "positions_get", self._session.client.positions_get, ticket=deal.position_id
        )
        position_closed = len(remaining) == 0
        self._push_account_state()
        if not position_closed:
            self._resize_protection_after_partial(deal, instrument_id, remaining)
            return
        for row in self._store.all_orders():
            if (
                row.kind in (KIND_SL, KIND_TP)
                and row.position_ticket == deal.position_id
                and row.status == "ACCEPTED"
            ):
                order = self._nt_cache.order(ClientOrderId(row.client_order_id))
                if order is not None and order.is_open:
                    self.generate_order_canceled(
                        order.strategy_id,
                        instrument_id,
                        order.client_order_id,
                        order.venue_order_id or VenueOrderId(f"SL:{deal.position_id}"),
                        self._now_ns(),
                    )
                self._store.update_order(row.client_order_id, status="DONE")

    def _resize_protection_after_partial(
        self, deal: DealRecord, instrument_id: InstrumentId, remaining: Any
    ) -> None:
        """Lane E: a partial KIND_EXIT fill left a position open. MT5 keeps ONE position-wide SL/TP
        the broker (untouched by the partial close), but the local Nautilus SL/TP child orders still
        carry the ORIGINAL quantity. Shrink them to the broker's remaining volume so that
        broker_open_quantity == local_remaining_quantity == stop-protected quantity. Broker truth
        (the position just read) is the size; a child is never enlarged here."""
        row = self._row_for_deal(deal)
        # Lane E2: a partial ENTRY fill (IOC remainder cancelled) leaves the SL/TP children sized to
        # the REQUESTED quantity just the same: shrink to the filled volume (never enlarge).
        if row is None or row.kind not in (KIND_EXIT, KIND_MARKET):
            return
        volume = Decimal(str(remaining[0].volume))
        self._resync_protective_quantity(int(deal.position_id), instrument_id, volume)

    def _resync_protective_quantity(
        self, position_ticket: int, instrument_id: InstrumentId, volume: Decimal
    ) -> None:
        instrument = self._provider.find(instrument_id)
        target = instrument.make_qty(float(volume))
        for row in self._store.all_orders():
            if (
                row.kind not in (KIND_SL, KIND_TP)
                or row.position_ticket != position_ticket
                or row.status != "ACCEPTED"
            ):
                continue
            order = self._nt_cache.order(ClientOrderId(row.client_order_id))
            if order is None or not order.is_open or target >= order.quantity:
                continue  # cold cache / already closed / never enlarge a protective order
            is_sl = row.kind == KIND_SL
            self.generate_order_updated(
                order.strategy_id,
                instrument_id,
                order.client_order_id,
                order.venue_order_id or VenueOrderId(row.venue_order_id or "0"),
                target,
                None if is_sl else order.price,
                order.trigger_price if is_sl else None,
                self._now_ns(),
            )
            self.audit.append(
                f"PROTECTIVE_RESIZED {row.client_order_id}: {order.quantity} -> {target}"
            )

    def _retry_protective_resize(self) -> None:
        """Re-run a failed resize from broker truth (never guesses; clears only on success)."""
        try:
            positions = self._broker_positions()
            for position in positions:
                mapping = self._provider.registry.by_broker_symbol(position.symbol)
                if mapping is None:
                    continue
                self._resync_protective_quantity(
                    int(position.ticket), mapping.instrument_id, position.volume
                )
            self._protective_resize_pending = False
        except Exception as exc:  # stays pending; the next sync retries
            self.audit.append(f"protective resize retry failed: {exc!r}")

    # ------------------------------------------------------------- reconciliation --

    async def reconcile_async(self) -> ReconciliationTracker:
        return await self._lane_run(self.reconcile)

    def _identity_problem(self) -> str | None:
        """None if the attached terminal is still the expected account (and demo if required)."""
        try:
            account = self._call("account_info", self._session.client.account_info)
        except Mt5CallError as exc:
            return f"ACCOUNT_IDENTITY_UNVERIFIABLE:{exc}"
        expected = self._session.expected_login
        if expected is not None and int(account.login) != int(expected):
            return "ACCOUNT_IDENTITY_CHANGED"
        if self._cfg.require_demo_account and int(account.trade_mode) != 0:
            return "ACCOUNT_IS_NOT_DEMO"
        if self._cfg.expected_server and str(getattr(account, "server", "")) != str(
            self._cfg.expected_server
        ):
            return "ACCOUNT_SERVER_CHANGED"
        return None

    def local_view(self) -> LocalView:
        positions: dict[str, Decimal] = {}
        for mapping in self._provider.registry.all():
            signed = ZERO
            for position in self._nt_cache.positions_open(instrument_id=mapping.instrument_id):
                signed += Decimal(str(position.signed_qty))
            if signed != ZERO:
                positions[mapping.broker_symbol] = signed
        known = frozenset(
            r.order_ticket for r in self._store.all_orders() if r.order_ticket is not None
        )
        protected = frozenset(
            r.position_ticket
            for r in self._store.all_orders()
            if r.kind in (KIND_SL, KIND_TP) and r.status == "ACCEPTED" and r.position_ticket
        )
        sl_levels: dict[int, Decimal] = {}
        tp_levels: dict[int, Decimal] = {}
        for row in self._store.all_orders():
            if row.kind not in (KIND_SL, KIND_TP) or row.status != "ACCEPTED":
                continue
            if not row.position_ticket:
                continue
            order = self._nt_cache.order(ClientOrderId(row.client_order_id))
            if order is None:
                continue
            if row.kind == KIND_SL and getattr(order, "trigger_price", None) is not None:
                sl_levels[row.position_ticket] = Decimal(str(order.trigger_price))
            elif row.kind == KIND_TP and getattr(order, "price", None) is not None:
                tp_levels[row.position_ticket] = Decimal(str(order.price))
        return LocalView(
            positions=positions,
            protective_positions=protected,
            known_order_tickets=known,
            sl_levels=sl_levels,
            tp_levels=tp_levels,
        )

    def reconcile(self) -> ReconciliationTracker:
        """Compare REAL broker state with Nautilus' cache. Only a clean VENUE_SNAPSHOT
        comparison grants RECONCILED; anything else leaves the adapter halted/unreconciled."""
        with contextlib.ExitStack() as stack:
            for mapping in self._provider.registry.all():
                stack.enter_context(self._section(mapping.instrument_id))
            return self._reconcile_locked()

    def _reconcile_locked(self) -> ReconciliationTracker:
        self._check_generation()
        self.recon.begin()
        try:
            self.sync_once()  # 1) inbound truth first (fills, SL/TP executions, externals)
            self._resolve_in_doubt()  # 2) settle orders whose send outcome was unknown
            positions = self._broker_positions()
            orders = self._broker_open_orders()
        except Mt5CallError as exc:
            self.recon.invalidate(f"reconcile aborted: {exc}")
            return self.recon
        blocking = list(self.recon.discrepancies)  # e.g. EXTERNAL_ACTIVITY recorded on ingest
        if self._failed_deals:
            blocking.append(
                Discrepancy(
                    kind=DiscrepancyKind.DEAL_INGEST_FAILED,
                    detail=f"{len(self._failed_deals)} deal(s) could not be booked",
                )
            )
        for row in self._store.unresolved():
            blocking.append(
                Discrepancy(
                    kind=DiscrepancyKind.UNRESOLVED_ORDER,
                    detail=f"{row.client_order_id} outcome still unknown ({row.status})",
                )
            )
        found, unprotected = compare(
            snapshot_from_records(positions, orders),
            self.local_view(),
            registered_symbols=frozenset(m.broker_symbol for m in self._provider.registry.all()),
            require_protection=self._cfg.require_attached_protection,
        )
        blocking.extend(found)
        if blocking:
            self.recon.complete_mismatch(tuple(blocking))
        else:
            self.recon.complete_match(ReconciliationSource.VENUE_SNAPSHOT, unprotected=unprotected)
        return self.recon

    def _resolve_in_doubt(self) -> None:
        """Orders written ahead but never confirmed: adopt the broker's record by token, or
        (after the grace period, with no trace at the broker) reject as never executed."""
        client = self._session.client
        start, end = self._history_window()
        history = self._call("history_orders_get", client.history_orders_get, start, end)
        by_token = {str(o.comment).split("[")[0].strip(): o for o in history if o.comment}
        other_trace: set[str] | None = None  # tokens seen in open orders / positions / deals
        for row in self._store.unresolved():
            if row.parent_client_order_id:
                continue  # children are resolved together with their parent
            found = by_token.get(row.token)
            order = self._nt_cache.order(ClientOrderId(row.client_order_id))
            if found is not None:
                self._store.update_order(
                    row.client_order_id,
                    status="ACCEPTED",
                    order_ticket=int(found.ticket),
                    position_ticket=int(found.position_id) or None,
                    venue_order_id=str(int(found.ticket)),
                )
                if order is not None and order.status in (
                    OrderStatus.SUBMITTED,
                    OrderStatus.INITIALIZED,
                ):
                    self.generate_order_accepted(
                        order.strategy_id,
                        order.instrument_id,
                        order.client_order_id,
                        VenueOrderId(str(int(found.ticket))),
                        self._now_ns(),
                    )
                continue  # its deals (if any) were/are ingested by sync_once via the token
            age = self._now() - datetime.fromtimestamp(row.created_ns / NS, tz=UTC)
            if age < self._cfg.in_doubt_grace:
                continue
            if other_trace is None:
                other_trace = self._broker_trace_tokens()
            if row.token in other_trace:
                continue  # broker shows the order/position/deal: adopted via ingest
            # No trace at the broker (history, open orders, positions, deals of the lookback) after
            # the grace period: the send never happened. Decided from BROKER TRUTH, so it works
            # after a restart too, when the Nautilus cache is empty (order is None). Never re-sent.
            self._store.update_order(row.client_order_id, status="REJECTED")
            self.audit.append(
                f"NO_TRACE {row.client_order_id} token={row.token}: no order/position/deal at the "
                f"broker within the lookback; marked REJECTED, NOT re-sent"
            )
            if order is not None:
                self.generate_order_rejected(
                    order.strategy_id,
                    order.instrument_id,
                    order.client_order_id,
                    "NO_BROKER_RECORD_AFTER_RECONCILIATION",
                    self._now_ns(),
                )
                self._deny_children(order, "PARENT_NO_BROKER_RECORD")
            else:
                for child in self._store.children_of(row.client_order_id):
                    if child.status in ("INTENT", "SENT", "IN_DOUBT", "ACCEPTED"):
                        self._store.update_order(child.client_order_id, status="REJECTED")

    def _broker_trace_tokens(self) -> set[str]:
        """Tokens of OUR requests visible anywhere at the broker (open orders, positions, deals)."""
        comments: list[str] = []
        comments.extend(o.comment for o in self._broker_open_orders())
        comments.extend(p.comment for p in self._broker_positions())
        comments.extend(
            d.comment for d in self._broker_deals(self._now() - self._cfg.deal_lookback)
        )
        return {str(c).split("[")[0].strip() for c in comments if c}

    # ------------------------------------------------------------- controlled sync --

    def _ensure_sync_task(self) -> None:
        if not self._cfg.autostart_sync:
            return
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = self._loop.create_task(self._sync_loop(), name="mt5-exec-sync")

    def _reconnect_core(self) -> None:
        if self._session.reconnect().success:  # attach-only; NOT reconciled afterwards
            self._check_generation()

    async def _stop_sync_task(self) -> None:
        task, self._sync_task = self._sync_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _sync_loop(self) -> None:
        while True:
            try:
                await self._lane_run(self.sync_once)
                self._backoff_index = 0
                await asyncio.sleep(self._cfg.sync_interval_secs)
            except Mt5CallError as exc:
                self.last_sync_error = str(exc)
                self.recon.invalidate(f"sync failed: {exc}")
                table = self._cfg.reconnect_backoff_secs
                delay = table[min(self._backoff_index, len(table) - 1)]
                self._backoff_index += 1
                await asyncio.sleep(delay)
                await self._lane_run(self._reconnect_core)

    # ---------------------------------------------------------------- outbound: submit --

    @exposure_op
    async def _submit_order(self, command: Any) -> None:
        self.submit_sync(command.order)

    @exposure_op
    async def _submit_order_list(self, command: Any) -> None:
        orders = list(command.order_list.orders)
        parent = next((o for o in orders if not o.is_reduce_only), None)
        if parent is None:
            for order in orders:
                self._deny(order, "ORDER_LIST_WITHOUT_ENTRY")
            return
        children = [o for o in orders if o is not parent]
        sl_order = next((o for o in children if o.order_type == OrderType.STOP_MARKET), None)
        tp_order = next((o for o in children if o.order_type == OrderType.LIMIT), None)
        stray = [o for o in children if o is not sl_order and o is not tp_order]
        for order in stray:
            self._deny(order, "UNSUPPORTED_CHILD_ORDER_TYPE")
        self.submit_sync(parent, stop_loss=sl_order, take_profit=tp_order)
        parent_now = self._nt_cache.order(parent.client_order_id)
        if parent_now is not None and parent_now.status in (
            OrderStatus.DENIED,
            OrderStatus.REJECTED,
        ):
            self._deny_children(parent, f"PARENT_{parent_now.status.name}")

    def _deny(self, order: Any, reason: str) -> None:
        self.audit.append(f"DENIED {order.client_order_id}: {reason}")
        self.generate_order_denied(
            order.strategy_id, order.instrument_id, order.client_order_id, reason, self._now_ns()
        )

    def submit_sync(self, order: Any, *, stop_loss: Any = None, take_profit: Any = None) -> None:
        """Deterministic core of submit (also driven directly by tests)."""
        with self._section(order.instrument_id):
            if self._store.by_client_order_id(str(order.client_order_id)) is not None:
                # Re-delivery of a ClientOrderId we already recorded: NEVER send a second request.
                return self._deny(order, "DUPLICATE_CLIENT_ORDER_ID_ALREADY_RECORDED")
            self._submit_locked(order, stop_loss, take_profit)

    def _submit_locked(self, order: Any, stop_loss: Any, take_profit: Any) -> None:
        if order.order_type == OrderType.MARKET:
            if order.is_reduce_only:
                self._submit_reduce_only(order)
            else:
                self._submit_entry(order, stop_loss, take_profit)
        elif order.is_reduce_only and order.order_type in (OrderType.STOP_MARKET, OrderType.LIMIT):
            self._submit_protective(order)
        else:
            self._deny(order, f"UNSUPPORTED_ORDER_TYPE_{order.order_type.name}")

    def _prepare(
        self,
        order: Any,
        kind: OutboundKind,
        *,
        position_verified: bool = False,
        defer_admission: bool = False,
    ) -> Any:
        """Shared pre-flight: session, gate. Returns spec+mapping or None (already denied).

        ``defer_admission``: the caller admits itself after reading the broker position (the
        reduce-only path, whose gate depends on a broker-verified own position)."""
        self._check_generation()
        if not self._session.is_connected:
            self._deny(order, f"SESSION_{self._session.state.value}")
            return None
        if not defer_admission:
            admission = admit(kind, self.status, position_verified=position_verified)
            if not admission.ok:
                self._deny(order, admission.reason)
                return None
        mapping = self._provider.registry.by_instrument_id(order.instrument_id)
        return mapping, self._provider.spec(order.instrument_id)

    def _filling_mask(self, broker_symbol: str) -> int:
        info = self._call("symbol_info", self._session.client.symbol_info, broker_symbol)
        return int(info.filling_mode)

    def _local_signed(self, instrument_id: InstrumentId) -> Decimal:
        return sum(
            (
                Decimal(str(p.signed_qty))
                for p in self._nt_cache.positions_open(instrument_id=instrument_id)
            ),
            ZERO,
        )

    def _submit_entry(self, order: Any, sl_order: Any, tp_order: Any) -> None:
        prepared = self._prepare(order, OutboundKind.NEW_EXPOSURE)
        if prepared is None:
            return
        mapping, spec = prepared
        try:
            existing = self._broker_positions(mapping.broker_symbol)
            if existing or self._local_signed(order.instrument_id) != ZERO:
                # v1: one position per instrument, no pyramiding, no same-tick flip.
                return self._deny(order, "POSITION_EXISTS_V1_ONE_POSITION_PER_INSTRUMENT")
            if self._store.unresolved():
                return self._deny(order, "UNRESOLVED_ORDER_OUTCOME")
            quote = self._quote(mapping.broker_symbol)
            sl = Decimal(str(sl_order.trigger_price)) if sl_order is not None else None
            tp = Decimal(str(tp_order.price)) if tp_order is not None else None
            if self._cfg.require_attached_protection and sl is None:
                return self._deny(order, "ENTRY_WITHOUT_ATTACHED_STOP_LOSS")
            token = self._store.record_intent(
                created_ns=self._now_ns(),
                client_order_id=str(order.client_order_id),
                strategy_id=str(order.strategy_id),
                instrument_id=str(order.instrument_id),
                kind=KIND_MARKET,
                side=order.side.name,
                quantity=str(order.quantity),
            )
            for child, kind in ((sl_order, KIND_SL), (tp_order, KIND_TP)):
                if child is not None:
                    self._store.record_intent(
                        created_ns=self._now_ns(),
                        client_order_id=str(child.client_order_id),
                        strategy_id=str(child.strategy_id),
                        instrument_id=str(child.instrument_id),
                        kind=kind,
                        side=child.side.name,
                        quantity=str(child.quantity),
                        parent_client_order_id=str(order.client_order_id),
                    )
            request = market_entry_request(
                spec,
                symbol_filling_mask=self._filling_mask(mapping.broker_symbol),
                is_buy=order.side == OrderSide.BUY,
                quantity=Decimal(str(order.quantity)),
                quote=quote,
                magic=self._cfg.magic,
                token=token,
                deviation_points=self._cfg.deviation_points,
                stop_loss=sl,
                take_profit=tp,
            )
        except RequestRejected as exc:
            self._store.update_order(str(order.client_order_id), status="REJECTED") if (
                self._store.by_client_order_id(str(order.client_order_id))
            ) else None
            return self._deny(order, f"{exc.code}:{exc.detail}")
        except Mt5CallError as exc:
            self.recon.invalidate(str(exc))
            return self._deny(order, f"BROKER_UNREACHABLE:{exc}")
        self._send_market(order, request, mapping.instrument_id, sl_order, tp_order)

    def _submit_reduce_only(self, order: Any) -> None:
        prepared = self._prepare(order, OutboundKind.REDUCE_ONLY, defer_admission=True)
        if prepared is None:
            return
        mapping, spec = prepared
        try:
            positions = self._broker_positions(mapping.broker_symbol)
            base = admit(OutboundKind.REDUCE_ONLY, self.status)  # normal (reconciled) authority
            if len(positions) != 1:
                return self._deny(
                    order,
                    f"REDUCE_ONLY_BROKER_POSITIONS_{len(positions)}"
                    if base.ok
                    else base.reason,
                )
            position = positions[0]
            position_is_long = str(position.side) == "BUY"
            quantity = Decimal(str(order.quantity))
            # A position read from the broker just now, OWN (our magic), closed by an order on the
            # opposite side that is not larger than the position can only REDUCE exposure: it is
            # allowed in every runtime/reconciliation state (the flatten of last resort).
            verified = (
                int(position.magic) == int(self._cfg.magic)
                and (order.side == OrderSide.SELL) == position_is_long
                and quantity <= position.volume
            )
            admission = admit(OutboundKind.REDUCE_ONLY, self.status, position_verified=verified)
            if not admission.ok:
                return self._deny(order, admission.reason)
            broker_signed = position.volume if position_is_long else -position.volume
            if base.ok and self._local_signed(order.instrument_id) != broker_signed:
                # Only enforced under normal authority; an unreconciled state is exactly when
                # local bookkeeping may be stale, and broker truth already proves the reduction.
                return self._deny(order, "REDUCE_ONLY_LOCAL_BROKER_POSITION_MISMATCH")
            if (order.side == OrderSide.SELL) != position_is_long:
                return self._deny(order, "REDUCE_ONLY_WRONG_SIDE")
            if quantity > position.volume:
                return self._deny(order, "REDUCE_ONLY_EXCEEDS_POSITION")  # never clamp silently
            token = self._store.record_intent(
                created_ns=self._now_ns(),
                client_order_id=str(order.client_order_id),
                strategy_id=str(order.strategy_id),
                instrument_id=str(order.instrument_id),
                kind=KIND_EXIT,
                side=order.side.name,
                quantity=str(order.quantity),
                position_ticket=position.ticket,
            )
            request = close_request(
                spec,
                symbol_filling_mask=self._filling_mask(mapping.broker_symbol),
                position_ticket=position.ticket,
                position_is_long=position_is_long,
                quantity=quantity,
                quote=self._quote(mapping.broker_symbol),
                magic=self._cfg.magic,
                token=token,
                deviation_points=self._cfg.deviation_points,
            )
        except RequestRejected as exc:
            return self._deny(order, f"{exc.code}:{exc.detail}")
        except Mt5CallError as exc:
            self.recon.invalidate(str(exc))
            return self._deny(order, f"BROKER_UNREACHABLE:{exc}")
        self._send_market(order, request, mapping.instrument_id, None, None)

    def _send_market(
        self,
        order: Any,
        request: dict[str, Any],
        instrument_id: InstrumentId,
        sl_order: Any,
        tp_order: Any,
    ) -> None:
        cid = str(order.client_order_id)
        self.generate_order_submitted(
            order.strategy_id, instrument_id, order.client_order_id, self._now_ns()
        )
        # order_check: success is retcode 0 ("Done") -- a different namespace than order_send.
        try:
            check = self._call("order_check", self._session.client.order_check, request)
        except Mt5CallError as exc:
            self._store.update_order(cid, status="REJECTED")
            self.recon.invalidate(str(exc))
            return self._reject(order, f"ORDER_CHECK_UNAVAILABLE:{exc}")
        if int(check.retcode) != ORDER_CHECK_OK:
            self._store.update_order(cid, status="REJECTED")
            return self._reject(order, f"ORDER_CHECK_{int(check.retcode)}:{check.comment}")

        pre_volume: Decimal | None = None
        if "position" in request:  # reduce-only: the broker position may have moved since admission
            try:
                rows = self._broker_positions_by_ticket(int(request["position"]))
            except Mt5CallError as exc:
                self._store.update_order(cid, status="REJECTED")
                self.recon.invalidate(str(exc))
                return self._reject(order, f"REDUCE_ONLY_RECHECK_UNAVAILABLE:{exc}")
            wanted_side = "SELL" if request["type"] == 0 else "BUY"  # position side being closed
            if (
                not rows
                or str(rows[0].side) != wanted_side
                or rows[0].volume < Decimal(str(request["volume"]))
            ):
                self._store.update_order(cid, status="REJECTED")
                return self._reject(order, "REDUCE_ONLY_POSITION_CHANGED_BEFORE_SEND")
            pre_volume = rows[0].volume
        if self._cfg.dry_run:
            self._store.update_order(cid, status="REJECTED")
            self.audit.append(f"DRY_RUN order_check OK, order_send NOT called: {request}")
            return self._reject(order, f"DRY_RUN_ORDER_CHECK_OK:{check.comment}")
        problem = self._identity_problem()
        if problem is not None:
            self._store.update_order(cid, status="REJECTED")
            self.recon.invalidate(problem)
            return self._reject(order, problem)
        self._store.update_order(cid, status="SENT")
        try:
            result = self._session.client.order_send(request)
        except Exception as exc:
            return self._in_doubt(order, f"order_send raised: {exc!r}")
        if result is None:
            return self._in_doubt(
                order, f"order_send returned None (last_error={self._session.last_error()})"
            )
        outcome = classify_send_retcode(int(result.retcode))
        if outcome is SendOutcome.REJECTED:
            self._store.update_order(cid, status="REJECTED")
            return self._reject(order, f"ORDER_SEND_{int(result.retcode)}:{result.comment}")
        if outcome is SendOutcome.IN_DOUBT:
            return self._in_doubt(order, f"retcode {int(result.retcode)} {result.comment}")

        venue_order_id = VenueOrderId(str(int(result.order)))
        self._store.update_order(
            cid,
            status="ACCEPTED",
            order_ticket=int(result.order),
            venue_order_id=str(venue_order_id),
        )
        self.generate_order_accepted(
            order.strategy_id, instrument_id, order.client_order_id, venue_order_id, self._now_ns()
        )
        self._book_deals_of_order(int(result.order))
        if pre_volume is not None:
            self._verify_reduce_effect(int(request["position"]), pre_volume, request["symbol"])
        self._promote_children(cid)
        self._finalize_if_broker_terminal(cid)

    def _verify_reduce_effect(self, ticket: int, pre_volume: Decimal, symbol: str) -> None:
        """A reduce-only order must never leave MORE exposure than before (or a flipped side)."""
        try:
            rows = self._broker_positions(symbol)
        except Mt5CallError:
            self.recon.invalidate("reduce-only effect unverifiable")
            return
        for position in rows:
            if position.volume > pre_volume or position.ticket != ticket:
                self.recon.complete_mismatch(
                    (
                        *self.recon.discrepancies,
                        Discrepancy(
                            kind=DiscrepancyKind.POSITION_QTY_MISMATCH,
                            detail=f"reduce-only left {position.volume} (was {pre_volume}) "
                            f"on ticket {position.ticket}",
                        ),
                    )
                )

    def _book_deals_of_order(self, order_ticket: int) -> None:
        for deal in self._broker_deals(self._now() - self._cfg.deal_lookback):
            if deal.order == order_ticket:
                self._ingest_deal(deal)

    def _finalize_if_broker_terminal(self, client_order_id: str) -> None:
        """MT5 market orders never rest: once the broker's order is CANCELED/REJECTED/EXPIRED
        (IOC remainder), Nautilus' still-open order must be closed too. Derived from broker
        history (not from the send retcode) so a delayed deal cannot leave it PARTIALLY_FILLED."""
        row = self._store.by_client_order_id(client_order_id)
        order = self._nt_cache.order(ClientOrderId(client_order_id))
        if (
            row is None
            or order is None
            or not order.is_open
            or row.kind not in (KIND_MARKET, KIND_EXIT)
            or row.order_ticket is None
        ):
            return
        try:
            history = self._call(
                "history_orders_get",
                self._session.client.history_orders_get,
                ticket=row.order_ticket,
            )
        except Mt5CallError:
            return
        if history and int(history[0].state) in (2, 5, 6):  # CANCELED / REJECTED / EXPIRED
            self.generate_order_canceled(
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                order.venue_order_id or VenueOrderId(str(row.order_ticket)),
                self._now_ns(),
            )

    def _promote_children(self, parent_client_order_id: str) -> None:
        """Bracket children (SL/TP) become live protective orders once their parent exists at
        the broker. Idempotent: only children still in INTENT are promoted."""
        parent_row = self._store.by_client_order_id(parent_client_order_id)
        if parent_row is None:
            return
        pending = [
            c
            for c in self._store.children_of(parent_client_order_id)
            if c.status in ("INTENT", "IN_DOUBT")
        ]
        if not pending:
            return
        position_ticket = parent_row.position_ticket
        if position_ticket is None:
            symbol = self._provider.registry.by_instrument_id(
                InstrumentId.from_str(parent_row.instrument_id)
            ).broker_symbol
            positions = self._broker_positions(symbol)
            position_ticket = positions[0].ticket if positions else None
        if position_ticket is None:
            return
        for child in pending:
            order = self._nt_cache.order(ClientOrderId(child.client_order_id))
            venue_id = VenueOrderId(f"{'SL' if child.kind == KIND_SL else 'TP'}:{position_ticket}")
            self._store.update_order(
                child.client_order_id,
                status="ACCEPTED",
                venue_order_id=str(venue_id),
                position_ticket=position_ticket,
            )
            if order is not None and order.status == OrderStatus.INITIALIZED:
                self.generate_order_submitted(
                    order.strategy_id, order.instrument_id, order.client_order_id, self._now_ns()
                )
                self.generate_order_accepted(
                    order.strategy_id,
                    order.instrument_id,
                    order.client_order_id,
                    venue_id,
                    self._now_ns(),
                )

    def _deny_children(self, parent: Any, reason: str) -> None:
        for child in self._store.children_of(str(parent.client_order_id)):
            if child.status in ("INTENT", "ACCEPTED"):
                self._store.update_order(child.client_order_id, status="REJECTED")
            order = self._nt_cache.order(ClientOrderId(child.client_order_id))
            if order is not None and order.status == OrderStatus.INITIALIZED:
                self._deny(order, reason)

    def _reject(self, order: Any, reason: str) -> None:
        self.audit.append(f"REJECTED {order.client_order_id}: {reason}")
        self.generate_order_rejected(
            order.strategy_id, order.instrument_id, order.client_order_id, reason, self._now_ns()
        )

    def _in_doubt(self, order: Any, why: str) -> None:
        """Outcome unknown: NOT a rejection. Exposure may exist -> lose authority, reconcile."""
        self.audit.append(f"IN_DOUBT {order.client_order_id}: {why}")
        self._store.update_order(str(order.client_order_id), status="IN_DOUBT")
        self.recon.invalidate(f"ORDER_OUTCOME_UNKNOWN {order.client_order_id}")
        self._session.mark_degraded(f"order outcome unknown: {why}")

    # ------------------------------------------------------- protective (SL/TP) orders --

    def _submit_protective(self, order: Any) -> None:
        is_sl = order.order_type == OrderType.STOP_MARKET
        level = Decimal(str(order.trigger_price if is_sl else order.price))
        mapping = self._provider.registry.by_instrument_id(order.instrument_id)
        # tighten-vs-loosen decides the gate, so read the broker position first (evidence).
        if not self._session.is_connected:
            return self._deny(order, f"SESSION_{self._session.state.value}")
        try:
            positions = self._broker_positions(mapping.broker_symbol)
        except Mt5CallError as exc:
            self.recon.invalidate(str(exc))
            return self._deny(order, f"BROKER_UNREACHABLE:{exc}")
        if len(positions) != 1:
            return self._deny(order, f"PROTECTIVE_BROKER_POSITIONS_{len(positions)}")
        position = positions[0]
        outcome = self.apply_protection(
            position,
            stop_loss=level if is_sl else None,
            take_profit=None if is_sl else level,
            order=order,
        )
        if outcome is not None:
            self._deny(order, outcome)

    def apply_protection(
        self,
        position: PositionRecord,
        *,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
        order: Any | None = None,
    ) -> str | None:
        """Set SL/TP on a broker position. Returns a denial reason or None on success.

        Used both for a Nautilus protective order (`order` given) and for `emergency_protect`
        (no Nautilus order: the entry filled remotely while local state was lost)."""
        mapping = self._provider.registry.by_broker_symbol(position.symbol)
        if mapping is None:
            return "PROTECT_UNKNOWN_SYMBOL"
        spec = self._provider.spec(mapping.instrument_id)
        is_long = str(position.side) == "BUY"
        if order is not None:
            if Decimal(str(order.quantity)) != position.volume:
                return "PROTECTIVE_QUANTITY_MUST_EQUAL_POSITION"  # MT5 SL is per position
            if (order.side == OrderSide.SELL) != is_long:
                return "PROTECTIVE_WRONG_SIDE"
        try:
            quote = self._quote(position.symbol)
            validate_stops(
                spec,
                position_is_long=is_long,
                quote=quote,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            tightening = stop_loss is None or is_tightening(
                position_is_long=is_long, current_sl=position.stop_loss, new_sl=stop_loss
            )
            if take_profit is not None and position.take_profit is not None:
                tightening = tightening and (
                    take_profit <= position.take_profit
                    if is_long
                    else take_profit >= position.take_profit
                )
            kind = (
                OutboundKind.PROTECT_TIGHTEN if tightening else OutboundKind.PROTECT_LOOSEN_REMOVE
            )
            admission = admit(
                kind, self.status, position_verified=True
            )  # ticket just read from broker
            if not admission.ok:
                return admission.reason
            request = sltp_request(
                spec,
                position_ticket=position.ticket,
                stop_loss=stop_loss,
                take_profit=take_profit,
                keep_sl=position.stop_loss,
                keep_tp=position.take_profit,
                magic=self._cfg.magic,
            )
            check = self._call("order_check", self._session.client.order_check, request)
            if int(check.retcode) != ORDER_CHECK_OK:
                return f"ORDER_CHECK_{int(check.retcode)}:{check.comment}"
        except RequestRejected as exc:
            return f"{exc.code}:{exc.detail}"
        except Mt5CallError as exc:
            self.recon.invalidate(str(exc))
            return f"BROKER_UNREACHABLE:{exc}"

        problem = self._identity_problem()
        if problem is not None:
            self.recon.invalidate(problem)
            return problem
        cid = str(order.client_order_id) if order is not None else None
        kind_name = KIND_SL if stop_loss is not None else KIND_TP
        if order is not None:
            self._store.record_intent(
                created_ns=self._now_ns(),
                client_order_id=cid,
                strategy_id=str(order.strategy_id),
                instrument_id=str(order.instrument_id),
                kind=kind_name,
                side=order.side.name,
                quantity=str(order.quantity),
                position_ticket=position.ticket,
            )
            self.generate_order_submitted(
                order.strategy_id, order.instrument_id, order.client_order_id, self._now_ns()
            )
        try:
            result = self._session.client.order_send(request)
        except Exception as exc:
            result = None
            why = f"order_send raised: {exc!r}"
        else:
            why = "order_send returned None"
        verified = self._sl_tp_present(position.ticket, stop_loss, take_profit)
        if result is not None:
            outcome = classify_send_retcode(int(result.retcode))
        else:
            outcome = SendOutcome.ACCEPTED_PLACED if verified else SendOutcome.IN_DOUBT
        if outcome is SendOutcome.REJECTED:
            if order is not None:
                self._store.update_order(cid, status="REJECTED")
                self._reject(order, f"ORDER_SEND_{int(result.retcode)}:{result.comment}")
                return None
            return f"ORDER_SEND_{int(result.retcode)}:{result.comment}"  # caller must know
        if outcome is SendOutcome.IN_DOUBT or (
            outcome is not SendOutcome.REJECTED and not verified
        ):
            if order is not None:
                self._in_doubt(order, why if result is None else f"retcode {int(result.retcode)}")
            self.recon.invalidate("PROTECTIVE_OUTCOME_UNKNOWN")
            return None if order is not None else "PROTECTIVE_OUTCOME_UNKNOWN"
        if order is not None:
            venue_id = VenueOrderId(f"{'SL' if stop_loss is not None else 'TP'}:{position.ticket}")
            self._store.update_order(cid, status="ACCEPTED", venue_order_id=str(venue_id))
            self.generate_order_accepted(
                order.strategy_id,
                order.instrument_id,
                order.client_order_id,
                venue_id,
                self._now_ns(),
            )
        return None

    def _sl_tp_present(self, ticket: int, sl: Decimal | None, tp: Decimal | None) -> bool:
        try:
            rows = self._broker_positions_by_ticket(ticket)
        except Mt5CallError:
            return False
        if not rows:
            return False
        pos = rows[0]
        return (sl is None or pos.stop_loss == sl) and (tp is None or pos.take_profit == tp)

    def _broker_positions_by_ticket(self, ticket: int) -> list[PositionRecord]:
        raw = self._call("positions_get", self._session.client.positions_get, ticket=ticket)
        return [position_to_position_record(p) for p in raw]

    def emergency_protect(self, position_ticket: int, stop_loss: Decimal) -> str | None:
        """Protect a broker position that Nautilus does not know about (entry filled remotely,
        local reconciliation lost). Tighten-only, requires fresh broker evidence of the ticket,
        allowed in ANY reconciliation state. Returns a denial reason or None."""
        return self._emergency_protect_locked(position_ticket, stop_loss)

    def _emergency_protect_locked(self, position_ticket: int, stop_loss: Decimal) -> str | None:
        if not self._session.is_connected:
            return f"SESSION_{self._session.state.value}"
        try:
            rows = self._broker_positions_by_ticket(position_ticket)
        except Mt5CallError as exc:
            return f"BROKER_UNREACHABLE:{exc}"
        if len(rows) != 1:
            return "PROTECT_TICKET_NOT_AT_BROKER"
        return self.apply_protection(rows[0], stop_loss=stop_loss, take_profit=None)

    # ------------------------------------------------------------ modify / cancel --

    @exposure_op
    async def _modify_order(self, command: Any) -> None:
        row = self._store.by_client_order_id(str(command.client_order_id))
        order = self._nt_cache.order(command.client_order_id)
        if (
            row is None
            or order is None
            or row.kind not in (KIND_SL, KIND_TP)
            or row.status != "ACCEPTED"
        ):
            return self.generate_order_modify_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                "ONLY_ACCEPTED_PROTECTIVE_ORDERS_CAN_BE_MODIFIED",
                self._now_ns(),
            )
        level = command.trigger_price if row.kind == KIND_SL else command.price
        if level is None:
            return self.generate_order_modify_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                "NO_NEW_LEVEL",
                self._now_ns(),
            )
        try:
            rows = self._broker_positions_by_ticket(int(row.position_ticket))
        except Mt5CallError as exc:
            self.recon.invalidate(str(exc))
            return self.generate_order_modify_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                f"BROKER_UNREACHABLE:{exc}",
                self._now_ns(),
            )
        if not rows:
            return self.generate_order_modify_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                "POSITION_GONE",
                self._now_ns(),
            )
        reason = self.apply_protection(
            rows[0],
            stop_loss=Decimal(str(level)) if row.kind == KIND_SL else None,
            take_profit=Decimal(str(level)) if row.kind == KIND_TP else None,
        )
        if reason is not None:
            return self.generate_order_modify_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                reason,
                self._now_ns(),
            )
        self.generate_order_updated(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            command.venue_order_id or VenueOrderId(row.venue_order_id or "0"),
            order.quantity,
            command.price or order.price if hasattr(order, "price") else None,
            command.trigger_price or getattr(order, "trigger_price", None),
            self._now_ns(),
        )

    @exposure_op
    async def _cancel_order(self, command: Any) -> None:
        row = self._store.by_client_order_id(str(command.client_order_id))
        if row is None or row.kind not in (KIND_SL, KIND_TP) or row.status != "ACCEPTED":
            return self.generate_order_cancel_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                "NOT_A_WORKING_PROTECTIVE_ORDER",
                self._now_ns(),
            )
        # Removing protection increases risk: needs READY + RECONCILED (venue snapshot).
        admission = admit(OutboundKind.PROTECT_LOOSEN_REMOVE, self.status)
        if not admission.ok:
            return self.generate_order_cancel_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                admission.reason,
                self._now_ns(),
            )
        try:
            rows = self._broker_positions_by_ticket(int(row.position_ticket))
            if rows:
                pos = rows[0]
                request = sltp_request(
                    self._provider.spec(command.instrument_id),
                    position_ticket=pos.ticket,
                    stop_loss=None if row.kind == KIND_SL else pos.stop_loss,
                    take_profit=None if row.kind == KIND_TP else pos.take_profit,
                    magic=self._cfg.magic,
                )
                problem = self._identity_problem()
                if problem is not None:
                    self.recon.invalidate(problem)
                    return self.generate_order_cancel_rejected(
                        command.strategy_id,
                        command.instrument_id,
                        command.client_order_id,
                        command.venue_order_id,
                        problem,
                        self._now_ns(),
                    )
                try:
                    result = self._session.client.order_send(request)
                except Exception:
                    result = None
                if result is None or classify_send_retcode(int(result.retcode)) is (
                    SendOutcome.IN_DOUBT
                ):
                    # Unknown outcome: the SL may or may not be gone. Drop authority, then verify
                    # against the broker below instead of guessing.
                    self.recon.invalidate("PROTECTIVE_CANCEL_OUTCOME_UNKNOWN")
                elif classify_send_retcode(int(result.retcode)) not in (
                    SendOutcome.ACCEPTED_FILLED,
                    SendOutcome.ACCEPTED_PLACED,
                ):
                    return self.generate_order_cancel_rejected(
                        command.strategy_id,
                        command.instrument_id,
                        command.client_order_id,
                        command.venue_order_id,
                        "BROKER_REFUSED",
                        self._now_ns(),
                    )
        except Mt5CallError as exc:
            self.recon.invalidate(str(exc))
            return self.generate_order_cancel_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                f"BROKER_UNREACHABLE:{exc}",
                self._now_ns(),
            )
        removed = True
        try:
            check = self._broker_positions_by_ticket(int(row.position_ticket))
            if check:
                still = check[0].stop_loss if row.kind == KIND_SL else check[0].take_profit
                removed = still is None
        except Mt5CallError:
            removed = False
        if not removed:
            self.recon.invalidate("PROTECTIVE_CANCEL_UNVERIFIED")
            return self.generate_order_cancel_rejected(
                command.strategy_id,
                command.instrument_id,
                command.client_order_id,
                command.venue_order_id,
                "PROTECTION_STILL_PRESENT_AT_BROKER",
                self._now_ns(),
            )
        self._store.update_order(row.client_order_id, status="CANCELED")
        self.generate_order_canceled(
            command.strategy_id,
            command.instrument_id,
            command.client_order_id,
            command.venue_order_id or VenueOrderId(row.venue_order_id or "0"),
            self._now_ns(),
        )

    @lane_op
    async def _cancel_all_orders(self, command: Any) -> None:
        for row in self._store.all_orders():
            if row.status == "ACCEPTED" and row.kind in (KIND_SL, KIND_TP):
                order = self._nt_cache.order(ClientOrderId(row.client_order_id))
                if order is not None and order.instrument_id == command.instrument_id:
                    from types import SimpleNamespace

                    await self._cancel_order(
                        SimpleNamespace(
                            client_order_id=order.client_order_id,
                            strategy_id=order.strategy_id,
                            instrument_id=order.instrument_id,
                            venue_order_id=order.venue_order_id,
                        )
                    )

    @lane_op
    async def _batch_cancel_orders(self, command: Any) -> None:
        for cancel in command.cancels:
            await self._cancel_order(cancel)

    # ------------------------------------------------------------------- reports --

    def _order_report_from_row(self, row: OrderRow) -> OrderStatusReport | None:
        order = self._nt_cache.order(ClientOrderId(row.client_order_id))
        if order is None:
            return None
        return OrderStatusReport(
            account_id=self.account_id,
            instrument_id=order.instrument_id,
            venue_order_id=VenueOrderId(row.venue_order_id or (str(row.order_ticket or 0))),
            order_side=order.side,
            order_type=order.order_type,
            time_in_force=order.time_in_force,
            order_status=order.status
            if order.status != OrderStatus.INITIALIZED
            else OrderStatus.SUBMITTED,
            quantity=order.quantity,
            filled_qty=order.filled_qty,
            report_id=UUID4(),
            ts_accepted=order.ts_init,
            ts_last=order.ts_last,
            ts_init=self._now_ns(),
            client_order_id=order.client_order_id,
            reduce_only=order.is_reduce_only,
        )

    @lane_op
    async def generate_order_status_report(self, command: Any) -> OrderStatusReport | None:
        for row in self._store.all_orders():
            if (
                command.client_order_id is not None
                and row.client_order_id == str(command.client_order_id)
            ) or (
                command.venue_order_id is not None
                and row.venue_order_id == str(command.venue_order_id)
            ):
                return self._order_report_from_row(row)
        return None

    @lane_op
    async def generate_order_status_reports(self, command: Any) -> list[OrderStatusReport]:
        reports = []
        for row in self._store.all_orders():
            if command.instrument_id is not None and row.instrument_id != str(
                command.instrument_id
            ):
                continue
            if command.open_only and row.status not in ("ACCEPTED", "SENT", "IN_DOUBT"):
                continue
            report = self._order_report_from_row(row)
            if report is not None:
                reports.append(report)
        return reports

    @lane_op
    async def generate_fill_reports(self, command: Any) -> list[FillReport]:
        reports = []
        for deal in self._broker_deals(self._now() - self._cfg.deal_lookback):
            mapping = self._provider.registry.by_broker_symbol(deal.symbol)
            if mapping is None or str(deal.kind) not in ("BUY", "SELL"):
                continue
            if command.instrument_id is not None and mapping.instrument_id != command.instrument_id:
                continue
            common = self._deal_common(deal, mapping.instrument_id)
            row = self._row_for_deal(deal)
            reports.append(
                FillReport(
                    account_id=self.account_id,
                    instrument_id=mapping.instrument_id,
                    venue_order_id=VenueOrderId(
                        (row.venue_order_id if row is not None and row.venue_order_id else None)
                        or str(deal.order or deal.ticket)
                    ),
                    trade_id=TradeId(str(deal.ticket)),
                    order_side=common["side"],
                    last_qty=common["qty"],
                    last_px=common["px"],
                    commission=common["commission"],
                    liquidity_side=LiquiditySide.TAKER,
                    report_id=UUID4(),
                    ts_event=common["ts_event"],
                    ts_init=self._now_ns(),
                    client_order_id=ClientOrderId(row.client_order_id) if row else None,
                )
            )
        return reports

    @lane_op
    async def generate_position_status_reports(self, command: Any) -> list[PositionStatusReport]:
        reports = []
        broker = {p.symbol: p for p in self._broker_positions()}
        for mapping in self._provider.registry.all():
            if command.instrument_id is not None and mapping.instrument_id != command.instrument_id:
                continue
            instrument = self._provider.find(mapping.instrument_id)
            position = broker.get(mapping.broker_symbol)
            if position is None:  # explicit FLAT so Nautilus can close a stale local position
                side, qty, avg = PositionSide.FLAT, instrument.make_qty(0.0), None
            else:
                side = PositionSide.LONG if str(position.side) == "BUY" else PositionSide.SHORT
                qty, avg = instrument.make_qty(float(position.volume)), position.price_open
            reports.append(
                PositionStatusReport(
                    account_id=self.account_id,
                    instrument_id=mapping.instrument_id,
                    position_side=side,
                    quantity=qty,
                    report_id=UUID4(),
                    ts_last=self._now_ns(),
                    ts_init=self._now_ns(),
                    avg_px_open=avg,
                )
            )
        return reports

    @lane_op
    async def generate_mass_status(
        self, lookback_mins: int | None = None
    ) -> ExecutionMassStatus | None:
        mass = ExecutionMassStatus(
            client_id=self.id,
            account_id=self.account_id,
            venue=VENUE,
            report_id=UUID4(),
            ts_init=self._now_ns(),
        )
        from types import SimpleNamespace

        every = SimpleNamespace(instrument_id=None, open_only=False, venue_order_id=None)
        mass.add_order_reports(await self.generate_order_status_reports(every))
        mass.add_fill_reports(await self.generate_fill_reports(every))
        mass.add_position_reports(await self.generate_position_status_reports(every))
        return mass


__all__ = [
    "Mt5ExecClientConfig",
    "Mt5LiveExecutionClient",
    "Price",
    "ReconciliationState",
    "RuntimeMode",
]
