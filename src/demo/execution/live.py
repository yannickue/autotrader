# ruff: noqa: E501
"""``Mt5DemoStack``: the long-running, multi-symbol ActivTrades DEMO execution stack.

Generalises the proven one-shot C7 path (``nautilus_mt5.demo_slice``) to a process that lives for
days and serves ``StackPort`` (``demo.execution.stack_port``) to the DEMO runner.

Threads (three, and only three):

* the RUNNER thread calls the public methods (synchronous ``StackPort`` API);
* the KERNEL thread runs the asyncio loop with the real Nautilus kernel (message bus, cache,
  portfolio, data/risk/live-execution engines, Trader, ``DemoTraderStrategy``, our MT5 data and
  execution clients). All Nautilus objects are only ever touched there;
* the MT5 LANE thread (``Mt5Executor``) carries EVERY MT5 IPC call - including the bar/quote reads
  of ``LiveBarSource`` - strictly one at a time, and the session refuses MT5 calls from anywhere else.

Money model. Broker truth (account equity/balance, deals, positions) is read from MT5 and drives
the risk gate (``demo.execution.risk_policy``); Nautilus' own PnL of cross-currency / multiplier
instruments (XAUUSD, EURUSD) is NOT money-correct and is never used for decisions or events.
Every event amount (fill price, commission, swap, profit) comes from the broker's deals.

Safety properties (all tested against the fake broker, zero real MT5):

* attach-only: refuses ``MT5_ALLOW_ACCOUNT_LOGIN=1``; verifies DEMO trade mode, expected login and
  server, netting, leverage ceiling and account currency before anything can be sent;
* single-owner terminal lock held for the whole process life, heartbeat-refreshed (stale rule 90 s);
* exposure-changing requests are NEVER retried by the stack; an unknown outcome halts new exposure;
* every entry carries a mandatory broker-side stop; an unconfirmed stop => immediate reduce-only
  flatten + halt of new exposure; unprotected adopted positions get their stop or are flattened;
* shadow mode (``dry_run=True``): the raw client is wrapped so ``order_send`` raises
  ``ShadowModeViolation`` - no code path can reach it; ``order_check`` still runs.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common import Environment
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.engine import DataEngine
from nautilus_trader.live.execution_engine import LiveExecutionEngine
from nautilus_trader.model.identifiers import InstrumentId, TraderId
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.risk.engine import RiskEngine
from nautilus_trader.trading.trader import Trader

from adapters.activtrades_mt5.history import AmbiguousServerTime, validate_rates_schema
from adapters.config import MT5ConnectionConfig
from demo.contracts import TradeIntent
from demo.execution import registry as reg
from demo.execution.events import (
    Accepted,
    ExecutionEvent,
    Fill,
    PositionClosed,
    ProtectionConfirmed,
    Rejected,
)
from demo.execution.market_config import DemoMarketSpec, load_demo_market_specs
from demo.execution.parity import executable_price, parity_reject, parse_utc
from demo.execution.registry import StackRegistry
from demo.execution.risk_policy import (
    BROKER_LEVERAGE_CEILING,
    CLUSTERS,
    POLICY_ID,
    DemoRiskGate,
    GateAccount,
    MarketFacts,
    OpenRisk,
)
from demo.execution.stack_port import AccountSnapshot, StackFailClosed
from demo.execution.strategy import (
    DemoTraderStrategy,
    EntryJob,
    FlattenJob,
    JobOutcome,
    client_order_id_for,
)
from demo.opportunity.bar_source import Quote, validate_frame
from nautilus_mt5.data_client import Mt5DataClientConfig
from nautilus_mt5.execution_client import Mt5ExecClientConfig
from nautilus_mt5.factory import Mt5Adapter, build_mt5_adapter
from nautilus_mt5.instruments import InstrumentAssumptions
from nautilus_mt5.session import AttachOnlyViolation, Mt5CallError, SessionState
from nautilus_mt5.symbols import SymbolRegistry, demo_registry
from risk.models import ReconciliationState

ZERO = Decimal(0)
M5_SECONDS = 300
MT5_TIMEFRAME_M5 = 5
DEAL_TYPES_TRADE = (0, 1)
ENTRY_IN, ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY = 0, 1, 2, 3
_REASON_SL, _REASON_TP, _REASON_EXPERT = 4, 5, 3


# ---------------------------------------------------------------------------------------------
# shadow-mode hard guard
# ---------------------------------------------------------------------------------------------


class ShadowModeViolation(RuntimeError):
    """``order_send`` was reached in shadow (dry-run) mode. This must be impossible."""


class ShadowGuardClient:
    """Wraps the raw MT5 client in shadow mode: everything passes through except ``order_send``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.blocked_sends = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def order_send(self, *args: Any, **kwargs: Any) -> Any:
        self.blocked_sends += 1
        raise ShadowModeViolation("order_send is unreachable in shadow (dry-run) mode")


