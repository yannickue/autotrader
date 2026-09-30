# ruff: noqa: E501
"""Universe classification, deterministic selection, readiness assessment, build determinism, evidence schema (Lane U)."""

from __future__ import annotations

import copy
import json
import random
import tomllib
from pathlib import Path

import pytest

from instruments.universe import (
    SELECTION_RULE_ID,
    STOCK_BLUE_CHIPS,
    assess,
    canonical_for,
    classify_cluster,
    eur_rate,
    select_candidates,
    unprobed_reason,
)
from instruments.universe_build import (
    ACTIVE_DISCOVERY_UNIVERSE,
    build_inventory,
    render_markdown,
    render_shadow_toml,
)
from markets.shadow import shadow_spec_from_dict

B = "\\"


def _row(name, path, tm=4, desc=""):
    return {"name": name, "path": path, "trade_mode": tm, "description": desc, "visible": False}


LISTING = [
    _row("GBPUSD", f"Forex{B}Majors{B}GBPUSD"),
    _row("Ger40", f"Cash Indices{B}Ger40"),
    _row("BTCUSD", f"Cryptocurrency{B}BTCUSD"),
    _row("Brent", f"Spot Energy{B}Brent"),
    _row("Coffee", f"Spot Energy{B}Coffee"),
    _row("GOLD", f"Metals{B}GOLD"),
    _row("SAP.GE", f"CFD Ger Shares{B}Ger Shares 20{B}SAP.GE"),
    _row("ZZZ.GE", f"CFD Ger Shares{B}Ger Shares 20{B}ZZZ.GE"),
    _row("Ger40Dec26", f"CFD Forward 5{B}6{B}Ger40Dec26", desc="DAX December 2026 CFD"),
    _row("EuBundDec26", f"CFD Forward 1{B}6{B}EuBundDec26", desc="EUROBUND December 2026 CFD"),
    _row("USDHKD", f"Forex{B}Minors{B}USDHKD", tm=0),
    _row("EURTRY", f"Forex{B}Exotics{B}EURTRY", tm=3),
]


def test_classification():
    got = {r["name"]: classify_cluster(r) for r in LISTING}
    assert got == {
        "GBPUSD": "FX", "Ger40": "INDEX", "BTCUSD": "CRYPTO", "Brent": "ENERGY", "Coffee": "COMMODITY_SOFT",
        "GOLD": "METALS", "SAP.GE": "STOCKS", "ZZZ.GE": "STOCKS", "Ger40Dec26": "FUTURES_DATED",
        "EuBundDec26": "BONDS", "USDHKD": "FX", "EURTRY": "FX",
    }  # fmt: skip


def test_selection_rule_and_reasons():
    sel = select_candidates(LISTING)
    names = [c.name for c in sel]
    assert "SAP.GE" in names and "ZZZ.GE" not in names  # whitelist only
    assert not {"Ger40Dec26", "EuBundDec26", "USDHKD", "EURTRY"} & set(names)
    assert {c.name: c.cluster for c in sel}["Coffee"] == "COMMODITY_SOFT"
    chosen = set(names)
    assert unprobed_reason(LISTING[7], chosen) == "not_sampled_stock_cfd"
    assert unprobed_reason(LISTING[8], chosen) == "dated_futures_contract_expiry_roll"
    assert unprobed_reason(LISTING[10], chosen) == "trade_mode_0_not_full"
    assert unprobed_reason(LISTING[0], chosen) is None


def test_selection_is_deterministic_and_order_independent():
    a = select_candidates(LISTING)
    for seed in range(5):
        shuffled = copy.deepcopy(LISTING)
        random.Random(seed).shuffle(shuffled)
        assert select_candidates(shuffled) == a
    assert select_candidates(LISTING) == a
    assert a == sorted(a, key=lambda c: (c.cluster, c.name))
    assert SELECTION_RULE_ID and len(set(STOCK_BLUE_CHIPS)) == len(STOCK_BLUE_CHIPS)


