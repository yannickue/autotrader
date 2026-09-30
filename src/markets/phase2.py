# ruff: noqa: E501
"""Phase-2 markets (Lane M): BRENT (cluster ENERGY) and BTCUSD (cluster CRYPTO) on the ActivTrades DEMO.

Everything here is generic (driven by ``configs/markets_phase2/*.toml``); no market-specific logic.
Phase-2 markets are deliberately outside ``markets.spec.CANONICALS`` (every research module iterates that
tuple and needs downloaded history); this module is their loader.

Enablement is a DOUBLE gate: the ``enabled`` flag in ``configs/markets_phase2/enablement.toml`` (flipped by
the lead) AND a GREEN ``markets.preflight`` verdict. A flag alone never enables a market.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from markets.spec import (
    MAX_LEVERAGE_CAP,
    PHASE2_CANONICALS,
    PHASE2_CONFIG_DIR,
    MarketSpec,
    MarketSpecError,
    load_market_spec,
)

PHASE2_MARKETS: tuple[str, ...] = PHASE2_CANONICALS
ENABLEMENT_FILE = "enablement.toml"


def load_phase2_spec(canonical: str, config_dir: Path | str | None = None) -> MarketSpec:
    if canonical not in PHASE2_MARKETS:
        raise MarketSpecError(f"{canonical!r} is not a Phase-2 market {PHASE2_MARKETS}")
    return load_market_spec(canonical, config_dir if config_dir is not None else PHASE2_CONFIG_DIR)


def load_phase2_specs(config_dir: Path | str | None = None) -> dict[str, MarketSpec]:
    return {c: load_phase2_spec(c, config_dir) for c in PHASE2_MARKETS}


@dataclass(frozen=True, slots=True, kw_only=True)
class Phase2Cost:
    """Per-instrument cost inputs (spread, slippage rule, commission, swap, movement_to_cost)."""

    reference_median_spread_price: float  # PRICE units; PLACEHOLDER until the probe observes it
    spread_source: str
    commission_eur_per_lot: float
    commission_source: str
    swap_mode: int
    swap_modelled: bool
    min_movement_to_cost: float
    swap_long: float | None  # broker units per swap_mode (points, or annual percent for mode 5)
    swap_short: float | None
    reference_structural_stop_price: float  # only validates stops_level headroom; never a sizing rule


def _raw(canonical: str, config_dir: Path | str | None) -> dict[str, Any]:
    base = Path(config_dir) if config_dir is not None else PHASE2_CONFIG_DIR
    path = base / f"{canonical}.toml"
    if not path.is_file():
        raise MarketSpecError(f"no market config {path}")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_phase2_cost(canonical: str, config_dir: Path | str | None = None) -> Phase2Cost:
    raw = _raw(canonical, config_dir)
    cost, pre = raw["cost"], raw["preflight"]
    long_ = cost.get("swap_long_points", cost.get("swap_long_annual_pct"))
    short = cost.get("swap_short_points", cost.get("swap_short_annual_pct"))
    out = Phase2Cost(
        reference_median_spread_price=float(cost["reference_median_spread_price"]),
        spread_source=str(cost["spread_source"]),
        commission_eur_per_lot=float(cost["commission_eur_per_lot"]),
        commission_source=str(cost["commission_source"]),
        swap_mode=int(cost["swap_mode"]),
        swap_modelled=bool(cost["swap_modelled"]),
        min_movement_to_cost=float(cost["min_movement_to_cost"]),
        swap_long=None if long_ is None else float(long_),
        swap_short=None if short is None else float(short),
        reference_structural_stop_price=float(pre["reference_structural_stop_price"]),
    )
    if not (out.reference_median_spread_price > 0 and out.min_movement_to_cost > 0):
        raise MarketSpecError(f"{canonical}: cost inputs must be > 0")
    return out


def load_enablement(config_dir: Path | str | None = None) -> dict[str, bool]:
    """``{market: enabled}``; a missing file or entry means disabled (fail closed)."""
    base = Path(config_dir) if config_dir is not None else PHASE2_CONFIG_DIR
    path = base / ENABLEMENT_FILE
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {c: bool(raw.get(c, {}).get("enabled", False)) is True for c in PHASE2_MARKETS}


def flag_enabled_markets(config_dir: Path | str | None = None) -> tuple[str, ...]:
    """Markets whose ``enablement.toml`` switch is on (first gate only; the live stack start-up runs the
    per-market preflight as the second gate and disables a failing market on its own)."""
    flags = load_enablement(config_dir)
    return tuple(m for m in PHASE2_MARKETS if flags.get(m, False))


def enabled_market_names(
    verdicts: Mapping[str, Any], config_dir: Path | str | None = None
) -> tuple[str, ...]:
    """Markets that may be handed to the DEMO stack: flag on AND preflight verdict GREEN."""
    flags = load_enablement(config_dir)
    return tuple(
        m for m in PHASE2_MARKETS if flags.get(m, False) and getattr(verdicts.get(m), "verdict", None) == "GREEN"
    )


def demo_market_specs(names: tuple[str, ...], config_dir: Path | str | None = None) -> dict[str, Any]:
    """``DemoMarketSpec`` objects (the execution path's market facts) for the named Phase-2 markets."""
    from demo.execution.market_config import load_demo_market_specs

    for n in names:
        if n not in PHASE2_MARKETS:
            raise MarketSpecError(f"{n!r} is not a Phase-2 market")
    root = Path(config_dir) if config_dir is not None else PHASE2_CONFIG_DIR
    return load_demo_market_specs(root, tuple(names))


@dataclass(frozen=True, slots=True, kw_only=True)
class Phase2CostModel:
    canonical: str
    median_spread_price: float
    spread_source: str
    slippage_base_price: float
    slippage_stress_price: float
    round_trip_cost_price: float  # spread + base slippage, PRICE units per one-way trade
    min_risk_pts: float
    max_risk_pts: float
    eur_per_price_unit_per_lot: float
    min_lot_risk_eur_at_reference_stop: float
    movement_to_cost_at_reference_stop: float  # 1R (reference stop) / cost, TCA semantics
    min_movement_to_cost: float
    commission_eur_per_lot: float
    commission_source: str
    swap_modelled: bool
    leverage_cap: float


def phase2_cost_model(
    spec: MarketSpec,
    cost: Phase2Cost,
    *,
    median_spread_price: float | None = None,
    spread_source: str | None = None,
    account_eur: float = 100_000.0,
) -> Phase2CostModel:
    """Wires the existing ``alpha.common.market_costs`` derivations (slippage = fraction of the median
    spread, risk band in median spreads, EUR conversion, leverage cap) to a Phase-2 market. The median
    spread defaults to the config placeholder; pass the probe's observed median to replace it."""
    from alpha.common.market_costs import cost_scenarios_for, sizing_for

    med = cost.reference_median_spread_price if median_spread_price is None else float(median_spread_price)
    scen = cost_scenarios_for(spec, median_spread_price=med)
    sz = sizing_for(spec, account_eur, median_spread_price=med)
    base, stress = scen["BASE"].slippage_pts, scen["SLIPPAGE_STRESS"].slippage_pts
    one_way_cost = med + base
    ref = cost.reference_structural_stop_price
    lev = float(spec.max_leverage)
    if not 0.0 < lev <= MAX_LEVERAGE_CAP:
        raise MarketSpecError(f"{spec.canonical}: leverage {lev} outside (0, {MAX_LEVERAGE_CAP}]")
    return Phase2CostModel(
        canonical=spec.canonical,
        median_spread_price=med,
        spread_source=spread_source or cost.spread_source,
        slippage_base_price=base,
        slippage_stress_price=stress,
        round_trip_cost_price=one_way_cost,
        min_risk_pts=sz.min_risk_pts,
        max_risk_pts=sz.max_risk_pts,
        eur_per_price_unit_per_lot=sz.contract_size,
        min_lot_risk_eur_at_reference_stop=sz.min_lot * ref * sz.contract_size,
        movement_to_cost_at_reference_stop=ref / one_way_cost if one_way_cost > 0 else float("inf"),
        min_movement_to_cost=cost.min_movement_to_cost,
        commission_eur_per_lot=cost.commission_eur_per_lot,
        commission_source=cost.commission_source,
        swap_modelled=cost.swap_modelled,
        leverage_cap=min(sz.max_leverage, lev),
    )
