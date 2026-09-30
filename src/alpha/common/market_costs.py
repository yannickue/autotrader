# ruff: noqa: E501
"""Principled per-market cost and sizing definitions (research only, V2).

``CostScenario.slippage_pts`` and ``SizingSpec.min_risk_pts / max_risk_pts`` are PRICE units of the
market (despite the ``_pts`` names). The V1 numbers (0.5 / 1.5 slippage, 5 / 400 risk band) were
calibrated on GER40, where the observed M5 median recorded spread is 145 points = 1.45 price units.
This module keeps GER40 EXACTLY on those numbers and derives every other market from its own
MarketSpec plus its observed spread distribution:

* slippage = (V1 GER40 slippage / GER40 median spread) x THIS market's median spread, rounded to the
  market tick. I.e. slippage is a fixed fraction of the market's observed median spread (0.345 base,
  1.034 stress); a spread-normalised cost, independent of price level and point size.
* min risk distance = (5.0 / GER40 median spread) = 3.448 median spreads: a stop closer than that makes
  the round-trip spread a disproportionate share of 1R. max risk distance = 275.9 median spreads: a
  sanity ceiling (rejects absurd stops), NOT a target; the effective sizing bound is
  ``risk_fraction`` x equity together with the leverage cap and ``volume_min``.
* lot step / min lot / contract size from the spec (volume_min, volume_step, contract_size).
* EUR conversion: ``contract_size`` of SizingSpec means EUR per price unit per lot. Non-EUR profit
  currencies are converted with a single constant reference rate (``REFERENCE_EUR_PER_USD``).
* leverage: ``min(RESEARCH_LEVERAGE_CAP=10, spec.max_leverage)``. The 30x permitted ceiling is an
  upper bound and is never a target (asserted).
* risk-based sizing already refuses a trade whose minimum lot risks more than the approved risk
  (``risk_eur / (risk x contract)`` floored to the lot step is < ``volume_min``): the kernel counts it
  under the existing ``size_below_min`` skip label (also when the leverage cap pushes the quantity
  below the minimum lot).

NOT modelled (returned in ``MarketCostModel.unmodelled``): commission (GER40 demo observed 0; other
markets unverified, 0 assumed; COMBINED_ADVERSE keeps the V1 1.0 EUR/lot placeholder), swap/overnight
financing (no overnight positions: forced flat), FX-rate variation (constant reference rate; EURUSD
notional is exactly contract_size EUR but is modelled as fill/rate x contract), margin/stop-out
mechanics, slippage as an observation (a calibrated fraction of spread, not measured), volume-dependent
slippage, holiday/half-day early closes, and the broker ``tick_value`` (price x contract is used).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from alpha.common.sim import COST_SCENARIOS, DEFAULT_SIZING, CostScenario, SizingSpec
from markets.spec import MAX_LEVERAGE_CAP

DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[3] / "research" / "reports" / "v2_markets"

# ---- V1 GER40 anchors (the numbers the whole V1 pipeline was calibrated with) ----------------
GER40_MEDIAN_SPREAD_PRICE = 1.45  # observed M5 median 145 recorded points x 0.01 (GER40_quality.json)
GER40_MIN_RISK = DEFAULT_SIZING.min_risk_pts  # 5.0 price units
GER40_MAX_RISK = DEFAULT_SIZING.max_risk_pts  # 400.0 price units
MIN_RISK_SPREAD_MULT = GER40_MIN_RISK / GER40_MEDIAN_SPREAD_PRICE  # ~3.448 median spreads
MAX_RISK_SPREAD_MULT = GER40_MAX_RISK / GER40_MEDIAN_SPREAD_PRICE  # ~275.9 median spreads

RESEARCH_LEVERAGE_CAP = DEFAULT_SIZING.max_leverage  # 10x; well below the 30x permitted ceiling
# Constant reference conversion: broker order_calc_margin observation 2026-09-30 (NAS100, 1 lot @30369
# USD at 5% = 1339.0 EUR) implies 1339.0 / (30369 * 0.05) EUR per USD.
REFERENCE_EUR_PER_USD = 1339.0 / (30369.0 * 0.05)

# Observed notional of ONE lot in EUR (broker order_calc_margin snapshot 2026-09-30, see the
# `margin_notes` of configs/markets/*.toml): index/metal = quote price x contract x EUR/ccy rate,
# EURUSD = 100000 EUR (base currency). Informational only (min-lot leverage feasibility).
OBSERVED_LOT_NOTIONAL_EUR = {
    "GER40": 25474.0,
    "NAS100": 30369.0 * REFERENCE_EUR_PER_USD,
    "SPX500": 7682.0 * REFERENCE_EUR_PER_USD,
    "XAUUSD": 4169.6 * 100.0 * REFERENCE_EUR_PER_USD,
    "EURUSD": 100000.0,
}

UNMODELLED_COMMON = (
    "commission: GER40 demo observed 0; other markets UNVERIFIED (0 assumed); COMBINED_ADVERSE keeps the V1 1.0 EUR/lot placeholder",
    "swap/financing: not modelled (positions are forced flat, no overnight)",
    "FX rate variation: constant reference EUR/USD rate for non-EUR profit currency",
    "slippage is a calibrated fraction of the median recorded spread, not an observed fill quality",
    "margin/stop-out mechanics, holiday/half-day early closes, tick_value",
)


class MarketCostError(ValueError):
    """A market cost/sizing definition cannot be derived (fail closed)."""


def _quantize(value: float, tick: float, *, at_least_one_tick: bool = True) -> float:
    """Round ``value`` to a multiple of ``tick`` (as ``n * tick``; exact for GER40: 50*0.01 = 0.5)."""
    n = round(value / tick)
    if at_least_one_tick and value > 0.0:
        n = max(n, 1)
    return n * tick


def observed_median_spread_price(spec: Any, report_dir: Path | str | None = None) -> float:
    """Median recorded M5 spread of the market in PRICE units, from its quality report."""
    base = Path(report_dir) if report_dir is not None else DEFAULT_REPORT_DIR
    path = base / f"{spec.canonical}_quality.json"
    if not path.is_file():
        raise MarketCostError(f"no quality report {path}: cannot derive spread-based costs")
    try:
        med = float(json.loads(path.read_text(encoding="utf-8"))["timeframes"]["M5"]["analysis"]["spread_pts"]["median"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketCostError(f"{path}: no M5 spread_pts median") from exc
    if not med > 0.0:
        raise MarketCostError(f"{path}: non-positive median spread")
    return med * spec.point_size


def eur_per_price_unit_per_lot(spec: Any, eur_per_ccy: dict[str, float] | None = None) -> float:
    """``SizingSpec.contract_size``: EUR moved per 1.0 price unit per lot (profit ccy -> EUR)."""
    ccy = spec.currency_profit
    if ccy == "EUR":
        rate = 1.0
    elif eur_per_ccy is not None and ccy in eur_per_ccy:
        rate = float(eur_per_ccy[ccy])
    elif ccy == "USD":
        rate = REFERENCE_EUR_PER_USD
    else:
        raise MarketCostError(f"{spec.canonical}: no EUR conversion for profit currency {ccy!r}")
    return spec.contract_size * rate


def cost_scenarios_for(
    spec: Any, *, median_spread_price: float | None = None, report_dir: Path | str | None = None
) -> dict[str, CostScenario]:
    """The V1 scenario set with slippage expressed as a fraction of this market's median spread."""
    med = (
        median_spread_price
        if median_spread_price is not None
        else observed_median_spread_price(spec, report_dir)
    )
    out: dict[str, CostScenario] = {}
    for name, base in COST_SCENARIOS.items():
        frac = base.slippage_pts / GER40_MEDIAN_SPREAD_PRICE
        slip = 0.0 if base.slippage_pts == 0.0 else _quantize(frac * med, spec.tick_size)
        out[name] = replace(base, slippage_pts=slip)
    return out


