"""Canonical CFD instrument specification.

This module is intentionally standalone: it has no dependency on the
`metatrader5` package, `src/adapters`, `src/risk`, `src/execution`, or any
other live-broker code, and performs no venue I/O. It is fully testable with
plain Python fixtures.

A CFD instrument is NOT simply `price x quantity`: sizing, margin, and PnL
depend on contract size, tick size/value, currencies, and broker-specific
volume constraints, all of which are captured by `InstrumentSpec`.

Unknown-fields rule (directive-mandated): every `| None` field below means
"the broker may not expose this value". When a real broker genuinely does not
provide a value, callers MUST pass `None` -- never a fabricated or
guessed-at default (e.g. never default `margin_initial` to a plausible-looking
number). Fields that are never optional (contract size, tick size/value,
volume constraints, currencies) are validated as present/finite/positive in
`__post_init__` and fail closed with `ValueError` when missing or invalid,
because a spec without them cannot safely size a position at all.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class MarginCalculationMode(StrEnum):
    """Mirrors MetaTrader5's `SYMBOL_CALC_MODE_*` constants (real names).

    Verified by inspecting `MetaTrader5.__dict__` without a live terminal
    connection (the package exposes these as plain module attributes that do
    not require `initialize()`). Only the modes relevant to CFD/forex/futures
    instruments are modeled here; exchange-traded stock/bond/option modes are
    out of scope for this task.
    """

    FOREX = "FOREX"
    FOREX_NO_LEVERAGE = "FOREX_NO_LEVERAGE"
    FUTURES = "FUTURES"
    CFD = "CFD"
    CFDINDEX = "CFDINDEX"
    CFDLEVERAGE = "CFDLEVERAGE"


class TradeMode(StrEnum):
    """Mirrors MetaTrader5's `SYMBOL_TRADE_MODE_*` constants (real names)."""

    DISABLED = "DISABLED"
    LONGONLY = "LONGONLY"
    SHORTONLY = "SHORTONLY"
    CLOSEONLY = "CLOSEONLY"
    FULL = "FULL"


class ExecutionMode(StrEnum):
    """Mirrors MetaTrader5's `SYMBOL_TRADE_EXECUTION_*` constants (real names)."""

    REQUEST = "REQUEST"
    INSTANT = "INSTANT"
    MARKET = "MARKET"
    EXCHANGE = "EXCHANGE"


class FillingMode(StrEnum):
    """Mirrors MetaTrader5's `ORDER_FILLING_*` constants (real names)."""

    FOK = "FOK"
    IOC = "IOC"
    RETURN = "RETURN"
    BOC = "BOC"


def _require_finite(name: str, value: Decimal) -> None:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


def _require_finite_positive(name: str, value: Decimal) -> None:
    _require_finite(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_finite_positive_optional(name: str, value: Decimal | None) -> None:
    if value is not None:
        _require_finite_positive(name, value)


def _require_non_empty(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentSpec:
    """Broker-reported CFD instrument specification, decimal-precise.

    Required fields (contract size, tick size/value, volume constraints,
    currencies) are validated as present/finite/positive in `__post_init__`.
    All other broker-reported fields are `| None` and must be left `None`
    when the broker does not expose them -- never fabricated.
    """

    # Identity
    canonical_symbol: str
    broker_symbol: str
    description: str

    # Price
    digits: int
    point: Decimal
    trade_tick_size: Decimal
    trade_tick_value: Decimal
    trade_tick_value_profit: Decimal | None = None
    trade_tick_value_loss: Decimal | None = None

    # Contract
    trade_contract_size: Decimal

    # Volume
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    volume_limit: Decimal | None = None

    # Currencies
    currency_base: str
    currency_profit: str
    currency_margin: str

    # Margin
    margin_initial: Decimal | None = None
    margin_maintenance: Decimal | None = None
    margin_hedged: Decimal | None = None
    margin_calculation_mode: MarginCalculationMode | None = None

    # Execution
    trade_mode: TradeMode | None = None
    execution_mode: ExecutionMode | None = None
    filling_modes: tuple[FillingMode, ...] = ()
    stop_level: Decimal | None = None
    freeze_level: Decimal | None = None

    # Session
    is_tradable: bool = True
    session_note: str | None = None

    # Metadata
    source: str
    retrieved_at: datetime
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty("canonical_symbol", self.canonical_symbol)
        _require_non_empty("broker_symbol", self.broker_symbol)
        _require_non_empty("source", self.source)

        if self.digits < 0:
            raise ValueError("digits must be non-negative")

        for name in ("point", "trade_tick_size", "trade_tick_value", "trade_contract_size"):
            _require_finite_positive(name, getattr(self, name))

        for name in ("trade_tick_value_profit", "trade_tick_value_loss"):
            value = getattr(self, name)
            if value is not None and not value.is_finite():
                raise ValueError(f"{name} must be finite")

        for name in ("volume_min", "volume_max", "volume_step"):
            _require_finite_positive(name, getattr(self, name))
        if self.volume_min > self.volume_max:
            raise ValueError("volume_min cannot exceed volume_max")
        _require_finite_positive_optional("volume_limit", self.volume_limit)

        for name in ("currency_base", "currency_profit", "currency_margin"):
            _require_non_empty(name, getattr(self, name))

        for name in ("margin_initial", "margin_maintenance", "margin_hedged"):
            value = getattr(self, name)
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")

        for name in ("stop_level", "freeze_level"):
            value = getattr(self, name)
            if value is not None and (not value.is_finite() or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")

        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() != timedelta(0):
            raise ValueError("retrieved_at must be UTC")