def test_eur_rate():
    mids = {"EURUSD": 1.25, "USDJPY": 150.0, "GBPUSD": 1.5, "EURCHF": 0.8}
    assert eur_rate("EUR", mids) == 1.0
    assert eur_rate("USD", mids) == pytest.approx(0.8)
    assert eur_rate("CHF", mids) == pytest.approx(1.25)
    assert eur_rate("JPY", mids) == pytest.approx(0.8 / 150.0)
    assert eur_rate("GBP", mids) == pytest.approx(1.5 / 1.25)
    assert eur_rate("HKD", mids) is None
    assert eur_rate("USD", {}) is None
    assert canonical_for("SAP.GE") == "SAP_GE"


def _probe(**over):
    p = {
        "symbol": "X",
        "symbol_info": {
            "trade_mode": 4, "point": 0.01, "trade_tick_size": 0.01, "trade_contract_size": 1.0, "volume_min": 0.1,
            "volume_step": 0.1, "currency_profit": "EUR", "currency_margin": "EUR", "trade_calc_mode": 4,
            "swap_mode": 5, "swap_long": -1.0, "swap_short": 0.5, "digits": 2, "volume_max": 100.0,
        },
        "tick_now": {"bid": 100.0, "ask": 100.05, "time": 1, "tick_time_minus_utc_s": 7195},
        "calc": {"calc_price": 100.05, "calc_price_source": "tick_ask", "margin_lot_min_buy_acct_ccy": 0.5},
        "rates_m1": {"available": True, "n": 3000},
        "rates_m5": {
            "available": True, "n": 12000, "first_bar_server_epoch": 0, "last_bar_server_epoch": 30 * 86400,
            "spread_points": {"median": 5, "p95": 10, "max": 50}, "last_close": 100.0,
        },
    }  # fmt: skip
    p.update(over)
    return p


def _assess(p, **kw):
    return assess({"name": "X", "path": f"Cash Indices{B}X", "trade_mode": 4}, p, mids={"EURUSD": 1.1}, server_offset_s=7200, **kw)


def test_assess_ready():
    a = _assess(_probe())
    assert a["verdict"] == "SHADOW_READY", a["reasons"]
    assert a["facts"]["implied_leverage"] == pytest.approx(0.1 * 100.025 / 0.5)
    assert a["facts"]["eur_conversion_feasible"] is True


def test_assess_stale_quote_is_retryable_not_a_hard_failure():
    p = _probe()
    p["tick_now"]["tick_time_minus_utc_s"] = -20000
    a = _assess(p)
    assert a["verdict"] == "NOT_READY"
    assert a["retryable_reasons"] and a["retryable_reasons"] == a["reasons"]


def test_assess_hard_failures():
    p = _probe()
    p["rates_m5"]["n"] = 100
    assert any(r.startswith("history_m5") for r in _assess(p)["reasons"])
    p = _probe()
    p["rates_m5"]["spread_points"] = {"median": 500, "p95": 900, "max": 900}
    assert any(r.startswith("spread_median") for r in _assess(p)["reasons"])
    p = _probe()
    p["symbol_info"]["trade_mode"] = 3
    assert any("trade_mode_3" in r for r in _assess(p)["reasons"])
    p = _probe()
    p["calc"]["margin_lot_min_buy_acct_ccy"] = 0.0001  # leverage absurd
    assert any(r.startswith("implied_leverage") for r in _assess(p)["reasons"])
    p = _probe()
    p["symbol_info"]["currency_profit"] = p["symbol_info"]["currency_margin"] = "HKD"
    assert any(r.startswith("eur_conversion") for r in _assess(p)["reasons"])
    assert _assess({"symbol": "X", "error": "symbol_info None"})["verdict"] == "NOT_READY"


