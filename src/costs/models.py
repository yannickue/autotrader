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


class CostConfidence(StrEnum):
    """How much a numeric cost field should be trusted.

    Ordered from most to least trustworthy: `VERIFIED` (confirmed against
    real broker data -- a fee schedule document, a signed contract, or a
    reconciled statement), `OBSERVED` (derived from a real, live broker
    bid/ask quote or fill at runtime), `ESTIMATED` (a reasonable placeholder
    pending real data), `UNKNOWN` (no data at all). `UNKNOWN` must never be
    silently treated as equivalent to a verified zero -- see
    `is_safe_for_risk_decisions` in `costs.engine`.
    """

    VERIFIED = "verified"
    OBSERVED = "observed"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


# Severity ordering used to resolve a schedule's *worst* confidence across
# every component that contributes to it -- fail closed: one UNKNOWN
# component makes the whole computed breakdown UNKNOWN, never silently
# averaged away.
_CONFIDENCE_SEVERITY: dict[CostConfidence, int] = {
    CostConfidence.VERIFIED: 0,
    CostConfidence.OBSERVED: 1,
    CostConfidence.ESTIMATED: 2,
    CostConfidence.UNKNOWN: 3,
}

# Field names on `VenueCostSchedule` that `component_confidence` may key on.
# Kept as an explicit allowlist so a typo'd key fails loudly instead of
# silently being ignored.
_COST_COMPONENT_FIELDS = frozenset(
    {
        "maker_fee_rate",
        "taker_fee_rate",
        "commission_rate",
        "commission_fixed",
        "commission_minimum",
        "estimated_spread_bps",
        "entry_slippage_bps",
        "exit_slippage_bps",
        "funding_rate_long_per_interval",
        "funding_rate_short_per_interval",
        "swap_rate_long_per_day",
        "swap_rate_short_per_day",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class VenueCostSchedule:
    """Venue-specific cost parameters. The only place fee values may live.

    All rates are fractions of notional (e.g. `Decimal("0.0002")` == 2 bps)
    unless the field name says otherwise (`*_bps` fields are basis points).

    `instrument_class` declares which asset class this schedule is FOR (e.g.
    a schedule built from Sprint 1's crypto-shaped defaults is
    `InstrumentClass.PERPETUAL` and must never be reused, even accidentally,
    to price a CFD trade -- `costs.engine.calculate_trade_costs` enforces
    this by raising `ValueError` on a mismatch against the request it prices).

    A CFD schedule has no real maker/taker-perpetual-funding concept: set
    `maker_fee_rate`/`taker_fee_rate` to `Decimal("0")` and price the trade
    through `commission_rate`/`commission_fixed`/`commission_minimum` (a CFD
    trade's cost is dominated by spread + a flat/percentage commission) and
    `swap_rate_long_per_day`/`swap_rate_short_per_day` (overnight financing)
    instead, leaving `funding_rate_*_per_interval` at `Decimal("0")`. See
    `make_cfd_cost_schedule` for a constructor that does this for you.

    `cost_confidence` records how much this schedule's numbers should be
    trusted, applied to every field by default. Individual fields can be
    overridden via `component_confidence` (a dict keyed by field name from
    `_COST_COMPONENT_FIELDS`) when confidence genuinely differs per
    component -- e.g. a commission rate copied verbatim from a broker's
    published fee sheet (`VERIFIED`) alongside an overnight swap rate that is
    still a placeholder guess (`ESTIMATED`). `costs.engine.overall_confidence`
    resolves the two into the single worst-case confidence that
    `CostBreakdown.cost_confidence` carries forward.
    """

    venue: str
    instrument_class: InstrumentClass
    cost_confidence: CostConfidence

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

    # Per-component confidence overrides, keyed by field name. Fields absent
    # here inherit `cost_confidence`. See class docstring.
    component_confidence: dict[str, CostConfidence] | None = None

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

        if self.component_confidence is not None:
            unknown_keys = set(self.component_confidence) - _COST_COMPONENT_FIELDS
            if unknown_keys:
                raise ValueError(
                    f"component_confidence has unknown field name(s): {sorted(unknown_keys)}"
                )

    def overall_confidence(self) -> CostConfidence:
        """Worst-case confidence across `cost_confidence` and any
        `component_confidence` overrides -- see class docstring. Fails
        closed: a single UNKNOWN component makes the whole schedule UNKNOWN.
        """
        confidences = [self.cost_confidence, *(self.component_confidence or {}).values()]
        return max(confidences, key=lambda c: _CONFIDENCE_SEVERITY[c])


def make_cfd_cost_schedule(
    *,
    venue: str,
    commission_rate: Decimal = Decimal("0"),
    commission_fixed: Decimal = Decimal("0"),
    commission_minimum: Decimal | None = None,
    estimated_spread_bps: Decimal | None = None,
    entry_slippage_bps: Decimal | None = None,
    exit_slippage_bps: Decimal | None = None,
    swap_rate_long_per_day: Decimal = Decimal("0"),
    swap_rate_short_per_day: Decimal = Decimal("0"),
    cost_confidence: CostConfidence = CostConfidence.ESTIMATED,
    component_confidence: dict[str, CostConfidence] | None = None,
) -> VenueCostSchedule:
    """Build a `VenueCostSchedule` shaped for a CFD instrument.

    `maker_fee_rate`/`taker_fee_rate` are forced to zero and
    `funding_rate_*_per_interval` are forced to zero -- perpetual-funding
    concepts do not apply to a CFD trade. Cost is priced through
    `commission_rate`/`commission_fixed`/`commission_minimum` (spread +
    commission) and `swap_rate_*_per_day` (overnight financing) instead.

    `cost_confidence` defaults to `ESTIMATED` because, absent real broker
    data (no MT5/ActivTrades connection exists yet in this environment),
    every numeric field passed here is a placeholder. Callers must not pass
    `CostConfidence.VERIFIED` unless the values genuinely come from
    confirmed real broker data -- there is no such data yet for CFD/MT5.
    """
    if cost_confidence is CostConfidence.VERIFIED:
        raise ValueError(
            "make_cfd_cost_schedule cannot be called with cost_confidence=VERIFIED: "
            "no real ActivTrades/MT5 broker data exists yet to verify against. "
            "Use ESTIMATED (a reasoned placeholder) or UNKNOWN (no data at all), "
            "or construct VenueCostSchedule directly once real data is available."
        )
    component_values = (component_confidence or {}).values()
    if CostConfidence.VERIFIED in component_values:
        raise ValueError(
            "make_cfd_cost_schedule cannot be called with a component_confidence entry of "
            "VERIFIED: no real ActivTrades/MT5 broker data exists yet to verify against."
        )

    return VenueCostSchedule(
        venue=venue,
        instrument_class=InstrumentClass.CFD,
        cost_confidence=cost_confidence,
        component_confidence=component_confidence,
        maker_fee_rate=Decimal("0"),
        taker_fee_rate=Decimal("0"),
        commission_rate=commission_rate,
        commission_fixed=commission_fixed,
        commission_minimum=commission_minimum,
        estimated_spread_bps=estimated_spread_bps,
        entry_slippage_bps=entry_slippage_bps,
        exit_slippage_bps=exit_slippage_bps,
        funding_rate_long_per_interval=Decimal("0"),
        funding_rate_short_per_interval=Decimal("0"),
        swap_rate_long_per_day=swap_rate_long_per_day,
        swap_rate_short_per_day=swap_rate_short_per_day,
    )


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

    `cost_confidence` carries forward `VenueCostSchedule.overall_confidence()`
    at the time this breakdown was computed -- see
    `costs.engine.is_safe_for_risk_decisions` before using a breakdown for
    anything that affects real risk (as opposed to reporting-only
    attribution, which may tolerate `UNKNOWN`/`ESTIMATED` numbers).
    """

    trade_id: str

    cost_confidence: CostConfidence

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
