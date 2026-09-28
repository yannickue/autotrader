"""Configuration, inputs, and outputs for the transaction cost model.

This module is intentionally standalone: it has no dependency on `src/pipeline`,
`src/execution`, or `src/risk`, and nothing here calls a venue API or mutates
external state. All venue-specific fee/commission/spread/funding parameters
live in `VenueCostSchedule` -- the calculation logic in `engine.py` never
hardcodes a fee value.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum


class LiquidityRole(StrEnum):
    """Whether a fill added or removed liquidity, which selects the fee rate."""

    MAKER = "maker"
    TAKER = "taker"


class TradeSide(StrEnum):
    """Directional side of the trade, used for PnL and funding/swap sign."""

    LONG = "long"
    SHORT = "short"


class InstrumentClass(StrEnum):
    """Determines which carry-cost abstraction (if any) applies to a trade."""

    PERPETUAL = "perpetual"
    CFD = "cfd"
    SPOT = "spot"


@dataclass(frozen=True, slots=True, kw_only=True)
class VenueCostSchedule:
    """Venue-specific cost parameters. The only place fee values may live.

    All rates are fractions of notional (e.g. `Decimal("0.0002")` == 2 bps)
    unless the field name says otherwise (`*_bps` fields are basis points).
    """

    venue: str

    maker_fee_rate: Decimal
    taker_fee_rate: Decimal

    commission_rate: Decimal = Decimal("0")
    commission_fixed: Decimal = Decimal("0")
    commission_minimum: Decimal | None = None

    # Fallback half-spread cost rate (bps of price) used only when a trade
    # does not carry observed bid/ask quotes.
    estimated_spread_bps: Decimal | None = None

    # Fallback slippage cost rate (bps of price) used only when a trade does
    # not carry an expected/reference execution price.
    entry_slippage_bps: Decimal | None = None
    exit_slippage_bps: Decimal | None = None

    # Perpetual/research funding abstraction: rate applied per funding
    # interval to entry notional, signed per side.
    funding_rate_long_per_interval: Decimal = Decimal("0")
    funding_rate_short_per_interval: Decimal = Decimal("0")
    funding_interval: timedelta = timedelta(hours=8)

    # CFD/live venue overnight financing abstraction: rate applied per day
    # (prorated) to entry notional, signed per side.
    swap_rate_long_per_day: Decimal = Decimal("0")
    swap_rate_short_per_day: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.venue:
            raise ValueError("venue must be non-empty")

        for name in ("maker_fee_rate", "taker_fee_rate", "commission_rate", "commission_fixed"):
            _require_finite_non_negative(name, getattr(self, name))

        if self.commission_minimum is not None:
            _require_finite_non_negative("commission_minimum", self.commission_minimum)

        for name in ("estimated_spread_bps", "entry_slippage_bps", "exit_slippage_bps"):
            value = getattr(self, name)
            if value is not None:
                _require_finite_non_negative(name, value)

        for name in (
            "funding_rate_long_per_interval",
            "funding_rate_short_per_interval",
            "swap_rate_long_per_day",
            "swap_rate_short_per_day",
        ):
            _require_finite(name, getattr(self, name))

        if self.funding_interval <= timedelta(0):
            raise ValueError("funding_interval must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class CostCalculationRequest:
    """One completed (entry + exit) trade/fill to price out."""

    trade_id: str
    instrument: str
    instrument_class: InstrumentClass
    side: TradeSide
    liquidity_role: LiquidityRole

    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal

    # Reference/mid price the strategy expected to fill at, before slippage.
    # When omitted, the schedule's `*_slippage_bps` fallback is used instead.
    expected_entry_price: Decimal | None = None
    expected_exit_price: Decimal | None = None

    # Observed top-of-book quotes at fill time, used to price the spread
    # actually crossed. When omitted, the schedule's `estimated_spread_bps`
    # fallback is used instead.
    entry_bid: Decimal | None = None
    entry_ask: Decimal | None = None
    exit_bid: Decimal | None = None
    exit_ask: Decimal | None = None

    holding_period: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if not self.trade_id:
            raise ValueError("trade_id must be non-empty")
        if not self.instrument:
            raise ValueError("instrument must be non-empty")

        _require_finite_positive("quantity", self.quantity)
        _require_finite_positive("entry_price", self.entry_price)
        _require_finite_positive("exit_price", self.exit_price)

        if self.expected_entry_price is not None:
            _require_finite_positive("expected_entry_price", self.expected_entry_price)
        if self.expected_exit_price is not None:
            _require_finite_positive("expected_exit_price", self.expected_exit_price)

        for label, bid, ask in (
            ("entry", self.entry_bid, self.entry_ask),
            ("exit", self.exit_bid, self.exit_ask),
        ):
            if bid is not None:
                _require_finite_positive(f"{label}_bid", bid)
            if ask is not None:
                _require_finite_positive(f"{label}_ask", ask)
            if bid is not None and ask is not None and bid > ask:
                raise ValueError(f"{label}_bid cannot exceed {label}_ask")

        if self.holding_period < timedelta(0):
            raise ValueError("holding_period cannot be negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class CostBreakdown:
    """Result of pricing a `CostCalculationRequest` against a schedule.

    `fees`, `spread_cost`, `slippage_cost`, and a positive `funding_or_swap`
    are costs (they reduce `net_pnl`); a negative `funding_or_swap` is a
    credit (it increases `net_pnl`). The granular `*_cost`/`exchange_fee`/
    `commission` fields are provided for transparency in addition to the
    required summary fields.
    """

    trade_id: str

    gross_pnl: Decimal

    exchange_fee: Decimal
    commission: Decimal
    fees: Decimal

    entry_spread_cost: Decimal
    exit_spread_cost: Decimal
    spread_cost: Decimal

    entry_slippage_cost: Decimal
    exit_slippage_cost: Decimal
    slippage_cost: Decimal

    funding_or_swap: Decimal

    net_pnl: Decimal


def _require_finite(name: str, value: Decimal) -> None:
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


def _require_finite_non_negative(name: str, value: Decimal) -> None:
    _require_finite(name, value)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _require_finite_positive(name: str, value: Decimal) -> None:
    _require_finite(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be positive")