class _Reject(Exception):
    """Soft, intent-level refusal raised inside lane functions (becomes a ``Rejected`` event)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class StackConfig:
    magic: int = 740_003
    deviation_points: int = 20
    max_quote_age_s: float = 30.0  # older => intent Rejected("stale_feed")
    feed_fatal_age_s: float = 180.0  # older => StackFailClosed("stale_feed")
    clock_skew_s: float = 2.0  # quote timestamped this far in the FUTURE => clock anomaly
    submit_wait_s: float = 35.0  # caller-side bound for one entry (adapter bound is 30 s)
    exposure_timeout_s: float = 30.0  # adapter lane timeout for exposure-changing operations
    flatten_wait_s: float = 35.0
    protection_tolerance_ticks: int = 1  # broker SL may differ from ours by rounding only
    disconnect_grace_s: float = 30.0
    lock_heartbeat_s: float = 20.0  # lock is considered stale after 90 s
    sync_interval_s: float = 1.0
    start_timeout_s: float = 90.0
    lookback_days: int = 30  # deal history for consecutive losses
    close_grace_s: float = 90.0  # wait for exit deals to become visible before EXTERNAL/None
    flatten_max_failures: int = 3
    bar_settle_s: float = 1.0  # a bar counts as closed this long after its nominal close
    bar_min_refetch_s: float = 2.0
    bar_lookback: int = 600
    expected_server: str | None = None
    account_currency: str = "EUR"
    reconcile_retry_s: float = 5.0


# ---------------------------------------------------------------------------------------------
# LiveBarSource: opportunity BarSource over the MT5 lane
# ---------------------------------------------------------------------------------------------


class LiveBarSource:
    """Closed-M5 frames + executable quote + tick activity for the OpportunityEngine.

    * frames: ``ts`` (UTC bar OPEN), ``open, high, low, close`` (BID), ``tick_volume``,
      ``spread_pts``; only bars whose close (+ ``bar_settle_s``) is <= now (the forming bar is never
      returned); strictly increasing, on the 5-minute grid;
    * server time -> UTC through the session ``ServerTimePolicy`` (Europe/Berlin, DST-safe). Bars
      inside an ambiguous/non-existent DST hour are DROPPED (never guessed, counted in ``stats``);
      if the NEWEST tick is ambiguous the source fails closed (``StackFailClosed``);
    * a per-market cache is refreshed only when a new closed bar should exist;
    * every MT5 read runs on the ONE dedicated lane thread (via the stack).
    """

    def __init__(self, stack: Mt5DemoStack) -> None:
        self._stack = stack
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[pd.DataFrame, float]] = {}  # market -> (frame, fetched mono)
        self._last_quote: dict[str, Quote] = {}
        self.stats = {"fetches": 0, "cache_hits": 0, "dropped_ambiguous_bars": 0}

    # -- clock helpers -----------------------------------------------------------------------

    def _now(self) -> datetime:
        return self._stack._now().astimezone(UTC)

    def expected_last_open(self, now: datetime | None = None) -> datetime:
        """Open time of the newest bar that must be closed at ``now`` (grid-aligned, UTC)."""
        current = (now or self._now()).astimezone(UTC)
        settled = current.timestamp() - self._stack._cfg.bar_settle_s - M5_SECONDS
        return datetime.fromtimestamp((settled // M5_SECONDS) * M5_SECONDS, tz=UTC)

    # -- MT5 reads (lane) --------------------------------------------------------------------------

    def _fetch(self, market: str, count: int, now: datetime) -> pd.DataFrame:
        stack = self._stack
        info = stack._market(market)
        session = stack._session_or_fail()
        client = session.client
        rows = session.call(
            "copy_rates_from_pos",
            client.copy_rates_from_pos,
            info.broker_symbol,
            MT5_TIMEFRAME_M5,
            0,
            count + 1,
        )
        validate_rates_schema(rows)
        policy = session.time_policy
        settle = stack._cfg.bar_settle_s
        keep: list[tuple[Any, ...]] = []
        for row in rows:
            try:
                opened = policy.server_epoch_to_utc(float(row["time"]))
            except AmbiguousServerTime:
                self.stats["dropped_ambiguous_bars"] += 1
                continue
            if opened.timestamp() + M5_SECONDS + settle > now.timestamp():
                continue  # still forming (or not yet settled)
            keep.append(
                (
                    opened,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    int(row["tick_volume"]),
                    int(row["spread"]),
                )
            )
        keep.sort(key=lambda r: r[0])
        dedup: dict[datetime, tuple[Any, ...]] = {r[0]: r for r in keep}
        ordered = [dedup[k] for k in sorted(dedup)]
        frame = pd.DataFrame(
            {
                "ts": pd.to_datetime([r[0] for r in ordered], utc=True),
                "open": [r[1] for r in ordered],
                "high": [r[2] for r in ordered],
                "low": [r[3] for r in ordered],
                "close": [r[4] for r in ordered],
                "tick_volume": [r[5] for r in ordered],
                "spread_pts": [r[6] for r in ordered],
            }
        )
        if len(frame):
            frame["ts"] = frame["ts"].astype("datetime64[ns, UTC]")
        validate_frame(frame, market)
        return frame

    # -- BarSource surface ---------------------------------------------------------------------------

    def m5_frame(self, market: str, n: int | None = None) -> pd.DataFrame:
        want = n if n is not None else self._stack._cfg.bar_lookback
        with self._lock:
            now = self._now()
            cached = self._cache.get(market)
            if cached is not None:
                frame, fetched = cached
                expected = pd.Timestamp(self.expected_last_open(now))
                fresh = len(frame) > 0 and frame["ts"].iloc[-1] >= expected
                recent = time.monotonic() - fetched < self._stack._cfg.bar_min_refetch_s
                if len(frame) >= want and (fresh or recent):
                    self.stats["cache_hits"] += 1
                    return frame.iloc[-want:].reset_index(drop=True).copy()
            count = max(want, self._stack._cfg.bar_lookback)
            frame = self._stack._on_lane(self._fetch, market, count, now)
            self.stats["fetches"] += 1
            self._cache[market] = (frame, time.monotonic())
            return frame.iloc[-want:].reset_index(drop=True).copy()

    def latest_quote(self, market: str) -> Quote | None:
        quote = self._stack._on_lane(self._quote, market)
        if quote is not None:
            self._last_quote[market] = quote
        return quote

    def _quote(self, market: str) -> Quote | None:
        stack = self._stack
        info = stack._market(market)
        session = stack._session_or_fail()
        try:
            tick = session.call(
                "symbol_info_tick", session.client.symbol_info_tick, info.broker_symbol
            )
        except Mt5CallError:
            return None
        try:
            ts = session.time_policy.server_epoch_to_utc(float(tick.time_msc) / 1000.0)
        except AmbiguousServerTime:
            raise StackFailClosed("ambiguous_server_time") from None
        return Quote(ts_utc=ts, bid=float(tick.bid), ask=float(tick.ask))

    def tick_activity(self, market: str) -> float | None:
        """Broker tick volume per minute of the newest closed bar (None if unknown)."""
        frame = self.m5_frame(market, 1)
        if not len(frame):
            return None
        return float(frame["tick_volume"].iloc[-1]) / 5.0

    # -- freshness / new-bar accessors for the runner ------------------------------------------------

    def last_closed_bar_open(self, market: str) -> datetime | None:
        frame = self.m5_frame(market, 1)
        return None if not len(frame) else frame["ts"].iloc[-1].to_pydatetime()

    def last_closed_bar_close_utc(self, market: str) -> datetime | None:
        """UTC close time of the newest closed bar - changes exactly when a new M5 bar closed."""
        opened = self.last_closed_bar_open(market)
        return None if opened is None else opened + timedelta(seconds=M5_SECONDS)

    def quote_age_seconds(self, market: str) -> float | None:
        quote = self.latest_quote(market)
        return None if quote is None else (self._now() - quote.ts_utc).total_seconds()

    def is_fresh(self, market: str, max_age_s: float | None = None) -> bool:
        age = self.quote_age_seconds(market)
        limit = max_age_s if max_age_s is not None else self._stack._cfg.max_quote_age_s
        return age is not None and -self._stack._cfg.clock_skew_s <= age <= limit

    def feed_ages(self) -> dict[str, float | None]:
        return {m: self.quote_age_seconds(m) for m in self._stack.markets}


# ---------------------------------------------------------------------------------------------
# kernel thread
# ---------------------------------------------------------------------------------------------


class _KernelThread:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="demo-kernel", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(10)

    def call(self, coro: Any, timeout: float | None = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        if self._thread.is_alive():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=15)
        if not self.loop.is_running():
            self.loop.close()


# ---------------------------------------------------------------------------------------------
# internal records
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MarketInfo:
    canonical: str
    broker_symbol: str
    instrument_id: InstrumentId
    spec: DemoMarketSpec  # effective (broker volume constraints, checked-in spread/leverage)
    profit_currency: str
    digits: int
    diffs: tuple[str, ...] = ()


@dataclass(slots=True)
class _Prepared:
    bid: Decimal
    ask: Decimal
    quote_utc: datetime
    account: GateAccount
    facts: MarketFacts
    positions_seen: int = 0


@dataclass(slots=True)
class _Snap:
    equity: float = 0.0
    balance: float = 0.0
    profit: float = 0.0
    open_positions: int = 0
    open_orders: int = 0
    protected: bool = True
    server_time: datetime | None = None


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------------------------
# the stack
# ---------------------------------------------------------------------------------------------


class Mt5DemoStack:
    """``StackPort`` implementation over the attach-only ActivTrades DEMO MT5 terminal.

    Constructor arguments:
      ``client``       MT5 client object (``MT5ClientProtocol``); tests pass ``FakeMT5Broker``, the
                       runner passes ``get_real_client()``. Never obtained here.
      ``connection``   ``MT5ConnectionConfig`` (expected login/server; ``allow_account_login`` must be False).
      ``state_dir``    directory for the adapter state DB and the intent registry (restart safety).
      ``market_specs`` per-market checked-in facts (default: ``configs/markets/*.toml``).
      ``dry_run``      shadow mode: NO code path may reach ``order_send`` (hard guard); ``order_check`` only.
      ``lock_path``    single-owner terminal lock file (default: repository default lock).
      ``now``          injectable UTC clock (default ``datetime.now(UTC)``).
      ``config``       ``StackConfig`` tunables (timeouts, thresholds).
      ``registry``     symbol registry (default ``demo_registry()``: GER40, NAS100, SPX500, XAUUSD, EURUSD).
    """

    def __init__(
        self,
        *,
        client: Any,
        connection: MT5ConnectionConfig,
        state_dir: Path | str,
        market_specs: dict[str, DemoMarketSpec] | None = None,
        dry_run: bool = False,
        lock_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
        config: StackConfig | None = None,
        registry: SymbolRegistry | None = None,
    ) -> None:
        self._client_raw = client
        self._connection = connection
        self._state_dir = Path(state_dir)
        self._toml_specs = market_specs or load_demo_market_specs()
        self._dry_run = bool(dry_run)
        self._lock_path = lock_path
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))
        self._cfg = config or StackConfig()
        self._symbols = registry or demo_registry()
        self.bar_source = LiveBarSource(self)

        self._registry: StackRegistry | None = None
        self._kernel: _KernelThread | None = None
        self._adapter: Mt5Adapter | None = None
        self._strategy: DemoTraderStrategy | None = None
        self._trader: Trader | None = None
        self._engines: list[Any] = []
        self._lane: Any = None
        self._markets: dict[str, _MarketInfo] = {}
        self._gate = DemoRiskGate()
        self._results: dict[str, list[ExecutionEvent]] = {}
        self._submit_lock = threading.RLock()
        self._pending_events: list[ExecutionEvent] = []
        self._pending_lock = threading.Lock()
        self._halt_reason: str | None = None
        self._fatal: str | None = None
        self._started = False
        self._stopped = False
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._disconnected_since: float | None = None
        self._last_reconcile_attempt = 0.0
        self._flatten_failures: dict[str, int] = {}
        self._last_snap = _Snap()
        self._closing_seen: dict[str, float] = {}
        self._foreign: tuple[str, ...] = ()
        self._start_notes: dict[str, Any] = {}

    # ------------------------------------------------------------------------------- properties

    @property
    def markets(self) -> tuple[str, ...]:
        return tuple(self._markets) if self._markets else tuple(self._toml_specs)

    @property
    def shadow(self) -> bool:
        return self._dry_run

    @property
    def halted_reason(self) -> str | None:
        return self._halt_reason

    def _market(self, market: str) -> _MarketInfo:
        try:
            return self._markets[market]
        except KeyError:
            raise StackFailClosed(f"unknown_market:{market}") from None

    def _session_or_fail(self) -> Any:
        if self._adapter is None:
            raise StackFailClosed("stack_not_started")
        return self._adapter.session

    # ---------------------------------------------------------------------------------- guards

    def _check_fatal(self) -> None:
        if self._fatal:
            raise StackFailClosed(self._fatal)
        if self._stopped or not self._started:
            raise StackFailClosed("stack_not_running")

    def _set_fatal(self, reason: str) -> None:
        if self._fatal is None:
            self._fatal = reason
        self._halt(reason)

    def _halt(self, reason: str) -> None:
        if self._halt_reason is None:
            self._halt_reason = reason

    def halt_new_exposure(self, reason: str) -> None:
        self._halt(reason)

    def _on_lane(self, fn: Callable[..., Any], *args: Any, timeout: float | None = None) -> Any:
        """Run ``fn`` on the MT5 lane thread and wait. Broker-call failures become fail-closed."""
        if self._lane is None:
            raise StackFailClosed("stack_not_started")
        try:
            return self._lane.run_sync(fn, *args, timeout=timeout)
        except Mt5CallError as exc:
            session = self._adapter.session if self._adapter else None
            if session is None or session.state is not SessionState.CONNECTED:
                raise StackFailClosed(f"broker_disconnect:{exc.what}") from exc
            raise _Reject("broker_call_failed") from exc

    # ---------------------------------------------------------------------------------- start

    def start(self) -> AccountSnapshot:
        if self._started:
            return self.account_snapshot()
        if os.environ.get("MT5_ALLOW_ACCOUNT_LOGIN", "").strip() == "1" or (
            self._connection.allow_account_login
        ):
            raise StackFailClosed("account_login_enabled: the demo stack is attach-only")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._registry = StackRegistry(self._state_dir / "demo_stack.db")
        self._kernel = _KernelThread()
        self._kernel.start()
        try:
            self._kernel.call(self._async_start(), timeout=self._cfg.start_timeout_s)
        except StackFailClosed:
            self._teardown()
            raise
        except AttachOnlyViolation as exc:
            self._teardown()
            raise StackFailClosed(f"account_login_enabled:{exc}") from exc
        except BaseException as exc:
            self._teardown()
            raise StackFailClosed(f"start_failed:{type(exc).__name__}:{exc}") from exc
        self._started = True
        try:
            self._repair_unprotected_at_start()
        except StackFailClosed:
            self.stop()
            raise
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(target=self._heartbeat, name="demo-lock-hb", daemon=True)
        self._hb_thread.start()
        return self.account_snapshot()

    async def _async_start(self) -> None:
        loop = asyncio.get_running_loop()
        clock = LiveClock()
        trader_id = TraderId("DEMO-001")
        msgbus = MessageBus(trader_id=trader_id, clock=clock)
        cache = Cache()
        portfolio = Portfolio(msgbus, cache, clock)
        data_engine = DataEngine(msgbus, cache, clock)
        risk_engine = RiskEngine(portfolio, msgbus, cache, clock)
        exec_engine = LiveExecutionEngine(
            loop,
            msgbus,
            cache,
            clock,
            LiveExecEngineConfig(reconciliation=False, inflight_check_interval_ms=0),
        )
        client = ShadowGuardClient(self._client_raw) if self._dry_run else self._client_raw
        adapter = build_mt5_adapter(
            client=client,
            connection=self._connection,
            assumptions=InstrumentAssumptions(  # broker reports no margin rates: labelled ASSUMED
                margin_init=Decimal("0.05"), margin_maint=Decimal("0.05")
            ),
            loop=loop,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            state_path=self._state_dir / "mt5_state.db",
            lock_path=self._lock_path,
            registry=self._symbols,
            exec_config=Mt5ExecClientConfig(
                magic=self._cfg.magic,
                deviation_points=self._cfg.deviation_points,
                require_attached_protection=True,
                autostart_sync=True,
                sync_interval_secs=self._cfg.sync_interval_s,
                exposure_timeout_secs=self._cfg.exposure_timeout_s,
                dry_run=self._dry_run,
                require_demo_account=True,
            ),
            data_config=Mt5DataClientConfig(autostart_poller=False),
            allow_multiplier_and_cross_currency=True,
        )
        self._adapter = adapter
        self._lane = adapter.lane
        for engine in (data_engine, risk_engine, exec_engine):
            self._engines.append(engine)
        data_engine.register_client(adapter.data_client)
        exec_engine.register_client(adapter.exec_client)
        strategy = DemoTraderStrategy(
            loop=loop, now=self._now, halt_reason=lambda: self._halt_reason
        )
        trader = Trader(
            trader_id=trader_id,
            instance_id=UUID4(),
            msgbus=msgbus,
            cache=cache,
            portfolio=portfolio,
            data_engine=data_engine,
            risk_engine=risk_engine,
            exec_engine=exec_engine,
            clock=clock,
            environment=Environment.LIVE,
        )
        self._strategy, self._trader = strategy, trader
        self._exec_engine = exec_engine
        self._cache = cache

        try:
            await adapter.exec_client._connect()
            adapter.exec_client._set_connected(True)
            await adapter.data_client._connect()
            adapter.data_client._set_connected(True)
        except (ConnectionError, RuntimeError) as exc:
            raise StackFailClosed(f"connect_failed:{type(exc).__name__}:{exc}") from exc
        await self._lane.run(self._lane_verify_start)

        for engine in (data_engine, risk_engine, exec_engine):
            engine.start()
        trader.add_strategy(strategy)
        trader.start()

        await self._adopt_and_reconcile()

    # -- start verification (lane) -----------------------------------------------------------------

    def _lane_verify_start(self) -> None:
        adapter = self._adapter
        assert adapter is not None
        session = adapter.session
        client = session.client
        try:
            account = session.call("account_info", client.account_info)
        except Mt5CallError as exc:
            raise StackFailClosed("unknown_account") from exc
        if int(account.trade_mode) != 0:
            raise StackFailClosed("non_demo_account")
        expected_login = session.expected_login
        if expected_login is not None and int(account.login) != int(expected_login):
            raise StackFailClosed("account_identity_mismatch")
        expected_server = self._cfg.expected_server or self._connection.server
        observed_server = getattr(account, "server", None)
        if not observed_server:
            raise StackFailClosed("account_server_unknown")
        if expected_server and str(observed_server) != str(expected_server):
            raise StackFailClosed("unexpected_server")
        if str(account.currency) != self._cfg.account_currency:
            raise StackFailClosed(f"unsupported_account_currency:{account.currency}")
        if int(account.leverage) > int(BROKER_LEVERAGE_CEILING):
            raise StackFailClosed("broker_leverage_above_ceiling")
        session.account_mode()  # RETAIL_NETTING only, else UnsupportedAccountMode
        markets: dict[str, _MarketInfo] = {}
        for mapping in self._symbols.all():
            canonical = mapping.canonical
            toml = self._toml_specs.get(canonical)
            if toml is None:
                raise StackFailClosed(f"missing_market_config:{canonical}")
            if toml.broker_symbol != mapping.broker_symbol:
                raise StackFailClosed(f"symbol_mapping_mismatch:{canonical}")
            broker = adapter.provider.spec(mapping.instrument_id)
            if broker.trade_contract_size != toml.contract_size:
                raise StackFailClosed(f"spec_mismatch:{canonical}:contract_size")
            if broker.trade_tick_size != toml.tick_size:
                raise StackFailClosed(f"spec_mismatch:{canonical}:tick_size")
            diffs = tuple(
                f"{name}:{getattr(toml, name)}->{getattr(broker, name)}"
                for name in ("volume_min", "volume_step", "volume_max")
                if getattr(toml, name) != getattr(broker, name)
            )
            effective = replace(
                toml,
                volume_min=broker.volume_min,
                volume_step=broker.volume_step,
                volume_max=broker.volume_max,
            )
            if broker.currency_profit not in ("EUR", "USD"):
                raise StackFailClosed(f"unsupported_profit_currency:{canonical}")
            markets[canonical] = _MarketInfo(
                canonical=canonical,
                broker_symbol=mapping.broker_symbol,
                instrument_id=mapping.instrument_id,
                spec=effective,
                profit_currency=broker.currency_profit,
                digits=int(broker.digits),
            )
            if diffs:
                markets[canonical] = replace(markets[canonical], diffs=diffs)
        for canonical in CLUSTERS:
            if canonical not in markets:
                raise StackFailClosed(f"market_not_registered:{canonical}")
        self._markets = markets

    # -- restart adoption ---------------------------------------------------------------------------

    async def _adopt_and_reconcile(self) -> None:
        adapter = self._adapter
        assert adapter is not None
        exec_client = adapter.exec_client
        if exec_client.recon.state is not ReconciliationState.RECONCILED:
            # A restart with an open broker position: hand Nautilus the venue snapshot (positions,
            # orders, fills) through its own reconciliation, then require a fresh venue comparison.
            mass = await exec_client.generate_mass_status()
            result = self._exec_engine.reconcile_execution_mass_status(mass)
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
            await asyncio.sleep(0.05)
            await exec_client.reconcile_async()
        await self._lane.run(self._lane_link_positions)
        if exec_client.recon.state is not ReconciliationState.RECONCILED:
            self._start_notes["reconcile_discrepancies"] = [
                f"{d.kind.value}:{d.detail}" for d in exec_client.recon.discrepancies
            ]

    def _lane_link_positions(self) -> None:
        """Re-adopt open broker positions of ours by intent id (never duplicated)."""
        assert self._registry is not None
        adopted: list[str] = []
        for position, _market, row in self._lane_own_positions():
            if row is None:
                continue
            ticket = int(position.ticket)
            if row.status != reg.OPEN or row.position_ticket != ticket:
                self._registry.update(row.intent_id, status=reg.OPEN, position_ticket=ticket)
            adopted.append(row.intent_id)
        self._start_notes["adopted"] = adopted

    # -- position enumeration (lane) --------------------------------------------------------------------

    def _lane_positions(self) -> list[Any]:
        assert self._adapter is not None
        session = self._adapter.session
        return list(session.call("positions_get", session.client.positions_get))

    def _canonical_of(self, broker_symbol: str) -> str | None:
        for canonical, info in self._markets.items():
            if info.broker_symbol == broker_symbol:
                return canonical
        mapping = self._symbols.by_broker_symbol(broker_symbol)
        return None if mapping is None else mapping.canonical

    def _lane_own_positions(self) -> list[tuple[Any, str, reg.IntentRow | None]]:
        """(raw position, market, registry row or None) for positions carrying our magic."""
        assert self._registry is not None and self._adapter is not None
        store = self._adapter.store
        result = []
        for position in self._lane_positions():
            market = self._canonical_of(str(position.symbol))
            if market is None or int(position.magic) != self._cfg.magic:
                continue
            row = None
            token = str(position.comment or "").split("[")[0].strip()
            order_row = store.by_token(token) if token else None
            if order_row is not None:
                parent = order_row.parent_client_order_id or order_row.client_order_id
                row = self._registry.by_client_order_id(parent)
            if row is None:
                candidates = [
                    r for r in self._registry.open_for_market(market)
                    if r.position_ticket in (None, int(position.ticket))
                ]
                if len(candidates) == 1:
                    row = candidates[0]
            result.append((position, market, row))
        return result

    # ----------------------------------------------------------------------------- heartbeat

    def _heartbeat(self) -> None:
        while not self._hb_stop.wait(self._cfg.lock_heartbeat_s):
            session = self._adapter.session if self._adapter else None
            if session is not None and not session.refresh_lock():
                self._set_fatal("terminal_lock_lost")
                return

    # ----------------------------------------------------------------------------- stop

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=5)
        self._teardown()

    def _teardown(self) -> None:
        kernel, adapter = self._kernel, self._adapter
        if kernel is not None and adapter is not None:
            with contextlib.suppress(Exception):
                kernel.call(self._async_stop(), timeout=30)
        if adapter is not None:
            with contextlib.suppress(Exception):
                if adapter.lane is not None:
                    adapter.lane.shutdown()
            with contextlib.suppress(Exception):
                adapter.store.close()
        if kernel is not None:
            kernel.stop()
        if self._registry is not None:
            with contextlib.suppress(Exception):
                self._registry.close()
        self._kernel = None
        self._adapter = None
        self._lane = None

    async def _async_stop(self) -> None:
        adapter = self._adapter
        assert adapter is not None
        if self._trader is not None:
            with contextlib.suppress(Exception):
                self._trader.stop()
        for engine in self._engines:
            with contextlib.suppress(Exception):
                engine.stop()
        await asyncio.sleep(0.05)
        with contextlib.suppress(Exception):
            await adapter.exec_client._disconnect()  # the last user releases session + lock
        with contextlib.suppress(Exception):
            await adapter.data_client._disconnect()

    # ------------------------------------------------------------------------------ read: account

    def account_snapshot(self) -> AccountSnapshot:
        if self._adapter is None or self._lane is None or self._stopped:
            return AccountSnapshot(
                is_demo=False, account_id_hash="", equity=0.0, balance=0.0, profit=0.0,
                reconciliation="NOT_RECONCILED", connected=False, open_positions=0,
                open_orders=0, all_positions_protected=True, kill_switch=True,
                extra={"fatal": self._fatal or "not_running"},
            )
        connected = True
        try:
            snap, is_demo, login_hash = self._on_lane(self._lane_snapshot)
            self._last_snap = snap
        except (StackFailClosed, _Reject, Mt5CallError, concurrent.futures.TimeoutError):
            snap, connected = self._last_snap, False
            is_demo, login_hash = True, self._login_hash()
        recon = self._adapter.exec_client.recon
        return AccountSnapshot(
            is_demo=is_demo,
            account_id_hash=login_hash,
            equity=snap.equity,
            balance=snap.balance,
            profit=snap.profit,
            reconciliation=recon.state.name,
            connected=connected and self._adapter.session.is_connected,
            open_positions=snap.open_positions,
            open_orders=snap.open_orders,
            all_positions_protected=snap.protected,
            kill_switch=self._halt_reason is not None or self._fatal is not None,
            server_time_utc=snap.server_time,
            extra={
                "policy_id": POLICY_ID,
                "shadow": self._dry_run,
                "halt_reason": self._halt_reason,
                "fatal": self._fatal,
                "runtime": recon.runtime.value,
                "foreign_positions": list(self._foreign),
                "spec_diffs": {m: list(i.diffs) for m, i in self._markets.items() if i.diffs},
                "start_notes": dict(self._start_notes),
                "lane_max_concurrent": self._lane.stats.max_concurrent,
            },
        )

    def _login_hash(self) -> str:
        return hashlib.sha256(str(self._connection.login).encode()).hexdigest()[:16]

    def _lane_snapshot(self) -> tuple[_Snap, bool, str]:
        assert self._adapter is not None
        session = self._adapter.session
        client = session.client
        account = session.call("account_info", client.account_info)
        positions = session.call("positions_get", client.positions_get)
        orders = session.call("orders_get", client.orders_get)
        protected = all(
            float(p.sl or 0.0) > 0.0 for p in positions if int(p.magic) == self._cfg.magic
        )
        server_time = None
        try:
            for info in self._markets.values():
                tick = session.call("symbol_info_tick", client.symbol_info_tick, info.broker_symbol)
                server_time = session.time_policy.server_epoch_to_utc(float(tick.time_msc) / 1000)
                break
        except (Mt5CallError, AmbiguousServerTime):
            server_time = None
        snap = _Snap(
            equity=float(account.equity),
            balance=float(account.balance),
            profit=float(account.profit),
            open_positions=len(positions),
            open_orders=len(orders),
            protected=protected,
            server_time=server_time,
        )
        return snap, int(account.trade_mode) == 0, self._login_hash()

    def has_position(self, market: str) -> bool:
        info = self._market(market)
        assert self._registry is not None
        if any(
            r.status in (reg.ACCEPTED, reg.SENT, reg.IN_DOUBT)
            for r in self._registry.open_for_market(market)
        ):
            return True
        return bool(self._on_lane(self._lane_symbol_positions, info.broker_symbol))

    def _lane_symbol_positions(self, broker_symbol: str) -> list[Any]:
        assert self._adapter is not None
        session = self._adapter.session
        return list(
            session.call("positions_get", session.client.positions_get, symbol=broker_symbol)
        )

    def open_intents(self) -> Sequence[str]:
        if self._registry is None:
            return ()
        return tuple(r.intent_id for r in self._registry.with_status(*reg.LIVE_STATUSES))

    # ================================================================================== submit

    def submit(self, intent: TradeIntent) -> list[ExecutionEvent]:
        """Risk-size and send ONE intent exactly once (entry + mandatory broker stop [+ TP]).

        Pre-sizing refusals return ``[Rejected]``; refusals after sizing return
        ``[Accepted, Rejected]`` (the Accepted carries the risk numbers). A protection failure
        returns ``[Accepted, Fill, PositionClosed?, Rejected("protection_unconfirmed")]``: the
        position existed, was flattened, and new exposure is halted. Structural fail-closed
        conditions (non-demo, unknown account, disconnect, unreconciled, clock anomaly, very stale
        feed, unprotected exposure) raise ``StackFailClosed`` instead.
        """
        with self._submit_lock:
            cached = self._results.get(intent.intent_id)
            if cached is not None:
                return cached
            self._check_fatal()
            events = self._submit_once(intent)
            self._results[intent.intent_id] = events
            return events

    def _reject(
        self, intent: TradeIntent, reason: str, *, accepted: Accepted | None = None
    ) -> list[ExecutionEvent]:
        assert self._registry is not None
        self._registry.insert(
            intent_id=intent.intent_id,
            client_order_id=client_order_id_for(intent.intent_id),
            market=intent.market,
            direction=intent.direction,
            stop=str(intent.stop),
            target=None if intent.target is None else str(intent.target),
            forced_flat_utc=intent.forced_flat_utc,
            status=reg.REJECTED,
            created_utc=_iso(self._now()),
            detail=reason,
        )
        rejected = Rejected(intent_id=intent.intent_id, reason=reason)
        return [rejected] if accepted is None else [accepted, rejected]

    def _pre_reject(self, intent: TradeIntent, now: datetime) -> str | None:
        if self._halt_reason:
            return "halted"
        info = self._markets.get(intent.market)
        if info is None:
            return "unknown_market"
        if intent.broker_symbol != info.broker_symbol:
            return "symbol_mismatch"
        if intent.direction not in (1, -1):
            return "invalid_direction"
        if now > parse_utc(intent.valid_until_utc):
            return "stale_signal"
        if intent.forced_flat_utc is not None and now >= parse_utc(intent.forced_flat_utc):
            return "past_forced_flat"
        return None

    def _submit_once(self, intent: TradeIntent) -> list[ExecutionEvent]:
        assert self._registry is not None and self._strategy is not None
        now = self._now().astimezone(UTC)
        coid = client_order_id_for(intent.intent_id)
        if self._registry.get(intent.intent_id) is not None:
            return [Rejected(intent_id=intent.intent_id, reason="duplicate_intent")]
        early = self._pre_reject(intent, now)
        if early:
            return self._reject(intent, early)
        try:
            prepared: _Prepared = self._on_lane(self._lane_prepare, intent, now)
        except _Reject as exc:
            return self._reject(intent, exc.reason)
        info = self._markets[intent.market]
        parity = parity_reject(
            intent, bid=prepared.bid, ask=prepared.ask, max_spread=info.spec.max_spread
        )
        if parity:
            return self._reject(intent, parity)
        approval = self._gate.size(
            intent=intent,
            market=prepared.facts,
            account=prepared.account,
            bid=prepared.bid,
            ask=prepared.ask,
            quote_time=min(prepared.quote_utc, now),
            now=now,
        )
        if isinstance(approval, str):
            return self._reject(intent, approval)
        inserted = self._registry.insert(
            intent_id=intent.intent_id,
            client_order_id=coid,
            market=intent.market,
            direction=intent.direction,
            stop=str(intent.stop),
            target=None if intent.target is None else str(intent.target),
            forced_flat_utc=intent.forced_flat_utc,
            status=reg.ACCEPTED,
            created_utc=_iso(now),
            risk_money=str(approval.stop_risk_money),
        )
        if not inserted:
            return [Rejected(intent_id=intent.intent_id, reason="duplicate_intent")]
        accepted = Accepted(
            intent_id=intent.intent_id,
            quantity=approval.quantity,
            equity=approval.equity,
            risk_fraction=approval.risk_fraction,
            risk_budget=approval.risk_budget,
            leverage=approval.leverage,
        )
        job = EntryJob(
            intent=intent,
            instrument_id=info.instrument_id,
            quantity=approval.quantity,
            stop=Decimal(str(intent.stop)),
            target=None if intent.target is None else Decimal(str(intent.target)),
            client_order_id=coid,
        )
        self._registry.update(intent.intent_id, status=reg.SENT)
        self._strategy.enqueue(job)
        try:
            outcome: JobOutcome = job.future.result(timeout=self._cfg.submit_wait_s)
        except concurrent.futures.TimeoutError:
            outcome = JobOutcome("timeout", "no_outcome_within_bound")
        try:
            return self._finish_entry(intent, info, prepared, accepted, outcome)
        finally:
            self._strategy.forget(coid)

    # -- after the strategy resolved the job ---------------------------------------------------------

    def _finish_entry(
        self,
        intent: TradeIntent,
        info: _MarketInfo,
        prepared: _Prepared,
        accepted: Accepted,
        outcome: JobOutcome,
    ) -> list[ExecutionEvent]:
        assert self._registry is not None
        status = outcome.status
        if status == "dry_run_ok":
            self._registry.update(intent.intent_id, status=reg.SHADOW, detail=outcome.reason[:200])
            return [accepted]
        confirm = self._on_lane(self._lane_confirm_entry, info, intent, prepared)
        if confirm is None:
            if status in ("denied", "rejected"):
                reason = _machine_refusal(outcome.reason)
                if reason in ("not_reconciled", "runtime_not_ready", "unprotected_position"):
                    self._set_fatal(reason)  # authority is gone: the runner must stop
                self._registry.update(
                    intent.intent_id, status=reg.REJECTED, detail=outcome.reason[:200]
                )
                return [accepted, Rejected(intent_id=intent.intent_id, reason=reason)]
            # timeout / failed / "filled" without a position: the send outcome is UNKNOWN
            self._registry.update(intent.intent_id, status=reg.IN_DOUBT, detail=outcome.reason[:200])
            self._halt("order_outcome_unknown")
            return [accepted, Rejected(intent_id=intent.intent_id, reason="order_outcome_unknown")]

        fill, ticket, protected, stop_seen, target_seen = confirm
        events: list[ExecutionEvent] = [accepted, fill]
        if protected:
            self._registry.update(intent.intent_id, status=reg.OPEN, position_ticket=ticket)
            events.append(
                ProtectionConfirmed(
                    intent_id=intent.intent_id,
                    broker_position_id=str(ticket),
                    stop=stop_seen,
                    target=target_seen,
                )
            )
            return events
        # The broker position exists WITHOUT the stop we asked for: never leave it open.
        self._registry.update(intent.intent_id, status=reg.OPEN, position_ticket=ticket)
        self._halt("protection_unconfirmed")
        self._flatten(info, tag=f"protection-fail:{intent.intent_id}", hint="MANUAL")
        row = self._registry.get(intent.intent_id)
        if row is not None:
            closed = self._on_lane(self._lane_build_closed, row)
            if closed is not None:
                events.append(closed)
        events.append(Rejected(intent_id=intent.intent_id, reason="protection_unconfirmed"))
        return events

    # -- flatten (reduce-only, through Nautilus) --------------------------------------------------------

    def _flatten(self, info: _MarketInfo, *, tag: str, hint: str | None = None) -> bool:
        assert self._strategy is not None and self._registry is not None
        if hint is not None:
            for row in self._registry.open_for_market(info.canonical):
                self._registry.update(row.intent_id, exit_hint=hint)
        job = FlattenJob(instrument_id=info.instrument_id, tag=tag)
        self._strategy.enqueue(job)
        try:
            outcome: JobOutcome = job.future.result(timeout=self._cfg.flatten_wait_s)
        except concurrent.futures.TimeoutError:
            outcome = JobOutcome("failed", "flatten_timeout")
        still_open = self._on_lane(self._lane_symbol_positions, info.broker_symbol)
        if outcome.status == "flat" and not still_open:
            self._flatten_failures.pop(info.canonical, None)
            return True
        count = self._flatten_failures.get(info.canonical, 0) + 1
        self._flatten_failures[info.canonical] = count
        self._halt("flatten_failed")
        if count >= self._cfg.flatten_max_failures:
            self._set_fatal(f"flatten_failed:{info.canonical}")
            raise StackFailClosed(self._fatal or "flatten_failed")
        return False

    # ================================================================================= lane reads

    def _lane_health(self) -> Any:
        """Fail-closed identity/connection/reconciliation checks. Runs on the MT5 lane."""
        assert self._adapter is not None
        adapter = self._adapter
        session = adapter.session
        client = session.client
        if session.state is not SessionState.CONNECTED:
            raise StackFailClosed("broker_disconnect")
        terminal = session.call("terminal_info", client.terminal_info, none_ok=True)
        if terminal is None or not getattr(terminal, "connected", False):
            raise StackFailClosed("broker_disconnect")
        try:
            account = session.call("account_info", client.account_info)
        except Mt5CallError as exc:
            raise StackFailClosed("unknown_account") from exc
        if int(account.trade_mode) != 0:
            self._set_fatal("non_demo_account")
            raise StackFailClosed("non_demo_account")
        expected = session.expected_login
        if expected is not None and int(account.login) != int(expected):
            self._set_fatal("account_identity_changed")
            raise StackFailClosed("account_identity_changed")
        expected_server = self._cfg.expected_server or self._connection.server
        if expected_server and str(getattr(account, "server", "")) != str(expected_server):
            self._set_fatal("unexpected_server")
            raise StackFailClosed("unexpected_server")
        if int(account.leverage) > int(BROKER_LEVERAGE_CEILING):
            raise StackFailClosed("broker_leverage_above_ceiling")
        # New exposure needs a FRESH venue-snapshot comparison (read-only): the last comparison may
        # predate a manual trade, a foreign working order or a lost response.
        adapter.exec_client.reconcile()
        recon = adapter.exec_client.recon
        if recon.state is not ReconciliationState.RECONCILED:
            raise StackFailClosed(f"not_reconciled:{recon.state.value}")
        return account

    def _fx(self, profit_currency: str, now: datetime) -> Decimal:
        """Account currency (EUR) per unit of the market's profit currency."""
        if profit_currency == self._cfg.account_currency:
            return Decimal(1)
        if profit_currency != "USD":
            raise _Reject("unsupported_profit_currency")
        assert self._adapter is not None
        session = self._adapter.session
        eurusd = self._markets["EURUSD"]
        try:
            tick = session.call(
                "symbol_info_tick", session.client.symbol_info_tick, eurusd.broker_symbol
            )
            quoted = session.time_policy.server_epoch_to_utc(float(tick.time_msc) / 1000.0)
        except (Mt5CallError, AmbiguousServerTime):
            raise _Reject("fx_rate_unavailable") from None
        if (now - quoted).total_seconds() > self._cfg.max_quote_age_s:
            raise _Reject("fx_rate_stale")
        mid = (_dec(tick.bid) + _dec(tick.ask)) / 2
        if mid <= 0:
            raise _Reject("fx_rate_unavailable")
        return Decimal(1) / mid

    def _deal_utc(self, deal: Any, fallback: datetime) -> tuple[datetime, bool]:
        assert self._adapter is not None
        try:
            epoch = float(getattr(deal, "time_msc", 0) or 0) / 1000.0 or float(deal.time)
            return self._adapter.session.time_policy.server_epoch_to_utc(epoch), True
        except AmbiguousServerTime:
            return fallback, False

    def _lane_deals(self, now: datetime, since: datetime | None = None) -> list[Any]:
        assert self._adapter is not None
        session = self._adapter.session
        policy = session.time_policy
        start = policy.utc_to_request_datetime(since or (now - timedelta(days=self._cfg.lookback_days)))
        end = policy.utc_to_request_datetime(now + timedelta(days=1))
        return list(session.call("history_deals_get", session.client.history_deals_get, start, end))

    def _lane_gate_account(self, account: Any, positions: list[Any], now: datetime) -> GateAccount:
        assert self._registry is not None
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        deals = [d for d in self._lane_deals(now) if int(d.type) in DEAL_TYPES_TRADE]
        realized_today = ZERO
        by_position: dict[int, list[Any]] = {}
        for d in deals:
            net = _dec(d.profit) + _dec(d.commission) + _dec(d.swap) + _dec(getattr(d, "fee", 0.0))
            when, exact = self._deal_utc(d, now)
            if when >= day_start and when <= now + timedelta(minutes=5):
                realized_today += net
            elif not exact:
                realized_today += min(net, ZERO)  # unknown day: losses only (fail closed)
            by_position.setdefault(int(d.position_id), []).append((when, net, int(d.entry)))
        open_ids = {int(p.ticket) for p in positions}
        closed = []
        for pid, items in by_position.items():
            if pid in open_ids or not any(e in (ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY) for _, _, e in items):
                continue
            closed.append((max(t for t, _, _ in items), sum((n for _, n, _ in items), ZERO)))
        closed.sort(key=lambda item: item[0])
        losses = 0
        for _, net in reversed(closed):
            if net < 0:
                losses += 1
            else:
                break
        equity, balance = _dec(account.equity), _dec(account.balance)
        peak_raw = self._registry.meta("peak_equity")
        peak = max(equity, _dec(peak_raw) if peak_raw else equity)
        if peak_raw is None or peak > _dec(peak_raw):
            self._registry.set_meta("peak_equity", str(peak))
        gross = net_notional = ZERO
        notionals: dict[str, Decimal] = {}
        signed: dict[str, Decimal] = {}
        risks: list[OpenRisk] = []
        for p in positions:
            market = self._canonical_of(str(p.symbol))
            if market is None:
                continue
            info = self._markets[market]
            fx = self._fx(info.profit_currency, now)
            units = _dec(p.volume) * info.spec.contract_size
            notional = units * _dec(p.price_current) * fx
            side = 1 if int(p.type) == 0 else -1
            gross += notional
            net_notional += notional * side
            notionals[market] = notionals.get(market, ZERO) + notional
            signed[market] = signed.get(market, ZERO) + units * side
            sl = _dec(p.sl or 0)
            if sl > 0:
                risks.append(
                    OpenRisk(
                        market=market,
                        cluster=CLUSTERS[market],
                        risk_money=units * abs(_dec(p.price_open) - sl) * fx,
                    )
                )
        return GateAccount(
            equity=equity,
            balance=balance,
            peak_equity=peak,
            start_of_day_equity=balance - realized_today,
            realized_pnl_today=realized_today,
            unrealized_pnl=_dec(account.profit),
            consecutive_losses=losses,
            gross_notional=gross,
            net_notional=net_notional,
            notionals=notionals,
            signed_units=signed,
            account_leverage=_dec(account.leverage),
            day_start=day_start,
            open_risks=tuple(risks),
            state_version=f"broker@{now.isoformat()}",
        )

    def _lane_prepare(self, intent: TradeIntent, now: datetime) -> _Prepared:
        assert self._adapter is not None
        account = self._lane_health()
        info = self._markets[intent.market]
        session = self._adapter.session
        client = session.client
        try:
            tick = session.call("symbol_info_tick", client.symbol_info_tick, info.broker_symbol)
        except Mt5CallError:
            raise _Reject("stale_feed") from None
        try:
            quote_utc = session.time_policy.server_epoch_to_utc(
                float(getattr(tick, "time_msc", 0) or tick.time * 1000) / 1000.0
            )
        except AmbiguousServerTime:
            raise StackFailClosed("ambiguous_server_time") from None
        age = (now - quote_utc).total_seconds()
        if age < -self._cfg.clock_skew_s:
            self._set_fatal("clock_anomaly")
            raise StackFailClosed("clock_anomaly")
        if age > self._cfg.feed_fatal_age_s:
            raise StackFailClosed("stale_feed")
        if age > self._cfg.max_quote_age_s:
            raise _Reject("stale_feed")
        bid, ask = _dec(tick.bid), _dec(tick.ask)
        if bid <= 0 or ask < bid:
            raise _Reject("invalid_quote")
        positions = self._lane_positions()
        foreign: list[str] = []
        same_symbol = False
        for p in positions:
            market = self._canonical_of(str(p.symbol))
            if market is None or int(p.magic) != self._cfg.magic:
                foreign.append(str(p.symbol))
            elif float(p.sl or 0.0) == 0.0:
                self._set_fatal("unprotected_exposure")
                raise StackFailClosed("unprotected_exposure")
            if str(p.symbol) == info.broker_symbol:
                same_symbol = True
        if same_symbol:
            raise _Reject("position_exists")
        self._foreign = tuple(foreign)
        if foreign:
            raise _Reject("foreign_position_at_broker")
        fx = self._fx(info.profit_currency, now)
        gate_account = self._lane_gate_account(account, positions, now)
        facts = MarketFacts(
            market=intent.market,
            contract_size=info.spec.contract_size,
            volume_min=info.spec.volume_min,
            volume_step=info.spec.volume_step,
            volume_max=info.spec.volume_max,
            max_leverage=info.spec.max_leverage,
            max_spread=info.spec.max_spread,
            fx=fx,
        )
        return _Prepared(
            bid=bid, ask=ask, quote_utc=quote_utc, account=gate_account, facts=facts,
            positions_seen=len(positions),
        )

    def _lane_confirm_entry(
        self, info: _MarketInfo, intent: TradeIntent, prepared: _Prepared
    ) -> tuple[Fill, int, bool, Decimal, Decimal | None] | None:
        """Broker truth after the entry: Fill from the entry deals + is the stop really attached?"""
        now = self._now().astimezone(UTC)
        positions = self._lane_symbol_positions(info.broker_symbol)
        if not positions:
            return None
        position = positions[0]
        ticket = int(position.ticket)
        entry_deals = [
            d
            for d in self._lane_deals(now, since=now - timedelta(days=2))
            if int(d.position_id) == ticket and int(d.entry) == ENTRY_IN and int(d.type) in DEAL_TYPES_TRADE
        ]
        if entry_deals:
            quantity = sum((_dec(d.volume) for d in entry_deals), ZERO)
            price = sum((_dec(d.volume) * _dec(d.price) for d in entry_deals), ZERO) / quantity
            commission = sum((_dec(d.commission) + _dec(getattr(d, "fee", 0.0)) for d in entry_deals), ZERO)
            order_id = str(int(entry_deals[0].order))
        else:  # deal history lags: the position itself is still broker truth
            quantity, price, commission = _dec(position.volume), _dec(position.price_open), ZERO
            order_id = "0"
        executable = executable_price(intent.direction, prepared.bid, prepared.ask)
        slippage = (price - executable) if intent.direction == 1 else (executable - price)
        fill = Fill(
            intent_id=intent.intent_id,
            price=price,
            quantity=quantity,
            spread=prepared.ask - prepared.bid,
            slippage=slippage,
            commission=commission,
            swap=_dec(position.swap),
            broker_order_id=order_id,
            broker_position_id=str(ticket),
        )
        stop = Decimal(str(intent.stop))
        stop_seen = _dec(position.sl or 0)
        tolerance = info.spec.tick_size * self._cfg.protection_tolerance_ticks
        protected = stop_seen > 0 and abs(stop_seen - stop) <= tolerance
        tp_seen = _dec(position.tp or 0)
        return fill, ticket, protected, stop_seen, (tp_seen if tp_seen > 0 else None)

    # ------------------------------------------------------------------------------- closures

    def _lane_build_closed(self, row: reg.IntentRow) -> PositionClosed | None:
        """PositionClosed from the broker's deals once the position is gone; None if not yet."""
        assert self._registry is not None and self._adapter is not None
        if row.position_ticket is None:
            return None
        ticket = int(row.position_ticket)
        session = self._adapter.session
        if session.call("positions_get", session.client.positions_get, ticket=ticket):
            return None
        now = self._now().astimezone(UTC)
        deals = [
            d
            for d in self._lane_deals(now, since=now - timedelta(days=self._cfg.lookback_days))
            if int(d.position_id) == ticket and int(d.type) in DEAL_TYPES_TRADE
        ]
        exits = [d for d in deals if int(d.entry) in (ENTRY_OUT, ENTRY_INOUT, ENTRY_OUT_BY)]
        if not exits:
            first = self._closing_seen.setdefault(row.intent_id, time.monotonic())
            if time.monotonic() - first < self._cfg.close_grace_s:
                return None
            event = PositionClosed(
                intent_id=row.intent_id, broker_position_id=str(ticket), exit_reason="EXTERNAL",
                closed_utc=_iso(now),
            )
            self._registry.update(row.intent_id, status=reg.CLOSED, detail="closed_without_deals")
            return event
        exits.sort(key=lambda d: (float(getattr(d, "time_msc", 0) or d.time), int(d.ticket)))
        quantity = sum((_dec(d.volume) for d in exits), ZERO)
        price = sum((_dec(d.volume) * _dec(d.price) for d in exits), ZERO) / quantity
        commission = sum((_dec(d.commission) + _dec(getattr(d, "fee", 0.0)) for d in deals), ZERO)
        swap = sum((_dec(d.swap) for d in deals), ZERO)
        profit = sum((_dec(d.profit) for d in exits), ZERO)
        last = exits[-1]
        closed_at, _ = self._deal_utc(last, now)
        reason_code = int(getattr(last, "reason", _REASON_EXPERT))
        if reason_code == _REASON_SL:
            reason = "STOP"
        elif reason_code == _REASON_TP:
            reason = "TARGET"
        elif reason_code == _REASON_EXPERT:
            reason = row.exit_hint if row.exit_hint in ("SESSION_END", "MANUAL") else "MANUAL"
        else:
            reason = "EXTERNAL"
        event = PositionClosed(
            intent_id=row.intent_id,
            broker_position_id=str(ticket),
            exit_reason=reason,  # type: ignore[arg-type]
            exit_price=price,
            exit_quantity=quantity,
            closed_utc=_iso(closed_at),
            commission=commission,
            swap=swap,
            profit_eur=profit,
        )
        self._registry.update(row.intent_id, status=reg.CLOSED)
        self._closing_seen.pop(row.intent_id, None)
        return event

    # ================================================================================= poll/clock

    def _add_pending(self, *events: ExecutionEvent) -> None:
        with self._pending_lock:
            self._pending_events.extend(events)

    def _drain_pending(self) -> list[ExecutionEvent]:
        with self._pending_lock:
            out, self._pending_events = self._pending_events, []
        return out

    def poll_events(self) -> list[ExecutionEvent]:
        """Broker-side events since the last call: stop/target exits, external closes, late fills."""
        with self._submit_lock:
            self._check_fatal()
            events = self._drain_pending()
            try:
                late, repairs = self._on_lane(self._lane_poll)
            except StackFailClosed as exc:
                if str(exc).startswith("broker_disconnect"):
                    since = self._disconnected_since
                    if since is None:
                        self._disconnected_since = time.monotonic()
                        return events
                    if time.monotonic() - since <= self._cfg.disconnect_grace_s:
                        return events
                self._set_fatal(str(exc))
                raise
            except _Reject:
                return events
            self._disconnected_since = None
            events.extend(late)
            for row, ticket in repairs:
                events.extend(self._repair_protection(row, ticket))
            return events

    def _lane_poll(self) -> tuple[list[ExecutionEvent], list[tuple[reg.IntentRow | None, Any]]]:
        assert self._adapter is not None and self._registry is not None
        adapter = self._adapter
        session = adapter.session
        if session.state is not SessionState.CONNECTED:
            raise StackFailClosed("broker_disconnect")
        terminal = session.call("terminal_info", session.client.terminal_info, none_ok=True)
        if terminal is None or not getattr(terminal, "connected", False):
            raise StackFailClosed("broker_disconnect")
        adapter.exec_client.sync_once()  # inbound broker truth is always ingested
        recon = adapter.exec_client.recon
        if (
            recon.state is not ReconciliationState.RECONCILED
            and time.monotonic() - self._last_reconcile_attempt >= self._cfg.reconcile_retry_s
        ):
            self._last_reconcile_attempt = time.monotonic()
            adapter.exec_client.reconcile()
        late: list[ExecutionEvent] = []
        repairs: list[tuple[reg.IntentRow | None, Any]] = []
        own = self._lane_own_positions()
        now = self._now().astimezone(UTC)
        seen_tickets = set()
        foreign = []
        for position in self._lane_positions():
            if self._canonical_of(str(position.symbol)) is None or int(position.magic) != self._cfg.magic:
                foreign.append(str(position.symbol))
        self._foreign = tuple(foreign)
        if foreign:
            self._halt("foreign_position_at_broker")
        for position, market, row in own:
            ticket = int(position.ticket)
            seen_tickets.add(ticket)
            unresolved = row is not None and row.status in (reg.SENT, reg.IN_DOUBT, reg.ACCEPTED)
            if row is not None and (unresolved or row.position_ticket != ticket):
                late.extend(self._late_fill_events(row, market, position, now))
                self._registry.update(row.intent_id, status=reg.OPEN, position_ticket=ticket)
            if float(position.sl or 0.0) == 0.0:
                repairs.append((row, position))
        for row in self._registry.with_status(reg.OPEN):
            if row.position_ticket is None or int(row.position_ticket) in seen_tickets:
                continue
            closed = self._lane_build_closed(row)
            if closed is not None:
                late.append(closed)
        for row in self._registry.with_status(reg.SENT, reg.IN_DOUBT):
            store_row = adapter.store.by_client_order_id(row.client_order_id)
            if (
                store_row is not None
                and store_row.status == "REJECTED"
                and not any(int(p.magic) == self._cfg.magic and self._canonical_of(str(p.symbol)) == row.market for p, _, _ in own)
            ):
                self._registry.update(row.intent_id, status=reg.REJECTED, detail="no_broker_record")
                late.append(Rejected(intent_id=row.intent_id, reason="no_broker_record"))
        return late, repairs

    def _late_fill_events(
        self, row: reg.IntentRow, market: str, position: Any, now: datetime
    ) -> list[ExecutionEvent]:
        """A position we did not see the fill for (crash window / in-doubt send): report it."""
        ticket = int(position.ticket)
        info = self._markets[market]
        price, quantity = _dec(position.price_open), _dec(position.volume)
        stop_seen = _dec(position.sl or 0)
        events: list[ExecutionEvent] = [
            Fill(
                intent_id=row.intent_id, price=price, quantity=quantity, spread=ZERO,
                slippage=ZERO, commission=ZERO, swap=_dec(position.swap),
                broker_order_id="0", broker_position_id=str(ticket),
            )
        ]
        tolerance = info.spec.tick_size * self._cfg.protection_tolerance_ticks
        if stop_seen > 0 and abs(stop_seen - Decimal(row.stop)) <= tolerance:
            tp = _dec(position.tp or 0)
            events.append(
                ProtectionConfirmed(
                    intent_id=row.intent_id, broker_position_id=str(ticket), stop=stop_seen,
                    target=tp if tp > 0 else None,
                )
            )
        return events

    # -- protection repair ---------------------------------------------------------------------------

    def _repair_protection(self, row: reg.IntentRow | None, position: Any) -> list[ExecutionEvent]:
        """A position of ours has no broker stop: give it the intent's stop or flatten it."""
        ticket = int(position.ticket)
        market = self._canonical_of(str(position.symbol))
        assert market is not None
        info = self._markets[market]
        self._halt("unprotected_position")
        if row is not None:
            try:
                denial = self._on_lane(self._lane_protect, ticket, Decimal(row.stop))
            except (StackFailClosed, _Reject):
                denial = "protect_unavailable"
            if denial is None:
                return [
                    ProtectionConfirmed(
                        intent_id=row.intent_id, broker_position_id=str(ticket),
                        stop=Decimal(row.stop),
                        target=None if row.target is None else Decimal(row.target),
                    )
                ]
        self._flatten(info, tag=f"unprotected:{ticket}", hint="MANUAL")
        if row is None:
            return []
        fresh = self._registry.get(row.intent_id) if self._registry else None
        closed = self._on_lane(self._lane_build_closed, fresh or row)
        return [closed] if closed is not None else []

    def _lane_protect(self, ticket: int, stop: Decimal) -> str | None:
        """Tighten-only stop on a broker position (adapter ``emergency_protect``), then verify."""
        assert self._adapter is not None
        denial = self._adapter.exec_client.emergency_protect(ticket, stop)
        if denial is not None:
            return denial
        session = self._adapter.session
        rows = session.call("positions_get", session.client.positions_get, ticket=ticket)
        if not rows or float(rows[0].sl or 0.0) == 0.0:
            return "protection_not_visible_at_broker"
        return None

    def _repair_unprotected_at_start(self) -> None:
        repairs = self._on_lane(self._lane_unprotected_positions)
        for row, position in repairs:
            self._add_pending(*self._repair_protection(row, position))

    def _lane_unprotected_positions(self) -> list[tuple[reg.IntentRow | None, Any]]:
        return [
            (row, position)
            for position, _market, row in self._lane_own_positions()
            if float(position.sl or 0.0) == 0.0
        ]

    # -- forced flat ------------------------------------------------------------------------------------

    def on_clock(self, now: datetime) -> list[ExecutionEvent]:
        """Reduce-only close of every open intent whose ``forced_flat_utc`` has arrived."""
        assert self._registry is not None
        with self._submit_lock:
            self._check_fatal()
            current = now.astimezone(UTC)
            events: list[ExecutionEvent] = []
            for row in self._registry.with_status(reg.OPEN):
                if row.forced_flat_utc is None or current < parse_utc(row.forced_flat_utc):
                    continue
                info = self._markets[row.market]
                still_open = self._on_lane(self._lane_symbol_positions, info.broker_symbol)
                if still_open and not self._flatten(
                    info, tag=f"forced-flat:{row.intent_id}", hint="SESSION_END"
                ):
                    continue
                fresh = self._registry.get(row.intent_id) or row
                closed = self._on_lane(self._lane_build_closed, fresh)
                if closed is not None:
                    events.append(closed)
            return events


def _machine_refusal(reason: str) -> str:
    """Map an adapter/strategy refusal text to a stable machine reason code."""
    text = reason or ""
    upper = text.upper()
    if "POSITION_EXISTS" in upper or text == "position_exists":
        return "position_exists"
    if upper.startswith("ORDER_CHECK") or upper.startswith("ORDER_SEND") or "REJECT" in upper:
        return "broker_reject"
    if "NOT_VENUE_RECONCILED" in upper:
        return "not_reconciled"
    if upper.startswith("RUNTIME_"):
        return "runtime_not_ready"
    if "UNPROTECTED" in upper:
        return "unprotected_position"
    if "ACCOUNT_IS_NOT_DEMO" in upper:
        return "non_demo_account"
    if "ACCOUNT_IDENTITY" in upper:
        return "account_identity_changed"
    if "BROKER_UNREACHABLE" in upper or upper.startswith("SESSION_"):
        return "broker_disconnect"
    if "DUPLICATE" in upper:
        return "duplicate_intent"
    if text in ("halted", "stale_signal", "past_forced_flat", "no_stop", "quantity_precision"):
        return text
    return "execution_denied:" + text[:60]