def sizing_for(
    spec: Any,
    account_eur: float = 500.0,
    *,
    risk_fraction: float = DEFAULT_SIZING.risk_fraction,
    median_spread_price: float | None = None,
    report_dir: Path | str | None = None,
    eur_per_ccy: dict[str, float] | None = None,
) -> SizingSpec:
    """Per-market sizing. ``sizing_for(GER40, account_eur=10_000)`` == ``DEFAULT_SIZING`` exactly."""
    if not account_eur > 0.0:
        raise MarketCostError("account_eur must be > 0")
    med = (
        median_spread_price
        if median_spread_price is not None
        else observed_median_spread_price(spec, report_dir)
    )
    lev = min(RESEARCH_LEVERAGE_CAP, float(spec.max_leverage))
    if not 0.0 < lev <= MAX_LEVERAGE_CAP:
        raise MarketCostError(f"leverage {lev} outside (0, {MAX_LEVERAGE_CAP}]")
    min_risk = _quantize(MIN_RISK_SPREAD_MULT * med, spec.tick_size)
    max_risk = _quantize(MAX_RISK_SPREAD_MULT * med, spec.tick_size)
    if not min_risk < max_risk:
        raise MarketCostError(f"{spec.canonical}: empty risk band {min_risk}..{max_risk}")
    return SizingSpec(
        equity_eur=float(account_eur),
        risk_fraction=risk_fraction,
        lot_step=spec.volume_step,
        min_lot=spec.volume_min,
        max_leverage=lev,
        min_risk_pts=min_risk,
        max_risk_pts=max_risk,
        contract_size=eur_per_price_unit_per_lot(spec, eur_per_ccy),
    )


