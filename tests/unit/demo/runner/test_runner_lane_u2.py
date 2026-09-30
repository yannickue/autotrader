# ruff: noqa: E501
"""Lane U2 runner integration: out-of-window shadow + shadow universe + observability counters. Fakes only (no MT5)."""

from __future__ import annotations

import dataclasses
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alpha.fast.sim import CandidateArrays
from demo import funnel, report
from demo.contracts import Decision
from demo.execution.events import Rejected
from demo.opportunity import engine as eng
from demo.opportunity.bar_source import Quote
from demo.opportunity.engine import InMemorySeenStore
from demo.opportunity.production_spec import load_production_spec
from demo.runner import RunnerConfig
from demo.shadow_universe import ShadowUniverseScanner, select_shadow_markets
from demo.testing import M5, T0, ScriptedEngine, make_pair
from markets.spec import load_market_spec

OOW_NOW = datetime(2026, 10, 1, 20, 5, 10, tzinfo=UTC)  # Thu 22:05 Berlin (CEST): GER40 cash closed, entry window closed
GER = load_market_spec("GER40")
SPECS = load_production_spec().specs_for("GER40")


class OowEngine(ScriptedEngine):
    """ScriptedEngine that also speaks the Lane-U2 shadow API (queued out-of-window pairs)."""

    supports_shadow_scan = True

    def __init__(self) -> None:
        super().__init__()
        self.shadow_calls: list[tuple[str, object]] = []
        self.shadow_queue: list = []
        self.shadow_raises: Exception | None = None

    def market_spec(self, market):
        return GER

    def specs_for(self, market):
        return SPECS

    def on_m5_close(self, market, now, *, catchup=None, shadow=None):
        if shadow is None:
            return super().on_m5_close(market, now, catchup=catchup)
        self.shadow_calls.append((market, shadow))
        if self.shadow_raises is not None:
            raise self.shadow_raises
        return [(s, d) for s, d in self.shadow_queue]

    def release_seen(self):
        pass


def oow_pair(tag="w", signal_ts=OOW_NOW - timedelta(minutes=5, seconds=10)):
    snap, _dec, _i = make_pair(accepted=False, signal_ts=signal_ts, horizon_s=1800, tag=tag)
    snap = dataclasses.replace(snap, signal={**snap.signal, "origin": "OUT_OF_WINDOW_SHADOW", "cluster": "INDEX"})
    dec = Decision(
        opportunity_id=snap.opportunity_id, phase="DISCOVERY", decided_utc=(signal_ts + timedelta(seconds=5)).isoformat(),
        accepted=False, reasons=("OUT_OF_WINDOW_SHADOW",), policy_id="static-demo-policy-v1",
    )
    return snap, dec


@pytest.fixture
def oow_env(env):
    env.clock.now = OOW_NOW
    env.engine = OowEngine()
    return env


def test_default_off_engine_never_sees_shadow_and_heartbeat_reports_disabled(oow_env):
    r = oow_env.build()
    r.start()
    r.run_cycle()
    st = r.status()
    assert oow_env.engine.shadow_calls == []
    assert st["out_of_window_shadow"] == {"enabled": False} and st["shadow_universe"] == {"enabled": False}
    assert r.cfg.out_of_window_shadow_enabled is False and r.cfg.shadow_universe == () and r.shadow_universe is None


def test_out_of_window_pass_persists_rejected_observation_never_submits(oow_env):
    oow_env.engine.shadow_queue = [oow_pair()]
    r = oow_env.build(out_of_window_shadow_enabled=True)
    r.start()
    r.run_cycle()
    assert [c[1].code for c in oow_env.engine.shadow_calls] == ["OUT_OF_WINDOW_SHADOW"]
    snap, _dec = oow_pair()
    assert oow_env.store.get_snapshot(snap.opportunity_id) is not None
    d = oow_env.store.get_decision(snap.opportunity_id)
    assert d.accepted is False and d.reasons == ("OUT_OF_WINDOW_SHADOW",)
    assert oow_env.stack.submits == [] and oow_env.stack.shadow_submits == [] and oow_env.store.list_intents() == []
    st = r.status()
    assert st["out_of_window_shadow"]["recorded"] == 1 and st["out_of_window_shadow"]["recorded_total"] == 1
    assert st["opportunities_today"] == {"raw": 0, "accepted": 0, "rejected": 0}  # measurement rows are not opportunities


