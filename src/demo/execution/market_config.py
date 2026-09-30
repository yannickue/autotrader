"""Checked-in market facts used by the explicit DEMO execution path."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

MARKETS = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD")
_CONFIG_ROOT = Path(__file__).resolve().parents[3] / "configs" / "markets"


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class DemoMarketSpec:
    canonical: str
    broker_symbol: str
    volume_min: Decimal
    volume_step: Decimal
    volume_max: Decimal
    contract_size: Decimal
    tick_size: Decimal
    max_spread: Decimal
    max_leverage: Decimal


def load_demo_market_specs(
    config_root: Path | None = None, markets: tuple[str, ...] = MARKETS
) -> dict[str, DemoMarketSpec]:
    """``markets`` defaults to the five live DEMO markets; the Phase-2 loader passes its own
    config root + market names (see ``markets.phase2``) so the default behaviour never changes."""
    root = config_root or _CONFIG_ROOT
    result: dict[str, DemoMarketSpec] = {}
    for expected in markets:
        path = root / f"{expected}.toml"
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
        market = raw["market"]
        instrument = raw["instrument"]
        risk = raw["risk"]
        canonical = str(market["canonical"])
        if canonical != expected:
            raise ValueError(f"{path}: expected canonical {expected}, got {canonical}")
        result[canonical] = DemoMarketSpec(
            canonical=canonical,
            broker_symbol=str(market["broker_symbol"]),
            volume_min=_decimal(instrument["volume_min"]),
            volume_step=_decimal(instrument["volume_step"]),
            volume_max=_decimal(instrument["volume_max"]),
            contract_size=_decimal(instrument["contract_size"]),
            tick_size=_decimal(instrument["tick_size"]),
            max_spread=_decimal(risk["max_entry_spread_price"]),
            max_leverage=_decimal(risk["max_leverage"]),
        )
    return result
