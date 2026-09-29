"""Typed, Decimal-precise conversions of raw MT5 return shapes.

Raw `MetaTrader5` objects/namedtuples must NEVER leak into the rest of the
system -- every function in this module takes a duck-typed raw object
(anything with the right attributes: a real `MetaTrader5` namedtuple, or a
`types.SimpleNamespace` fixture from `testing.FakeMT5Client`) and returns one
of this module's own frozen, kw_only, Decimal-based dataclasses.

Field-name provenance (important -- read before changing any mapping below):
the real `MetaTrader5==5.0.6231` package's `_core.cp312-win_amd64.pyd` is a
compiled C extension with no live-terminal requirement to introspect its
*static* surface. Its free functions' signatures were read via `__doc__` on
the installed package (see `client.py` module docstring). The raw return
objects' field names (`SymbolInfo`, `AccountInfo`, `TerminalInfo`, `Tick`,
`TradePosition`, `TradeOrder`, `TradeDeal`, `OrderCheckResult`,
`OrderSendResult`) cannot be listed via `inspect` without a live connection
(they are only constructed by the extension after `initialize()` succeeds),
so they were instead verified by extracting every embedded ASCII string from
the compiled `_core.cp312-win_amd64.pyd` binary itself and cross-referencing
that list against the well-documented MQL5 Python API field set -- every
field name used below (`trade_tick_value_profit`, `margin_so_call`,
`community_connection`, `time_update_msc`, etc.) is a literal string found
inside the actual installed binary, not a guess. Two known exceptions,
called out explicitly at their use site: `sl`/`tp` (order/position stop-loss
/take-profit price fields) are too short (2 characters) for the string
extraction to distinguish from binary noise and were not independently
verified this way -- they are used below on the strength of being extremely
widely and consistently documented MQL5 field names, not verified against
this package's own binary. The exact per-symbol bit layout of the raw
`filling_mode` bitmask (which `ORDER_FILLING_*` values a symbol allows) is
similarly NOT independently confirmed from this package (no `SYMBOL_FILLING_*`
constants are exported by it at all) -- see `_decode_filling_modes` for the
conservative, partial decoding this module performs instead of guessing.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from instruments.models import (
    ExecutionMode,
    FillingMode,
    InstrumentSpec,
    MarginCalculationMode,
    TradeMode,
)

# -- raw MT5 integer constant tables (verified against the installed
# `metatrader5` package's plain Python `__init__.py`, which defines these as
# literal module-level integers -- not compiled/opaque, so these values are
# ground truth, not introspection-derived guesses). Kept private: only the
# conversion functions in this module use them, callers only ever see the
# StrEnum results.

_CALC_MODE_MAP: dict[int, MarginCalculationMode] = {
    0: MarginCalculationMode.FOREX,
    1: MarginCalculationMode.FUTURES,
    2: MarginCalculationMode.CFD,
    3: MarginCalculationMode.CFDINDEX,
    4: MarginCalculationMode.CFDLEVERAGE,
    5: MarginCalculationMode.FOREX_NO_LEVERAGE,
    # 32+ are exchange-traded stock/bond/option modes -- explicitly out of
    # scope per `instruments.models.MarginCalculationMode`'s own docstring.
}

_TRADE_MODE_MAP: dict[int, TradeMode] = {
    0: TradeMode.DISABLED,
    1: TradeMode.LONGONLY,
    2: TradeMode.SHORTONLY,
    3: TradeMode.CLOSEONLY,
    4: TradeMode.FULL,
}

_EXECUTION_MODE_MAP: dict[int, ExecutionMode] = {
    0: ExecutionMode.REQUEST,
    1: ExecutionMode.INSTANT,
    2: ExecutionMode.MARKET,
    3: ExecutionMode.EXCHANGE,
}

_ACCOUNT_TRADE_MODE_DEMO = 0
_ACCOUNT_TRADE_MODE_CONTEST = 1
_ACCOUNT_TRADE_MODE_REAL = 2


def _to_decimal(value: Any) -> Decimal:
    """Float -> Decimal via `str()`, never `Decimal(float)` directly (binary
    float imprecision must never silently enter a Decimal money/price
    field)."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _to_decimal(value)


