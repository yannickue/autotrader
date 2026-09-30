# ruff: noqa: E501
"""DemoRunner driving the REAL Mt5DemoStack (real Nautilus kernel + MT5 lane threads) over the fake
broker: proves the runner <-> stack interface (LiveBarSource surface, context, risk_detail, TCA,
cost semantics, ADDON classification, funnel). ZERO real MT5."""

from __future__ import annotations

import dataclasses
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from demo.funnel import funnel
from demo.runner import DemoRunner, RunnerConfig
from demo.store import CLOSED, PROTECTED, RISK_REJECTED, DemoStore
from demo.testing import ScriptedEngine, make_pair
from tests.unit.demo.execution.stack_harness import (
    QUOTES,
    build_broker,
    m5_rows,
    make_stack,
)

GIB = 1024**3


@pytest.fixture
def rig(tmp_path):
    broker = build_broker()
    broker.rates[5] = m5_rows(broker, n=60)
    stack = make_stack(broker, tmp_path)
    store = DemoStore(tmp_path / "demo.sqlite")
    engine = ScriptedEngine()
    runner = DemoRunner(
        stack, engine, store,
        config=RunnerConfig(mode="demo-auto", markets=("GER40",), artifacts_dir=tmp_path / "art",
                            min_disk_free_bytes=GIB, poll_interval_s=1.0),
        sleep=lambda s: None, disk_free=lambda: 50 * GIB,
    )
    yield broker, stack, store, engine, runner
    stack.stop()
    store.close()


def _pair(tag: str, *, entry: float = 25002.0, stop: float = 24950.0, target: float = 25150.0):
    now = datetime.now(UTC)
    snap, dec, intent = make_pair(market="GER40", signal_ts=now, entry=entry, risk=entry - stop, tag=tag, valid_s=300)
    snap = dataclasses.replace(snap, broker_symbol="Ger40")
    intent = dataclasses.replace(intent, broker_symbol="Ger40", stop=stop, target=target, min_space_r=1.0)
    return snap, dec, intent


def test_runner_drives_the_real_stack_end_to_end(rig):
    broker, stack, store, engine, runner = rig
    runner.start()
    assert runner.fail_reason is None, runner.fail_reason
    a = _pair("a")
    engine.push("GER40", a)
    runner.run_cycle()  # feed via LiveBarSource -> new bar -> scan -> submit(context=...) -> events
    iid = a[2].intent_id
    assert runner.fail_reason is None, (runner.fail_reason, runner._last_error)
    assert store.get_state(iid) == PROTECTED
    acc = store.get_risk_detail(iid, "ACCEPTED")
    assert acc["decision"] == "TRADE" and acc["family"] == "orb" and acc["signal_inputs"]["confluence"] == 2
    assert acc["win_probability"] is None and "stop_risk_eur" in acc and "portfolio_risk_before" in acc
    risk = store.get_risk(iid)
    assert risk.approved and risk.equity == 10000.0 and risk.quantity > 0 and 0 < risk.risk_fraction <= 0.01
    tca = store.get_tca(iid, "ENTRY")
    assert tca["fill_price"] == pytest.approx(25001.5) and tca["intended_price"] == pytest.approx(25002.0)
    assert tca["slippage_vs_intended"] is not None and tca["cost_price_units"] is not None
    assert tca["latency_total_ms"] is not None
    ex = store.get_execution(iid)
    assert ex.protection_confirmed and ex.cost_status == "provisional"

    # a second same-symbol intent is NOT swallowed by the runner: the stack classifies it (TEMPORARY)
    b = _pair("b")
    engine.push("GER40", b)
    runner._scan_market("GER40", datetime.now(UTC))
    assert store.get_state(b[2].intent_id) == RISK_REJECTED
    rd = store.get_risk_detail(b[2].intent_id, "REJECTED")
    assert rd["reject_code"].startswith("ADDON_") and rd["gate_reject_class"] == "TEMPORARY"
    assert store.get_risk(b[2].intent_id).reject_reason == rd["reject_code"]

    # the broker stop executes; closing deal costs are added ONCE to the entry costs
    broker.set_quote(24940.0, 24941.5)
    runner.run_cycle()
    assert store.get_state(iid) == CLOSED, (runner._last_error, runner._warnings)
    out = store.get_outcome(iid)
    assert out.exit_reason == "STOP" and out.net_r < 0
    ex2 = store.get_execution(iid)
    assert ex2.cost_status in ("verified", "provisional")
    exit_tca = store.get_tca(iid, "EXIT")
    assert exit_tca["exit_reason"] == "STOP" and exit_tca["broker_profit_eur"] is not None
    assert not [w for w in runner._warnings if w.startswith("pnl_mismatch")], runner._warnings

    f = funnel(store, stack, "DISCOVERY")
    assert f["stack"]["temporary_limitation"]["otherwise_valid_blocked"] == 1
    assert f["summary"]["traded"] == 1 and f["stack_live"]["TEMPORARY_LIMITATION"]["total"] >= 1
    hb = json.loads(runner.cfg.heartbeat_path.read_text())
    assert hb["rejection_funnel"]["traded"] == 1
    broker.set_quote(*QUOTES["Ger40"])


