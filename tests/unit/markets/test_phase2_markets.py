# ruff: noqa: E501
"""Lane M: BRENT (ENERGY) + BTCUSD (CRYPTO) integration: registry, clusters, sizing maths, preflight, regression.

Real numbers = the broker facts OBSERVED via symbols_get on the ActivTrades DEMO (docs/evidence/phase2_symbol_probe.json).
Quote/margin/session blocks of the GREEN fixture are SYNTHETIC (clearly named) because the live MT5 probe was blocked by
the running DEMO runner's global lock; they exercise the logic, they are not claims about the broker.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from alpha.common.market_costs import REFERENCE_EUR_PER_USD
from demo.execution.market_config import load_demo_market_specs
from demo.execution.risk_policy import ALL_CLUSTERS, CLUSTERS, PHASE2_CLUSTERS, cluster_of
from demo.execution.sizing import DemoPositionSizer, PortfolioRisk, RiskCaps, SizingInput
from markets import phase2
from markets.preflight import Status, run_all, run_preflight, scan_hardcoded_markets
from markets.spec import CANONICALS, PHASE2_CANONICALS, load_all_specs
from nautilus_mt5.symbols import default_registry, demo_registry, phase2_registry, research_registry

from ._phase2_fingerprint import fingerprint

REPO = Path(__file__).resolve().parents[3]
EVIDENCE = REPO / "docs" / "evidence" / "phase2_symbol_probe.json"
EQUITY = Decimal("96900")  # DEMO account equity (EUR) quoted by the lead
FX = Decimal(str(REFERENCE_EUR_PER_USD))

# Golden fingerprint of the five pre-Phase-2 markets (configs, specs, DEMO specs, clusters, registries, derived costs),
# computed on the base commit d117016 BEFORE any Lane M change.
GOLDEN_FIVE_MARKET_FINGERPRINT = "2e6537a240be9b0d2fa9fb106b5f79783068cd51f01ea613d6a4a7c9639b8a0f"


# ----------------------------------------------------------------------------------------------- registry
def test_phase2_specs_load_with_observed_broker_facts():
    specs = phase2.load_phase2_specs()
    assert tuple(specs) == PHASE2_CANONICALS == ("BRENT", "BTCUSD")
    b, c = specs["BRENT"], specs["BTCUSD"]
    # exact observed symbols (not the dated futures CFDs, not Bitcoin Cash)
    assert (b.broker_symbol, b.broker_path) == ("Brent", "Spot Energy\\Brent")
    assert (c.broker_symbol, c.broker_path) == ("BTCUSD", "Cryptocurrency\\BTCUSD")
    # observed instrument facts
    assert (b.contract_size, b.tick_size, b.volume_min, b.volume_step, b.volume_max) == (1000.0, 0.01, 0.01, 0.01, 10.0)
    assert (c.contract_size, c.tick_size, c.volume_min, c.volume_step, c.volume_max) == (1.0, 0.01, 0.01, 0.01, 3.0)
    for s in (b, c):
        assert s.currency_profit == s.currency_margin == "USD"
        assert s.research_only and not s.trading_enabled
        assert 0 < s.max_leverage <= 30
        assert s.calendar.status == "provisional"  # never claimed broker-confirmed
    assert (b.asset_class, c.asset_class) == ("energy_cfd", "crypto_cfd")
    # BTC: no 24/7 assumption encoded in the calendar
    assert c.calendar.entry_end_min - c.calendar.entry_start_min < 24 * 60
    assert "UNVERIFIED" in c.calendar.weekend_policy


def test_phase2_markets_are_outside_research_canonicals_and_existing_spec_loader():
    assert not set(PHASE2_CANONICALS) & set(CANONICALS)
    assert set(load_all_specs()) == set(CANONICALS)


def test_phase2_registry_lookup_and_default_demo_registry_unchanged():
    reg = phase2_registry()
    assert reg.by_broker_symbol("Brent").canonical == "BRENT"
    assert reg.by_broker_symbol("BTCUSD").expected_path_prefix == "Cryptocurrency"
    assert reg.by_broker_symbol("BCHUSD") is None and reg.by_broker_symbol("BrentDec26") is None
    assert [m.canonical for m in demo_registry().all()] == list(CANONICALS)
    opted = demo_registry(extra_markets=("BRENT",))
    assert [m.canonical for m in opted.all()][-1] == "BRENT"
    assert not any(m.research_only for m in opted.all())
    with pytest.raises(KeyError):
        demo_registry(extra_markets=("DOGEUSD",))


def test_demo_market_specs_for_phase2_come_from_their_own_config():
    both = phase2.demo_market_specs(("BRENT", "BTCUSD"))
    assert both["BRENT"].broker_symbol == "Brent" and both["BRENT"].contract_size == Decimal("1000.0")
    assert both["BTCUSD"].volume_max == Decimal("3.0") and both["BTCUSD"].max_leverage == Decimal("2.0")
    assert set(load_demo_market_specs()) == set(CANONICALS)  # default path unchanged
    with pytest.raises(Exception):  # noqa: B017 - a non-Phase-2 name must not load from this root
        phase2.demo_market_specs(("GER40",))


def test_enablement_defaults_off_and_needs_green_preflight(tmp_path):
    assert phase2.load_enablement() == {"BRENT": False, "BTCUSD": False}
    (tmp_path / "enablement.toml").write_text("[BRENT]\nenabled = true\n[BTCUSD]\nenabled = true\n", encoding="utf-8")
    assert phase2.load_enablement(tmp_path) == {"BRENT": True, "BTCUSD": True}
    # double gate on the REAL live-probe evidence: both flags on, but only the GREEN market passes (Brent is RED:
    # stale quote during its daily break)
    real = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert phase2.enabled_market_names(run_all(real), tmp_path) == ("BTCUSD",)
    # flag off -> never enabled, even when GREEN
    (tmp_path / "enablement.toml").write_text("[BRENT]\nenabled = false\n[BTCUSD]\nenabled = false\n", encoding="utf-8")
    assert phase2.enabled_market_names(run_all(real), tmp_path) == ()
    assert phase2.flag_enabled_markets(tmp_path) == ()
    # missing file -> fail closed
    assert phase2.load_enablement(tmp_path / "nope") == {"BRENT": False, "BTCUSD": False}


# ----------------------------------------------------------------------------------------------- clusters
def test_cluster_mapping_energy_crypto_and_existing_clusters_untouched():
    assert cluster_of("BRENT") == "ENERGY" and cluster_of("BTCUSD") == "CRYPTO"
    assert dict(PHASE2_CLUSTERS) == {"BRENT": "ENERGY", "BTCUSD": "CRYPTO"}
    assert dict(CLUSTERS) == {"GER40": "INDEX", "NAS100": "INDEX", "SPX500": "INDEX", "XAUUSD": "METAL", "EURUSD": "FX"}
    assert dict(ALL_CLUSTERS) == {**CLUSTERS, **PHASE2_CLUSTERS}
    assert cluster_of("NOPE") is None


def _sizing_input(market: str, *, price: str, stop: str, portfolio: PortfolioRisk | None = None, **kw) -> SizingInput:
    s = phase2.load_phase2_spec(market)
    cfg = dict(
        market=market, cluster=cluster_of(market) or "?", family="F", direction=1,
        executable_price=Decimal(price), structural_stop=Decimal(stop),
        contract_size=Decimal(str(s.contract_size)), volume_min=Decimal(str(s.volume_min)),
        volume_step=Decimal(str(s.volume_step)), volume_max=Decimal(str(s.volume_max)),
        fx=FX, equity=EQUITY, target_risk_fraction=Decimal("0.01"),
        instrument_max_leverage=Decimal(str(s.max_leverage)),
        account_leverage=Decimal(30), portfolio=portfolio or PortfolioRisk(),
    )
    cfg.update(kw)
    return SizingInput(**cfg)


def test_cluster_stop_risk_cap_binds_per_cluster_not_globally():
    sizer = DemoPositionSizer()
    caps = RiskCaps()
    cap_eur = EQUITY * caps.max_cluster_stop_risk_fraction  # 6% of equity
    # ENERGY cluster already at its cap -> a further BRENT trade is rejected ...
    full = PortfolioRisk(total=cap_eur, by_cluster={"ENERGY": cap_eur})
    rej = sizer.size(_sizing_input("BRENT", price="70", stop="68.5", portfolio=full))
    assert not rej.accepted and rej.detail["tightest_cap"] == "max_cluster_stop_risk_fraction"
    # ... but the same open risk in the INDEX cluster does not block BRENT (no global one-position rule), nor BTC in CRYPTO
    other = PortfolioRisk(total=cap_eur / 2, by_cluster={"INDEX": cap_eur / 2})
    assert sizer.size(_sizing_input("BRENT", price="70", stop="68.5", portfolio=other)).accepted
    assert sizer.size(_sizing_input("BTCUSD", price="100000", stop="98000", portfolio=other)).accepted
    # partially used ENERGY cluster shrinks the size to the remaining cluster budget
    part = PortfolioRisk(total=cap_eur * Decimal("0.9"), by_cluster={"ENERGY": cap_eur * Decimal("0.9")})
    d = sizer.size(_sizing_input("BRENT", price="70", stop="68.5", portfolio=part))
    assert d.accepted and d.detail["stop_risk_eur"] <= cap_eur * Decimal("0.1") + Decimal("1")


# ----------------------------------------------------------------------------------------------- sizing maths
def test_min_lot_maths_on_observed_contract_numbers():
    # BRENT: 0.01 lot = 10 barrels -> USD 10 per 1.00 price unit; BTCUSD: 0.01 lot = 0.01 BTC -> USD 0.01 per 1.00
    sizer = DemoPositionSizer()
    b = sizer.size(_sizing_input("BRENT", price="70", stop="69", target_risk_fraction=Decimal("0.00001")))
    assert b.accepted and b.quantity == Decimal("0.01")  # minimum lot accepted whatever its actual risk (no fixed-% rule)
    assert b.detail["stop_risk_eur"] == Decimal("0.01") * Decimal(1000) * Decimal(1) * FX
    c = sizer.size(_sizing_input("BTCUSD", price="100000", stop="99000", target_risk_fraction=Decimal("0.00001")))
    assert c.accepted and c.quantity == Decimal("0.01")
    assert c.detail["stop_risk_eur"] == Decimal("0.01") * Decimal(1000) * FX


def test_structural_stop_is_used_as_is_and_size_fits_the_step_grid_and_leverage():
    sizer = DemoPositionSizer()
    for market, price, stop in (("BRENT", "70", "66.2"), ("BTCUSD", "100000", "93000")):
        s = phase2.load_phase2_spec(market)
        d = sizer.size(_sizing_input(market, price=price, stop=stop))
        assert d.accepted
        q = d.quantity
        assert Decimal(str(s.volume_min)) <= q <= Decimal(str(s.volume_max))
        assert (q - Decimal(str(s.volume_min))) % Decimal(str(s.volume_step)) == 0
        assert d.detail["structural_stop"] == Decimal(stop)  # never moved to fit
        notional = q * Decimal(str(s.contract_size)) * Decimal(price) * FX
        assert notional / EQUITY <= Decimal(str(s.max_leverage)) + Decimal("1e-9")  # instrument leverage respected
        assert notional / EQUITY <= 30  # hard ceiling
        assert d.detail["stop_risk_eur"] <= EQUITY * RiskCaps().max_position_stop_risk_fraction


def test_brent_and_btc_feasibility_on_the_100k_account_with_numbers():
    """Min lot risk at structural stops stays far below the per-trade hard cap: neither market is min-lot infeasible."""
    cap = EQUITY * RiskCaps().max_position_stop_risk_fraction  # 4845 EUR
    brent_min_lot_risk_at_5usd_stop = Decimal("0.01") * 1000 * 5 * FX
    btc_min_lot_risk_at_5000usd_stop = Decimal("0.01") * 1 * 5000 * FX
    assert brent_min_lot_risk_at_5usd_stop < cap / 50
    assert btc_min_lot_risk_at_5000usd_stop < cap / 50


def test_leverage_cap_uses_the_assumed_instrument_leverage_not_the_account_leverage():
    # BTC assumed 2x on a 96.9k account with a very tight stop: target risk would need > 2x notional -> leverage cap binds
    d = DemoPositionSizer().size(_sizing_input("BTCUSD", price="100000", stop="99900", target_risk_fraction=Decimal("0.05")))
    assert d.accepted
    assert d.detail["binding_cap"] in ("instrument_max_leverage", "max_leverage", "max_position_stop_risk_fraction")
    notional = d.quantity * 1 * Decimal("100000") * FX
    assert notional <= EQUITY * 2 + Decimal("1")


# ----------------------------------------------------------------------------------------------- cost model
def test_cost_model_wiring_uses_spec_and_config_inputs():
    for m in ("BRENT", "BTCUSD"):
        spec, cost = phase2.load_phase2_spec(m), phase2.load_phase2_cost(m)
        model = phase2.phase2_cost_model(spec, cost)
        assert model.spread_source.startswith("observed_rates_m1")  # Lane M2: the probe's observed median
        assert model.median_spread_price == {"BRENT": 0.06, "BTCUSD": 59.83}[m]
        assert model.eur_per_price_unit_per_lot == pytest.approx(spec.contract_size * REFERENCE_EUR_PER_USD)
        assert model.slippage_base_price < model.slippage_stress_price
        assert model.leverage_cap == min(10.0, spec.max_leverage)
        assert model.movement_to_cost_at_reference_stop >= cost.min_movement_to_cost
        assert not cost.swap_modelled and cost.commission_source == "unverified_assumed_zero"
    # an observed median replaces the placeholder and changes the derived numbers
    spec, cost = phase2.load_phase2_spec("BRENT"), phase2.load_phase2_cost("BRENT")
    a = phase2.phase2_cost_model(spec, cost)
    b = phase2.phase2_cost_model(spec, cost, median_spread_price=0.20, spread_source="rates_m1_recorded_spread")
    assert b.round_trip_cost_price > a.round_trip_cost_price and b.spread_source == "rates_m1_recorded_spread"
    assert phase2.load_phase2_cost("BTCUSD").swap_long == -21.0 and phase2.load_phase2_cost("BRENT").swap_short == -15.252


# ----------------------------------------------------------------------------------------------- preflight
def _green_probe() -> dict:
    """Observed static facts (evidence file) + SYNTHETIC quote/margin/rates/session blocks for a healthy open market."""
    probe = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    probe["mode"] = "probe"
    probe["retrieved_at"] = "2026-09-30T12:00:00+00:00"  # CEST: server clock = UTC+2
    probe["account"] = {"is_demo": True, "currency": "EUR", "leverage": 30, "equity": 96900.0, "margin_free": 96000.0}
    hours = {str(wd): {"hours_with_bars": list(range(24))} for wd in range(5)}
    sessions = {"bars": 12000, "weekdays_with_bars": [0, 1, 2, 3, 4], "weekday_hours_server_clock": hours, "gap_patterns_server_clock": {}}
    synth = {
        "Brent": dict(price=70.0, spread=0.03, m1=(4.0, 8.0), margin=70.0 * 1000 * 0.01 * REFERENCE_EUR_PER_USD / 10.0),
        "BTCUSD": dict(price=100000.0, spread=30.0, m1=(3000.0, 6000.0), margin=100000.0 * 0.01 * REFERENCE_EUR_PER_USD / 2.0),
    }
    for name, s in synth.items():
        sym = probe["symbols"][name]
        info = sym["symbol_info"]
        info.update(order_mode=127, filling_mode=3, trade_exemode=2)
        sym["tick_now"] = {"bid": s["price"], "ask": s["price"] + s["spread"], "time": 0, "tick_time_minus_utc_s": 7198}
        sym["tick_sample"] = {"n_distinct_ticks": 40, "spread_points": {"n": 40, "median": s["m1"][0], "p95": s["m1"][1]}}
        sym["calc"] = {
            "calc_price": s["price"] + s["spread"],
            "margin_lot_min_buy_acct_ccy": s["margin"], "margin_lot_min_sell_acct_ccy": s["margin"],
            "margin_lot_1.0_buy_acct_ccy": s["margin"] * 100,
            "profit_lot_1.0_buy_plus100ticks_acct_ccy": 100 * info["trade_tick_size"] * info["trade_contract_size"] * REFERENCE_EUR_PER_USD,
        }
        sym["rates_m1"] = {"available": True, "n": 5000, "spread_points": {"n": 5000, "median": s["m1"][0], "p95": s["m1"][1]}}
        sym["sessions_m5"] = copy.deepcopy(sessions)
    return probe


def _status(v, cid):
    return next(c.status for c in v.checks if c.id == cid)


def test_preflight_on_the_real_live_probe_brent_red_only_on_stale_quote_btc_green():
    real = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert real["mode"] == "probe"
    verdicts = run_all(real)
    assert verdicts["BTCUSD"].verdict == "GREEN" and verdicts["BTCUSD"].reasons == ()
    brent = verdicts["BRENT"]
    assert brent.verdict == "RED" and len(brent.reasons) == 1 and brent.reasons[0].startswith("quote_fresh: FAIL")
    for m, v in verdicts.items():
        for cid in ("account_demo_bound", "symbol_exact_mapped", "tradable", "contract_facts", "margin_calc",
                    "structural_sl_vs_stops_level", "protection_path", "persistence_reconciliation_reports",
                    "session_mapping_verified", "cost_model_enabled", "no_index_specific_hardcoding"):
            assert _status(v, cid) is Status.PASS, (m, cid)
        assert v.facts["cluster"] == {"BRENT": "ENERGY", "BTCUSD": "CRYPTO"}[m]
    # observed numbers the configs now carry (no placeholder left)
    assert verdicts["BRENT"].facts["implied_leverage"] == pytest.approx(10.0, rel=0.01)
    assert verdicts["BTCUSD"].facts["implied_leverage"] == pytest.approx(2.0, rel=0.01)


def test_preflight_green_on_a_healthy_complete_probe_fixture():
    probe = _green_probe()
    for m, v in run_all(probe).items():
        assert v.verdict == "GREEN", (m, v.reasons)
        assert v.facts["implied_leverage"] == pytest.approx({"BRENT": 10.0, "BTCUSD": 2.0}[m], rel=1e-2)
        assert v.facts["movement_to_cost_at_reference_stop"] > 3


@pytest.mark.parametrize(
    ("market", "mutate", "check_id", "status"),
    [
        ("Brent", lambda s: s["symbol_info"].update(trade_mode=3), "tradable", Status.FAIL),
        ("Brent", lambda s: s["symbol_info"].update(trade_contract_size=100.0), "contract_facts", Status.FAIL),
        ("Brent", lambda s: s["symbol_info"].update(path="CFD Forward 1\\6\\Brent"), "symbol_exact_mapped", Status.FAIL),
        ("BTCUSD", lambda s: s["symbol_info"].update(name="BCHUSD"), "symbol_exact_mapped", Status.FAIL),
        ("Brent", lambda s: s["tick_now"].update(tick_time_minus_utc_s=7198 - 3600), "quote_fresh", Status.FAIL),
        ("Brent", lambda s: s["tick_now"].update(bid=0.0), "quote_fresh", Status.FAIL),
        ("Brent", lambda s: s.update(tick_now=None), "quote_fresh", Status.FAIL),  # live probe ran, no quote -> FAIL
        ("Brent", lambda s: s["symbol_info"].update(trade_stops_level=150), "structural_sl_vs_stops_level", Status.FAIL),
        ("Brent", lambda s: s["symbol_info"].update(order_mode=1), "protection_path", Status.FAIL),
        ("Brent", lambda s: s["symbol_info"].update(filling_mode=0), "protection_path", Status.FAIL),
        ("BTCUSD", lambda s: s["calc"].update(margin_lot_min_buy_acct_ccy=s["calc"]["margin_lot_min_buy_acct_ccy"] * 2.5), "margin_calc", Status.FAIL),
        ("Brent", lambda s: s["calc"].update(margin_lot_min_buy_acct_ccy=s["calc"]["margin_lot_min_buy_acct_ccy"] / 5), "margin_calc", Status.FAIL),  # >30x
        ("Brent", lambda s: s["rates_m1"]["spread_points"].update(p95=40.0), "cost_model_enabled", Status.FAIL),  # cap < p95
        ("Brent", lambda s: s["rates_m1"]["spread_points"].update(median=0.5, p95=1.0), "cost_model_enabled", Status.FAIL),  # cap too loose
        ("Brent", lambda s: s["sessions_m5"]["weekday_hours_server_clock"]["2"]["hours_with_bars"].remove(11), "session_mapping_verified", Status.FAIL),
        ("Brent", lambda s: s.pop("calc"), "margin_calc", Status.FAIL),
    ],
)
def test_preflight_red_reasons_on_contradicting_probe_data(market, mutate, check_id, status):
    probe = _green_probe()
    mutate(probe["symbols"][market])
    canonical = "BRENT" if market == "Brent" else "BTCUSD"
    v = run_preflight(canonical, probe)
    assert v.verdict == "RED"
    assert _status(v, check_id) is status
    assert any(r.startswith(check_id) for r in v.reasons)


def test_preflight_red_when_account_not_demo_and_infeasible_min_lot_reports_numbers():
    probe = _green_probe()
    probe["account"]["is_demo"] = False
    assert _status(run_preflight("BRENT", probe), "account_demo_bound") is Status.FAIL
    # tiny account: min lot at the reference structural stop exceeds the 5% per-trade cap -> reported with numbers
    probe = _green_probe()
    probe["account"]["equity"] = 100.0
    v = run_preflight("BRENT", probe)
    assert _status(v, "cost_model_enabled") is Status.FAIL
    assert "infeasible" in next(r for r in v.reasons if r.startswith("cost_model_enabled")) and v.facts["per_trade_stop_risk_cap_eur"] == 5.0


def test_hardcoding_scan_detects_market_literals_in_generic_paths(tmp_path):
    assert scan_hardcoded_markets() == []  # the real repo is clean (registries + EURUSD FX quote are allow-listed)
    bad = tmp_path / "src" / "persistence"
    bad.mkdir(parents=True)
    (bad / "leak.py").write_text('MARKETS = ("GER40", "NAS100")\n', encoding="utf-8")
    hits = scan_hardcoded_markets(tmp_path)
    assert len(hits) == 2 and all("src/persistence/leak.py:1" in h for h in hits)
    v = run_preflight("BRENT", _green_probe(), repo_root=tmp_path)
    assert _status(v, "no_index_specific_hardcoding") is Status.FAIL and v.verdict == "RED"


# ----------------------------------------------------------------------------------------------- regression
def test_existing_five_markets_are_bit_identical():
    assert fingerprint() == GOLDEN_FIVE_MARKET_FINGERPRINT
    assert [m.canonical for m in default_registry().all()] == ["GER40"]
    assert [m.canonical for m in research_registry().all()] == list(CANONICALS)
