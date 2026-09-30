# ruff: noqa: E501
"""Lane I: runner <-> real-stack interface (context, risk_detail, TCA, cost semantics), factory, funnel."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from demo import runner as rn
from demo.execution.events import (
    Accepted,
    Fill,
    PositionClosed,
    ProtectionConfirmed,
    Rejected,
)
from demo.funnel import funnel, render
from demo.store import CANCELLED, CLOSED, PROTECTED, RISK_REJECTED
from demo.testing import M5, T0, FakeBarSource, FakeClock, FakeStack, floor5, make_pair

D = Decimal


def _cycle(r):
    r.start()
    r.run_cycle()


def _rich_events(intent, *, commission="-1.0"):
    iid = intent.intent_id
    detail = {
        "decision": "TRADE", "structural_stop": D("98.5"), "stop_distance": D("1.55"),
        "broker_min_lot": D("0.01"), "quantity": D("1"), "stop_risk_eur": D("15.5"),
        "equity_risk_fraction": D("0.0015"), "portfolio_risk_before": D("0"),
        "portfolio_risk_after": D("15.5"), "cluster": "INDEX", "family": "orb",
        "signal_inputs": {"confluence": 2}, "win_probability": None, "policy_id": "p",
    }
    fill = Fill(
        iid, D("100.05"), D("1"), D("0.1"), D("0.05"), D(commission), D("0"), "ord-" + iid, "pos-" + iid,
        intended_price=D("100"), reference_price=D("100.1"), bid_at_send=D("100"), ask_at_send=D("100.1"),
        slippage_vs_intended=D("0.05"), fill_vs_mid=D("0.0"), fees_price_units=D("0.01"),
        cost_price_units=D("0.16"), movement_to_cost=D("18.4"), latency_total_ms=41.5,
        latency_send_to_fill_ms=30.0,
    )
    return [
        Accepted(iid, D("1"), D("10000"), D("0.0015"), D("15"), D("2"), risk_detail=detail),
        fill,
        ProtectionConfirmed(iid, "pos-" + iid, D(str(intent.stop)), None if intent.target is None else D(str(intent.target))),
    ]


# ------------------------------------------------------------------------ context -> stack.submit
def test_submit_carries_context_family_quality_and_no_win_probability_when_absent(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    _cycle(env.build())
    ctx = env.stack.contexts[intent.intent_id]
    assert ctx["family"] == "orb" and ctx["confluence"] == 2 and ctx["quality"] == 0.5
    assert ctx["atr"] == 2.0
    assert ctx["win_probability"] is None and ctx["expected_payoff_r"] is None
    assert ctx["signal"]["strategy_id"] == "orb-a" and "independent_clusters" in ctx["signal"]


def test_context_takes_win_probability_and_expected_payoff_from_shadow_only(env):
    snap, dec, intent = make_pair()
    dec = dataclasses.replace(dec, shadow={
        "b_model": {"status": "ok", "p_target_before_stop": 0.61, "expected_r": 0.4},
        "a_model": {"status": "untrained", "p_target_before_stop": None, "expected_r": None},
    })
    env.engine.push("GER40", (snap, dec, intent))
    _cycle(env.build())
    ctx = env.stack.contexts[intent.intent_id]
    assert ctx["win_probability"] == pytest.approx(0.61) and ctx["expected_payoff_r"] == pytest.approx(0.4)
    assert intent.risk_fraction == 0.01  # the intent (sizing input) is untouched by the estimates


# ---------------------------------------------------------------- risk_detail + TCA + cost semantics
def test_accepted_risk_detail_tca_and_verified_cost_no_double_counting(env):
    snap, dec, intent = make_pair()
    iid = intent.intent_id
    env.stack.script[iid] = _rich_events(intent)
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    _cycle(r)
    st = env.store
    assert st.get_state(iid) == PROTECTED
    acc = st.get_risk_detail(iid, "ACCEPTED")
    assert acc["cluster"] == "INDEX" and acc["stop_risk_eur"] == "15.5" and acc["signal_inputs"] == {"confluence": 2}
    assert "win_probability" in acc and acc["win_probability"] is None
    tca = st.get_tca(iid, "ENTRY")
    assert tca["slippage_vs_intended"] == pytest.approx(0.05) and tca["fill_vs_mid"] == 0.0
    assert tca["fees_price_units"] == pytest.approx(0.01) and tca["cost_price_units"] == pytest.approx(0.16)
    assert tca["movement_to_cost"] == pytest.approx(18.4) and tca["latency_total_ms"] == 41.5
    assert tca["latency_send_to_ack_ms"] is None
    # entry fee -1.0 (Fill); closing deal -0.6 commission, -0.2 swap (PositionClosed = closing deal only)
    env.stack.pending.append(PositionClosed(
        iid, "pos-" + iid, "TARGET", exit_price=D("103.0"), exit_quantity=D("1"),
        closed_utc=(env.clock() + timedelta(minutes=30)).isoformat(), commission=D("-0.6"), swap=D("-0.2"),
        profit_eur=D("2.95"), net_pnl_eur=D("1.15"), exit_slippage_vs_level=D("0.0"), holding_seconds=1800.0,
    ))
    env.stack.positions.pop(iid, None)
    env.clock.advance(minutes=35)
    r.run_cycle()
    assert st.get_state(iid) == CLOSED
    ex = st.get_execution(iid)
    assert ex.cost_status == "verified" and ex.fees == pytest.approx(-1.6) and ex.swap == pytest.approx(-0.2)
    assert st.execution_history(iid)[0].fees == pytest.approx(-1.0)  # entry record stays entry-only
    out = st.get_outcome(iid)
    assert out.pnl_eur == pytest.approx(2.95 - 1.6 - 0.2)
    exit_tca = st.get_tca(iid, "EXIT")
    assert exit_tca["total_commission_eur"] == pytest.approx(-1.6) and exit_tca["cost_status"] == "verified"
    assert exit_tca["broker_net_pnl_eur"] == pytest.approx(1.15) and exit_tca["computed_pnl_eur"] == pytest.approx(1.15)
    assert not [w for w in r._warnings if w.startswith("pnl_mismatch")]
    # a replayed close event must not change anything (no double counting)
    r._handle_events([PositionClosed(iid, "pos-" + iid, "TARGET", exit_price=D("103.0"), exit_quantity=D("1"),
                                     commission=D("-0.6"), swap=D("-0.2"))], env.clock())
    assert st.get_execution(iid).fees == pytest.approx(-1.6) and st.get_outcome(iid) == out


def test_cost_status_stays_provisional_without_broker_closing_costs(env):
    snap, dec, intent = make_pair()
    iid = intent.intent_id
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    _cycle(r)
    env.stack.pending.append(PositionClosed(
        iid, "pos-" + iid, "STOP", exit_price=D("98.5"), exit_quantity=D("1"),
        closed_utc=(env.clock() + timedelta(minutes=30)).isoformat(), commission=None, swap=None))
    env.clock.advance(minutes=35)
    r.run_cycle()
    ex = env.store.get_execution(iid)
    assert ex.cost_status == "provisional" and ex.fees == pytest.approx(-1.0)  # entry deal only, flagged
    assert env.store.get_outcome(iid) is not None
    assert any(w.startswith("cost_status_provisional") for w in r._warnings)
    assert env.store.get_tca(iid, "EXIT")["cost_status"] == "provisional"


def test_rejected_before_sizing_records_exact_code_class_and_detail(env):
    snap, dec, intent = make_pair()
    iid = intent.intent_id
    det = {"decision": "SKIP", "reject_code": "size_below_min", "gate_reject_class": "SAFETY",
           "violated_cap": "max_position_stop_risk_fraction", "cap_limit": D("0.005"),
           "observed_at_min_lot": D("0.009"), "equity": D("10000"), "target_risk_fraction": D("0.005")}
    env.stack.script[iid] = [Rejected(iid, "size_below_min", det)]
    env.engine.push("GER40", (snap, dec, intent))
    _cycle(env.build())
    st = env.store
    assert st.get_state(iid) == RISK_REJECTED
    risk = st.get_risk(iid)
    assert not risk.approved and risk.reject_reason == "size_below_min" and risk.equity == 10000.0
    rows = st.list_risk_details("DISCOVERY", "REJECTED")
    assert [(x["reject_code"], x["gate_class"], x["opportunity_id"]) for x in rows] == [
        ("size_below_min", "SAFETY", snap.opportunity_id)]
    assert rows[0]["detail"]["violated_cap"] == "max_position_stop_risk_fraction"
    ev = [e for e in st.intent_events(iid) if e["to_state"] == RISK_REJECTED][0]
    assert ev["detail"] == {"reason": "size_below_min", "gate_class": "SAFETY"}


def test_rejected_after_sizing_keeps_accepted_and_rejected_detail(env):
    snap, dec, intent = make_pair()
    iid = intent.intent_id
    env.stack.script[iid] = [
        Accepted(iid, D("1"), D("10000"), D("0.0015"), D("15"), D("2"), risk_detail={"decision": "TRADE", "x": 1}),
        Rejected(iid, "broker_reject", {"decision": "SKIP", "reject_code": "broker_reject", "gate_reject_class": "SAFETY", "x": 1}),
    ]
    env.engine.push("GER40", (snap, dec, intent))
    _cycle(env.build())
    st = env.store
    assert st.get_state(iid) == CANCELLED and st.get_risk(iid).approved
    assert st.get_risk_detail(iid, "ACCEPTED")["decision"] == "TRADE"
    assert st.get_risk_detail(iid, "REJECTED")["reject_code"] == "broker_reject"


# ------------------------------------------------------------------------------------- funnel
def _mixed_env(env):
    """engine reject (legacy), engine reject (structural), traded, ADDON blocked, safety reject."""
    t = floor5(env.clock())
    legacy = make_pair(tag="l", accepted=False, signal_ts=t)
    legacy = (legacy[0], dataclasses.replace(legacy[1], reasons=("SPACE_BELOW_MIN_R",)), None)
    structural = make_pair(tag="s", accepted=False, signal_ts=t)
    structural = (structural[0], dataclasses.replace(structural[1], reasons=("ENTRY_OVERSHOT", "SPACE_BELOW_MIN_R")), None)
    ok = make_pair(tag="ok", signal_ts=t)
    addon = make_pair(tag="ad", signal_ts=t)
    safe = make_pair(tag="sf", signal_ts=t, market="GER40")
    env.stack.script[addon[2].intent_id] = [
        Accepted(addon[2].intent_id, D("1")),
        Rejected(addon[2].intent_id, "ADDON_EXPOSURE_NOT_SUPPORTED_V1", {"otherwise_valid": True}),
    ]
    env.stack.script[safe[2].intent_id] = [Rejected(safe[2].intent_id, "spread_cap", {"reject_code": "spread_cap"})]
    env.engine.push("GER40", legacy, structural, ok, addon, safe)
    r = env.build()
    _cycle(r)
    return r, ok, addon, safe


def test_funnel_combines_engine_and_stack_rejections_by_class_market_family(env):
    r, ok, addon, safe = _mixed_env(env)
    f = funnel(env.store, env.stack, "DISCOVERY")
    s = f["summary"]
    assert s["opportunities"] == 5 and s["engine_accepted"] == 3 and s["engine_rejected"] == 2
    assert f["engine"]["by_reason"] == {"SPACE_BELOW_MIN_R": 2, "ENTRY_OVERSHOT": 1}
    assert f["engine"]["by_class"] == {"LEGACY_ARBITRARY": 2, "STRUCTURAL": 1}
    assert f["engine"]["rejected_only_by_non_hard_classes"] == 1  # only the pure-legacy one
    assert f["stack"]["by_class"] == {"TEMPORARY": 1, "SAFETY": 1}
    assert f["stack"]["by_code"] == {"ADDON_EXPOSURE_NOT_SUPPORTED_V1": 1, "spread_cap": 1}
    assert f["stack"]["temporary_limitation"]["otherwise_valid_blocked"] == 1
    assert "ADDON_EXPOSURE_NOT_SUPPORTED_V1" in f["stack"]["temporary_limitation"]["lifting_conditions"]
    w = f["trades_that_would_have_existed"]
    assert w["actual"] == 1 and w["without_temporary_limitations"] == 2
    assert w["engine_side_without_legacy_quality_temporary"] == 3 + 1
    assert f["by_market"]["GER40"]["opportunities"] == 5 and f["by_family"]["orb"]["traded"] == 1
    assert f["stack_live"] == env.stack.rejection_funnel()
    assert "TEMPORARY_LIMITATION" in f["stack_live"]
    text = render(f)
    assert "REJECTION FUNNEL" in text and "spread_cap=1" in text
    # heartbeat carries the top-level summary
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    assert hb["rejection_funnel"]["engine_accepted"] == 3 and hb["rejection_funnel"]["traded"] == 1
    assert hb["rejection_funnel"]["stack_by_class"] == {"TEMPORARY": 1, "SAFETY": 1}


def test_funnel_in_milestone_report_and_analyze(env, tmp_path):
    from demo import monitor
    from demo.report import build_report, render_markdown

    _mixed_env(env)
    rep = build_report(env.store, "DISCOVERY")
    assert rep["rejection_funnel"]["summary"]["stack_rejected"] == 2
    assert "## Rejection funnel" in render_markdown(rep)
    out = monitor.analyze(env.store, "DISCOVERY", tmp_path / "rep", tmp_path / "exp")
    assert out["rejection_funnel"]["engine_accepted"] == 3
    assert (tmp_path / "rep" / "funnel-DISCOVERY.json").is_file()


def test_funnel_on_empty_store(env):
    f = funnel(env.store, None)
    assert f["summary"]["opportunities"] == 0 and f["stack_live"] is None and f["by_market"] == {}


# --------------------------------------------------------------------------------------- factory
def test_factory_shadow_and_demo_auto_modes_via_injected_stack(tmp_path, monkeypatch):
    monkeypatch.delenv("MT5_ALLOW_ACCOUNT_LOGIN", raising=False)
    calls = []

    def factory(**kw):
        calls.append(kw)
        return FakeStack(FakeClock(T0), markets=kw["markets"], shadow=kw["dry_run"])

    for mode, dry in (("shadow", True), ("demo-auto", False)):
        r = rn.build_live_runner(mode, artifacts_dir=tmp_path / mode, stack_factory=factory)
        assert calls[-1]["dry_run"] is dry and Path(calls[-1]["state_dir"]) == tmp_path / mode / "stack"
        assert r.engine._source is r.stack.bar_source  # no adapter in between
        assert r.cfg.mode == mode
        r.store.close()


def test_factory_refuses_account_login(tmp_path, monkeypatch):
    monkeypatch.setenv("MT5_ALLOW_ACCOUNT_LOGIN", "1")
    with pytest.raises(rn.LiveStackRefused):
        rn.build_live_runner("shadow", artifacts_dir=tmp_path, stack_factory=lambda **k: None)


def test_factory_real_path_wires_client_config_state_dir_dry_run(tmp_path, monkeypatch):
    """The ONLY place that touches the real client - here every real object is a sentinel."""
    monkeypatch.delenv("MT5_ALLOW_ACCOUNT_LOGIN", raising=False)
    import adapters.activtrades_mt5.real_client as rc
    import adapters.config as cfgmod
    import demo.execution.live as live

    client, conn = object(), object()
    seen = []

    class RecStack:
        def __init__(self, **kw):
            seen.append(kw)
            self.bar_source = FakeBarSource(FakeClock(T0), tuple(kw["market_specs"]))

    monkeypatch.setattr(rc, "get_real_client", lambda: client)
    monkeypatch.setattr(cfgmod, "load_attach_only_config", lambda *a, **k: conn)
    monkeypatch.setattr(live, "Mt5DemoStack", RecStack)
    r = rn.build_live_runner("shadow", artifacts_dir=tmp_path / "s", markets=("GER40", "NAS100"))
    r.store.close()
    kw = seen[-1]
    assert kw["client"] is client and kw["connection"] is conn and kw["dry_run"] is True
    assert Path(kw["state_dir"]) == tmp_path / "s" / "stack" and set(kw["market_specs"]) == {"GER40", "NAS100"}
    r = rn.build_live_runner("demo-auto", artifacts_dir=tmp_path / "d", markets=("GER40",))
    r.store.close()
    assert seen[-1]["dry_run"] is False


def test_real_client_and_live_stack_are_not_imported_at_module_import():
    code = (
        "import sys; sys.path.insert(0, 'src'); import demo.runner, demo.funnel;"
        "assert 'MetaTrader5' not in sys.modules and 'demo.execution.live' not in sys.modules;"
        "assert 'adapters.activtrades_mt5.real_client' not in sys.modules"
    )
    root = Path(__file__).resolve().parents[4]
    res = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr


def test_feed_uses_last_closed_bar_and_latest_quote(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    _cycle(r)
    fd = r.status(env.clock())["feed"]["GER40"]
    assert fd["stale"] is False and fd["quote_age_s"] is not None
    assert (env.clock() - env.stack.bar_source.last_closed_bar_close_utc("GER40")).total_seconds() == fd["bar_age_s"]
    assert M5 == timedelta(minutes=5)
