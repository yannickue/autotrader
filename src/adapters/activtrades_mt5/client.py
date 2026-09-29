"""Protocol describing the subset of the raw `MetaTrader5` module surface
this adapter depends on.

Directive-mandated: "Core trading modules must NOT import MetaTrader5
directly. Use protocols/interfaces." Nothing in this file imports
`MetaTrader5` -- `MT5ClientProtocol` is a structural (`typing.Protocol`)
description of the free functions the real `MetaTrader5` module exposes,
verified by inspecting the installed `metatrader5==5.0.6231` package (its
`__init__.py` re-exports every `SYMBOL_*`/`ORDER_*`/`ACCOUNT_*`/
`TRADE_RETCODE_*` constant as plain module attributes, and each function's
`__doc__` was read directly off the compiled `_core` extension without a
live terminal connection -- see `symbol_info_to_instrument_spec` in
`models.py` for how the real `SymbolInfo` field names were verified the same
way, by extracting the literal strings embedded in `_core.cp312-win_amd64.pyd`).

Return types are intentionally `Any` (or tuples of `Any`): the real
`MetaTrader5` functions return C-extension namedtuple-like objects
(`SymbolInfo`, `AccountInfo`, `Tick`, `TerminalInfo`, `TradePosition`,
`TradeOrder`, `TradeDeal`, `OrderCheckResult`, `OrderSendResult`) that this
package never imports a type for -- `models.py` converts them into this
repo's own frozen/Decimal dataclasses via `getattr`-based, defensive
extraction, so a caller passing anything duck-typed with the right
attributes (including `testing.FakeMT5Client`'s `types.SimpleNamespace`
fixtures) works identically to the real package.
"""

from datetime import datetime
from typing import Any, Protocol


class MT5ClientProtocol(Protocol):
    """Structural interface for the raw MT5 client surface this adapter uses.

    Every method signature matches the real `MetaTrader5` module function of
    the same name (verified via `inspect`/`__doc__` on the installed
    package, see module docstring). A real `MetaTrader5` module instance
    satisfies this protocol without any wrapping; `testing.FakeMT5Client`
    satisfies it without ever importing `MetaTrader5`.
    """

    def initialize(
        self,
        path: str | None = None,
        *,
        login: int | None = None,
        password: str | None = None,
        server: str | None = None,
        timeout: int | None = None,
        portable: bool = False,
    ) -> bool: ...

    def shutdown(self) -> None: ...

    def terminal_info(self) -> Any | None: ...

    def version(self) -> tuple[Any, ...] | None: ...

    def account_info(self) -> Any | None: ...

    def symbols_get(self, group: str | None = None) -> tuple[Any, ...] | None: ...

    def symbol_select(self, symbol: str, enable: bool = True) -> bool: ...

    def symbol_info(self, symbol: str) -> Any | None: ...

    def symbol_info_tick(self, symbol: str) -> Any | None: ...

    def positions_get(
        self,
        symbol: str | None = None,
        *,
        group: str | None = None,
        ticket: int | None = None,
    ) -> tuple[Any, ...] | None: ...

    def orders_get(
        self,
        symbol: str | None = None,
        *,
        group: str | None = None,
        ticket: int | None = None,
    ) -> tuple[Any, ...] | None: ...

    def history_orders_get(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        *,
        group: str | None = None,
        position: int | None = None,
        ticket: int | None = None,
    ) -> tuple[Any, ...] | None: ...

    def history_deals_get(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        *,
        group: str | None = None,
        position: int | None = None,
        ticket: int | None = None,
    ) -> tuple[Any, ...] | None: ...

    def copy_rates_from(
        self, symbol: str, timeframe: int, date_from: datetime, count: int
    ) -> Any | None: ...

    def copy_rates_range(
        self, symbol: str, timeframe: int, date_from: datetime, date_to: datetime
    ) -> Any | None: ...

    def copy_ticks_from(
        self, symbol: str, date_from: datetime, count: int, flags: int
    ) -> Any | None: ...

    def copy_ticks_range(
        self, symbol: str, date_from: datetime, date_to: datetime, flags: int
    ) -> Any | None: ...

    def order_calc_margin(
        self, action: int, symbol: str, volume: float, price: float
    ) -> float | None: ...

    def order_calc_profit(
        self,
        action: int,
        symbol: str,
        volume: float,
        price_open: float,
        price_close: float,
    ) -> float | None: ...

    def order_check(self, request: dict[str, Any]) -> Any | None: ...

    def order_send(self, request: dict[str, Any]) -> Any | None: ...

    def last_error(self) -> tuple[int, str]: ...
