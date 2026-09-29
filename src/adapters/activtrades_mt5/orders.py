"""Write-side (order-sending) interface for the MT5/ActivTrades adapter,
prepared but disabled by default.

`TradingMode` gates every method that could reach a real MT5 `order_check`/
`order_send` call. `MOCK` (the default) and `PAPER`/`SHADOW` never call the
injected `MT5ClientProtocol` at all for any write-side or preflight
operation -- they return a typed refusal result immediately. `DEMO`,
`LIVE_SMOKE`, and `LIVE` do call the real client, but `LIVE_SMOKE`/`LIVE`
additionally require an explicit `confirm=True` argument on every call that
could send real state to a broker; without it, the request is refused with
no client call made, even in those modes.

Default mode is `MOCK`, the most conservative option (never touches the
injected client for anything, not even a dry-run `order_check`) -- a caller
must opt into a less conservative mode explicitly; `MT5OrderGateway` never
silently defaults to something that could send a real order.

This module builds the interface shape and the refusal/guard logic only. It
does not implement a full live-trading pathway (translating a typed request
into a real MT5 request dict's exact field layout for every action) --
`preflight_check`/the send methods call `client.order_check`/`order_send`
with a minimal, documented request shape sufficient for `DEMO` testing
against a real terminal later; expanding that mapping to cover every
edge case (partial fills, stop-limit orders, etc.) is explicitly left as
follow-up work, not a blocker for this slice's guard-logic correctness.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from adapters.activtrades_mt5.client import MT5ClientProtocol
from adapters.activtrades_mt5.models import OrderCheckResult, order_check_result_from_mt5


class TradingMode(StrEnum):
    """How permissive the gateway is about actually reaching MT5.

    Ordered from most to least conservative in intent (not by enum value):
    `MOCK`, `PAPER`, `SHADOW` never call the injected client for any
    write-side or preflight operation. `DEMO` calls the real client against
    a demo account, no extra confirmation required. `LIVE_SMOKE`/`LIVE`
    call the real client and additionally require `confirm=True` threaded
    through every call.
    """

    MOCK = "MOCK"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    DEMO = "DEMO"
    LIVE_SMOKE = "LIVE_SMOKE"
    LIVE = "LIVE"


_NEVER_CALLS_CLIENT = frozenset({TradingMode.MOCK, TradingMode.PAPER, TradingMode.SHADOW})
_REQUIRES_CONFIRMATION = frozenset({TradingMode.LIVE_SMOKE, TradingMode.LIVE})

# Mirrors real `TRADE_ACTION_*`/`ORDER_TYPE_*` integer constants from the
# installed `metatrader5` package's `__init__.py` (plain literal ints, not
# introspection-derived -- see `models.py` module docstring for the general
# provenance note).
_TRADE_ACTION_DEAL = 1
_TRADE_ACTION_SLTP = 6
_TRADE_ACTION_REMOVE = 8
_ORDER_TYPE_BUY = 0
_ORDER_TYPE_SELL = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketOrderRequest:
    symbol: str
    side: str  # "BUY" | "SELL"
    volume: Decimal
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    magic: int = 0
    comment: str = ""

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")
        if self.volume <= 0:
            raise ValueError("volume must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class CloseRequest:
    ticket: int
    symbol: str
    volume: Decimal
    side: str  # side of the CLOSING order (opposite of the open position)

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")
        if self.volume <= 0:
            raise ValueError("volume must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartialCloseRequest:
    ticket: int
    symbol: str
    volume: Decimal
    side: str

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")
        if self.volume <= 0:
            raise ValueError("volume must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class StopUpdateRequest:
    ticket: int
    symbol: str
    new_stop_loss: Decimal

    def __post_init__(self) -> None:
        if self.new_stop_loss <= 0:
            raise ValueError("new_stop_loss must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class TakeProfitUpdateRequest:
    ticket: int
    symbol: str
    new_take_profit: Decimal

    def __post_init__(self) -> None:
        if self.new_take_profit <= 0:
            raise ValueError("new_take_profit must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelPendingRequest:
    ticket: int
    symbol: str


@dataclass(frozen=True, slots=True, kw_only=True)
class GatewayResult:
    """Uniform result of every `MT5OrderGateway` operation."""

    accepted: bool
    mode: TradingMode
    reason: str
    retcode: int | None = None
    raw_result: Any | None = None


def _refused(mode: TradingMode, reason: str) -> GatewayResult:
    return GatewayResult(accepted=False, mode=mode, reason=reason)


class MT5OrderGateway:
    """Typed write-side interface wrapping `MT5ClientProtocol`.

    Constructed with an explicit `mode` (default `TradingMode.MOCK`, the
    safest option) -- see module docstring for the guard rules this class
    enforces before ever reaching the injected client.
    """

    def __init__(self, client: MT5ClientProtocol, *, mode: TradingMode = TradingMode.MOCK) -> None:
        self._client = client
        self.mode = mode

    def _guard(self, *, confirm: bool) -> GatewayResult | None:
        """Returns a refusal `GatewayResult` if this call must not reach the
        client, or `None` if it may proceed."""
        if self.mode in _NEVER_CALLS_CLIENT:
            return _refused(
                self.mode,
                f"mode {self.mode} never calls the MT5 client for any write-side "
                "or preflight operation",
            )
        if self.mode in _REQUIRES_CONFIRMATION and not confirm:
            return _refused(
                self.mode,
                f"mode {self.mode} requires confirm=True to reach the MT5 client; "
                "no client call was made",
            )
        return None

    # -- preflight -----------------------------------------------------------

    def preflight_check(
        self, request: MarketOrderRequest, *, price: Decimal, confirm: bool = False
    ) -> GatewayResult:
        """Dry-run `order_check` -- never sends a real order even when it
        reaches the client (MT5's own `order_check` semantics), but is still
        gated by `TradingMode` like every other write-side call per the
        task's directive that MOCK/PAPER/SHADOW never call `order_check`
        either."""
        refusal = self._guard(confirm=confirm)
        if refusal is not None:
            return refusal

        raw_request = self._market_order_request_dict(request, price=price)
        try:
            raw_result = self._client.order_check(raw_request)
        except Exception as exc:
            return GatewayResult(
                accepted=False, mode=self.mode, reason=f"order_check() raised: {exc}"
            )
        if raw_result is None:
            return GatewayResult(
                accepted=False, mode=self.mode, reason="order_check() returned None"
            )

        result: OrderCheckResult = order_check_result_from_mt5(raw_result)
        return GatewayResult(
            accepted=result.success,
            mode=self.mode,
            reason=result.comment or ("ok" if result.success else "order_check rejected"),
            retcode=result.retcode,
            raw_result=result,
        )

    def _market_order_request_dict(
        self, request: MarketOrderRequest, *, price: Decimal
    ) -> dict[str, Any]:
        return {
            "action": _TRADE_ACTION_DEAL,
            "symbol": request.symbol,
            "volume": float(request.volume),
            "type": _ORDER_TYPE_BUY if request.side == "BUY" else _ORDER_TYPE_SELL,
            "price": float(price),
            "sl": float(request.stop_loss) if request.stop_loss is not None else 0.0,
            "tp": float(request.take_profit) if request.take_profit is not None else 0.0,
            "magic": request.magic,
            "comment": request.comment,
        }

    # -- write-side operations -------------------------------------------

    def submit_market_order(
        self, request: MarketOrderRequest, *, price: Decimal, confirm: bool = False
    ) -> GatewayResult:
        refusal = self._guard(confirm=confirm)
        if refusal is not None:
            return refusal
        raw_request = self._market_order_request_dict(request, price=price)
        return self._send(raw_request)

    def close_position(
        self, request: CloseRequest, *, price: Decimal, confirm: bool = False
    ) -> GatewayResult:
        refusal = self._guard(confirm=confirm)
        if refusal is not None:
            return refusal
        raw_request = {
            "action": _TRADE_ACTION_DEAL,
            "symbol": request.symbol,
            "volume": float(request.volume),
            "type": _ORDER_TYPE_BUY if request.side == "BUY" else _ORDER_TYPE_SELL,
            "position": request.ticket,
            "price": float(price),
        }
        return self._send(raw_request)

    def partial_close(
        self, request: PartialCloseRequest, *, price: Decimal, confirm: bool = False
    ) -> GatewayResult:
        refusal = self._guard(confirm=confirm)
        if refusal is not None:
            return refusal
        raw_request = {
            "action": _TRADE_ACTION_DEAL,
            "symbol": request.symbol,
            "volume": float(request.volume),
            "type": _ORDER_TYPE_BUY if request.side == "BUY" else _ORDER_TYPE_SELL,
            "position": request.ticket,
            "price": float(price),
        }
        return self._send(raw_request)

    def update_stop(self, request: StopUpdateRequest, *, confirm: bool = False) -> GatewayResult:
        refusal = self._guard(confirm=confirm)
        if refusal is not None:
            return refusal
        raw_request = {
            "action": _TRADE_ACTION_SLTP,
            "symbol": request.symbol,
            "position": request.ticket,
            "sl": float(request.new_stop_loss),
        }
        return self._send(raw_request)

    def update_take_profit(
        self, request: TakeProfitUpdateRequest, *, confirm: bool = False
    ) -> GatewayResult:
        refusal = self._guard(confirm=confirm)
        if refusal is not None:
            return refusal
        raw_request = {
            "action": _TRADE_ACTION_SLTP,
            "symbol": request.symbol,
            "position": request.ticket,
            "tp": float(request.new_take_profit),
        }
        return self._send(raw_request)

    def cancel_pending(
        self, request: CancelPendingRequest, *, confirm: bool = False
    ) -> GatewayResult:
        refusal = self._guard(confirm=confirm)
        if refusal is not None:
            return refusal
        raw_request = {
            "action": _TRADE_ACTION_REMOVE,
            "symbol": request.symbol,
            "order": request.ticket,
        }
        return self._send(raw_request)

    def _send(self, raw_request: dict[str, Any]) -> GatewayResult:
        try:
            raw_result = self._client.order_send(raw_request)
        except Exception as exc:
            return GatewayResult(
                accepted=False, mode=self.mode, reason=f"order_send() raised: {exc}"
            )
        if raw_result is None:
            return GatewayResult(
                accepted=False, mode=self.mode, reason="order_send() returned None"
            )

        retcode = int(raw_result.retcode)
        comment = str(getattr(raw_result, "comment", ""))
        accepted = retcode in (10009, 10010, 10008)  # DONE, DONE_PARTIAL, PLACED
        return GatewayResult(
            accepted=accepted,
            mode=self.mode,
            reason=comment or ("ok" if accepted else "order_send rejected"),
            retcode=retcode,
            raw_result=raw_result,
        )