def test_out_of_window_needs_a_fresh_quote_not_just_a_fresh_bar(oow_env, monkeypatch):
    oow_env.engine.shadow_queue = [oow_pair()]
    old = Quote(ts_utc=OOW_NOW - timedelta(hours=3), bid=100.0, ask=100.1)
    monkeypatch.setattr(oow_env.stack.bar_source, "latest_quote", lambda m: old)
    r = oow_env.build(out_of_window_shadow_enabled=True)
    r.start()
    r.run_cycle()
    assert oow_env.engine.shadow_calls == []
    assert r.status()["out_of_window_shadow"]["skipped_not_tradable"] >= 1


def test_out_of_window_not_run_while_the_window_is_open(env):
    env.engine = OowEngine()  # T0 = 11:00 Berlin: every GER40 window is open
    env.engine.shadow_queue = [oow_pair()]
    r = env.build(out_of_window_shadow_enabled=True)
    r.start()
    r.run_cycle()
    assert env.engine.shadow_calls == [] and r.status()["out_of_window_shadow"]["skipped_window_open"] >= 1


def test_out_of_window_failure_is_contained_counted_and_does_not_halt_trading(oow_env):
    oow_env.engine.shadow_raises = ValueError("generator exploded")
    r = oow_env.build(out_of_window_shadow_enabled=True)
    r.start()
    r.run_cycle()
    st = r.status()
    assert st["out_of_window_shadow"]["errors"] == 1 and "generator exploded" in st["out_of_window_shadow"]["last_error"]
    assert r.fail_reason is None and r.halt_reason is None and r.can_trade()


def test_out_of_window_observation_is_labelled_with_its_own_source_and_funnel_section(oow_env):
    oow_env.engine.shadow_queue = [oow_pair()]
    r = oow_env.build(out_of_window_shadow_enabled=True)
    r.start()
    r.run_cycle()
    oow_env.clock.advance(minutes=50)
    assert r.label_now(oow_env.clock()) == 1
    snap, _ = oow_pair()
    row = {x["opportunity_id"]: x for x in oow_env.store.counterfactual_rows()}[snap.opportunity_id]
    assert row["source"] == "OUT_OF_WINDOW_SHADOW" and row["gate_code"] == "OUT_OF_WINDOW_SHADOW" and row["gate_class"] == "WINDOW"
    fun = funnel.funnel(oow_env.store, None, "DISCOVERY")
    sec = fun["out_of_window_shadow"]
    assert sec["recorded"] == 1 and sec["gate_class"] == "WINDOW" and sec["counterfactual"]["n_labelled"] == 1
    assert "OUT_OF_WINDOW_SHADOW" in fun["analysis"]["gates"] and fun["analysis"]["gates"]["OUT_OF_WINDOW_SHADOW"]["class"] == "WINDOW"
    assert [s["stage"] for s in fun["analysis"]["stages"] if s["blocked_here"]] == ["STRATEGY_WINDOW"]
    assert "OUT_OF_WINDOW_SHADOW [WINDOW" in funnel.render(fun)


# ---------------------------------------------------------------------------------- shadow universe
@pytest.fixture
def stub_generate(monkeypatch):
    def gen(data, spec, thr):
        i = len(data) - 2
        return CandidateArrays(
            np.array([i], dtype=np.int64), np.array([1], dtype=np.int8), np.array([float(data.c[i]) - 0.002]),
            np.array([np.nan]), np.array([1.5]), np.array([0], dtype=np.int8),
        )

    monkeypatch.setattr(eng, "generate_candidates", gen)


def _scanner_for(env, names="AUDUSD,ETHUSD", **kw):
    import test_shadow_universe as t

    sel, _pending = select_shadow_markets(names)
    c = env.clock()
    last_open = c.replace(minute=c.minute - c.minute % 5, second=0, microsecond=0) - M5
    src = t.FakeSource(sel, last_open=last_open)
    sc = ShadowUniverseScanner(
        specs=sel, source=src, phase="DISCOVERY", seen_store=InMemorySeenStore(), commit="t", forbidden=("GER40", "NAS100"), **kw,
    )
    return sc, src


