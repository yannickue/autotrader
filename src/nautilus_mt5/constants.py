"""MT5 numeric constants, verified against the installed metatrader5==5.0.6231
package (`dir(MetaTrader5)`), duplicated here so this package never imports
MetaTrader5 (only `adapters.activtrades_mt5.real_client` may)."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class TradeAction(IntEnum):
    DEAL = 1
    PENDING = 5
    SLTP = 6
    MODIFY = 7
    REMOVE = 8
    CLOSE_BY = 10


class OrderType(IntEnum):
    BUY = 0
    SELL = 1
    BUY_LIMIT = 2
    SELL_LIMIT = 3
    BUY_STOP = 4
    SELL_STOP = 5


class Filling(IntEnum):
    FOK = 0
    IOC = 1
    RETURN = 2
    BOC = 3


class OrderTime(IntEnum):
    GTC = 0
    DAY = 1


class MarginMode(IntEnum):
    RETAIL_NETTING = 0
    EXCHANGE = 1
    RETAIL_HEDGING = 2


class ExecMode(IntEnum):
    REQUEST = 0
    INSTANT = 1
    MARKET = 2
    EXCHANGE = 3


class DealReason(IntEnum):
    CLIENT = 0
    MOBILE = 1
    WEB = 2
    EXPERT = 3
    SL = 4
    TP = 5
    SO = 6
    ROLLOVER = 7
    VMARGIN = 8
    SPLIT = 9


# order_check(): success is retcode == 0 (comment "Done"). order_send(): success is
# DONE / DONE_PARTIAL (market) or PLACED (pending). Different namespaces: never mix.
ORDER_CHECK_OK = 0


class Retcode(IntEnum):
    REQUOTE = 10004
    REJECT = 10006
    CANCEL = 10007
    PLACED = 10008
    DONE = 10009
    DONE_PARTIAL = 10010
    ERROR = 10011
    TIMEOUT = 10012
    INVALID = 10013
    INVALID_VOLUME = 10014
    INVALID_PRICE = 10015
    INVALID_STOPS = 10016
    TRADE_DISABLED = 10017
    MARKET_CLOSED = 10018
    NO_MONEY = 10019
    PRICE_CHANGED = 10020
    PRICE_OFF = 10021
    INVALID_EXPIRATION = 10022
    ORDER_CHANGED = 10023
    TOO_MANY_REQUESTS = 10024
    NO_CHANGES = 10025
    SERVER_DISABLES_AT = 10026
    CLIENT_DISABLES_AT = 10027
    LOCKED = 10028
    FROZEN = 10029
    INVALID_FILL = 10030
    CONNECTION = 10031
    ONLY_REAL = 10032
    LIMIT_ORDERS = 10033
    LIMIT_VOLUME = 10034
    INVALID_ORDER = 10035
    POSITION_CLOSED = 10036
    INVALID_CLOSE_VOLUME = 10038
    CLOSE_ORDER_EXIST = 10039
    LIMIT_POSITIONS = 10040
    REJECT_CANCEL = 10041
    LONG_ONLY = 10042
    SHORT_ONLY = 10043
    CLOSE_ONLY = 10044
    FIFO_CLOSE = 10045


class SendOutcome(StrEnum):
    """What an order_send() result means for order state."""

    ACCEPTED_FILLED = "ACCEPTED_FILLED"  # DONE
    ACCEPTED_PARTIAL = "ACCEPTED_PARTIAL"  # DONE_PARTIAL
    ACCEPTED_PLACED = "ACCEPTED_PLACED"  # PLACED (pending order / SLTP applied)
    REJECTED = "REJECTED"  # broker definitively refused; no exposure created
    IN_DOUBT = "IN_DOUBT"  # outcome unknown: may have executed -> must reconcile


_ACCEPT = {
    Retcode.DONE: SendOutcome.ACCEPTED_FILLED,
    Retcode.DONE_PARTIAL: SendOutcome.ACCEPTED_PARTIAL,
    Retcode.PLACED: SendOutcome.ACCEPTED_PLACED,
}
# Codes that do NOT prove the request failed: the server may have processed it.
_IN_DOUBT = {Retcode.TIMEOUT, Retcode.CONNECTION, Retcode.ERROR, Retcode.ORDER_CHANGED}


def classify_send_retcode(retcode: int) -> SendOutcome:
    try:
        code = Retcode(retcode)
    except ValueError:
        return SendOutcome.IN_DOUBT  # unknown code: never assume "definitely rejected"
    if code in _ACCEPT:
        return _ACCEPT[code]
    if code in _IN_DOUBT:
        return SendOutcome.IN_DOUBT
    return SendOutcome.REJECTED