def _to_utc(epoch_seconds: Any) -> datetime:
    return datetime.fromtimestamp(int(epoch_seconds), tz=UTC)


def _to_utc_or_none(epoch_seconds: Any) -> datetime | None:
    if epoch_seconds is None or epoch_seconds == 0:
        return None
    return _to_utc(epoch_seconds)


# -- Part 1: connection/terminal/account/tick models ------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountState:
    """Typed conversion of raw `AccountInfo`."""

    login: int
    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    margin_level: Decimal
    currency: str
    leverage: int
    trade_allowed: bool
    is_demo: bool


def account_info_to_account_state(raw: Any) -> AccountState:
    """Convert a raw MT5 `AccountInfo`-like object into `AccountState`.

    `is_demo` is derived strictly from the real `trade_mode` field
    (`ACCOUNT_TRADE_MODE_DEMO == 0`): `CONTEST` (1) and `REAL` (2) both map
    to `is_demo=False` -- a contest account is not a real-money account, but
    it is also not the paper/demo mode this repo's fail-closed live-trading
    guards care about, so it is never fabricated as "safe like demo".
    """
    return AccountState(
        login=int(raw.login),
        balance=_to_decimal(raw.balance),
        equity=_to_decimal(raw.equity),
        margin=_to_decimal(raw.margin),
        free_margin=_to_decimal(raw.margin_free),
        margin_level=_to_decimal(raw.margin_level),
        currency=str(raw.currency),
        leverage=int(raw.leverage),
        trade_allowed=bool(raw.trade_allowed),
        is_demo=int(raw.trade_mode) == _ACCOUNT_TRADE_MODE_DEMO,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class TerminalState:
    """Typed conversion of raw `TerminalInfo`."""

    connected: bool
    trade_allowed: bool
    community_connection: bool
    company: str
    name: str
    build: int


def terminal_info_to_terminal_state(raw: Any) -> TerminalState:
    return TerminalState(
        connected=bool(raw.connected),
        trade_allowed=bool(raw.trade_allowed),
        community_connection=bool(raw.community_connection),
        company=str(raw.company),
        name=str(raw.name),
        build=int(raw.build),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MT5VersionInfo:
    """Typed conversion of the raw `version()` 3-tuple
    `(terminal_version, build, release_date)`."""

    terminal_version: int
    build: int
    release_date: str


def version_tuple_to_version_info(raw: tuple[Any, ...]) -> MT5VersionInfo:
    terminal_version, build, release_date = raw
    return MT5VersionInfo(
        terminal_version=int(terminal_version), build=int(build), release_date=str(release_date)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolTick:
    """Typed conversion of raw `Tick` (from `symbol_info_tick`)."""

    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    volume: Decimal
    time: datetime


def tick_to_symbol_tick(raw: Any, *, symbol: str) -> SymbolTick:
    return SymbolTick(
        symbol=symbol,
        bid=_to_decimal(raw.bid),
        ask=_to_decimal(raw.ask),
        last=_to_decimal(raw.last),
        volume=_to_decimal(raw.volume),
        time=_to_utc(raw.time),
    )


# -- Part 1 continued: SymbolInfo -> InstrumentSpec --------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolInfoRaw:
    """Typed, Decimal-precise mirror of the fields this adapter reads off
    real MT5 `SymbolInfo`, extracted from the raw object via
    `symbol_info_raw_from_mt5` before any enum/InstrumentSpec conversion
    happens. Exists so `symbol_info_to_instrument_spec` never touches a
    duck-typed raw object directly -- the float->Decimal boundary crossing
    happens exactly once, in `symbol_info_raw_from_mt5`.
    """

    name: str
    description: str
    digits: int
    point: Decimal
    trade_tick_size: Decimal
    trade_tick_value: Decimal
    trade_tick_value_profit: Decimal
    trade_tick_value_loss: Decimal
    trade_contract_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    volume_limit: Decimal
    currency_base: str
    currency_profit: str
    currency_margin: str
    margin_initial: Decimal
    margin_maintenance: Decimal
    margin_hedged: Decimal
    trade_calc_mode: int
    trade_mode: int
    trade_exemode: int
    filling_mode: int
    trade_stops_level: int
    trade_freeze_level: int
    select: bool
    visible: bool


def symbol_info_raw_from_mt5(raw: Any) -> SymbolInfoRaw:
    """Extract `SymbolInfoRaw` from a duck-typed raw MT5 `SymbolInfo`-like
    object (real MT5, or a `types.SimpleNamespace` test fixture)."""
    return SymbolInfoRaw(
        name=str(raw.name),
        description=str(raw.description),
        digits=int(raw.digits),
        point=_to_decimal(raw.point),
        trade_tick_size=_to_decimal(raw.trade_tick_size),
        trade_tick_value=_to_decimal(raw.trade_tick_value),
        trade_tick_value_profit=_to_decimal(raw.trade_tick_value_profit),
        trade_tick_value_loss=_to_decimal(raw.trade_tick_value_loss),
        trade_contract_size=_to_decimal(raw.trade_contract_size),
        volume_min=_to_decimal(raw.volume_min),
        volume_max=_to_decimal(raw.volume_max),
        volume_step=_to_decimal(raw.volume_step),
        volume_limit=_to_decimal(raw.volume_limit),
        currency_base=str(raw.currency_base),
        currency_profit=str(raw.currency_profit),
        currency_margin=str(raw.currency_margin),
        margin_initial=_to_decimal(raw.margin_initial),
        margin_maintenance=_to_decimal(raw.margin_maintenance),
        margin_hedged=_to_decimal(raw.margin_hedged),
        trade_calc_mode=int(raw.trade_calc_mode),
        trade_mode=int(raw.trade_mode),
        trade_exemode=int(raw.trade_exemode),
        filling_mode=int(raw.filling_mode),
        trade_stops_level=int(raw.trade_stops_level),
        trade_freeze_level=int(raw.trade_freeze_level),
        select=bool(raw.select),
        visible=bool(raw.visible),
    )


def _decode_filling_modes(raw_bitmask: int) -> tuple[FillingMode, ...]:
    """Decode the real MT5 per-symbol `filling_mode` bitmask into the
    `FillingMode`s it allows.

    Judgement call / open question (flagged for lead review, not verified
    against this package's own introspectable surface -- see module
    docstring): only bit 0 (`SYMBOL_FILLING_FOK`, value 1) and bit 1
    (`SYMBOL_FILLING_IOC`, value 2) are decoded, since those two bit
    positions are the only ones consistently documented across MQL5's
    `ENUM_SYMBOL_TRADE_EXECUTION`/filling-mode references and this installed
    `metatrader5` package exports no `SYMBOL_FILLING_*` constants at all to
    verify a `BOC` or `RETURN` bit position against. Never fabricate a
    `RETURN`/`BOC` entry from an unverified bit -- an empty result (no bit
    set) or a bitmask with higher bits set beyond 0/1 intentionally yields
    only whatever of FOK/IOC is actually set, dropping the unverifiable
    remainder rather than guessing.
    """
    modes: list[FillingMode] = []
    if raw_bitmask & 0b01:
        modes.append(FillingMode.FOK)
    if raw_bitmask & 0b10:
        modes.append(FillingMode.IOC)
    return tuple(modes)


def symbol_info_to_instrument_spec(
    raw: SymbolInfoRaw, *, canonical_symbol: str, retrieved_at: datetime
) -> InstrumentSpec:
    """Convert `SymbolInfoRaw` into this repo's canonical `InstrumentSpec`
    (`src/instruments/models.py`, owned elsewhere -- imported, never
    redefined here).

    `stop_level`/`freeze_level`: the real MT5 `trade_stops_level`/
    `trade_freeze_level` fields are integer POINT counts, not price units
    -- converted to price-unit Decimals here via `* raw.point`, matching how
    every other price-shaped `InstrumentSpec` field is expressed.

    `margin_calculation_mode`/`trade_mode`/`execution_mode`: left `None`
    (never fabricated) whenever the raw integer does not match a mapped
    enum value; the unmapped raw integer is preserved in `quality_flags` so
    a caller can see exactly what was dropped and why, per
    `InstrumentSpec`'s own "unknown-fields rule".

    `is_tradable`: `True` only when the real `trade_mode` maps to a known,
    non-`DISABLED` `TradeMode`. An unmapped/unknown raw `trade_mode` value
    fails closed to `is_tradable=False` (never assume tradable when the
    mode could not even be identified) and is recorded in `quality_flags`.
    """
    quality_flags: list[str] = []

    calc_mode = _CALC_MODE_MAP.get(raw.trade_calc_mode)
    if calc_mode is None:
        quality_flags.append(f"UNMAPPED_CALC_MODE:{raw.trade_calc_mode}")

    trade_mode = _TRADE_MODE_MAP.get(raw.trade_mode)
    if trade_mode is None:
        quality_flags.append(f"UNMAPPED_TRADE_MODE:{raw.trade_mode}")

    execution_mode = _EXECUTION_MODE_MAP.get(raw.trade_exemode)
    if execution_mode is None:
        quality_flags.append(f"UNMAPPED_EXECUTION_MODE:{raw.trade_exemode}")

    filling_modes = _decode_filling_modes(raw.filling_mode)
    if not filling_modes and raw.filling_mode != 0:
        quality_flags.append(f"UNVERIFIED_FILLING_BITS:{raw.filling_mode}")

    is_tradable = trade_mode is not None and trade_mode is not TradeMode.DISABLED

    return InstrumentSpec(
        canonical_symbol=canonical_symbol,
        broker_symbol=raw.name,
        description=raw.description,
        digits=raw.digits,
        point=raw.point,
        trade_tick_size=raw.trade_tick_size,
        trade_tick_value=raw.trade_tick_value,
        trade_tick_value_profit=raw.trade_tick_value_profit,
        trade_tick_value_loss=raw.trade_tick_value_loss,
        trade_contract_size=raw.trade_contract_size,
        volume_min=raw.volume_min,
        volume_max=raw.volume_max,
        volume_step=raw.volume_step,
        volume_limit=raw.volume_limit if raw.volume_limit > 0 else None,
        currency_base=raw.currency_base,
        currency_profit=raw.currency_profit,
        currency_margin=raw.currency_margin,
        margin_initial=raw.margin_initial if raw.margin_initial > 0 else None,
        margin_maintenance=raw.margin_maintenance if raw.margin_maintenance > 0 else None,
        margin_hedged=raw.margin_hedged if raw.margin_hedged > 0 else None,
        margin_calculation_mode=calc_mode,
        trade_mode=trade_mode,
        execution_mode=execution_mode,
        filling_modes=filling_modes,
        stop_level=(
            Decimal(raw.trade_stops_level) * raw.point if raw.trade_stops_level > 0 else None
        ),
        freeze_level=(
            Decimal(raw.trade_freeze_level) * raw.point if raw.trade_freeze_level > 0 else None
        ),
        is_tradable=is_tradable,
        source="ACTIVTRADES_MT5_CFD",
        retrieved_at=retrieved_at,
        quality_flags=tuple(quality_flags),
    )


# -- Part 1 continued: positions / orders / deals ----------------------------


class MT5PositionSide(StrEnum):
    """Mirrors `POSITION_TYPE_*` (real names/values: BUY=0, SELL=1)."""

    BUY = "BUY"
    SELL = "SELL"


_POSITION_SIDE_MAP: dict[int, MT5PositionSide] = {0: MT5PositionSide.BUY, 1: MT5PositionSide.SELL}


class MT5OrderKind(StrEnum):
    """Mirrors `ORDER_TYPE_*` (real names/values 0-8)."""

    BUY = "BUY"
    SELL = "SELL"
    BUY_LIMIT = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"
    BUY_STOP_LIMIT = "BUY_STOP_LIMIT"
    SELL_STOP_LIMIT = "SELL_STOP_LIMIT"
    CLOSE_BY = "CLOSE_BY"


_ORDER_KIND_MAP: dict[int, MT5OrderKind] = {
    0: MT5OrderKind.BUY,
    1: MT5OrderKind.SELL,
    2: MT5OrderKind.BUY_LIMIT,
    3: MT5OrderKind.SELL_LIMIT,
    4: MT5OrderKind.BUY_STOP,
    5: MT5OrderKind.SELL_STOP,
    6: MT5OrderKind.BUY_STOP_LIMIT,
    7: MT5OrderKind.SELL_STOP_LIMIT,
    8: MT5OrderKind.CLOSE_BY,
}


class MT5OrderState(StrEnum):
    """Mirrors `ORDER_STATE_*` (real names/values 0-9)."""

    STARTED = "STARTED"
    PLACED = "PLACED"
    CANCELED = "CANCELED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REQUEST_ADD = "REQUEST_ADD"
    REQUEST_MODIFY = "REQUEST_MODIFY"
    REQUEST_CANCEL = "REQUEST_CANCEL"


_ORDER_STATE_MAP: dict[int, MT5OrderState] = {
    0: MT5OrderState.STARTED,
    1: MT5OrderState.PLACED,
    2: MT5OrderState.CANCELED,
    3: MT5OrderState.PARTIAL,
    4: MT5OrderState.FILLED,
    5: MT5OrderState.REJECTED,
    6: MT5OrderState.EXPIRED,
    7: MT5OrderState.REQUEST_ADD,
    8: MT5OrderState.REQUEST_MODIFY,
    9: MT5OrderState.REQUEST_CANCEL,
}


class MT5DealType(StrEnum):
    """Mirrors `DEAL_TYPE_*` (real names/values, see `MetaTrader5/__init__.py`)."""

    BUY = "BUY"
    SELL = "SELL"
    BALANCE = "BALANCE"
    CREDIT = "CREDIT"
    CHARGE = "CHARGE"
    CORRECTION = "CORRECTION"
    BONUS = "BONUS"
    COMMISSION = "COMMISSION"
    COMMISSION_DAILY = "COMMISSION_DAILY"
    COMMISSION_MONTHLY = "COMMISSION_MONTHLY"
    COMMISSION_AGENT_DAILY = "COMMISSION_AGENT_DAILY"
    COMMISSION_AGENT_MONTHLY = "COMMISSION_AGENT_MONTHLY"
    INTEREST = "INTEREST"
    BUY_CANCELED = "BUY_CANCELED"
    SELL_CANCELED = "SELL_CANCELED"
    DIVIDEND = "DIVIDEND"
    DIVIDEND_FRANKED = "DIVIDEND_FRANKED"
    TAX = "TAX"


_DEAL_TYPE_MAP: dict[int, MT5DealType] = {
    0: MT5DealType.BUY,
    1: MT5DealType.SELL,
    2: MT5DealType.BALANCE,
    3: MT5DealType.CREDIT,
    4: MT5DealType.CHARGE,
    5: MT5DealType.CORRECTION,
    6: MT5DealType.BONUS,
    7: MT5DealType.COMMISSION,
    8: MT5DealType.COMMISSION_DAILY,
    9: MT5DealType.COMMISSION_MONTHLY,
    10: MT5DealType.COMMISSION_AGENT_DAILY,
    11: MT5DealType.COMMISSION_AGENT_MONTHLY,
    12: MT5DealType.INTEREST,
    13: MT5DealType.BUY_CANCELED,
    14: MT5DealType.SELL_CANCELED,
    15: MT5DealType.DIVIDEND,
    16: MT5DealType.DIVIDEND_FRANKED,
    17: MT5DealType.TAX,
}


class MT5DealEntry(StrEnum):
    """Mirrors `DEAL_ENTRY_*` (real names/values 0-3)."""

    IN = "IN"
    OUT = "OUT"
    INOUT = "INOUT"
    OUT_BY = "OUT_BY"


_DEAL_ENTRY_MAP: dict[int, MT5DealEntry] = {
    0: MT5DealEntry.IN,
    1: MT5DealEntry.OUT,
    2: MT5DealEntry.INOUT,
    3: MT5DealEntry.OUT_BY,
}


def _unmapped(name: str, raw_value: int) -> str:
    return f"UNMAPPED_{name}:{raw_value}"


@dataclass(frozen=True, slots=True, kw_only=True)
class PositionRecord:
    """Typed conversion of raw `TradePosition` (from `positions_get`)."""

    ticket: int
    symbol: str
    side: MT5PositionSide | None
    volume: Decimal
    price_open: Decimal
    price_current: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    swap: Decimal
    profit: Decimal
    opened_at: datetime
    updated_at: datetime
    magic: int
    identifier: int
    comment: str
    external_id: str
    quality_flags: tuple[str, ...] = field(default_factory=tuple)


def position_to_position_record(raw: Any) -> PositionRecord:
    side = _POSITION_SIDE_MAP.get(int(raw.type))
    quality_flags = () if side is not None else (_unmapped("POSITION_TYPE", int(raw.type)),)
    # `sl`/`tp` == 0.0 means "not set" on a real MT5 position, per MQL5
    # convention -- never surfaced as a fabricated zero price.
    stop_loss = _to_decimal(raw.sl) if raw.sl else None
    take_profit = _to_decimal(raw.tp) if raw.tp else None
    return PositionRecord(
        ticket=int(raw.ticket),
        symbol=str(raw.symbol),
        side=side,
        volume=_to_decimal(raw.volume),
        price_open=_to_decimal(raw.price_open),
        price_current=_to_decimal(raw.price_current),
        stop_loss=stop_loss,
        take_profit=take_profit,
        swap=_to_decimal(raw.swap),
        profit=_to_decimal(raw.profit),
        opened_at=_to_utc(raw.time),
        updated_at=_to_utc(raw.time_update),
        magic=int(raw.magic),
        identifier=int(raw.identifier),
        comment=str(raw.comment),
        external_id=str(raw.external_id),
        quality_flags=quality_flags,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderRecord:
    """Typed conversion of raw `TradeOrder` (from `orders_get`/
    `history_orders_get`)."""

    ticket: int
    symbol: str
    kind: MT5OrderKind | None
    state: MT5OrderState | None
    volume_initial: Decimal
    volume_current: Decimal
    price_open: Decimal
    price_current: Decimal
    stop_loss: Decimal | None
    take_profit: Decimal | None
    set_up_at: datetime
    expires_at: datetime | None
    magic: int
    position_id: int
    comment: str
    external_id: str
    quality_flags: tuple[str, ...] = field(default_factory=tuple)


def order_to_order_record(raw: Any) -> OrderRecord:
    kind = _ORDER_KIND_MAP.get(int(raw.type))
    state = _ORDER_STATE_MAP.get(int(raw.state))
    quality_flags = tuple(
        flag
        for flag in (
            _unmapped("ORDER_TYPE", int(raw.type)) if kind is None else None,
            _unmapped("ORDER_STATE", int(raw.state)) if state is None else None,
        )
        if flag is not None
    )
    stop_loss = _to_decimal(raw.sl) if raw.sl else None
    take_profit = _to_decimal(raw.tp) if raw.tp else None
    return OrderRecord(
        ticket=int(raw.ticket),
        symbol=str(raw.symbol),
        kind=kind,
        state=state,
        volume_initial=_to_decimal(raw.volume_initial),
        volume_current=_to_decimal(raw.volume_current),
        price_open=_to_decimal(raw.price_open),
        price_current=_to_decimal(raw.price_current),
        stop_loss=stop_loss,
        take_profit=take_profit,
        set_up_at=_to_utc(raw.time_setup),
        expires_at=_to_utc_or_none(raw.time_expiration),
        magic=int(raw.magic),
        position_id=int(raw.position_id),
        comment=str(raw.comment),
        external_id=str(raw.external_id),
        quality_flags=quality_flags,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class DealRecord:
    """Typed conversion of raw `TradeDeal` (from `history_deals_get`)."""

    ticket: int
    order: int
    symbol: str
    kind: MT5DealType | None
    entry: MT5DealEntry | None
    volume: Decimal
    price: Decimal
    commission: Decimal
    swap: Decimal
    profit: Decimal
    fee: Decimal
    executed_at: datetime
    position_id: int
    magic: int
    comment: str
    external_id: str
    quality_flags: tuple[str, ...] = field(default_factory=tuple)


def deal_to_deal_record(raw: Any) -> DealRecord:
    kind = _DEAL_TYPE_MAP.get(int(raw.type))
    entry = _DEAL_ENTRY_MAP.get(int(raw.entry))
    quality_flags = tuple(
        flag
        for flag in (
            _unmapped("DEAL_TYPE", int(raw.type)) if kind is None else None,
            _unmapped("DEAL_ENTRY", int(raw.entry)) if entry is None else None,
        )
        if flag is not None
    )
    # `fee` was not independently confirmed against the compiled binary's
    # string table (3-character name, below this module's 4-character
    # extraction threshold -- see module docstring) but is a standard,
    # widely documented `TradeDeal` field; defaults to 0 if absent on a
    # fixture/older field set rather than raising.
    fee_raw = getattr(raw, "fee", 0)
    return DealRecord(
        ticket=int(raw.ticket),
        order=int(raw.order),
        symbol=str(raw.symbol),
        kind=kind,
        entry=entry,
        volume=_to_decimal(raw.volume),
        price=_to_decimal(raw.price),
        commission=_to_decimal(raw.commission),
        swap=_to_decimal(raw.swap),
        profit=_to_decimal(raw.profit),
        fee=_to_decimal(fee_raw),
        executed_at=_to_utc(raw.time),
        position_id=int(raw.position_id),
        magic=int(raw.magic),
        comment=str(raw.comment),
        external_id=str(raw.external_id),
        quality_flags=quality_flags,
    )


# -- Part 1 continued: margin/profit/order-check estimates -------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class MarginEstimate:
    """Typed conversion of `order_calc_margin`'s raw `float | None` result."""

    symbol: str
    volume: Decimal
    price: Decimal
    margin: Decimal | None
    available: bool


def make_margin_estimate(
    *, symbol: str, volume: Decimal, price: Decimal, raw_margin: float | None
) -> MarginEstimate:
    return MarginEstimate(
        symbol=symbol,
        volume=volume,
        price=price,
        margin=_to_decimal_or_none(raw_margin),
        available=raw_margin is not None,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfitEstimate:
    """Typed conversion of `order_calc_profit`'s raw `float | None` result."""

    symbol: str
    volume: Decimal
    price_open: Decimal
    price_close: Decimal
    profit: Decimal | None
    available: bool


def make_profit_estimate(
    *,
    symbol: str,
    volume: Decimal,
    price_open: Decimal,
    price_close: Decimal,
    raw_profit: float | None,
) -> ProfitEstimate:
    return ProfitEstimate(
        symbol=symbol,
        volume=volume,
        price_open=price_open,
        price_close=price_close,
        profit=_to_decimal_or_none(raw_profit),
        available=raw_profit is not None,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class OrderCheckResult:
    """Typed conversion of raw `OrderCheckResult` (from `order_check`).

    `success` is `True` when the real `retcode` is `0` -- the actual,
    verified-against-a-live-account convention `order_check()` uses for "no
    errors, this request is valid" (confirmed empirically against a real
    ActivTrades demo account, 2026-09-29: a genuinely valid, fundable
    request -- correct margin/margin_free numbers, well within account
    equity -- returned `retcode=0, comment='Done'`). This is NOT the same
    convention `order_send()`/a real fill uses (`TRADE_RETCODE_DONE` =
    10009, `TRADE_RETCODE_PLACED` = 10008): `order_check()`'s `retcode`
    field is a distinct namespace, not a `TRADE_RETCODE_*` value, even
    though MT5 happens to reuse the human-readable string "Done" for both.
    An earlier version of this dataclass incorrectly treated 10008/10009 as
    the success codes for `order_check()` specifically, which produced a
    false FAIL for every genuinely valid check -- kept here (alongside 0)
    only as defensive extras in case a different MT5 build/venue ever uses
    them for order_check instead, never as the primary signal.
    """

    success: bool
    retcode: int
    comment: str
    balance: Decimal
    equity: Decimal
    profit: Decimal
    margin: Decimal
    margin_free: Decimal
    margin_level: Decimal


_ORDER_CHECK_SUCCESS_RETCODES = frozenset({0, 10008, 10009})


def order_check_result_from_mt5(raw: Any) -> OrderCheckResult:
    return OrderCheckResult(
        success=int(raw.retcode) in _ORDER_CHECK_SUCCESS_RETCODES,
        retcode=int(raw.retcode),
        comment=str(raw.comment),
        balance=_to_decimal(raw.balance),
        equity=_to_decimal(raw.equity),
        profit=_to_decimal(raw.profit),
        margin=_to_decimal(raw.margin),
        margin_free=_to_decimal(raw.margin_free),
        margin_level=_to_decimal(raw.margin_level),
    )
