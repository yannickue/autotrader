"""Checked-in market facts used by the explicit DEMO execution path."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from risk.models import InstrumentRiskLimits

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

    def risk_limits(self) -> InstrumentRiskLimits:
        """Translate only checked-in facts; 30x is enforced as a ceiling."""
        leverage = min(self.max_leverage, Decimal("30"))
        return InstrumentRiskLimits(
            instrument=self.canonical,
            max_leverage=leverage,
            quantity_step=self.volume_step,
            min_quantity=self.volume_min,
            min_notional=Decimal(0),
            # The evaluator also applies account/policy notional caps.  This
            # instrument ceiling is the config's maximum contract exposure.
            max_notional=self.volume_max * self.contract_size,
            max_spread_bps=self.max_spread,
            maintenance_margin_rate=Decimal(1) / leverage,
        )


def load_demo_market_specs(config_root: Path | None = None) -> dict[str, DemoMarketSpec]:
    root = config_root or _CONFIG_ROOT
    result: dict[str, DemoMarketSpec] = {}
    for expected in MARKETS:
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
