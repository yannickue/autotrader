from decimal import Decimal

import pytest

from data.binance_usdm import InstrumentRules
from universe.selector import (
    InsufficientUniverseError,
    MarketCandidate,
    UniverseSelectionConfig,
    select_universe,
)


def candidate(
    number: int,
    *,
    status: str = "TRADING",
    quote_volume: str = "1000000",
    spread_bps: str = "5",
    volatility: str = "0.02",
    movement: str = "0.03",
    liquidity: str = "500000",
    min_notional: str = "5",
) -> MarketCandidate:
    symbol = f"ASSET{number}USDT"
    rules = InstrumentRules(
        instrument=f"{symbol}-PERP",
        venue_symbol=symbol,
        base_asset=f"ASSET{number}",
        quote_asset="USDT",
        margin_asset="USDT",
        status=status,
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal(min_notional),
        market_quantity_step=Decimal("0.001"),
    )
    return MarketCandidate(
        rules=rules,
        quote_volume=Decimal(quote_volume),
        spread_bps=Decimal(spread_bps),
        realized_volatility=Decimal(volatility),
        movement=Decimal(movement),
        liquidity=Decimal(liquidity),
    )


def test_universe_config_enforces_production_size_range() -> None:
    # Catches accidental tiny or unbounded production universe configuration.
    with pytest.raises(ValueError, match="target_size must be between 20 and 100"):
        UniverseSelectionConfig(target_size=19)
    with pytest.raises(ValueError, match="target_size must be between 20 and 100"):
        UniverseSelectionConfig(target_size=101)


def test_universe_filters_every_required_liquidity_and_tradeability_dimension() -> None:
    # Catches omission or inversion of any required filter.
    eligible = [candidate(number) for number in range(20)]
    rejected = [
        candidate(20, quote_volume="999"),
        candidate(21, spread_bps="11"),
        candidate(22, volatility="0.001"),
        candidate(23, volatility="0.2"),
        candidate(24, movement="0.001"),
        candidate(25, liquidity="999"),
        candidate(26, min_notional="51"),
        candidate(27, status="PENDING_TRADING"),
    ]
    config = UniverseSelectionConfig(
        target_size=20,
        min_quote_volume=Decimal("1000"),
        max_spread_bps=Decimal("10"),
        min_realized_volatility=Decimal("0.005"),
        max_realized_volatility=Decimal("0.1"),
        min_movement=Decimal("0.005"),
        min_liquidity=Decimal("1000"),
        max_min_notional=Decimal("50"),
    )

    selected = select_universe(eligible + rejected, config)

    assert {item.rules.venue_symbol for item in selected} == {
        f"ASSET{number}USDT" for number in range(20)
    }


def test_universe_ranking_rewards_volume_liquidity_movement_and_tighter_spread() -> None:
    # Catches a ranking that only filters and then falls back to input order.
    ordinary = [candidate(number) for number in range(20)]
    best = candidate(
        99,
        quote_volume="5000000",
        spread_bps="1",
        volatility="0.08",
        movement="0.09",
        liquidity="3000000",
        min_notional="1",
    )

    selected = select_universe([*ordinary, best], UniverseSelectionConfig(target_size=20))

    assert selected[0] == best
    assert len(selected) == 20


def test_universe_selection_fails_closed_when_too_few_markets_are_eligible() -> None:
    # Catches silently running outside the configured 20-market safety envelope.
    with pytest.raises(InsufficientUniverseError, match="19 eligible markets; 20 required"):
        select_universe(
            [candidate(number) for number in range(19)],
            UniverseSelectionConfig(target_size=20),
        )
