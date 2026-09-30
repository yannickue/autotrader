# ruff: noqa: E501
"""Broker-universe classification, deterministic candidate selection and SHADOW readiness assessment (Lane U).

Pure functions over plain dicts (the `universe_inventory.py list|scan` output); no MT5 access here.

Selection rule (``SELECTION_RULE_ID``; explicit and reproducible, sorted by (cluster, name), independent of input order):
  1. every symbol with ``trade_mode == 4`` (FULL) whose cluster is INDEX, FX, METALS, ENERGY, CRYPTO or COMMODITY_SOFT
     is probed (cash CFDs, no dated contracts);
  2. single-stock CFDs: only the fixed ``STOCK_BLUE_CHIPS`` whitelist (liquid large caps), if present and FULL;
  3. dated futures (path ``CFD Forward*``) are classified but NOT probed (expiry/roll makes them unsuitable for a
     continuous shadow history), all other shares are NOT_SAMPLED; non-FULL trade modes are NOT_READY without a probe.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any

SELECTION_RULE_ID = "U1-all-cash-cfd-non-stock-plus-bluechip-stocks"
TRADE_MODE_FULL = 4

CLUSTERS = ("INDEX", "FX", "METALS", "ENERGY", "CRYPTO", "COMMODITY_SOFT", "STOCKS", "BONDS", "FUTURES_DATED", "OTHER")

# Spot Energy path also holds soft commodities: classify by name, not by path.
_SOFT_NAMES = frozenset({"Coffee", "CoffeeR", "Cotton", "Sugar", "Cocoa"})
_ENERGY_NAMES = frozenset({"Brent", "LCrude", "NGas", "Diesel"})
_METAL_NAMES = frozenset({"GOLD", "SILVER", "Platinum", "Palladium"})

STOCK_BLUE_CHIPS: tuple[str, ...] = (
    "SAP.GE", "SIE.GE", "ALV.GE", "BMW.GE", "BAS.GE", "DTE.GE",
    "AIR.FR", "MC.FR", "OR.FR", "BNP.FR", "SAN.FR", "GLE.FR",
    "ASML.NE", "SHELL.NE", "INGA.NE",
    "NESN.CH", "NOVN.CH", "ABBN.CH",
    "AZN.UK", "HSBA.UK", "BP.UK",
    "IBE.ES", "ITX.ES", "SAN.ES",
    "ENI.IT", "ISP.IT",
)  # fmt: skip

_STOCK_SUFFIXES = frozenset({"GE", "FR", "NE", "CH", "UK", "ES", "IT", "BE", "PO", "SE", "AT", "IE"})
_BOND_WORDS = ("bund", "bobl", "schatz", "btp", "treasury", "bond")


def _root(path: str) -> str:
    return path.split("\\", 1)[0]


def classify_cluster(row: dict) -> str:
    name, path = str(row["name"]), str(row.get("path", ""))
    root = _root(path)
    if root.startswith("CFD Forward"):
        desc = str(row.get("description", "")).lower()
        return "BONDS" if any(k in desc for k in _BOND_WORDS) else "FUTURES_DATED"
    if "Shares" in root or root.startswith("CFD Swe") or ("." in name and name.rsplit(".", 1)[-1] in _STOCK_SUFFIXES):
        return "STOCKS"
    if name in _SOFT_NAMES:
        return "COMMODITY_SOFT"
    if name in _METAL_NAMES or root == "Metals":
        return "METALS"
    if name in _ENERGY_NAMES:
        return "ENERGY"
    if root == "Cash Indices":
        return "INDEX"
    if root == "Cryptocurrency":
        return "CRYPTO"
    if root == "Forex":
        return "FX"
    if root == "Spot Energy":
        return "ENERGY"
    return "OTHER"


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    cluster: str
    rule: str


_PROBED_CLUSTERS = frozenset({"INDEX", "FX", "METALS", "ENERGY", "CRYPTO", "COMMODITY_SOFT"})


def select_candidates(listing: list[dict]) -> list[Candidate]:
    """Deterministic probe selection (see module docstring)."""
    out: list[Candidate] = []
    blue = set(STOCK_BLUE_CHIPS)
    for row in listing:
        if int(row.get("trade_mode") or 0) != TRADE_MODE_FULL:
            continue
        cluster = classify_cluster(row)
        if cluster in _PROBED_CLUSTERS:
            out.append(Candidate(row["name"], cluster, "cash_cfd_non_stock"))
        elif cluster == "STOCKS" and row["name"] in blue:
            out.append(Candidate(row["name"], cluster, "stock_bluechip_whitelist"))
    return sorted(out, key=lambda c: (c.cluster, c.name))


def unprobed_reason(row: dict, selected: set[str]) -> str | None:
    """Why a listed symbol was not probed (None when it was selected)."""
    if row["name"] in selected:
        return None
    if int(row.get("trade_mode") or 0) != TRADE_MODE_FULL:
        return f"trade_mode_{row.get('trade_mode')}_not_full"
    if classify_cluster(row) in ("FUTURES_DATED", "BONDS"):
        return "dated_futures_contract_expiry_roll"
    if classify_cluster(row) == "STOCKS":
        return "not_sampled_stock_cfd"
    return "not_selected"


# ---------------------------------------------------------------- EUR conversion / assessment


def eur_rate(ccy: str, mids: dict[str, float]) -> float | None:
    """EUR per 1 unit of ``ccy`` from observed FX mids (``{'EURUSD': 1.17, ...}``); None when not derivable."""
    if ccy == "EUR":
        return 1.0
    if f"EUR{ccy}" in mids:
        return 1.0 / mids[f"EUR{ccy}"]
    if f"{ccy}EUR" in mids:
        return mids[f"{ccy}EUR"]
    usd_per_eur = mids.get("EURUSD")
    if usd_per_eur is None:
        return None
    if ccy == "USD":
        return 1.0 / usd_per_eur
    if f"USD{ccy}" in mids:
        return (1.0 / mids[f"USD{ccy}"]) / usd_per_eur
    if f"{ccy}USD" in mids:
        return mids[f"{ccy}USD"] / usd_per_eur
    return None


@dataclass(frozen=True, slots=True)
class Thresholds:
    quote_max_age_s: float = 900.0
    m5_min_bars: int = 2000
    m5_min_span_days: float = 10.0
    m1_min_bars: int = 1000
    spread_p50_max_frac: float = 0.003
    spread_p95_max_frac: float = 0.010
    max_leverage: float = 30.0 * 1.02  # broker FX conversion vs our mid drift tolerance; account cap is 30
    min_leverage: float = 0.9


_WIDE_OK = Thresholds(spread_p50_max_frac=0.010, spread_p95_max_frac=0.030)


def thresholds_for(cluster: str) -> Thresholds:
    """Cluster-normalised spread gates (fraction of price). Crypto / metals / energies / softs structurally trade
    wider than FX and index CFDs, so their median gate is 1.0 % (p95 3.0 %) instead of 0.3 % (p95 1.0 %)."""
    return _WIDE_OK if cluster in ("CRYPTO", "METALS", "ENERGY", "COMMODITY_SOFT") else Thresholds()


def estimate_server_offset_s(probes: dict[str, dict]) -> int:
    """Server clock minus UTC in seconds: median of near-fresh ticks rounded to the hour (observed +2 h)."""
    vals = [
        p["tick_now"]["tick_time_minus_utc_s"]
        for p in probes.values()
        if p.get("tick_now") and abs(p["tick_now"]["tick_time_minus_utc_s"]) < 4 * 3600
    ]
    if not vals:
        return 7200
    return round(statistics.median(vals) / 3600.0) * 3600


def assess(
    row: dict, probe: dict, *, mids: dict[str, float], server_offset_s: int, th: Thresholds | None = None
) -> dict:
    """SHADOW technical readiness of one probed symbol. Never raises on missing data; reasons are codes."""
    th = th or thresholds_for(classify_cluster(row))
    reasons: list[str] = []
    retryable: list[str] = []
    info = probe.get("symbol_info") or {}
    facts: dict[str, Any] = {}
    if probe.get("error") or not info:
        return {"verdict": "NOT_READY", "reasons": [probe.get("error") or "symbol_info_missing"], "retryable_reasons": [], "facts": facts}
    tm = int(info.get("trade_mode", row.get("trade_mode", 0)) or 0)
    if tm != TRADE_MODE_FULL:
        reasons.append(f"trade_mode_{tm}_not_full")
    for key in ("point", "trade_tick_size", "trade_contract_size", "volume_min", "volume_step"):
        if not float(info.get(key) or 0) > 0:
            reasons.append(f"contract_data_{key}_invalid")
    tick = probe.get("tick_now")
    price = None
    m5, m1 = probe.get("rates_m5") or {}, probe.get("rates_m1") or {}
    if not tick or not tick.get("bid") or not tick.get("ask") or tick["ask"] < tick["bid"]:
        if m5.get("available") and m1.get("available"):
            retryable.append("no_quote_since_market_watch_selection_market_closed_recheck_when_open")
        else:
            reasons.append("no_valid_quote")
    else:
        price = 0.5 * (tick["bid"] + tick["ask"])
        age = server_offset_s - tick["tick_time_minus_utc_s"]
        facts["quote_age_s"] = age
        if age > th.quote_max_age_s:
            retryable.append(f"quote_stale_{int(age)}s_market_closed_or_halted")
    span_days = None
    if m5.get("available"):
        span_days = (m5["last_bar_server_epoch"] - m5["first_bar_server_epoch"]) / 86400.0
    facts["m5_bars"], facts["m5_span_days"], facts["m1_bars"] = m5.get("n", 0), span_days, m1.get("n", 0)
    if m5.get("n", 0) < th.m5_min_bars or (span_days or 0) < th.m5_min_span_days:
        reasons.append(f"history_m5_insufficient_n{m5.get('n', 0)}_span{(span_days or 0):.1f}d")
    if m1.get("n", 0) < th.m1_min_bars:
        reasons.append(f"history_m1_insufficient_n{m1.get('n', 0)}")
    point = float(info.get("point") or 0)
    ref_price = price or float((probe.get("calc") or {}).get("calc_price") or 0) or float(m5.get("last_close") or 0)
    if m5.get("available") and ref_price > 0 and point > 0:
        sp = m5["spread_points"]
        f50, f95 = sp["median"] * point / ref_price, sp["p95"] * point / ref_price
        facts["spread_median_points"], facts["spread_p95_points"] = sp["median"], sp["p95"]
        facts["spread_median_frac"], facts["spread_p95_frac"] = f50, f95
        if f50 > th.spread_p50_max_frac:
            reasons.append(f"spread_median_{f50:.4%}_of_price_too_wide")
        elif f95 > th.spread_p95_max_frac:
            reasons.append(f"spread_p95_{f95:.4%}_of_price_too_wide")
    ccy_profit, ccy_margin = str(info.get("currency_profit", "")), str(info.get("currency_margin", ""))
    facts["currency_profit"], facts["currency_margin"] = ccy_profit, ccy_margin
    rate = eur_rate(ccy_margin or ccy_profit, mids)
    facts["eur_per_margin_ccy"] = rate
    facts["eur_conversion_feasible"] = rate is not None and eur_rate(ccy_profit, mids) is not None
    if not facts["eur_conversion_feasible"]:
        reasons.append(f"eur_conversion_not_derivable_{ccy_profit}")
    calc = probe.get("calc") or {}
    margin = calc.get("margin_lot_min_buy_acct_ccy")
    facts["margin_min_lot_eur"] = margin
    if not margin or margin <= 0:
        reasons.append("margin_calc_unavailable")
    else:
        calc_price = price or float(calc.get("calc_price") or 0)
        vmin = float(info.get("volume_min") or 0)
        contract = float(info.get("trade_contract_size") or 0)
        if int(info.get("trade_calc_mode") or 0) == 0:  # FOREX calc mode: notional = lots * contract in BASE (= margin) ccy
            notional_eur = vmin * contract * rate if rate else None
        else:  # CFD calc modes: notional = lots * contract * price in the profit ccy
            r_profit = eur_rate(ccy_profit, mids)
            notional_eur = calc_price * contract * vmin * r_profit if r_profit else None
        if notional_eur:
            lev = notional_eur / margin
            facts["implied_leverage"] = lev
            facts["calc_price_source"] = calc.get("calc_price_source")
            if not th.min_leverage <= lev <= th.max_leverage:
                reasons.append(f"implied_leverage_{lev:.2f}_out_of_range")
    facts["swap"] = {"mode": info.get("swap_mode"), "long": info.get("swap_long"), "short": info.get("swap_short")}
    verdict = "SHADOW_READY" if not reasons and not retryable else "NOT_READY"
    return {"verdict": verdict, "reasons": reasons + retryable, "retryable_reasons": retryable, "facts": facts}


_RE_SAFE = re.compile(r"[^A-Za-z0-9_]")


def canonical_for(symbol: str) -> str:
    """Stable upper-case canonical id for a broker symbol ('SAP.GE' -> 'SAP_GE')."""
    return _RE_SAFE.sub("_", symbol).upper()
