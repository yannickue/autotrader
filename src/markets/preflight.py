# ruff: noqa: E501
"""New-market technical preflight (Phase 2, Lane M): deterministic, pure (no MT5 calls, no clock reads).

``run_preflight(canonical, probe)`` consumes the JSON written by ``scripts/phase2_symbol_probe.py``
(attach-only, read-only MT5 probe) plus the checked-in market config, and returns a per-market verdict:
GREEN only if EVERY check PASSes. A check that cannot be decided from the probe data is UNVERIFIED (never
silently PASS) and makes the verdict RED; a check that contradicts the data is FAIL. The reasons are listed.

The preflight never enables anything: it is the technical gate in front of the enablement flag
(``markets.phase2.enabled_market_names``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from markets.phase2 import Phase2Cost, load_phase2_cost, load_phase2_spec, phase2_cost_model
from markets.spec import MAX_LEVERAGE_CAP, MarketSpec

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_TZ = "Europe/Berlin"  # MT5 server clock (inferred, not broker-confirmed; see configs provenance)
SYMBOL_TRADE_MODE_FULL = 4
ORDER_MODE_SL = 16  # SYMBOL_ORDER_SL bit of symbol_info.order_mode
MAX_QUOTE_AGE_S = 30.0  # same as demo.execution.risk_policy.MAX_QUOTE_AGE
DATED_RE = re.compile(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\d{2}$", re.IGNORECASE)
# Reference EUR per USD used only when the probe gives no conversion (same constant as market_costs).
EUR_PER_USD_FALLBACK = 1339.0 / (30369.0 * 0.05)
MAX_POSITION_STOP_RISK_FRACTION = 0.05  # demo.execution.sizing.RiskCaps default (per-trade hard cap)

MARKET_LITERAL_RE = re.compile(
    r"""["'](GER40|NAS100|SPX500|XAUUSD|EURUSD|Ger40|UsaTec|Usa500|GOLD)["']"""
)
# Generic code paths that must not carry market-name literals (persistence / reports / exits / risk ...).
SCAN_DIRS = (
    "src/demo",
    "src/persistence",
    "src/monitoring",
    "src/portfolio",
    "src/health",
    "src/costs",
    "src/risk",
    "src/margin",
    "src/exits",
)
SCAN_EXCLUDE_PARTS = ("src/demo/opportunity", "src/demo/testing.py")
# (path, literal): deliberate, reviewed occurrences: the registries themselves + the EURUSD FX-conversion quote.
SCAN_ALLOW = {
    ("src/demo/execution/market_config.py", "GER40"),
    ("src/demo/execution/market_config.py", "NAS100"),
    ("src/demo/execution/market_config.py", "SPX500"),
    ("src/demo/execution/market_config.py", "XAUUSD"),
    ("src/demo/execution/market_config.py", "EURUSD"),
    ("src/demo/execution/risk_policy.py", "GER40"),
    ("src/demo/execution/risk_policy.py", "NAS100"),
    ("src/demo/execution/risk_policy.py", "SPX500"),
    ("src/demo/execution/risk_policy.py", "XAUUSD"),
    ("src/demo/execution/risk_policy.py", "EURUSD"),
    ("src/demo/execution/live.py", "EURUSD"),  # USD->EUR conversion quote, not a tradable-market whitelist
}


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True, slots=True, kw_only=True)
class Check:
    id: str
    status: Status
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Verdict:
    market: str
    verdict: str  # "GREEN" | "RED"
    checks: tuple[Check, ...]
    reasons: tuple[str, ...]  # one line per non-PASS check
    facts: Mapping[str, Any]  # derived numbers for the report (margin, leverage, spread, feasibility ...)

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "checks": [{"id": c.id, "status": c.status.value, "detail": c.detail} for c in self.checks],
            "facts": dict(self.facts),
        }


def _close(a: Any, b: Any, rel: float = 1e-9) -> bool:
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(fa - fb) <= rel * max(1.0, abs(fa), abs(fb))