@dataclass(frozen=True)
class MarketCostModel:
    canonical: str
    costs: dict[str, CostScenario]
    sizing: SizingSpec
    median_spread_price: float
    min_lot_risk_at_min_stop_eur: float  # EUR risked by the minimum lot at the minimum stop
    approved_risk_eur: float
    min_lot_feasible_at_min_stop: bool
    min_lot_leverage: float | None  # min lot notional / equity at the observed price (None: unknown market)
    unmodelled: tuple[str, ...]


def market_cost_model(
    spec: Any, account_eur: float = 500.0, *, report_dir: Path | str | None = None,
    risk_fraction: float = DEFAULT_SIZING.risk_fraction,
) -> MarketCostModel:
    med = observed_median_spread_price(spec, report_dir)
    sz = sizing_for(spec, account_eur, risk_fraction=risk_fraction, median_spread_price=med)
    lot_risk = sz.min_lot * sz.min_risk_pts * sz.contract_size
    approved = sz.equity_eur * sz.risk_fraction
    unmodelled = UNMODELLED_COMMON
    if spec.canonical == "GER40":
        unmodelled = tuple(u for u in unmodelled if not u.startswith("FX rate"))
    notional = OBSERVED_LOT_NOTIONAL_EUR.get(spec.canonical)
    return MarketCostModel(
        canonical=spec.canonical,
        costs=cost_scenarios_for(spec, median_spread_price=med),
        sizing=sz,
        median_spread_price=med,
        min_lot_risk_at_min_stop_eur=lot_risk,
        approved_risk_eur=approved,
        min_lot_feasible_at_min_stop=lot_risk <= approved + 1e-12,
        min_lot_leverage=None if notional is None else sz.min_lot * notional / sz.equity_eur,
        unmodelled=unmodelled,
    )


def derived_table(specs: dict[str, Any], account_eur: float = 500.0) -> str:
    """Markdown table of the derived cost/sizing definitions (used by docs and the test log)."""
    rows = [
        "| Market | median spread | slip BASE/STRESS | min..max risk | lot min/step | EUR per price unit per lot | "
        "lev cap | min lot risk at min stop (EUR) | approved risk (EUR) | min lot leverage |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, spec in specs.items():
        m = market_cost_model(spec, account_eur)
        s = m.sizing
        rows.append(
            f"| {name} | {m.median_spread_price:.6g} | {m.costs['BASE'].slippage_pts:.6g} / "
            f"{m.costs['SLIPPAGE_STRESS'].slippage_pts:.6g} | {s.min_risk_pts:.6g}..{s.max_risk_pts:.6g} | "
            f"{s.min_lot:g}/{s.lot_step:g} | {s.contract_size:.6g} | {s.max_leverage:g} | "
            f"{m.min_lot_risk_at_min_stop_eur:.4g} | {m.approved_risk_eur:.4g} | "
            f"{'n/a' if m.min_lot_leverage is None else format(m.min_lot_leverage, '.3g') + 'x'} |"
        )
    return "\n".join(rows)


__all__ = (
    "GER40_MEDIAN_SPREAD_PRICE",
    "REFERENCE_EUR_PER_USD",
    "RESEARCH_LEVERAGE_CAP",
    "MarketCostError",
    "MarketCostModel",
    "cost_scenarios_for",
    "derived_table",
    "eur_per_price_unit_per_lot",
    "market_cost_model",
    "observed_median_spread_price",
    "sizing_for",
)
