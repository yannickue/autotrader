"""Fake MT5 client for testing, implementing `MT5ClientProtocol`.

Deliberately has NO import of `MetaTrader5` anywhere -- this is what makes
every test in this adapter slice runnable in an environment with no MT5
terminal or account at all (this one). Every method returns configurable
canned responses set up via the `set_*` methods below; nothing here talks to
any real process or network.
"""

from datetime import datetime
from typing import Any


class FakeMT5Client:
    """Configurable fake implementing `MT5ClientProtocol`.

    Raw return objects are plain `types.SimpleNamespace`/`dict`-friendly
    duck-typed objects built by the test itself (or via the small
    `make_symbol_info`/`make_tick`/... helpers below) -- `models.py`'s
    conversion functions only ever use `getattr`, so any object with the
    right attribute names works, exactly like a real MT5 namedtuple would.
    """

    def __init__(self) -> None:
        self._initialize_result: bool = True
        self._last_error: tuple[int, str] = (1, "Success")
        self._terminal_info: Any | None = None
        self._version: tuple[Any, ...] | None = None
        self._account_info: Any | None = None
        self._symbols: tuple[Any, ...] = ()
        self._symbol_select_results: dict[str, bool] = {}
        self._symbol_select_default: bool = True
        self._symbol_info: dict[str, Any] = {}
        self._ticks: dict[str, Any] = {}
        self._positions: tuple[Any, ...] = ()
        self._orders: tuple[Any, ...] = ()
        self._history_orders: tuple[Any, ...] = ()
        self._history_deals: tuple[Any, ...] = ()
        self._rates: Any | None = None
        self._ticks_history: Any | None = None
        self._margin_result: float | None = None
        self._profit_result: float | None = None
        self._order_check_result: Any | None = None
        self._order_send_result: Any | None = None
        self.shutdown_calls: int = 0
        self.initialize_calls: list[dict[str, Any]] = []
        self.order_check_calls: list[dict[str, Any]] = []
        self.order_send_calls: list[dict[str, Any]] = []

    # -- configuration -----------------------------------------------------

    def set_initialize_result(
        self, success: bool, *, error: tuple[int, str] | None = None
    ) -> None:
        self._initialize_result = success
        if error is not None:
            self._last_error = error

    def set_last_error(self, code: int, description: str) -> None:
        self._last_error = (code, description)

    def set_terminal_info(self, terminal_info: Any | None) -> None:
        self._terminal_info = terminal_info

    def set_version(self, version: tuple[Any, ...] | None) -> None:
        self._version = version

    def set_account_info(self, account_info: Any | None) -> None:
        self._account_info = account_info

    def set_symbols(self, symbols: tuple[Any, ...]) -> None:
        self._symbols = symbols

    def set_symbol_select_result(self, symbol: str, result: bool) -> None:
        self._symbol_select_results[symbol] = result

    def set_symbol_select_default(self, result: bool) -> None:
        self._symbol_select_default = result

    def set_symbol_info(self, symbol: str, info: Any | None) -> None:
        if info is None:
            self._symbol_info.pop(symbol, None)
        else:
            self._symbol_info[symbol] = info

    def set_tick(self, symbol: str, tick: Any | None) -> None:
        if tick is None:
            self._ticks.pop(symbol, None)
        else:
            self._ticks[symbol] = tick

    def set_positions(self, positions: tuple[Any, ...]) -> None:
        self._positions = positions

    def set_orders(self, orders: tuple[Any, ...]) -> None:
        self._orders = orders

    def set_history_orders(self, orders: tuple[Any, ...]) -> None:
        self._history_orders = orders

    def set_history_deals(self, deals: tuple[Any, ...]) -> None:
        self._history_deals = deals

    def set_rates(self, rates: Any | None) -> None:
        self._rates = rates

    def set_ticks_history(self, ticks: Any | None) -> None:
        self._ticks_history = ticks

    def set_margin_result(self, margin: float | None) -> None:
        self._margin_result = margin

    def set_profit_result(self, profit: float | None) -> None:
        self._profit_result = profit

    def set_order_check_result(self, result: Any | None) -> None:
        self._order_check_result = result

    def set_order_send_result(self, result: Any | None) -> None:
        self._order_send_result = result

    # -- MT5ClientProtocol ---------------------------------------------------

    def initialize(
        self,
        path: str | None = None,
        *,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        timeout: int | None = None,
        portable: bool = False,
    ) -> bool:
        self.initialize_calls.append(
            {"path": path, "login": login, "server": server, "timeout": timeout}
        )
        return self._initialize_result

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def terminal_info(self) -> Any | None:
        return self._terminal_info

    def version(self) -> tuple[Any, ...] | None:
        return self._version

    def account_info(self) -> Any | None:
        return self._account_info

    def symbols_get(self, group: str | None = None) -> tuple[Any, ...] | None:
        del group
        return self._symbols

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        del enable
        return self._symbol_select_results.get(symbol, self._symbol_select_default)

    def symbol_info(self, symbol: str) -> Any | None:
        return self._symbol_info.get(symbol)

    def symbol_info_tick(self, symbol: str) -> Any | None:
        return self._ticks.get(symbol)

    def positions_get(
        self,
        symbol: str | None = None,
        *,
        group: str | None = None,
        ticket: int | None = None,
    ) -> tuple[Any, ...] | None:
        del group, ticket
        if symbol is None:
            return self._positions
        return tuple(p for p in self._positions if p.symbol == symbol)

    def orders_get(
        self,
        symbol: str | None = None,
        *,
        group: str | None = None,
        ticket: int | None = None,
    ) -> tuple[Any, ...] | None:
        del group, ticket
        if symbol is None:
            return self._orders
        return tuple(o for o in self._orders if o.symbol == symbol)

    def history_orders_get(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        *,
        group: str | None = None,
        position: int | None = None,
        ticket: int | None = None,
    ) -> tuple[Any, ...] | None:
        del date_from, date_to, group, position, ticket
        return self._history_orders

    def history_deals_get(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        *,
        group: str | None = None,
        position: int | None = None,
        ticket: int | None = None,
    ) -> tuple[Any, ...] | None:
        del date_from, date_to, group, position, ticket
        return self._history_deals

    def copy_rates_from(
        self, symbol: str, timeframe: int, date_from: datetime, count: int
    ) -> Any | None:
        del symbol, timeframe, date_from, count
        return self._rates

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int
    ) -> Any | None:
        del symbol, timeframe, start_pos, count
        return self._rates

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> Any | None:
        del symbol, timeframe, date_from, date_to
        return self._rates

    def copy_ticks_from(
        self, symbol: str, date_from: datetime, count: int, flags: int
    ) -> Any | None:
        del symbol, date_from, count, flags
        return self._ticks_history

    def copy_ticks_range(
        self, symbol: str, date_from: datetime, date_to: datetime, flags: int
    ) -> Any | None:
        del symbol, date_from, date_to, flags
        return self._ticks_history

    def order_calc_margin(
        self, action: int, symbol: str, volume: float, price: float
    ) -> float | None:
        del action, symbol, volume, price
        return self._margin_result

    def order_calc_profit(
        self,
        action: int,
        symbol: str,
        volume: float,
        price_open: float,
        price_close: float,
    ) -> float | None:
        del action, symbol, volume, price_open, price_close
        return self._profit_result

    def order_check(self, request: dict[str, Any]) -> Any | None:
        self.order_check_calls.append(request)
        return self._order_check_result

    def order_send(self, request: dict[str, Any]) -> Any | None:
        self.order_send_calls.append(request)
        return self._order_send_result

    def last_error(self) -> tuple[int, str]:
        return self._last_error