def _undecided(probe: Mapping[str, Any], cid: str, detail: str) -> Check:
    """No data for a check: FAIL if a live probe ran and the data is still absent, else UNVERIFIED."""
    live = probe.get("mode") == "probe"
    return Check(id=cid, status=Status.FAIL if live else Status.UNVERIFIED, detail=detail)


def _server_offset_s(at: datetime) -> float:
    off = at.astimezone(ZoneInfo(SERVER_TZ)).utcoffset()
    return off.total_seconds() if off is not None else 0.0


def _retrieved_at(probe: Mapping[str, Any]) -> datetime | None:
    raw = probe.get("retrieved_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ----------------------------------------------------------------------------------------------
# individual checks
# ----------------------------------------------------------------------------------------------
def _c_account(probe: Mapping[str, Any]) -> Check:
    acct = probe.get("account")
    if not acct:
        return Check(id="account_demo_bound", status=Status.UNVERIFIED, detail="no account block in probe data")
    if acct.get("is_demo") is not True:
        return Check(id="account_demo_bound", status=Status.FAIL, detail=f"account is not DEMO (is_demo={acct.get('is_demo')})")
    if acct.get("currency") not in (None, "EUR"):
        return Check(id="account_demo_bound", status=Status.FAIL, detail=f"account currency {acct.get('currency')} != EUR")
    lev = acct.get("leverage")
    if lev is not None and float(lev) > MAX_LEVERAGE_CAP:
        return Check(id="account_demo_bound", status=Status.FAIL, detail=f"account leverage {lev} > {MAX_LEVERAGE_CAP}")
    return Check(id="account_demo_bound", status=Status.PASS, detail=f"DEMO account, currency={acct.get('currency')}, leverage={lev}")


def _c_symbol(spec: MarketSpec, info: Mapping[str, Any] | None) -> Check:
    from nautilus_mt5.symbols import phase2_registry

    if not info:
        return Check(id="symbol_exact_mapped", status=Status.FAIL, detail=f"exact broker symbol {spec.broker_symbol!r} not present in probe data")
    errs: list[str] = []
    if info.get("name") != spec.broker_symbol:
        errs.append(f"name {info.get('name')!r} != {spec.broker_symbol!r}")
    if info.get("path") != spec.broker_path:
        errs.append(f"path {info.get('path')!r} != {spec.broker_path!r}")
    if DATED_RE.search(str(info.get("name", ""))):
        errs.append("dated futures-style symbol")
    mapping = phase2_registry().by_broker_symbol(spec.broker_symbol)
    if mapping is None or mapping.canonical != spec.canonical:
        errs.append("broker symbol not registered for this canonical in nautilus_mt5.symbols")
    elif not str(info.get("path", "")).startswith(mapping.expected_path_prefix):
        errs.append(f"path outside expected category {mapping.expected_path_prefix!r}")
    if errs:
        return Check(id="symbol_exact_mapped", status=Status.FAIL, detail="; ".join(errs))
    return Check(id="symbol_exact_mapped", status=Status.PASS, detail=f"{spec.broker_symbol!r} @ {spec.broker_path!r} ({info.get('description')})")


def _c_tradable(probe: Mapping[str, Any], info: Mapping[str, Any] | None) -> Check:
    if not info or info.get("trade_mode") is None:
        return _undecided(probe, "tradable", "trade_mode unknown")
    if int(info["trade_mode"]) != SYMBOL_TRADE_MODE_FULL:
        return Check(id="tradable", status=Status.FAIL, detail=f"trade_mode={info['trade_mode']} (need {SYMBOL_TRADE_MODE_FULL}=FULL)")
    return Check(id="tradable", status=Status.PASS, detail="trade_mode=FULL")


def _quote_age_s(probe: Mapping[str, Any], tick: Mapping[str, Any]) -> float | None:
    at = _retrieved_at(probe)
    if at is None or tick.get("tick_time_minus_utc_s") is None:
        return None
    # tick.time is the server wall clock encoded as epoch: a fresh tick sits at +offset from UTC now.
    return -(float(tick["tick_time_minus_utc_s"]) - _server_offset_s(at))


def _c_quote(probe: Mapping[str, Any], sym: Mapping[str, Any] | None) -> tuple[Check, float | None]:
    tick = (sym or {}).get("tick_now")
    if not tick:
        return _undecided(probe, "quote_fresh", "no quote: symbol not quoting / not in Market Watch (visible=false at last snapshot)"), None
    bid, ask = float(tick.get("bid", 0)), float(tick.get("ask", 0))
    if not (bid > 0 and ask >= bid):
        return Check(id="quote_fresh", status=Status.FAIL, detail=f"invalid quote bid={bid} ask={ask}"), None
    age = _quote_age_s(probe, tick)
    if age is None:
        return Check(id="quote_fresh", status=Status.UNVERIFIED, detail="quote age not derivable"), ask
    n = ((sym or {}).get("tick_sample") or {}).get("n_distinct_ticks")
    if age > MAX_QUOTE_AGE_S:
        return Check(id="quote_fresh", status=Status.FAIL, detail=f"quote age {age:.0f}s > {MAX_QUOTE_AGE_S:.0f}s (market closed or stale)"), ask
    if n is not None and n < 2:
        return Check(id="quote_fresh", status=Status.FAIL, detail=f"only {n} distinct tick(s) in the sample window"), ask
    return Check(id="quote_fresh", status=Status.PASS, detail=f"age {age:.1f}s, bid={bid} ask={ask}, distinct ticks={n}"), ask


def _c_contract(spec: MarketSpec, info: Mapping[str, Any] | None, eur_per_usd: float) -> Check:
    if not info:
        return Check(id="contract_facts", status=Status.FAIL, detail="no symbol_info")
    pairs = {
        "trade_contract_size": spec.contract_size,
        "trade_tick_size": spec.tick_size,
        "point": spec.point_size,
        "digits": spec.digits,
        "volume_min": spec.volume_min,
        "volume_step": spec.volume_step,
        "volume_max": spec.volume_max,
    }
    errs = [f"{k}: broker {info.get(k)} != spec {v}" for k, v in pairs.items() if not _close(info.get(k), v)]
    for k, v in (("currency_profit", spec.currency_profit), ("currency_margin", spec.currency_margin)):
        if info.get(k) != v:
            errs.append(f"{k}: broker {info.get(k)} != spec {v}")
    tv = info.get("trade_tick_value")
    expected_profit_ccy = spec.contract_size * spec.tick_size
    if tv is None:
        errs.append("trade_tick_value missing")
    else:
        ok_profit = _close(tv, expected_profit_ccy, 0.01)
        ok_acct = spec.currency_profit == "USD" and _close(tv, expected_profit_ccy * eur_per_usd, 0.15)
        if not (ok_profit or ok_acct):
            errs.append(f"trade_tick_value {tv} matches neither contract x tick ({expected_profit_ccy}) nor its EUR value")
    if errs:
        return Check(id="contract_facts", status=Status.FAIL, detail="; ".join(errs))
    return Check(
        id="contract_facts",
        status=Status.PASS,
        detail=(
            f"contract={spec.contract_size}, tick={spec.tick_size}, tick_value={tv}, "
            f"vol {spec.volume_min}/{spec.volume_step}/{spec.volume_max} match the broker"
        ),
    )


def _c_margin(
    probe: Mapping[str, Any], spec: MarketSpec, info: Mapping[str, Any] | None, sym: Mapping[str, Any] | None,
    eur_per_usd: float, facts: dict[str, Any],
) -> Check:
    calc = (sym or {}).get("calc") or {}
    price = calc.get("calc_price")
    m_min = calc.get("margin_lot_min_buy_acct_ccy")
    if not (price and m_min):
        return _undecided(probe, "margin_calc", "order_calc_margin result not available (symbol not quoting / not probed)")
    rate = 1.0 if spec.currency_profit == "EUR" else eur_per_usd
    vmin = spec.volume_min
    notional_min = float(price) * spec.contract_size * vmin * rate
    implied = notional_min / float(m_min)
    facts.update(
        calc_price=float(price), margin_min_lot_eur=float(m_min), notional_min_lot_eur=notional_min, implied_leverage=implied,
        margin_lot_1_eur=calc.get("margin_lot_1.0_buy_acct_ccy"),
    )
    errs: list[str] = []
    if implied > MAX_LEVERAGE_CAP + 1e-9:
        errs.append(f"implied leverage {implied:.2f}x exceeds the hard {MAX_LEVERAGE_CAP}x cap")
    if spec.max_leverage > implied * 1.05:
        errs.append(f"spec max_leverage {spec.max_leverage}x > broker-implied {implied:.2f}x (sizing would understate margin)")
    m_sell = calc.get("margin_lot_min_sell_acct_ccy")
    if m_sell is not None and m_sell > 0 and abs(m_sell - m_min) / m_min > 0.5:
        errs.append(f"buy/sell margin differ a lot ({m_min} vs {m_sell})")
    p_lot = calc.get("profit_lot_1.0_buy_plus100ticks_acct_ccy")
    if p_lot is not None:
        expected = 100 * spec.tick_size * spec.contract_size * rate
        facts["profit_100_ticks_lot_1_eur"] = p_lot
        if not _close(p_lot, expected, 0.15):
            errs.append(f"order_calc_profit {p_lot:.2f} vs contract x ticks x fx {expected:.2f}")
    acct = probe.get("account") or {}
    free = acct.get("margin_free")
    if free:
        facts["min_lot_margin_fraction_of_free_margin"] = float(m_min) / float(free)
        if float(m_min) > 0.9 * float(free):
            errs.append(f"min lot margin {m_min:.0f} EUR exceeds 90% of free margin {free:.0f} EUR")
    if errs:
        return Check(id="margin_calc", status=Status.FAIL, detail="; ".join(errs))
    return Check(
        id="margin_calc",
        status=Status.PASS,
        detail=f"min-lot margin {float(m_min):.2f} EUR at {float(price)}: implied leverage {implied:.2f}x >= spec {spec.max_leverage}x, <= 30x",
    )


def _c_stops(
    probe: Mapping[str, Any], spec: MarketSpec, cost: Phase2Cost, info: Mapping[str, Any] | None,
    sym: Mapping[str, Any] | None, facts: dict[str, Any],
) -> Check:
    if not info or info.get("trade_stops_level") is None or info.get("trade_freeze_level") is None:
        return _undecided(probe, "structural_sl_vs_stops_level", "trade_stops_level / trade_freeze_level unknown")
    stops = float(info["trade_stops_level"]) * spec.point_size
    freeze = float(info["trade_freeze_level"]) * spec.point_size
    tick = (sym or {}).get("tick_now") or {}
    spread = (float(tick["ask"]) - float(tick["bid"])) if tick.get("ask") and tick.get("bid") else 0.0
    ref = cost.reference_structural_stop_price
    facts.update(stops_level_price=stops, freeze_level_price=freeze, reference_structural_stop=ref)
    need = stops + spread
    if ref <= need:
        return Check(
            id="structural_sl_vs_stops_level",
            status=Status.FAIL,
            detail=f"reference structural stop {ref} <= stops_level {stops} + spread {spread:.4f}: a normal structural stop would be rejected",
        )
    return Check(
        id="structural_sl_vs_stops_level",
        status=Status.PASS,
        detail=f"stops_level={stops} freeze_level={freeze} spread={spread:.4f} leave headroom for a structural stop of {ref} (no stop is tightened to fit)",
    )


def _c_protection(probe: Mapping[str, Any], info: Mapping[str, Any] | None) -> Check:
    if not info or info.get("order_mode") is None or info.get("filling_mode") is None:
        return _undecided(probe, "protection_path", "order_mode / filling_mode not exposed in probe data (older snapshot or symbol not probed)")
    om, fm = int(info["order_mode"]), int(info["filling_mode"])
    if not om & ORDER_MODE_SL:
        return Check(id="protection_path", status=Status.FAIL, detail=f"order_mode={om}: stop-loss orders not allowed on this symbol")
    if fm == 0:
        return Check(id="protection_path", status=Status.FAIL, detail="no allowed filling mode (filling_mode=0)")
    return Check(
        id="protection_path",
        status=Status.PASS,
        detail=f"SL allowed (order_mode={om}), filling_mode bitmask={fm}, exemode={info.get('trade_exemode')}; no order_check/order_send was performed by the probe (lane rule) - first live order is reconciled by the existing stack",
    )


def _c_repo_support(spec: MarketSpec) -> Check:
    """Persistence / reconciliation / reports / risk are generic: prove the repo wiring accepts the symbol."""
    from demo.execution.risk_policy import cluster_of
    from nautilus_mt5.symbols import demo_registry

    errs: list[str] = []
    cluster = cluster_of(spec.canonical)
    if cluster is None:
        errs.append("no portfolio cluster (market would be skipped as unknown_cluster)")
    if spec.currency_profit not in ("USD", "EUR"):
        errs.append(f"profit currency {spec.currency_profit} has no account-currency conversion path in the stack")
    try:
        reg = demo_registry(extra_markets=(spec.canonical,))
        m = next((x for x in reg.all() if x.canonical == spec.canonical), None)
        if m is None or m.broker_symbol != spec.broker_symbol or m.research_only:
            errs.append("demo registry mapping missing or inconsistent")
    except KeyError as exc:
        errs.append(f"demo registry: {exc}")
    try:
        from markets.phase2 import demo_market_specs

        ds = demo_market_specs((spec.canonical,))[spec.canonical]
        if ds.broker_symbol != spec.broker_symbol or not (0 < ds.max_leverage <= MAX_LEVERAGE_CAP):
            errs.append("demo market spec inconsistent")
    except Exception as exc:  # any loader failure is a FAIL with the reason
        errs.append(f"demo market spec load failed: {exc}")
    if errs:
        return Check(id="persistence_reconciliation_reports", status=Status.FAIL, detail="; ".join(errs))
    return Check(
        id="persistence_reconciliation_reports",
        status=Status.PASS,
        detail=f"cluster={cluster}, demo registry + demo market spec load; stores/reports/reconciliation are keyed by the registry market name, not a whitelist",
    )


def _hours_covered(spec: MarketSpec, sessions: Mapping[str, Any], at: datetime) -> tuple[list[str], list[int]]:
    """Mon-Fri entry-window hours (calendar tz -> server tz) that have NO observed bars."""
    cal_tz, srv_tz = ZoneInfo(spec.calendar.tz), ZoneInfo(SERVER_TZ)
    day = at.astimezone(cal_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    wd_hours = sessions.get("weekday_hours_server_clock", {})
    missing: list[str] = []
    checked: list[int] = []
    hour_min = spec.calendar.entry_start_min // 60
    hour_max = (spec.calendar.entry_end_min - 1) // 60
    for wd in range(5):
        have = set(wd_hours.get(str(wd), {}).get("hours_with_bars", []))
        for h in range(hour_min, hour_max + 1):
            local = day.replace(hour=h)
            sh = local.astimezone(srv_tz).hour
            checked.append(sh)
            if sh not in have:
                missing.append(f"wd{wd} local {h:02d}:00 (server {sh:02d}:00)")
    return missing, checked


def _c_sessions(probe: Mapping[str, Any], spec: MarketSpec, sym: Mapping[str, Any] | None, facts: dict[str, Any]) -> Check:
    sess = (sym or {}).get("sessions_m5") or {}
    at = _retrieved_at(probe)
    if not sess or not sess.get("weekday_hours_server_clock") or at is None or sess.get("bars", 0) < 1000:
        return _undecided(probe, "session_mapping_verified", "no observed M5 bar structure (need >= 1000 bars): sessions not derivable")
    missing, _ = _hours_covered(spec, sess, at)
    wds = sess.get("weekdays_with_bars", [])
    facts.update(observed_weekdays_with_bars=wds, observed_gap_patterns=dict(list((sess.get("gap_patterns_server_clock") or {}).items())[:10]))
    if missing:
        return Check(id="session_mapping_verified", status=Status.FAIL, detail="entry window not covered by observed bars: " + "; ".join(missing[:8]))
    return Check(
        id="session_mapping_verified",
        status=Status.PASS,
        detail=(
            f"every Mon-Fri entry-window hour has observed bars (weekdays with bars: {wds}; "
            f"calendar stays 'provisional' until broker-confirmed; weekend behaviour as observed, never assumed)"
        ),
    )


def _c_cost(
    probe: Mapping[str, Any], spec: MarketSpec, cost: Phase2Cost, sym: Mapping[str, Any] | None, facts: dict[str, Any]
) -> Check:
    m1 = ((sym or {}).get("rates_m1") or {}).get("spread_points") or {}
    ts = ((sym or {}).get("tick_sample") or {}).get("spread_points") or {}
    if (m1.get("n") or 0) >= 500 and m1.get("median"):
        src, d = "rates_m1_recorded_spread", m1
    elif (ts.get("n") or 0) >= 20 and ts.get("median"):
        src, d = "live_tick_sample", ts
    else:
        return _undecided(probe, "cost_model_enabled", "no observed spread distribution (M1 recorded spread >= 500 bars or >= 20 live ticks)")
    med_p, p95_p = float(d["median"]) * spec.point_size, float(d["p95"]) * spec.point_size
    cap = spec.max_entry_spread_price
    facts.update(spread_source=src, spread_median_price=med_p, spread_p95_price=p95_p, spread_cap_price=cap)
    errs: list[str] = []
    if cap < p95_p:
        errs.append(f"spread cap {cap} < observed p95 {p95_p:.4f}: the cap would reject normal conditions")
    if cap > 10 * med_p:
        errs.append(f"spread cap {cap} > 10x observed median {med_p:.4f}: cap too loose to be a safety bound")
    model = phase2_cost_model(spec, cost, median_spread_price=med_p, spread_source=src)
    facts.update(
        slippage_base_price=model.slippage_base_price,
        one_way_cost_price=model.round_trip_cost_price,
        movement_to_cost_at_reference_stop=model.movement_to_cost_at_reference_stop,
        min_lot_risk_eur_at_reference_stop=model.min_lot_risk_eur_at_reference_stop,
    )
    if model.movement_to_cost_at_reference_stop < cost.min_movement_to_cost:
        errs.append(
            f"movement_to_cost {model.movement_to_cost_at_reference_stop:.2f} < {cost.min_movement_to_cost} at the reference structural stop"
        )
    equity = (probe.get("account") or {}).get("equity")
    if equity:
        cap_eur = MAX_POSITION_STOP_RISK_FRACTION * float(equity)
        facts["per_trade_stop_risk_cap_eur"] = cap_eur
        if model.min_lot_risk_eur_at_reference_stop > cap_eur:
            errs.append(
                f"min lot risks {model.min_lot_risk_eur_at_reference_stop:.0f} EUR at the reference stop > per-trade cap {cap_eur:.0f} EUR: market infeasible on this account"
            )
    if errs:
        return Check(id="cost_model_enabled", status=Status.FAIL, detail="; ".join(errs))
    return Check(
        id="cost_model_enabled",
        status=Status.PASS,
        detail=f"{src}: median {med_p:.4f}, p95 {p95_p:.4f} <= cap {cap}; movement_to_cost {model.movement_to_cost_at_reference_stop:.1f} at the reference stop; commission {cost.commission_source}, swap not modelled (forced flat)",
    )


def scan_hardcoded_markets(repo_root: Path | None = None, extra_files: Iterable[Path] = ()) -> list[str]:
    """Market-name literals in generic code paths (excluding reviewed registry entries)."""
    root = repo_root or REPO_ROOT
    hits: list[str] = []
    files: list[Path] = []
    for d in SCAN_DIRS:
        files.extend(sorted((root / d).rglob("*.py")))
    files.extend(extra_files)
    for f in files:
        rel = f.relative_to(root).as_posix() if f.is_relative_to(root) else f.as_posix()
        if any(rel.startswith(p) for p in SCAN_EXCLUDE_PARTS):
            continue
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for m in MARKET_LITERAL_RE.finditer(line):
                if (rel, m.group(1)) not in SCAN_ALLOW:
                    hits.append(f"{rel}:{lineno}: {m.group(0)}")
    return hits


def _c_hardcoding(spec: MarketSpec, repo_root: Path | None) -> Check:
    hits = scan_hardcoded_markets(repo_root)
    errs = [f"market literal in generic code: {h}" for h in hits[:10]]
    if spec.asset_class in ("index_cfd",):
        errs.append("asset class is an index CFD")
    if errs:
        return Check(id="no_index_specific_hardcoding", status=Status.FAIL, detail="; ".join(errs))
    return Check(
        id="no_index_specific_hardcoding",
        status=Status.PASS,
        detail=f"no market-name literals outside the reviewed registries in {len(SCAN_DIRS)} generic source dirs; spec-driven calendar/cost/sizing",
    )


# ----------------------------------------------------------------------------------------------
# entry points
# ----------------------------------------------------------------------------------------------
def run_preflight(
    canonical: str,
    probe: Mapping[str, Any],
    *,
    config_dir: Path | str | None = None,
    repo_root: Path | None = None,
) -> Verdict:
    spec = load_phase2_spec(canonical, config_dir)
    cost = load_phase2_cost(canonical, config_dir)
    sym = (probe.get("symbols") or {}).get(spec.broker_symbol)
    info = (sym or {}).get("symbol_info") or None
    eur_per_usd = EUR_PER_USD_FALLBACK
    facts: dict[str, Any] = {
        "broker_symbol": spec.broker_symbol,
        "cluster": None,
        "probe_mode": probe.get("mode"),
        "probe_retrieved_at": probe.get("retrieved_at"),
    }
    from demo.execution.risk_policy import cluster_of

    facts["cluster"] = cluster_of(canonical)
    quote_check, _ask = _c_quote(probe, sym)
    checks = (
        _c_account(probe),
        _c_symbol(spec, info),
        _c_tradable(probe, info),
        quote_check,
        _c_contract(spec, info, eur_per_usd),
        _c_margin(probe, spec, info, sym, eur_per_usd, facts),
        _c_stops(probe, spec, cost, info, sym, facts),
        _c_protection(probe, info),
        _c_repo_support(spec),
        _c_sessions(probe, spec, sym, facts),
        _c_cost(probe, spec, cost, sym, facts),
        _c_hardcoding(spec, repo_root),
    )
    reasons = tuple(f"{c.id}: {c.status.value} - {c.detail}" for c in checks if c.status is not Status.PASS)
    return Verdict(
        market=canonical,
        verdict="GREEN" if not reasons else "RED",
        checks=checks,
        reasons=reasons,
        facts=facts,
    )


def calendar_open(spec: MarketSpec, now: datetime) -> bool:
    """MarketSpec calendar (same rule as the runner's closed-market idling): local weekday Mon-Fri and inside the cash
    session. Used to tell 'closed / idle' from a real stale-quote fault."""
    cal = spec.calendar
    local = now.astimezone(ZoneInfo(cal.tz))
    if local.weekday() >= 5:
        return False
    return cal.cash_open_min <= local.hour * 60 + local.minute < cal.cash_close_min


def info_to_dict(info: Any) -> dict[str, Any]:
    """MT5 ``symbol_info`` namedtuple / namespace -> plain dict of the fields the checks read."""
    fields = (
        "name", "path", "description", "trade_mode", "trade_contract_size", "trade_tick_size", "trade_tick_value",
        "point", "digits", "volume_min", "volume_step", "volume_max", "currency_profit", "currency_margin",
        "trade_stops_level", "trade_freeze_level", "order_mode", "filling_mode", "trade_exemode",
    )
    return {k: getattr(info, k) for k in fields if hasattr(info, k)}


def run_live_preflight(
    canonical: str,
    *,
    account: Mapping[str, Any],
    info: Mapping[str, Any] | None,
    bid: float | None,
    ask: float | None,
    quote_age_s: float | None,
    margin_min_buy: float | None,
    margin_min_sell: float | None,
    now: datetime,
    eur_per_usd: float | None = None,
    config_dir: Path | str | None = None,
) -> Verdict:
    """Start-up preflight of ONE enabled Phase-2 market from LIVE broker facts (same checks as ``run_preflight``'s
    runtime-relevant subset): exact symbol mapping, tradable (trade_mode FULL), quote validity + freshness WHILE the
    calendar says the market is open (a closed market with a stale quote is IDLE, not a fault), contract/volume facts,
    order_calc_margin implied leverage (<= 30x and >= the spec leverage), stops_level headroom for the reference
    structural stop, SL/filling allowed, repo wiring. The history-derived checks (session coverage, spread cost model)
    stay with the committed probe evidence. Pure: the caller does the broker reads (and owns the MT5 lane)."""
    spec = load_phase2_spec(canonical, config_dir)
    cost = load_phase2_cost(canonical, config_dir)
    from demo.execution.risk_policy import cluster_of

    probe: dict[str, Any] = {"mode": "probe", "retrieved_at": now.astimezone(UTC).isoformat(), "account": dict(account)}
    eur = EUR_PER_USD_FALLBACK if not eur_per_usd or eur_per_usd <= 0 else float(eur_per_usd)
    sym: dict[str, Any] = {"symbol_info": dict(info or {})}
    if bid is not None and ask is not None:
        sym["tick_now"] = {"bid": bid, "ask": ask}
    if margin_min_buy and ask:
        sym["calc"] = {
            "calc_price": ask,
            "margin_lot_min_buy_acct_ccy": margin_min_buy,
            "margin_lot_min_sell_acct_ccy": margin_min_sell,
        }
    facts: dict[str, Any] = {"broker_symbol": spec.broker_symbol, "cluster": cluster_of(canonical), "probe_mode": "live_start"}
    market_open = calendar_open(spec, now)
    facts["calendar_open"] = market_open
    if not (bid and ask and float(bid) > 0 and float(ask) >= float(bid)):
        quote = Check(id="quote_fresh", status=Status.FAIL, detail=f"invalid or missing quote bid={bid} ask={ask}")
    elif quote_age_s is None:
        quote = Check(id="quote_fresh", status=Status.FAIL, detail="quote age not derivable")
    elif quote_age_s <= MAX_QUOTE_AGE_S:
        quote = Check(id="quote_fresh", status=Status.PASS, detail=f"age {quote_age_s:.1f}s, bid={bid} ask={ask}")
    elif not market_open:
        quote = Check(id="quote_fresh", status=Status.PASS, detail=f"closed/idle: quote age {quote_age_s:.0f}s while the calendar says closed (not a fault)")
    else:
        quote = Check(id="quote_fresh", status=Status.FAIL, detail=f"quote age {quote_age_s:.0f}s > {MAX_QUOTE_AGE_S:.0f}s while the calendar says OPEN")
    checks = (
        _c_account(probe),
        _c_symbol(spec, sym["symbol_info"] or None),
        _c_tradable(probe, sym["symbol_info"] or None),
        quote,
        _c_contract(spec, sym["symbol_info"] or None, eur),
        _c_margin(probe, spec, sym["symbol_info"] or None, sym, eur, facts),
        _c_stops(probe, spec, cost, sym["symbol_info"] or None, sym, facts),
        _c_protection(probe, sym["symbol_info"] or None),
        _c_repo_support(spec),
    )
    reasons = tuple(f"{c.id}: {c.status.value} - {c.detail}" for c in checks if c.status is not Status.PASS)
    return Verdict(market=canonical, verdict="GREEN" if not reasons else "RED", checks=checks, reasons=reasons, facts=facts)


def run_all(probe: Mapping[str, Any], **kw: Any) -> dict[str, Verdict]:
    from markets.phase2 import PHASE2_MARKETS

    return {m: run_preflight(m, probe, **kw) for m in PHASE2_MARKETS}


__all__ = (
    "Check", "Status", "Verdict", "calendar_open", "info_to_dict", "run_all", "run_live_preflight", "run_preflight",
    "scan_hardcoded_markets",
)