def test_shadow_universe_cycle_records_but_never_trades_or_registers(env, stub_generate):
    sc, _src = _scanner_for(env)
    r = env.build(shadow_universe=tuple(sc.markets))
    r.shadow_universe = sc
    r.start()
    r.run_cycle()
    st = r.status()["shadow_universe"]
    assert st["symbols_total"] == 2 and st["scanned_last_cycle"] == 2 and st["opportunities_recorded"] == 8 and st["persisted_total"] == 8 and st["errors"] == 0
    decs = [d for d in env.store.list_decisions() if d.reasons and d.reasons[0] == "SHADOW_UNIVERSE"]
    assert len(decs) == 8 and all(d.accepted is False for d in decs)
    assert env.stack.submits == [] and env.stack.shadow_submits == [] and env.store.list_intents() == []
    assert not (set(sc.markets) & set(env.stack.markets)) and not (set(sc.markets) & set(r.cfg.markets))
    assert r.status()["opportunities_today"] == {"raw": 0, "accepted": 0, "rejected": 0}
    fun = funnel.funnel(env.store, None, "DISCOVERY")
    assert fun["shadow_universe"]["recorded"] == 8 and set(fun["shadow_universe"]["by_cluster"]) == {"FX", "CRYPTO"}
    assert fun["summary"]["opportunities"] == 0  # excluded from every trading metric


def test_shadow_universe_failure_isolated_and_reported_in_heartbeat(env, stub_generate):
    sc, src = _scanner_for(env, "AUDUSD,GBPUSD")
    src.fail.add("GBPUSD")
    r = env.build(shadow_universe=tuple(sc.markets))
    r.shadow_universe = sc
    r.start()
    r.run_cycle()
    st = r.status()["shadow_universe"]
    assert st["errors"] == 1 and st["scanned_last_cycle"] == 2 and st["persisted_total"] == 4
    assert r.fail_reason is None and r.can_trade()


def test_shadow_universe_hard_guard_trips_fail_closed_if_a_registry_market_slips_in(env):
    sc, _ = _scanner_for(env)
    snap, dec = oow_pair()
    snap = dataclasses.replace(snap, market="GER40")
    r = env.build(shadow_universe=("AUDUSD",))
    r.shadow_universe = sc
    r.start()
    r._process_shadow_pair(snap, dec, env.clock(), "shadow_universe")
    assert r.fail_reason and "shadow_observation_invariant" in r.fail_reason
    assert env.store.get_snapshot(snap.opportunity_id) is None  # never persisted


def test_shadow_observation_can_never_be_accepted(env):
    r = env.build()
    snap, _dec, _i = make_pair(accepted=False, tag="acc")
    accepted = Decision(opportunity_id=snap.opportunity_id, phase="DISCOVERY", decided_utc=snap.created_utc, accepted=True, reasons=("ACCEPTED",), policy_id="p")
    r.start()
    r._process_shadow_pair(snap, accepted, env.clock(), "out_of_window")
    assert r.fail_reason and "shadow_observation_invariant" in r.fail_reason


def test_shadow_universe_bars_reach_the_labeller_through_the_scanner_not_the_stack(env, stub_generate):
    sc, _src = _scanner_for(env, "AUDUSD")
    r = env.build(shadow_universe=("AUDUSD",))
    r.shadow_universe = sc
    r.start()
    r.run_cycle()
    rows = r._frame_rows("AUDUSD")  # would raise StackFailClosed("unknown_market") via the stack: shadow symbols are routed to the scanner
    assert rows and rows[-1][0] == sc.frame_rows("AUDUSD")[-1][0]


