"""Configurable, deterministic selection of liquid perpetual markets."""

from dataclasses import dataclass, field
from decimal import Decimal

from data.binance_usdm import InstrumentRules


class InsufficientUniverseError(RuntimeError):
    """Raised when safety filters leave fewer markets than configured."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RankingWeights:
    quote_volume: Decimal = Decimal("0.30")
    liquidity: Decimal = Decimal("0.25")
    spread: Decimal = Decimal("0.20")
    realized_volatility: Decimal = Decimal("0.10")
    movement: Decimal = Decimal("0.10")
    min_notional: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        values = (
            self.quote_volume,
            self.liquidity,
            self.spread,
            self.realized_volatility,
            self.movement,
            self.min_notional,
        )
        if any(not value.is_finite() or value < 0 for value in values):
            raise ValueError("ranking weights must be finite and non-negative")
        if sum(values, start=Decimal(0)) <= 0:
            raise ValueError("at least one ranking weight must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class UniverseSelectionConfig:
    target_size: int = 20
    min_quote_volume: Decimal = Decimal(0)
    max_spread_bps: Decimal = Decimal("50")
    min_realized_volatility: Decimal = Decimal(0)
    max_realized_volatility: Decimal = Decimal(1)
    min_movement: Decimal = Decimal(0)
    min_liquidity: Decimal = Decimal(0)
    max_min_notional: Decimal = Decimal("100")
    allowed_statuses: frozenset[str] = frozenset({"TRADING"})
    weights: RankingWeights = field(default_factory=RankingWeights)

    def __post_init__(self) -> None:
        if not 20 <= self.target_size <= 100:
            raise ValueError("target_size must be between 20 and 100")
        thresholds = (
            self.min_quote_volume,
            self.max_spread_bps,
            self.min_realized_volatility,
            self.max_realized_volatility,
            self.min_movement,
            self.min_liquidity,
            self.max_min_notional,
        )
        if any(not value.is_finite() or value < 0 for value in thresholds):
            raise ValueError("universe thresholds must be finite and non-negative")
        if self.max_realized_volatility < self.min_realized_volatility:
            raise ValueError("maximum volatility cannot be below minimum volatility")
        if not self.allowed_statuses or any(not status for status in self.allowed_statuses):
            raise ValueError("allowed_statuses cannot be empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketCandidate:
    rules: InstrumentRules
    quote_volume: Decimal
    spread_bps: Decimal
    realized_volatility: Decimal
    movement: Decimal
    liquidity: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "quote_volume",
            "spread_bps",
            "realized_volatility",
            "movement",
            "liquidity",
        ):
            value = getattr(self, field_name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")


def _is_eligible(candidate: MarketCandidate, config: UniverseSelectionConfig) -> bool:
    return (
        candidate.rules.status in config.allowed_statuses
        and candidate.quote_volume >= config.min_quote_volume
        and candidate.spread_bps <= config.max_spread_bps
        and config.min_realized_volatility
        <= candidate.realized_volatility
        <= config.max_realized_volatility
        and candidate.movement >= config.min_movement
        and candidate.liquidity >= config.min_liquidity
        and candidate.rules.min_notional <= config.max_min_notional
    )


def _normalized(value: Decimal, values: tuple[Decimal, ...], *, higher_is_better: bool) -> Decimal:
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        return Decimal("0.5")
    if higher_is_better:
        return (value - minimum) / (maximum - minimum)
    return (maximum - value) / (maximum - minimum)


def select_universe(
    candidates: list[MarketCandidate] | tuple[MarketCandidate, ...],
    config: UniverseSelectionConfig,
) -> tuple[MarketCandidate, ...]:
    """Filter and rank markets, failing closed below the requested universe size."""
    eligible = tuple(candidate for candidate in candidates if _is_eligible(candidate, config))
    if len(eligible) < config.target_size:
        raise InsufficientUniverseError(
            f"{len(eligible)} eligible markets; {config.target_size} required"
        )

    quote_volumes = tuple(item.quote_volume for item in eligible)
    liquidities = tuple(item.liquidity for item in eligible)
    spreads = tuple(item.spread_bps for item in eligible)
    volatilities = tuple(item.realized_volatility for item in eligible)
    movements = tuple(item.movement for item in eligible)
    min_notionals = tuple(item.rules.min_notional for item in eligible)
    weights = config.weights

    def score(candidate: MarketCandidate) -> Decimal:
        return (
            weights.quote_volume
            * _normalized(candidate.quote_volume, quote_volumes, higher_is_better=True)
            + weights.liquidity
            * _normalized(candidate.liquidity, liquidities, higher_is_better=True)
            + weights.spread
            * _normalized(candidate.spread_bps, spreads, higher_is_better=False)
            + weights.realized_volatility
            * _normalized(
                candidate.realized_volatility, volatilities, higher_is_better=True
            )
            + weights.movement
            * _normalized(candidate.movement, movements, higher_is_better=True)
            + weights.min_notional
            * _normalized(candidate.rules.min_notional, min_notionals, higher_is_better=False)
        )

    ranked = sorted(eligible, key=lambda item: (-score(item), item.rules.venue_symbol))
    return tuple(ranked[: config.target_size])
