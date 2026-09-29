"""Nautilus `LiveMarketDataClient` for ActivTrades MT5 (quotes + completed bars).

MT5's Python API is pull-only, so one CONTROLLED poller (a single asyncio task,
fixed interval, cancelled on disconnect / last unsubscribe, bounded reconnect
back-off) drives `poll_once()`. `poll_once()` is a plain synchronous method so
tests are deterministic and never need a real loop or sleep.

Semantics:
- Quotes: from `symbol_info_tick`. De-duplicated by (time_msc, bid, ask): an
  unchanged tick is never re-published. Bid/ask sizes are UNKNOWN for a CFD
  and published as 0 (never a fabricated depth).
- Bars: only COMPLETED bars (open + timeframe <= server-now); stamped at bar
  CLOSE like C4; MT5 bars are BID-priced so only `BID` + EXTERNAL bar types are
  accepted. De-duplicated per bar type by open time.
- Timestamps: MT5 `time` is SERVER-clock epoch. Normalised through
  `ServerTimePolicy` (Europe/Berlin, INFERRED -- NOT broker-confirmed, see
  DEVELOPMENT_LEDGER C3). Ambiguous/non-existent DST hours are dropped
  (counted), never guessed.
- Connection: a failed poll degrades the session (=> reconciliation required
  for execution); recovery uses attach-only `session.reconnect()`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import Bar, BarType, QuoteTick
from nautilus_trader.model.enums import BarAggregation, PriceType
from nautilus_trader.model.identifiers import ClientId, InstrumentId
from nautilus_trader.model.objects import Quantity

from adapters.activtrades_mt5.history import (
    TIMEFRAME_SECONDS,
    AmbiguousServerTime,
)
from nautilus_mt5.instruments import Mt5InstrumentProvider
from nautilus_mt5.session import Mt5CallError, Mt5Session
from nautilus_mt5.symbols import VENUE

NS = 1_000_000_000
MT5_TIMEFRAME_BY_MINUTES = {1: 1, 5: 5, 15: 15, 30: 30, 60: 16385}
_LABEL_BY_MINUTES = {1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h"}


@dataclass(frozen=True, slots=True, kw_only=True)
class Mt5DataClientConfig:
    poll_interval_secs: float = 0.5
    reconnect_backoff_secs: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)
    max_quote_age: timedelta = timedelta(seconds=30)
    autostart_poller: bool = True  # tests drive poll_once() directly with False


class Mt5LiveMarketDataClient(LiveMarketDataClient):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        msgbus: MessageBus,
        cache: Any,
        clock: LiveClock,
        instrument_provider: Mt5InstrumentProvider,
        session: Mt5Session,
        config: Mt5DataClientConfig | None = None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId("ACTIVTRADES-MT5-DATA"),
            venue=VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
        )
        self._provider = instrument_provider
        self._session = session
        self._cfg = config or Mt5DataClientConfig()
        self._quote_ids: set[InstrumentId] = set()
        self._bar_types: set[BarType] = set()
        self._last_quote_key: dict[InstrumentId, tuple[int, float, float]] = {}
        self._last_bar_open: dict[BarType, int] = {}
        self._poll_task: asyncio.Task | None = None
        self.stats = {
            "quotes": 0,
            "bars": 0,
            "duplicates_suppressed": 0,
            "dropped_ambiguous_time": 0,
            "poll_failures": 0,
            "reconnects": 0,
            "stale_quotes": 0,
        }
        self._backoff_index = 0

    # -- connection ---------------------------------------------------------------

    async def _connect(self) -> None:
        result = self._session.acquire()
        if result is not None and not result.success:
            raise ConnectionError(f"MT5 connect failed: {result.reason}")
        await self._provider.load_all_async()
        for instrument in self._provider.list_all():
            self._cache.add_instrument(instrument)
            self._handle_data(instrument)

    async def _disconnect(self) -> None:
        await self._stop_poller()
        self._session.release()

    # -- subscriptions ----------------------------------------------------------------

    async def _subscribe_quote_ticks(self, command: Any) -> None:
        self._provider.registry.by_instrument_id(command.instrument_id)  # fail closed if unknown
        self._quote_ids.add(command.instrument_id)
        self._ensure_poller()

    async def _unsubscribe_quote_ticks(self, command: Any) -> None:
        self._quote_ids.discard(command.instrument_id)
        self._last_quote_key.pop(command.instrument_id, None)
        await self._maybe_stop_poller()

    async def _subscribe_bars(self, command: Any) -> None:
        bar_type: BarType = command.bar_type
        self._validate_bar_type(bar_type)
        self._bar_types.add(bar_type)
        self._ensure_poller()

    async def _unsubscribe_bars(self, command: Any) -> None:
        self._bar_types.discard(command.bar_type)
        self._last_bar_open.pop(command.bar_type, None)
        await self._maybe_stop_poller()

    def _validate_bar_type(self, bar_type: BarType) -> None:
        spec = bar_type.spec
        if spec.aggregation != BarAggregation.MINUTE or spec.price_type != PriceType.BID:
            raise ValueError(f"only MINUTE/BID bars are supported (MT5 bars are BID): {bar_type}")
        if not bar_type.is_externally_aggregated():
            raise ValueError("bars must be EXTERNAL")
        if spec.step not in MT5_TIMEFRAME_BY_MINUTES:
            raise ValueError(f"unsupported bar step {spec.step}")
        self._provider.registry.by_instrument_id(bar_type.instrument_id)

    # -- requests ---------------------------------------------------------------------------

    async def _request_instrument(self, request: Any) -> None:
        await self._provider.load_async(request.instrument_id)
        instrument = self._provider.find(request.instrument_id)
        self._handle_instrument(instrument, request.id, request.start, request.end, request.params)

    async def _request_bars(self, request: Any) -> None:
        bar_type: BarType = request.bar_type
        self._validate_bar_type(bar_type)
        bars = self._fetch_bars(bar_type, count=request.limit or 100, completed_only=True)
        self._handle_bars(bar_type, bars, request.id, request.start, request.end, request.params)

    # -- controlled poller ----------------------------------------------------------------------

    def _ensure_poller(self) -> None:
        if not self._cfg.autostart_poller:
            return
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = self._loop.create_task(self._poll_loop(), name="mt5-data-poller")

    async def _maybe_stop_poller(self) -> None:
        if not self._quote_ids and not self._bar_types:
            await self._stop_poller()

    async def _stop_poller(self) -> None:
        task, self._poll_task = self._poll_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _poll_loop(self) -> None:
        while True:
            try:
                self.poll_once()
                self._backoff_index = 0
                await asyncio.sleep(self._cfg.poll_interval_secs)
            except Mt5CallError:
                self.stats["poll_failures"] += 1
                await asyncio.sleep(self._next_backoff())
                self.try_reconnect()

    def _next_backoff(self) -> float:
        table = self._cfg.reconnect_backoff_secs
        delay = table[min(self._backoff_index, len(table) - 1)]
        self._backoff_index += 1
        return delay

    def try_reconnect(self) -> bool:
        """One attach-only reconnect attempt (never logs in). Does NOT restore any
        reconciliation state: the execution client must re-reconcile on the new generation."""
        result = self._session.reconnect()
        if result.success:
            self.stats["reconnects"] += 1
        return result.success

    # -- deterministic core ------------------------------------------------------------------------

    def poll_once(self) -> int:
        """Publish anything new. Returns the number of events published."""
        published = 0
        for instrument_id in sorted(self._quote_ids, key=str):
            published += self._poll_quote(instrument_id)
        for bar_type in sorted(self._bar_types, key=str):
            published += self._poll_bars(bar_type)
        return published

    def _poll_quote(self, instrument_id: InstrumentId) -> int:
        mapping = self._provider.registry.by_instrument_id(instrument_id)
        tick = self._session.call(
            "symbol_info_tick", self._session.client.symbol_info_tick, mapping.broker_symbol
        )
        key = (int(tick.time_msc), float(tick.bid), float(tick.ask))
        if self._last_quote_key.get(instrument_id) == key:
            self.stats["duplicates_suppressed"] += 1
            return 0
        try:
            ts_event = self._session.time_policy.server_epoch_to_utc(int(tick.time_msc) / 1000.0)
        except AmbiguousServerTime:
            self.stats["dropped_ambiguous_time"] += 1
            return 0
        if not (float(tick.bid) > 0 and float(tick.ask) >= float(tick.bid)):
            return 0  # invalid quote is never published
        instrument = self._provider.find(instrument_id)
        now = datetime.fromtimestamp(self._clock.timestamp_ns() / NS, tz=UTC)
        if now - ts_event > self._cfg.max_quote_age:
            self.stats["stale_quotes"] += 1  # published (it is broker truth) but counted
        quote = QuoteTick(
            instrument_id=instrument_id,
            bid_price=instrument.make_price(float(tick.bid)),
            ask_price=instrument.make_price(float(tick.ask)),
            bid_size=Quantity(0, instrument.size_precision),  # depth unknown for a CFD
            ask_size=Quantity(0, instrument.size_precision),
            ts_event=int(ts_event.timestamp() * NS),
            ts_init=self._clock.timestamp_ns(),
        )
        self._last_quote_key[instrument_id] = key
        self._handle_data(quote)
        self.stats["quotes"] += 1
        return 1

    def _fetch_bars(self, bar_type: BarType, *, count: int, completed_only: bool) -> list[Bar]:
        mapping = self._provider.registry.by_instrument_id(bar_type.instrument_id)
        minutes = bar_type.spec.step
        rates = self._session.call(
            "copy_rates_from_pos",
            self._session.client.copy_rates_from_pos,
            mapping.broker_symbol,
            MT5_TIMEFRAME_BY_MINUTES[minutes],
            0,
            count + 1,  # +1: the last row is the still-forming bar
        )
        instrument = self._provider.find(bar_type.instrument_id)
        tf_seconds = TIMEFRAME_SECONDS[_LABEL_BY_MINUTES[minutes]]
        server_now = self._server_now_epoch()
        out: list[Bar] = []
        for row in rates:
            open_epoch = int(row["time"])
            if completed_only and open_epoch + tf_seconds > server_now:
                continue  # still forming
            try:
                close_utc = self._session.time_policy.server_epoch_to_utc(open_epoch + tf_seconds)
            except AmbiguousServerTime:
                self.stats["dropped_ambiguous_time"] += 1
                continue
            ts = int(close_utc.timestamp() * NS)
            out.append(
                Bar(
                    bar_type=bar_type,
                    open=instrument.make_price(float(row["open"])),
                    high=instrument.make_price(float(row["high"])),
                    low=instrument.make_price(float(row["low"])),
                    close=instrument.make_price(float(row["close"])),
                    volume=instrument.make_qty(float(row["tick_volume"])),  # tick activity
                    ts_event=ts,
                    ts_init=self._clock.timestamp_ns(),
                )
            )
        return out

    def _server_now_epoch(self) -> int:
        """Server-clock 'now' from the latest tick (never from the local clock)."""
        mapping = self._provider.registry.all()[0]
        tick = self._session.call(
            "symbol_info_tick", self._session.client.symbol_info_tick, mapping.broker_symbol
        )
        return int(tick.time)

    def _poll_bars(self, bar_type: BarType) -> int:
        bars = self._fetch_bars(bar_type, count=3, completed_only=True)
        if not bars:
            return 0
        last = self._last_bar_open.get(bar_type)
        if last is None:
            # First sight: remember the newest completed bar, publish nothing (no history flood).
            self._last_bar_open[bar_type] = max(b.ts_event for b in bars)
            return 0
        published = 0
        for bar in sorted(bars, key=lambda b: b.ts_event):
            if bar.ts_event <= last:
                continue
            last = self._last_bar_open[bar_type] = bar.ts_event
            self._handle_data(bar)
            self.stats["bars"] += 1
            published += 1
        return published