# ------------------------------------------------------------------------- observability (goal D)
def test_trades_today_is_submit_attempts_and_explicit_counters_split_intents_from_fills(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    st = r.status()
    assert st["trades_today"] == 1 and st["trades_today_semantics"] == "submit_attempts_not_fills"
    assert st["intents_today"] == 1 and st["broker_trades_today"] == 1
    assert st["opportunities_today"]["raw"] == 1


def test_rejected_submit_counts_as_attempt_and_intent_but_not_as_broker_trade(env):
    snap, dec, intent = make_pair()
    env.stack.script[intent.intent_id] = [Rejected(intent.intent_id, "size_below_min", {"decision": "SKIP", "reject_code": "size_below_min"})]
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    st = r.status()
    assert (st["trades_today"], st["intents_today"], st["broker_trades_today"]) == (1, 1, 0)


def test_day_counters_survive_a_restart_from_the_store(env):
    snap, dec, intent = make_pair(signal_ts=T0)
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    r2 = env.build()  # same store, fresh process state
    r2.start()
    r2._roll_day(env.clock())
    st = r2.status()
    assert st["intents_today"] == 1 and st["broker_trades_today"] == 1 and st["trades_today"] == 1


def test_counterfactual_unlabelled_never_negative_with_accepted_non_traded_labels(env):
    snap, dec, intent = make_pair(horizon_s=3600)
    env.stack.script[intent.intent_id] = [Rejected(intent.intent_id, "size_below_min", {"decision": "SKIP", "reject_code": "size_below_min", "gate_reject_class": "LEGACY_ARBITRARY"})]
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    env.clock.advance(minutes=70)
    assert r.label_now(env.clock()) == 1  # an ENGINE-ACCEPTED, stack-rejected opportunity gets a label
    avr = report.build_report(env.store, "DISCOVERY")["accepted_vs_rejected"]["rejected"]
    assert avr["decisions"] == 0 and avr["labelled"] == 1  # the old arithmetic (decisions - labelled) gave -1 here
    assert avr["unlabelled"] == 0 and avr["labelled_accepted_non_traded"] == 1 and avr["labelled_engine_rejected"] == 0


# ------------------------------------------------------------------------- factory / CLI wiring
def _factory(record):
    from demo.testing import FakeClock, FakeStack

    def make(*, dry_run, state_dir, markets):
        record.append(tuple(markets))
        return FakeStack(FakeClock(T0 + timedelta(seconds=10)), markets=tuple(markets), shadow=dry_run)

    return make


def test_factory_defaults_oow_on_universe_off_and_production_spec_untouched(tmp_path):
    from demo import runner as rn

    r = rn.build_live_runner("shadow", artifacts_dir=tmp_path / "a", stack_factory=_factory([]), learning=False)
    try:
        assert r.cfg.out_of_window_shadow_enabled is True and r._oow is not None  # cheap + cannot trade: factory default ON
        assert r.cfg.shadow_universe == () and r.shadow_universe is None  # the universe is ALWAYS opt-in
        assert r.engine.strategy_hash == "c3eae99e782888ac"  # production spec untouched
    finally:
        r.store.close()
    off = rn.build_live_runner("shadow", artifacts_dir=tmp_path / "b", stack_factory=_factory([]), learning=False, out_of_window_shadow=False)
    try:
        assert off.cfg.out_of_window_shadow_enabled is False and off._oow is None
    finally:
        off.store.close()
    assert RunnerConfig().out_of_window_shadow_enabled is False and RunnerConfig().shadow_universe == ()  # library default: bit-identical


def test_factory_wires_shadow_universe_with_a_read_only_source_and_the_registry_guard(tmp_path):
    import test_shadow_universe as t

    from demo import runner as rn

    seen = {}

    def src_factory(stack, symbols):
        seen["symbols"] = dict(symbols)
        sel, _ = select_shadow_markets(list(symbols))
        return t.FakeSource(sel)

    r = rn.build_live_runner(
        "shadow", artifacts_dir=tmp_path / "a", stack_factory=_factory([]), learning=False,
        shadow_universe="AUDUSD,ETHUSD", out_of_window_shadow=True, shadow_source_factory=src_factory,
    )
    try:
        assert seen["symbols"] == {"AUDUSD": "AUDUSD", "ETHUSD": "ETHUSD"}
        assert r.shadow_universe is not None and r.shadow_universe.markets == ("AUDUSD", "ETHUSD")
        assert r._oow is not None and r.cfg.shadow_universe == ("AUDUSD", "ETHUSD")
        assert not set(r.shadow_universe.markets) & set(r.cfg.markets)
        assert r.engine.strategy_hash == "c3eae99e782888ac"
    finally:
        r.store.close()


def test_cli_flags_parse_and_default_off():
    from scripts.demo_trader import _parser

    a = _parser().parse_args(["--shadow"])
    assert a.shadow_universe is None and a.out_of_window_shadow is None
    b = _parser().parse_args(["--shadow", "--shadow-universe", "--out-of-window-shadow"])
    assert b.shadow_universe == "all-ready" and b.out_of_window_shadow is True
    c = _parser().parse_args(["--shadow", "--shadow-universe", "AUDUSD,ETHUSD", "--no-out-of-window-shadow"])
    assert c.shadow_universe == "AUDUSD,ETHUSD" and c.out_of_window_shadow is False