def test_assess_forex_calc_mode_uses_base_currency_notional():
    p = _probe()
    p["symbol_info"].update(trade_calc_mode=0, trade_contract_size=100000.0, volume_min=0.01, currency_profit="JPY", currency_margin="EUR")
    p["tick_now"].update(bid=170.0, ask=170.01)
    p["calc"]["margin_lot_min_buy_acct_ccy"] = 1000.0 / 30.0
    a = assess(
        {"name": "EURJPY", "path": f"Forex{B}Majors{B}EURJPY", "trade_mode": 4}, p,
        mids={"EURUSD": 1.1, "USDJPY": 155.0}, server_offset_s=7200,
    )  # fmt: skip
    assert a["facts"]["implied_leverage"] == pytest.approx(30.0, rel=1e-6)
    assert a["verdict"] == "SHADOW_READY", a["reasons"]


# ---- build determinism + evidence schema --------------------------------------------------------------------


def _docs():
    sel = select_candidates(LISTING)
    listing_doc = {"retrieved_at": "t0", "account_currency": "EUR", "symbols": LISTING}
    probes = {c.name: _probe() for c in sel}
    raw = {"retrieved_at": "t1", "selection": [{"name": c.name, "cluster": c.cluster, "rule": c.rule} for c in sel], "probes": probes}
    return listing_doc, raw


def test_build_is_deterministic_and_shadow_spec_renders_loadable_toml(tmp_path: Path):
    listing_doc, raw = _docs()
    a, b = build_inventory(listing_doc, raw), build_inventory(copy.deepcopy(listing_doc), copy.deepcopy(raw))
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    assert render_markdown(a) == render_markdown(b)
    row = next(r for r in a["symbols"] if r["symbol"] == "GBPUSD")
    assert row["universe"] == "SHADOW"
    text = render_shadow_toml(row, a["server_utc_offset_s"], "2026-09-30")
    row_bad = dict(row, verdict="SHADOW_READY")
    assert render_shadow_toml(row_bad, 7200, "x") == render_shadow_toml(row_bad, 7200, "x")
    # GBPUSD is a production-neutral name: parses as a valid shadow spec
    spec = shadow_spec_from_dict(tomllib.loads(text))
    assert spec.tradable is False and spec.broker_symbol == "GBPUSD"


def test_active_universe_is_the_seven_named_markets():
    assert ACTIVE_DISCOVERY_UNIVERSE == {
        "GER40": "Ger40", "NAS100": "UsaTec", "SPX500": "Usa500", "XAUUSD": "GOLD", "EURUSD": "EURUSD",
        "BTCUSD": "BTCUSD", "BRENT": "Brent",
    }  # fmt: skip


def test_evidence_json_schema():
    path = Path(__file__).resolve().parents[3] / "docs" / "evidence" / "universe_inventory.json"
    inv = json.loads(path.read_text(encoding="utf-8"))
    assert inv["schema_version"] == 1 and inv["selection_rule_id"] == SELECTION_RULE_ID
    assert set(inv["active_discovery_universe"]) == set(ACTIVE_DISCOVERY_UNIVERSE)
    cnt = inv["counts"]
    assert cnt["listed"] == len(inv["symbols"])
    assert cnt["shadow_ready"] + cnt["not_ready"] == cnt["listed"]
    assert sum(cnt["by_cluster"].values()) == cnt["listed"]
    need = {"symbol", "canonical", "description", "path", "cluster", "universe", "trade_mode", "probed", "verdict", "reasons"}
    for r in inv["symbols"]:
        assert need <= set(r)
        assert r["verdict"] in ("SHADOW_READY", "NOT_READY")
        assert (r["verdict"] == "SHADOW_READY") == (not r["reasons"])
        if r["verdict"] == "NOT_READY":
            assert r["reasons"]
        if r["probed"]:
            assert {"contract", "facts", "history", "sessions_server_clock", "readiness_pending_quote"} <= set(r)
            for k in ("digits", "point", "trade_tick_size", "trade_contract_size", "volume_min", "volume_max", "volume_step"):
                assert k in r["contract"]
    ready = [r for r in inv["symbols"] if r["verdict"] == "SHADOW_READY"]
    assert len(ready) == cnt["shadow_ready"]