# ---------------------------------------------------------------------------------------------
# build_live_runner in BOTH modes over the real Mt5DemoStack + FakeMT5Broker (no FakeStack)
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def factory_env(tmp_path, monkeypatch):
    import adapters.activtrades_mt5.real_client as rc
    import adapters.config as cfgmod
    from tests.unit.demo.execution.stack_harness import FAST, connection

    monkeypatch.delenv("MT5_ALLOW_ACCOUNT_LOGIN", raising=False)
    built = []

    def build(mode: str):
        broker = build_broker()
        rows = m5_rows(broker, n=700)  # >= min history: the REAL engine runs on the REAL bar source
        broker.rates[5] = rows
        for sym in ("UsaTec", "Usa500", "GOLD", "EURUSD"):  # leader markets of the LEADLAG family
            broker.extra_rates.setdefault(sym, {})[5] = rows
        monkeypatch.setattr(rc, "get_real_client", lambda: broker)  # the fake broker, never MetaTrader5
        monkeypatch.setattr(cfgmod, "load_attach_only_config", lambda *a, **k: connection(broker))
        from demo import runner as rn

        r = rn.build_live_runner(
            mode, artifacts_dir=tmp_path / mode, markets=("GER40",), learning=False,
            stack_kwargs={"lock_path": tmp_path / f"{mode}.lock", "config": FAST},
        )
        built.append(r)
        return broker, r

    yield build
    for r in built:
        r.stack.stop()
        r.store.close()


def test_build_live_runner_shadow_mode_real_stack_never_sends(factory_env):
    broker, r = factory_env("shadow")
    assert r.stack.shadow is True and r.engine._source is r.stack.bar_source
    r.start()
    assert r.fail_reason is None, r.fail_reason
    eng = ScriptedEngine()
    a = _pair("a")
    eng.push("GER40", a)
    real_engine, r.engine = r.engine, eng
    r.run_cycle()
    assert r.fail_reason is None and r.store.list_intents() == []  # shadow: no intent, no order
    assert broker.order_send_calls == 0
    assert r.store.get_decision(a[0].opportunity_id).accepted
    # the REAL engine also ran on the REAL LiveBarSource (700 synthetic bars) without contract errors
    r.engine = real_engine
    real_engine.on_m5_close("GER40", datetime.now(UTC))
    assert real_engine.health["GER40"] in ("ok", "insufficient_history")


def test_build_live_runner_demo_auto_real_stack_trades_and_closes(factory_env):
    broker, r = factory_env("demo-auto")
    assert r.stack.shadow is False
    r.start()
    assert r.fail_reason is None, r.fail_reason
    eng = ScriptedEngine()
    a = _pair("a")
    eng.push("GER40", a)
    r.engine = eng
    r.run_cycle()
    assert r.store.get_state(a[2].intent_id) == PROTECTED and broker.order_send_calls == 1
    broker.set_quote(24940.0, 24941.5)
    r.run_cycle()
    assert r.store.get_state(a[2].intent_id) == CLOSED and r.store.get_outcome(a[2].intent_id) is not None
    assert r.fail_reason is None
