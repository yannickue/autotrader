"""Pure Nautilus -> MT5 request translation and validation (no I/O, no state).

Everything here fails closed with `RequestRejected` (a definite local "no", raised
BEFORE anything reaches the broker) instead of clamping or guessing:
volume outside min/max/step, unsupported filling mode, stops on the wrong side or
inside the broker's stop level, non-finite/non-positive prices.

MT5 specifics handled:
- `type_filling`: from the symbol's `filling_mode` bitmask (bit0 FOK, bit1 IOC).
  IOC is preferred (partial fills allowed, remainder cancelled); FOK next; RETURN is
  never used for market-execution symbols. No allowed mode => reject.
- `deviation`: max price slippage in POINTS (sent on market orders; a market-execution
  broker may ignore it, so it is NOT relied upon as protection).
- SL/TP: measured from the closing price (BUY position: bid; SELL: ask) and must be at
  least `trade_stops_level * point` away.
- `comment` carries the write-ahead token (<= 31 chars) used to re-associate a broker
  order/deal with its ClientOrderId after a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from instruments.models import InstrumentSpec
from nautilus_mt5.constants import Filling, OrderTime, OrderType, TradeAction

MAX_COMMENT = 31


class RequestRejected(ValueError):
    """Local, definite refusal: nothing was sent to the broker."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True, kw_only=True)
class Quote:
    bid: Decimal
    ask: Decimal


def normalize_volume(spec: InstrumentSpec, quantity: Decimal) -> float:
    if not quantity.is_finite() or quantity <= 0:
        raise RequestRejected("INVALID_VOLUME", f"quantity {quantity}")
    if quantity < spec.volume_min or quantity > spec.volume_max:
        raise RequestRejected(
            "INVALID_VOLUME", f"{quantity} outside [{spec.volume_min}, {spec.volume_max}]"
        )
    steps = (quantity - spec.volume_min) / spec.volume_step
    if steps != steps.to_integral_value():
        raise RequestRejected("INVALID_VOLUME", f"{quantity} not on volume step {spec.volume_step}")
    return float(quantity)


def round_price(spec: InstrumentSpec, price: Decimal) -> float:
    if not price.is_finite() or price <= 0:
        raise RequestRejected("INVALID_PRICE", f"price {price}")
    quantum = Decimal(1).scaleb(-spec.digits)
    return float(price.quantize(quantum, rounding=ROUND_HALF_EVEN))


def select_filling(spec: InstrumentSpec, symbol_filling_mask: int) -> Filling:
    """Pick from the broker-reported bitmask; never assume a mode the symbol lacks."""
    if symbol_filling_mask & 0b10:
        return Filling.IOC
    if symbol_filling_mask & 0b01:
        return Filling.FOK
    raise RequestRejected("UNSUPPORTED_FILLING", f"symbol filling mask {symbol_filling_mask}")


def validate_stops(
    spec: InstrumentSpec,
    *,
    position_is_long: bool,
    quote: Quote,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
) -> None:
    ref = quote.bid if position_is_long else quote.ask  # the price that would close it
    min_distance = spec.stop_level or Decimal(0)
    if stop_loss is not None:
        wrong = stop_loss >= ref if position_is_long else stop_loss <= ref
        if wrong or abs(ref - stop_loss) < min_distance:
            raise RequestRejected(
                "INVALID_STOPS", f"SL {stop_loss} vs {ref} (min distance {min_distance})"
            )
    if take_profit is not None:
        wrong = take_profit <= ref if position_is_long else take_profit >= ref
        if wrong or abs(ref - take_profit) < min_distance:
            raise RequestRejected(
                "INVALID_STOPS", f"TP {take_profit} vs {ref} (min distance {min_distance})"
            )


def is_tightening(*, position_is_long: bool, current_sl: Decimal | None, new_sl: Decimal) -> bool:
    """Adding a stop, or moving it toward the market, only reduces risk."""
    if current_sl is None:
        return True
    return new_sl >= current_sl if position_is_long else new_sl <= current_sl


def market_entry_request(
    spec: InstrumentSpec,
    *,
    symbol_filling_mask: int,
    is_buy: bool,
    quantity: Decimal,
    quote: Quote,
    magic: int,
    token: str,
    deviation_points: int,
    stop_loss: Decimal | None = None,
    take_profit: Decimal | None = None,
) -> dict[str, Any]:
    """A NEW-exposure market order; SL/TP (if any) are attached in the SAME request so the
    protection is broker-side from the first instant the position exists."""
    if len(token) > MAX_COMMENT:
        raise RequestRejected("INVALID_COMMENT", token)
    volume = normalize_volume(spec, quantity)
    validate_stops(
        spec,
        position_is_long=is_buy,
        quote=quote,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    return {
        "action": int(TradeAction.DEAL),
        "symbol": spec.broker_symbol,
        "volume": volume,
        "type": int(OrderType.BUY if is_buy else OrderType.SELL),
        "price": round_price(spec, quote.ask if is_buy else quote.bid),
        "sl": round_price(spec, stop_loss) if stop_loss is not None else 0.0,
        "tp": round_price(spec, take_profit) if take_profit is not None else 0.0,
        "deviation": int(deviation_points),
        "magic": int(magic),
        "comment": token,
        "type_time": int(OrderTime.GTC),
        "type_filling": int(select_filling(spec, symbol_filling_mask)),
    }


def close_request(
    spec: InstrumentSpec,
    *,
    symbol_filling_mask: int,
    position_ticket: int,
    position_is_long: bool,
    quantity: Decimal,
    quote: Quote,
    magic: int,
    token: str,
    deviation_points: int,
) -> dict[str, Any]:
    """Reduce/close: an opposite DEAL bound to the POSITION ticket (never opens a new one)."""
    volume = normalize_volume(spec, quantity)
    closing_is_buy = not position_is_long
    return {
        "action": int(TradeAction.DEAL),
        "symbol": spec.broker_symbol,
        "volume": volume,
        "type": int(OrderType.BUY if closing_is_buy else OrderType.SELL),
        "position": int(position_ticket),
        "price": round_price(spec, quote.ask if closing_is_buy else quote.bid),
        "deviation": int(deviation_points),
        "magic": int(magic),
        "comment": token,
        "type_time": int(OrderTime.GTC),
        "type_filling": int(select_filling(spec, symbol_filling_mask)),
    }


def sltp_request(
    spec: InstrumentSpec,
    *,
    position_ticket: int,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
    keep_sl: Decimal | None = None,
    keep_tp: Decimal | None = None,
    magic: int = 0,
) -> dict[str, Any]:
    """SLTP action. MT5 replaces BOTH values, so an unchanged side must be re-sent (`keep_*`);
    0.0 removes a level."""
    sl = stop_loss if stop_loss is not None else keep_sl
    tp = take_profit if take_profit is not None else keep_tp
    return {
        "action": int(TradeAction.SLTP),
        "symbol": spec.broker_symbol,
        "position": int(position_ticket),
        "sl": round_price(spec, sl) if sl is not None else 0.0,
        "tp": round_price(spec, tp) if tp is not None else 0.0,
        "magic": int(magic),
    }
