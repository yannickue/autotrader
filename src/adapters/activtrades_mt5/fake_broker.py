"""Stateful in-memory MT5 broker for tests (implements `MT5ClientProtocol`).

Test support only: NO `MetaTrader5` import, no I/O. Unlike `testing.FakeMT5Client`
(canned responses), this simulates a NETTING account end to end: order_check /
order_send semantics (order_check success is retcode 0; order_send success is
DONE 10009 / DONE_PARTIAL 10010 / PLACED 10008), positions with attached
SL/TP, deals with broker tickets, partial/progressive fills, delayed deals,
duplicate deal visibility, broker-side stop execution, and connection loss.

Times are SERVER-clock epochs (as the real terminal reports them); tests set
`broker.server_time` explicitly.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from nautilus_mt5.constants import (
    ORDER_CHECK_OK,
    DealReason,
    Filling,
    MarginMode,
    Retcode,
    TradeAction,
)

DEAL_BUY, DEAL_SELL = 0, 1
ENTRY_IN, ENTRY_OUT, ENTRY_INOUT = 0, 1, 2
ORDER_STATE_STARTED, ORDER_STATE_PLACED, ORDER_STATE_CANCELED = 0, 1, 2
ORDER_STATE_PARTIAL, ORDER_STATE_FILLED, ORDER_STATE_REJECTED = 3, 4, 5


@dataclass
class _Pending:
    """A deal that exists at the broker but is not yet visible in history."""

    deal: SimpleNamespace


@dataclass
class FakeBrokerConfig:
    login: int = 900001
    margin_mode: int = MarginMode.RETAIL_NETTING
    balance: float = 10_000.0
    leverage: int = 30
    currency: str = "EUR"
    margin_rate: float = 0.05
    commission_per_lot: float = 0.0
    symbol: str = "Ger40"
    magic_filter: int | None = None
    trade_mode: int = 0  # ACCOUNT_TRADE_MODE_DEMO; tests may inject REAL/CONTEST
    server: str = "FakeBroker-Demo"


class FakeMT5Broker:
    def __init__(self, symbol_info: Any, config: FakeBrokerConfig | None = None) -> None:
        self.cfg = config or FakeBrokerConfig()
        self.symbol_info_obj = symbol_info
        self._server_time = 1_790_000_000  # server-clock epoch seconds
        self.live_offset_s: int | None = None  # if set: server_time follows the real clock
        self.bid = 25_000.00
        self.ask = 25_001.50
        self.connected = True
        self.market_open = True
        self._next_ticket = 1_000_000
        self.balance = self.cfg.balance
        self.positions: dict[int, SimpleNamespace] = {}
        self.orders: dict[int, SimpleNamespace] = {}  # working pending orders
        self.history_orders: list[SimpleNamespace] = []
        self.deals: list[SimpleNamespace] = []  # ALL deals (broker truth)
        self._hidden: set[int] = set()  # deal tickets not yet visible in history
        self._duplicate_visible: set[int] = set()
        self.request_log: list[dict[str, Any]] = []
        self.check_log: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._last_error = (1, "Success")
        # failure / behaviour injection
        self.send_retcode_override: list[int] = []
        self.raise_on_send: list[Exception] = []
        self.lose_response_after_execute = 0  # execute at broker but return None N times
        self.fill_plan: list[list[tuple[float, float]]] = []  # per send: [(qty, price), ...]
        self.progressive = False  # hide plan deals until release_deals()
        self.fail_calls_when_disconnected = True
        self.initialize_calls: list[dict[str, Any]] = []
        self.order_send_calls = 0
        self.rates: dict[int, Any] = {}  # mt5 timeframe -> structured numpy array
        # -- additional symbols (multi-market DEMO tests); the primary symbol keeps bid/ask/rates
        self.extra_info: dict[str, Any] = {}
        self.extra_quotes: dict[str, list[float]] = {}  # name -> [bid, ask]
        self.extra_rates: dict[str, dict[int, Any]] = {}
        self.profit_fx: dict[str, float] = {}  # symbol -> account ccy per profit-ccy unit
        self.strip_stops_on_entry = False  # broker "forgets" the attached SL/TP (protection tests)
        # -- concurrency instrumentation (the real MT5 API must never see overlapping calls)
        self.call_latency_s = 0.0  # widen race windows in concurrency tests
        self.send_delay_s = 0.0  # order_send blocks this long BEFORE executing
        self.max_concurrency = 0
        self.call_threads: dict[str, set[int]] = {}
        self._active_by_thread: dict[int, int] = {}
        self._track_lock = threading.Lock()
        for name in (
            "initialize",
            "shutdown",
            "terminal_info",
            "version",
            "account_info",
            "symbols_get",
            "symbol_select",
            "symbol_info",
            "symbol_info_tick",
            "positions_get",
            "orders_get",
            "history_orders_get",
            "history_deals_get",
            "copy_rates_from",
            "copy_rates_from_pos",
            "copy_rates_range",
            "copy_ticks_from",
            "copy_ticks_range",
            "order_calc_margin",
            "order_calc_profit",
            "order_check",
            "order_send",
            "last_error",
        ):
            setattr(self, name, self._tracked(name, getattr(self, name)))

    # -- helpers -------------------------------------------------------------

    @property
    def server_time(self) -> int:
        if self.live_offset_s is not None:
            return int(time.time()) + self.live_offset_s
        return self._server_time

    @server_time.setter
    def server_time(self, value: int) -> None:
        self._server_time = value

    def _tracked(self, name: str, fn: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tid = threading.get_ident()
            with self._track_lock:
                # Nested calls on the SAME thread (order_check -> account_info) are not overlap;
                # concurrency = number of distinct threads inside the API at once.
                self._active_by_thread[tid] = self._active_by_thread.get(tid, 0) + 1
                self.max_concurrency = max(self.max_concurrency, len(self._active_by_thread))
                self.call_threads.setdefault(name, set()).add(tid)
            try:
                if self.call_latency_s:
                    time.sleep(self.call_latency_s)
                return fn(*args, **kwargs)
            finally:
                with self._track_lock:
                    self._active_by_thread[tid] -= 1
                    if self._active_by_thread[tid] == 0:
                        del self._active_by_thread[tid]

        return wrapper

    def all_call_threads(self) -> set[int]:
        return set().union(*self.call_threads.values()) if self.call_threads else set()

    def _ticket(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    @property
    def point(self) -> float:
        return float(self.symbol_info_obj.point)

    # -- multi-symbol helpers (the primary symbol behaves exactly as before) ---------------

    def add_symbol(self, info: Any, bid: float, ask: float, *, profit_fx: float = 1.0) -> None:
        self.extra_info[info.name] = info
        self.extra_quotes[info.name] = [bid, ask]
        self.profit_fx[info.name] = profit_fx

    def set_symbol_quote(self, symbol: str, bid: float, ask: float) -> None:
        if symbol == self.symbol_info_obj.name:
            return self.set_quote(bid, ask)
        self.extra_quotes[symbol] = [bid, ask]
        self._evaluate_stops()

    def _info(self, symbol: str) -> Any:
        if symbol == self.symbol_info_obj.name:
            return self.symbol_info_obj
        return self.extra_info.get(symbol)

    def _bidask(self, symbol: str) -> tuple[float, float]:
        if symbol == self.symbol_info_obj.name:
            return self.bid, self.ask
        bid, ask = self.extra_quotes[symbol]
        return bid, ask

    def _mult(self, symbol: str) -> float:
        """contract size x profit->account FX (1.0 for the single-symbol legacy setup)."""
        info = self._info(symbol)
        size = float(getattr(info, "trade_contract_size", 1.0)) if info is not None else 1.0
        return size * self.profit_fx.get(symbol, 1.0)

    def _guard(self, name: str) -> bool:
        self.calls.append(name)
        if not self.connected and self.fail_calls_when_disconnected:
            self._last_error = (-10004, "No IPC connection")
            return False
        return True

    def set_quote(self, bid: float, ask: float) -> None:
        self.bid, self.ask = bid, ask
        self._evaluate_stops()

    def disconnect(self) -> None:
        self.connected = False

    def reconnect(self) -> None:
        self.connected = True

    # -- protocol: session -----------------------------------------------------

    def initialize(
        self, path=None, *, login=None, password=None, server=None, timeout=None, portable=False
    ) -> bool:
        self.initialize_calls.append({"path": path, "login": login, "server": server})
        return True

    def shutdown(self) -> None:
        self.calls.append("shutdown")

    def terminal_info(self) -> Any:
        if not self._guard("terminal_info"):
            return None
        return SimpleNamespace(
            connected=self.connected,
            trade_allowed=True,
            build=6231,
            company="FakeBroker",
            name="FakeTerminal",
        )

    def version(self) -> Any:
        return (500, 6231, "01 Jan 2026")

    def last_error(self) -> tuple[int, str]:
        return self._last_error

    def account_info(self) -> Any:
        if not self._guard("account_info"):
            return None
        profit = self._floating_profit()
        margin = sum(
            p.volume * p.price_current * self.cfg.margin_rate * self._mult(p.symbol)
            for p in self.positions.values()
        )
        equity = self.balance + profit
        return SimpleNamespace(
            login=self.cfg.login,
            balance=self.balance,
            equity=equity,
            profit=profit,
            margin=margin,
            margin_free=equity - margin,
            margin_level=(equity / margin * 100) if margin else 0.0,
            leverage=self.cfg.leverage,
            currency=self.cfg.currency,
            margin_mode=int(self.cfg.margin_mode),
            trade_mode=self.cfg.trade_mode,
            trade_allowed=True,
            server=self.cfg.server,
        )

    # -- protocol: symbols / quotes ---------------------------------------------

    def symbols_get(self, group=None):
        if not self._guard("symbols_get"):
            return None
        return (self.symbol_info_obj, *self.extra_info.values())

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        return self._guard("symbol_select") and self._info(symbol) is not None

    def symbol_info(self, symbol: str) -> Any:
        if not self._guard("symbol_info"):
            return None
        return self._info(symbol)

    def symbol_info_tick(self, symbol: str) -> Any:
        if not self._guard("symbol_info_tick") or self._info(symbol) is None:
            return None
        bid, ask = self._bidask(symbol)
        return SimpleNamespace(
            time=self.server_time,
            bid=bid,
            ask=ask,
            last=0.0,
            volume=0,
            time_msc=self.server_time * 1000,
            flags=6,
            volume_real=0.0,
        )

    def copy_rates_from(self, symbol, timeframe, date_from, count):
        return self._guard("copy_rates_from") and None

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        """Newest `count` rows ending `start_pos` bars back (last row = forming bar)."""
        if not self._guard("copy_rates_from_pos") or self._info(symbol) is None:
            return None
        if symbol == self.symbol_info_obj.name:
            rows = self.rates.get(int(timeframe))
        else:
            rows = self.extra_rates.get(symbol, {}).get(int(timeframe))
        if rows is None:
            return None
        end = len(rows) - int(start_pos)
        return rows[max(0, end - int(count)) : end]

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        return self._guard("copy_rates_range") and None

    def copy_ticks_from(self, symbol, date_from, count, flags):
        return self._guard("copy_ticks_from") and None

    def copy_ticks_range(self, symbol, date_from, date_to, flags):
        return self._guard("copy_ticks_range") and None

    def order_calc_margin(self, action, symbol, volume, price):
        return volume * price * self.cfg.margin_rate * self._mult(symbol)

    def order_calc_profit(self, action, symbol, volume, price_open, price_close):
        sign = 1 if action == 0 else -1
        return sign * (price_close - price_open) * volume

    # -- protocol: state queries ---------------------------------------------------

    def _reject_unnamed(self, args: tuple) -> bool:
        """Real MetaTrader5: positions_get/orders_get accept ONLY keyword filters."""
        if args:
            self._last_error = (-2, "Unnamed arguments not allowed")
            return True
        return False

    def positions_get(self, *args, symbol=None, group=None, ticket=None):
        if not self._guard("positions_get") or self._reject_unnamed(args):
            return None
        rows = [
            p
            for p in self.positions.values()
            if (symbol is None or p.symbol == symbol) and (ticket is None or p.ticket == ticket)
        ]
        return tuple(rows)

    def orders_get(self, *args, symbol=None, group=None, ticket=None):
        if not self._guard("orders_get") or self._reject_unnamed(args):
            return None
        rows = [
            o
            for o in self.orders.values()
            if (symbol is None or o.symbol == symbol) and (ticket is None or o.ticket == ticket)
        ]
        return tuple(rows)

    def visible_deals(self) -> tuple:
        """Test helper (NOT part of the MT5 API): every deal currently visible in history."""
        return tuple(d for d in self.deals if d.ticket not in self._hidden)

    def _history_args_invalid(self, date_from, date_to, group, position, ticket) -> bool:
        """Real MetaTrader5: (None, None) without ticket/position is 'Invalid arguments'."""
        if (date_from is None) != (date_to is None) or (
            date_from is None and ticket is None and position is None and group is None
        ):
            self._last_error = (-2, "Invalid arguments")
            return True
        return False

    def history_orders_get(
        self, date_from=None, date_to=None, *, group=None, position=None, ticket=None
    ):
        if not self._guard("history_orders_get"):
            return None
        if self._history_args_invalid(date_from, date_to, group, position, ticket):
            return None
        rows = [
            o
            for o in self.history_orders
            if (ticket is None or o.ticket == ticket)
            and (position is None or o.position_id == position)
        ]
        if ticket is not None and not rows:
            self._last_error = (-2, "Terminal: Invalid params")  # unknown ticket == None, as real
            return None
        return tuple(rows)

    def history_deals_get(
        self, date_from=None, date_to=None, *, group=None, position=None, ticket=None
    ):
        if not self._guard("history_deals_get"):
            return None
        if self._history_args_invalid(date_from, date_to, group, position, ticket):
            return None
        visible = [d for d in self.deals if d.ticket not in self._hidden]
        rows = [
            d
            for d in visible
            if (ticket is None or d.ticket == ticket)
            and (position is None or d.position_id == position)
            and (date_from is None or self._in_range(d.time, date_from, date_to))
        ]
        if ticket is not None and not rows:
            self._last_error = (-2, "Terminal: Invalid params")
            return None
        for ticket_ in self._duplicate_visible:  # same deal listed twice (observed-N-times case)
            rows.extend(d for d in visible if d.ticket == ticket_)
        return tuple(rows)

    @staticmethod
    def _in_range(epoch: int, date_from, date_to) -> bool:
        def to_epoch(value):
            return int(value.timestamp()) if isinstance(value, datetime) else value

        lo, hi = to_epoch(date_from), to_epoch(date_to)
        return (lo is None or epoch >= lo) and (hi is None or epoch <= hi)

    # -- deal visibility controls (delayed / duplicate / out-of-order reports) -----------

    def hide_deals(self, *tickets: int) -> None:
        self._hidden.update(tickets)

    def release_deals(self, *tickets: int) -> None:
        targets = set(tickets) if tickets else set(self._hidden)
        for t in sorted(targets):
            self._hidden.discard(t)
            self._apply_release(t)

    def show_deal_twice(self, ticket: int) -> None:
        self._duplicate_visible.add(ticket)

    def _apply_release(self, deal_ticket: int) -> None:
        """Progressive fills: order moves PARTIAL -> FILLED as its deals become visible."""
        deal = next(d for d in self.deals if d.ticket == deal_ticket)
        order = next((o for o in self.history_orders if o.ticket == deal.order), None)
        if order is None:
            return
        released = sum(
            d.volume for d in self.deals if d.order == order.ticket and d.ticket not in self._hidden
        )
        order.volume_current = round(order.volume_initial - released, 8)
        order.state = ORDER_STATE_FILLED if order.volume_current <= 1e-9 else ORDER_STATE_PARTIAL

    # -- order_check / order_send ------------------------------------------------------------

    def _validate(self, request: dict[str, Any]) -> tuple[int, str]:
        info = self._info(request.get("symbol"))
        action = request.get("action")
        if info is None:
            return Retcode.INVALID, "Invalid symbol"
        symbol = info.name
        if action == TradeAction.SLTP:
            position = self.positions.get(int(request.get("position", 0)))
            if position is None:
                return Retcode.POSITION_CLOSED, "Position not found"
            return self._validate_stops(
                position.type, request.get("sl", 0.0), request.get("tp", 0.0), symbol=symbol
            )
        if action == TradeAction.DEAL:
            volume = float(request.get("volume", 0))
            if volume < info.volume_min - 1e-9 or volume > info.volume_max + 1e-9:
                return Retcode.INVALID_VOLUME, "Invalid volume"
            steps = (volume - info.volume_min) / info.volume_step
            if abs(steps - round(steps)) > 1e-6:
                return Retcode.INVALID_VOLUME, "Invalid volume"
            fill = request.get("type_filling")
            allowed = {Filling.FOK: 1, Filling.IOC: 2}.get(fill)
            if allowed is None or not (int(info.filling_mode) & allowed):
                return Retcode.INVALID_FILL, "Unsupported filling mode"
            if not self.market_open:
                return Retcode.MARKET_CLOSED, "Market closed"
            position_ticket = request.get("position")
            if position_ticket and int(position_ticket) not in self.positions:
                return Retcode.POSITION_CLOSED, "Position not found"
            side_ok = request.get("type") in (0, 1)
            if not side_ok:
                return Retcode.INVALID, "Invalid order type"
            if not position_ticket:
                bad = self._validate_stops(
                    request["type"],
                    request.get("sl", 0.0),
                    request.get("tp", 0.0),
                    entry=True,
                    symbol=symbol,
                )
                if bad[0] != ORDER_CHECK_OK:
                    return bad
                margin_needed = (
                    volume * self._bidask(symbol)[1] * self.cfg.margin_rate * self._mult(symbol)
                )
                if margin_needed > (self.balance + self._floating_profit()):
                    return Retcode.NO_MONEY, "No money"
            return ORDER_CHECK_OK, "Done"
        return Retcode.INVALID, "Unsupported action in fake broker"

    def _validate_stops(
        self,
        order_type: int,
        sl: float,
        tp: float,
        *,
        entry: bool = False,
        symbol: str | None = None,
    ):
        is_buy = order_type == 0
        symbol = symbol or self.symbol_info_obj.name
        info = self._info(symbol)
        bid, ask = self._bidask(symbol)
        ref = bid if is_buy else ask  # stops measured from the CLOSE price
        min_dist = float(info.trade_stops_level) * float(info.point)
        sl_bad = sl and (
            (is_buy and sl >= ref - min_dist + 1e-9) or (not is_buy and sl <= ref + min_dist - 1e-9)
        )
        tp_bad = tp and (
            (is_buy and tp <= ref + min_dist - 1e-9) or (not is_buy and tp >= ref - min_dist + 1e-9)
        )
        if sl_bad or tp_bad:
            return Retcode.INVALID_STOPS, "Invalid stops"
        return ORDER_CHECK_OK, "Done"

    def order_check(self, request: dict[str, Any]) -> Any:
        if not self._guard("order_check"):
            return None
        self.check_log.append(dict(request))
        code, comment = self._validate(request)
        acct = self.account_info()
        return SimpleNamespace(
            retcode=int(code),
            comment=comment,
            balance=acct.balance,
            equity=acct.equity,
            profit=acct.profit,
            margin=acct.margin,
            margin_free=acct.margin_free,
            margin_level=acct.margin_level,
            volume=request.get("volume", 0.0),
            price=0.0,
            bid=self.bid,
            ask=self.ask,
            request_id=0,
            retcode_external=0,
            order=0,
            deal=0,
        )

    def order_send(self, request: dict[str, Any]) -> Any:
        self.order_send_calls += 1
        if self.send_delay_s:
            time.sleep(self.send_delay_s)
        if not self._guard("order_send"):
            return None
        self.request_log.append(dict(request))
        if self.raise_on_send:
            raise self.raise_on_send.pop(0)
        if self.send_retcode_override:
            code = self.send_retcode_override.pop(0)
            return self._result(code, comment="override", request=request)
        code, comment = self._validate(request)
        if code != ORDER_CHECK_OK:
            return self._result(code, comment=comment, request=request)

        action = request["action"]
        if action == TradeAction.SLTP:
            position = self.positions[int(request["position"])]
            position.sl = float(request.get("sl", position.sl))
            position.tp = float(request.get("tp", position.tp))
            position.time_update = self.server_time
            return self._result(Retcode.DONE, comment="Done", request=request)

        result = self._execute_deal(request)
        if self.lose_response_after_execute > 0:
            self.lose_response_after_execute -= 1
            self._last_error = (-10003, "IPC timeout")
            return None
        return result

    # -- execution core -----------------------------------------------------------------------

    def _result(
        self,
        retcode: int,
        *,
        comment: str,
        request: dict[str, Any],
        order: int = 0,
        deal: int = 0,
        volume: float = 0.0,
        price: float = 0.0,
    ) -> Any:
        return SimpleNamespace(
            retcode=int(retcode),
            comment=comment,
            order=order,
            deal=deal,
            volume=volume,
            price=price,
            bid=self.bid,
            ask=self.ask,
            request_id=len(self.request_log),
            retcode_external=0,
        )

    def _execute_deal(self, request: dict[str, Any]) -> Any:
        is_buy = request["type"] == 0
        volume = float(request["volume"])
        bid, ask = self._bidask(request["symbol"])
        plan = self.fill_plan.pop(0) if self.fill_plan else [(volume, ask if is_buy else bid)]
        filled = round(sum(q for q, _ in plan), 8)
        order_ticket = self._ticket()
        order = SimpleNamespace(
            ticket=order_ticket,
            time_setup=self.server_time,
            time_done=self.server_time,
            time_expiration=0,
            type=request["type"],
            state=ORDER_STATE_FILLED,
            magic=int(request.get("magic", 0)),
            position_id=0,
            volume_initial=volume,
            volume_current=round(volume - filled, 8),
            price_open=plan[0][1],
            sl=float(request.get("sl", 0.0)),
            tp=float(request.get("tp", 0.0)),
            price_current=plan[0][1],
            symbol=request["symbol"],
            comment=str(request.get("comment", "")),
            external_id="",
            type_filling=int(request.get("type_filling", Filling.IOC)),
            reason=DealReason.EXPERT,
        )
        self.history_orders.append(order)
        deals = []
        for qty, price in plan:
            deals.append(self._book_fill(order, request, is_buy, qty, price))
        if filled < volume - 1e-9:
            order.state = ORDER_STATE_PARTIAL if self.progressive else ORDER_STATE_CANCELED
            order.volume_current = round(volume - filled, 8)
        if self.progressive:
            for d in deals:
                self._hidden.add(d.ticket)
            order.state = ORDER_STATE_PLACED
            order.volume_current = volume
        code = Retcode.DONE if filled >= volume - 1e-9 else Retcode.DONE_PARTIAL
        if filled <= 0:
            code = Retcode.REJECT
        return self._result(
            code,
            comment="Request executed",
            request=request,
            order=order_ticket,
            deal=deals[-1].ticket if deals else 0,
            volume=filled,
            price=plan[-1][1],
        )

    def _book_fill(
        self,
        order,
        request,
        is_buy: bool,
        qty: float,
        price: float,
        reason: int = DealReason.EXPERT,
    ) -> SimpleNamespace:
        symbol = order.symbol
        position_ticket = int(request.get("position", 0)) or self._net_position(symbol)
        existing = self.positions.get(position_ticket) if position_ticket else None
        commission = -self.cfg.commission_per_lot * qty
        profit = 0.0
        if existing is not None and (existing.type == 0) != is_buy:  # reduces/closes
            close_qty = min(qty, existing.volume)
            sign = 1 if existing.type == 0 else -1
            profit = sign * (price - existing.price_open) * close_qty * self._mult(symbol)
            existing.volume = round(existing.volume - close_qty, 8)
            entry = ENTRY_OUT
            pos_id = existing.identifier
            if existing.volume <= 1e-9:
                del self.positions[existing.ticket]
            self.balance += profit + commission
        elif existing is not None:  # adds to same direction (netting average)
            total = existing.volume + qty
            existing.price_open = round(
                (existing.price_open * existing.volume + price * qty) / total, 5
            )
            existing.volume = round(total, 8)
            entry = ENTRY_IN
            pos_id = existing.identifier
            self.balance += commission
        else:  # opens
            pos_id = self._ticket()
            self.positions[pos_id] = SimpleNamespace(
                ticket=pos_id,
                identifier=pos_id,
                symbol=symbol,
                type=0 if is_buy else 1,
                volume=round(qty, 8),
                price_open=price,
                price_current=price,
                sl=0.0 if self.strip_stops_on_entry else float(request.get("sl", 0.0)),
                tp=0.0 if self.strip_stops_on_entry else float(request.get("tp", 0.0)),
                swap=0.0,
                profit=0.0,
                time=self.server_time,
                time_update=self.server_time,
                time_msc=self.server_time * 1000,
                time_update_msc=self.server_time * 1000,
                magic=int(request.get("magic", 0)),
                reason=DealReason.EXPERT,
                comment=str(request.get("comment", "")),
                external_id="",
            )
            entry = ENTRY_IN
            self.balance += commission
        order.position_id = pos_id
        deal = SimpleNamespace(
            ticket=self._ticket(),
            order=order.ticket,
            time=self.server_time,
            time_msc=self.server_time * 1000,
            type=DEAL_BUY if is_buy else DEAL_SELL,
            entry=entry,
            magic=order.magic,
            position_id=pos_id,
            reason=int(reason),
            volume=qty,
            price=price,
            commission=commission,
            swap=0.0,
            profit=profit,
            fee=0.0,
            symbol=symbol,
            comment=order.comment,
            external_id="",
        )
        self.deals.append(deal)
        return deal

    def _net_position(self, symbol: str) -> int:
        for ticket, p in self.positions.items():
            if p.symbol == symbol:
                return ticket
        return 0

    def _floating_profit(self) -> float:
        total = 0.0
        for p in self.positions.values():
            bid, ask = self._bidask(p.symbol)
            mark = bid if p.type == 0 else ask
            p.price_current = mark
            own = (
                (1 if p.type == 0 else -1)
                * (mark - p.price_open)
                * p.volume
                * self._mult(p.symbol)
            )
            total += own
            p.profit = own
        return total

    def _evaluate_stops(self) -> None:
        """Broker-side SL/TP execution: closes the position at the stop level (with a deal)."""
        for ticket, p in list(self.positions.items()):
            long = p.type == 0
            bid, ask = self._bidask(p.symbol)
            mark = bid if long else ask
            hit_sl = p.sl and ((long and mark <= p.sl) or (not long and mark >= p.sl))
            hit_tp = p.tp and ((long and mark >= p.tp) or (not long and mark <= p.tp))
            if not (hit_sl or hit_tp):
                continue
            reason = DealReason.SL if hit_sl else DealReason.TP
            level = p.sl if hit_sl else p.tp
            order = SimpleNamespace(
                ticket=self._ticket(),
                time_setup=self.server_time,
                time_done=self.server_time,
                time_expiration=0,
                type=1 if long else 0,
                state=ORDER_STATE_FILLED,
                magic=p.magic,
                position_id=p.identifier,
                volume_initial=p.volume,
                volume_current=0.0,
                price_open=level,
                sl=0.0,
                tp=0.0,
                price_current=level,
                symbol=p.symbol,
                comment="[sl]" if hit_sl else "[tp]",
                external_id="",
                type_filling=int(Filling.IOC),
                reason=int(reason),
            )
            self.history_orders.append(order)
            self._book_fill(
                order,
                {"position": ticket, "magic": p.magic},
                not long,
                p.volume,
                level,
                reason=reason,
            )

    # -- broker-initiated activity (not caused by us) -----------------------------------------

    def external_market_fill(
        self,
        *,
        is_buy: bool,
        volume: float,
        price: float | None = None,
        comment: str = "",
        magic: int = 0,
        sl: float = 0.0,
        tp: float = 0.0,
    ) -> SimpleNamespace:
        """A deal we did not request (manual trade / lost-response execution)."""
        request = {
            "symbol": self.symbol_info_obj.name,
            "type": 0 if is_buy else 1,
            "volume": volume,
            "comment": comment,
            "magic": magic,
            "sl": sl,
            "tp": tp,
            "type_filling": int(Filling.IOC),
            "action": TradeAction.DEAL,
        }
        price = price if price is not None else (self.ask if is_buy else self.bid)
        self.fill_plan.insert(0, [(volume, price)])
        result = self._execute_deal(request)
        return next(d for d in self.deals if d.ticket == result.deal)
