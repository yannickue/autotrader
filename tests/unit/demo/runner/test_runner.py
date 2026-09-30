# ruff: noqa: E501
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import timedelta

import pytest

from demo import monitor
from demo import runner as rn
from demo.execution.stack_port import StackFailClosed
from demo.store import (
    CANCELLED,
    CLOSED,
    FILLED,
    PLANNED,
    PROTECTED,
    DemoStore,
)
from demo.testing import M5, T0, FakeClock, FakeStack, ScriptedEngine, floor5, make_pair

GIB = 1024**3


def monitor_milestone_keys(store):
    return {m for m in (10, 25, 50) for p in ("DISCOVERY", "FROZEN", "ALL")
            if store.get_meta(f"milestone_emitted:{p}:{m}")}


def _cycle(r):
    r.start()
    r.run_cycle()


def _events(store, iid):
    return [e["to_state"] for e in store.intent_events(iid)]


# ------------------------------------------------------------------ happy path / lifecycle
def test_happy_path_full_lifecycle(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    # a bar that reaches the target after the fill (path used for MFE)
    env.stack.bar_source.overrides[("GER40", T0 + timedelta(minutes=20))] = (100.0, 103.5, 99.8, 103.0)
    r = env.build()
    _cycle(r)
    st = env.store
    assert st.get_snapshot(snap.opportunity_id) is not None
    assert st.get_decision(snap.opportunity_id).accepted
    assert st.get_state(intent.intent_id) == PROTECTED
    assert _events(st, intent.intent_id) == [
        "PLANNED", "RISK_APPROVED", "SENT", "FILLED", "PROTECTED"]
    assert st.get_risk(intent.intent_id).approved and st.get_risk(intent.intent_id).quantity == 1.0
    ex = st.get_execution(intent.intent_id)
    assert ex.fill_price == pytest.approx(100.05) and ex.protection_confirmed
    assert ex.parity_checks == {"target_crossed_at_fill": False, "stop_crossed_at_fill": False}
    assert ex.cost_status == "provisional" and ex.broker_position_id == "pos-" + intent.intent_id
    assert st.get_outcome(intent.intent_id) is None  # no outcome before CLOSED

    env.stack.close_position(intent.intent_id, reason="TARGET", exit_price=103.0)
    env.clock.advance(minutes=35)
    r.run_cycle()
    assert st.get_state(intent.intent_id) == CLOSED
    out = st.get_outcome(intent.intent_id)
    assert out.exit_reason == "TARGET"
    assert out.gross_r == pytest.approx((103.0 - 100.05) / (100.05 - 98.5))
    assert out.net_r < out.gross_r  # entry + exit commission
    assert out.mfe_r > out.gross_r  # 103.5 high inside the path
    assert st.get_execution(intent.intent_id).cost_status == "verified"
    assert st.get_execution(intent.intent_id).fees == pytest.approx(-2.0)
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    assert hb["cumulative_r"] == pytest.approx(out.net_r)
    assert hb["opportunities_today"] == {"raw": 1, "accepted": 1, "rejected": 0}


def test_broker_rejection_recorded(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    env.stack.mode = "reject"
    r = env.build()
    _cycle(r)
    assert env.store.get_state(intent.intent_id) == "RISK_REJECTED"
    assert env.store.get_risk(intent.intent_id).approved is False
    assert r.fail_reason is None


# ------------------------------------------------------------------------------- shadow
def test_shadow_mode_zero_real_submits_but_dry_run_pipeline(env):
    env.stack.shadow = True
    a = make_pair()
    b = make_pair(signal_ts=T0, tag="b", direction=-1)
    env.engine.push("GER40", a, b)
    r = env.build(mode="shadow")
    _cycle(r)
    assert env.stack.submits == [] and env.stack.positions == {}  # never a real send / position
    assert len(env.stack.shadow_submits) == 2 and r.shadow_submit_count == 2
    assert len(env.store.list_decisions()) == 2
    assert env.store.get_decision(a[0].opportunity_id).accepted
    assert r.submit_count == 0  # not a trade
    for _s, _d, intent in (a, b):
        assert _events(env.store, intent.intent_id) == ["PLANNED", "RISK_APPROVED", "CANCELLED"]
        assert env.store.get_state(intent.intent_id) == CANCELLED
        ev = env.store.intent_events(intent.intent_id)[-1]
        assert ev["detail"] == {"reason": "shadow_dry_run"}
        assert env.store.get_risk(intent.intent_id).approved
        assert env.store.get_risk_detail(intent.intent_id, "ACCEPTED")["decision"] == "TRADE"
        assert env.store.get_outcome(intent.intent_id) is None
    assert r.fail_reason is None


def test_shadow_reject_recorded_like_demo_auto_and_counterfactual_untouched(env):
    env.stack.shadow = True
    env.stack.mode = "reject"
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(mode="shadow")
    _cycle(r)
    assert env.store.get_state(intent.intent_id) == "RISK_REJECTED"
    assert env.store.get_risk(intent.intent_id).approved is False
    assert env.stack.submits == []


def test_shadow_refuses_real_send_and_non_dry_run_stack(env):
    snap, dec, intent = make_pair()
    # FakeStack in shadow refuses a scripted real fill
    env.stack.shadow = True
    from decimal import Decimal

    from demo.execution.events import Fill
    env.stack.script[intent.intent_id] = [Fill(intent.intent_id, Decimal("1"), Decimal("1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), "o", "p")]
    with pytest.raises(AssertionError):
        env.stack.submit(intent)
    # a stack that is NOT dry-run must never be driven by a shadow runner
    env.stack.shadow = False
    env.stack.script.clear()
    env.stack.shadow_submits.clear()
    env.stack.known.clear()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(mode="shadow")
    _cycle(r)
    assert env.stack.submits == [] and env.stack.shadow_submits == []
    assert r.fail_reason and "shadow_stack_not_dry_run" in r.fail_reason
    assert env.store.get_state(intent.intent_id) == CANCELLED


def test_shadow_execution_event_from_stack_fails_closed(env):
    from decimal import Decimal

    from demo.execution.events import Accepted, Fill
    env.stack.shadow = True
    snap, dec, intent = make_pair()
    iid = intent.intent_id
    # bypass FakeStack's own guard to prove the runner defends itself
    env.stack.submit = lambda i, context=None: [
        Accepted(iid, Decimal("1"), Decimal("1"), Decimal("0.01"), Decimal("1"), Decimal("1")),
        Fill(iid, Decimal("1"), Decimal("1"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), "o", "p")]
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(mode="shadow")
    _cycle(r)
    assert r.fail_reason and r.fail_reason.startswith("event_error") is False and "shadow_execution_event" in r.fail_reason
    assert env.store.get_state(iid) == CANCELLED


def test_shadow_funnel_counts_would_trade_separately_from_trades(env):
    from demo.funnel import funnel
    env.stack.shadow = True
    a = make_pair()
    b = make_pair(signal_ts=T0, tag="b", direction=-1)
    from demo.execution.events import Rejected
    env.stack.script[b[2].intent_id] = [Rejected(b[2].intent_id, "risk: scripted rejection")]
    env.engine.push("GER40", a, b)
    r = env.build(mode="shadow")
    _cycle(r)
    f = funnel(env.store, env.stack, "DISCOVERY")
    s = f["summary"]
    assert s["traded"] == 0 and s["shadow_would_trade"] + s["stack_rejected"] == s["engine_accepted"]
    assert s["shadow_would_trade"] == 1 and s["stack_rejected"] == 1
    assert f["stack"]["intent_states"].get("CANCELLED", 0) == s["shadow_would_trade"]
    assert f["trades_that_would_have_existed"]["actual"] == 0
    assert f["trades_that_would_have_existed"]["without_temporary_limitations"] >= s["shadow_would_trade"]
    assert "shadow would-trade" in __import__("demo.funnel", fromlist=["render"]).render(f)


# ----------------------------------------------------------------------- counterfactuals
def test_rejected_gets_counterfactual_after_horizon_only(env):
    snap, dec, _ = make_pair(accepted=False, horizon_s=3600)
    env.engine.push("GER40", (snap, dec, None))
    r = env.build()
    _cycle(r)
    assert r.label_now(env.clock()) == 0  # horizon not elapsed
    assert env.store.get_counterfactual(snap.opportunity_id) is None
    env.clock.advance(minutes=65)
    assert r.label_now(env.clock()) == 1
    lab = env.store.get_counterfactual(snap.opportunity_id)
    assert lab is not None and lab.phase == "DISCOVERY"
    assert lab.horizon_end_utc.startswith("2026-10-01T10:00:00")


def test_counterfactual_runs_periodically(env):
    snap, dec, _ = make_pair(accepted=False, horizon_s=1800)
    env.engine.push("GER40", (snap, dec, None))
    r = env.build(label_every_s=60.0)
    _cycle(r)
    env.clock.advance(minutes=40)
    r.run_cycle()
    assert env.store.get_counterfactual(snap.opportunity_id) is not None


# --------------------------------------------------------------------------- persistence
class FailingStore(DemoStore):
    fail = False

    def record_snapshot(self, snap):
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        return super().record_snapshot(snap)


def test_persistence_failure_halts_new_exposure(env, tmp_path):
    clock = env.clock
    store = FailingStore(tmp_path / "f.sqlite", clock=lambda: clock().isoformat())
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(store=store)
    r.start()
    store.fail = True
    code = r.run(max_cycles=3)
    assert code == 7
    assert r.fail_reason.startswith("persistence_failure")
    assert env.stack.submits == []  # never acted on an unpersisted opportunity
    assert env.stack.halts and env.stack.stopped
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    assert hb["process_alive"] is False and "persistence_failure" in hb["last_error"]["text"]
    store.close()


# --------------------------------------------------------------------- exactly once / restart
def test_duplicate_and_restart_never_second_order(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r1 = env.build()
    _cycle(r1)
    assert len(env.stack.submits) == 1
    # duplicate inside the same run on the next bar
    env.clock.advance(minutes=5)
    env.engine.push("GER40", (snap, dec, intent))
    r1.run_cycle()
    assert len(env.stack.submits) == 1
    # restart: new runner, same store, broker still holds the position
    r2 = env.build()
    env.engine.push("GER40", (snap, dec, intent))
    env.clock.advance(minutes=5)
    r2.start()
    r2.run_cycle()
    assert len(env.stack.submits) == 1
    assert len(env.store.list_intents()) == 1
    assert env.store.get_state(intent.intent_id) == PROTECTED
    assert r2.fail_reason is None


def test_restart_unknown_to_broker_rules(env):
    a = make_pair(tag="planned", signal_ts=T0)
    b = make_pair(tag="sent", signal_ts=T0 + M5 * 1)
    c = make_pair(tag="prot", signal_ts=T0 + M5 * 2)
    st = env.store
    for snap, dec, intent in (a, b, c):
        st.record_snapshot(snap)
        st.record_decision(dec)
        st.record_intent(intent)
    st.record_risk(b[2].intent_id, _risk())
    for step in ("RISK_APPROVED", "SENT"):
        st.transition(b[2].intent_id, step)
    for step in ("RISK_APPROVED", "SENT", "FILLED", "PROTECTED"):
        st.transition(c[2].intent_id, step)
    r = env.build()
    r.start()  # broker knows nothing
    assert st.get_state(a[2].intent_id) == CANCELLED
    assert st.get_state(b[2].intent_id) == CANCELLED
    assert st.get_state(c[2].intent_id) == CLOSED
    assert st.closed_without_outcome() == [c[2].intent_id]
    assert any("needs_manual_review" in w for w in r._warnings)
    assert env.stack.submits == []  # nothing is ever re-sent


def _risk():
    from demo.contracts import RiskRecord

    return RiskRecord(equity=1.0, risk_fraction=0.01, risk_budget=1.0, quantity=1.0, leverage=1.0, approved=True)


def test_orphan_broker_position_fails_closed(env):
    _s, _d, intent = make_pair()
    env.stack.positions["int-ghost"] = intent
    r = env.build()
    r.start()
    assert r.fail_reason and "orphan_broker_intent" in r.fail_reason


def test_store_seen_adapter_exactly_once(env):
    seen = rn.StoreSeenAdapter(env.store)
    assert seen.add_if_new("opp-x") is True
    assert seen.add_if_new("opp-x") is False
    snap, _d, _ = make_pair()
    env.store.record_snapshot(snap)
    assert seen.add_if_new(snap.opportunity_id) is False  # snapshot table counts as seen


# ------------------------------------------------------------------------ fail closed
def test_stack_fail_closed_on_submit(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    env.stack.mode = "fail_closed"
    r = env.build()
    code = r.run(max_cycles=3)
    assert code == 7
    assert env.store.get_state(intent.intent_id) == CANCELLED  # not open at the broker
    assert env.stack.halts and env.stack.stopped and len(env.stack.submits) == 1
    assert "scripted fail closed" in json.loads(r.cfg.heartbeat_path.read_text())["last_error"]["text"]


def test_unknown_submit_error_never_resent(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    env.stack.mode = "unknown_error"
    r = env.build()
    assert r.run(max_cycles=2) == 7
    assert env.store.get_state(intent.intent_id) == PLANNED
    r2 = env.build()
    env.stack.mode = "full"
    r2.start()
    assert env.store.get_state(intent.intent_id) == CANCELLED  # reconciled, not re-driven
    assert len(env.stack.submits) == 1


@pytest.mark.parametrize(
    ("change", "expect"),
    [
        (dict(is_demo=False), "account_not_demo"),
        (dict(reconciliation="MISMATCH"), "reconciliation=MISMATCH"),
        (dict(connected=False), "broker_disconnected"),
        (dict(kill_switch=True), "kill_switch"),
        (dict(open_positions=1, all_positions_protected=False), "unprotected_exposure"),
    ],
)
def test_account_guards_fail_closed(env, change, expect):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(unprotected_grace_s=0.0, transient_grace_s=0.0)  # grace 0: transient => fail closed at once
    r.start()
    env.stack.set_account(**change)
    if "open_positions" in change:
        env.stack.positions["x"] = intent  # account_snapshot derives open_positions from positions
        env.stack.set_account(all_positions_protected=False)
    r.run_cycle()
    assert r.fail_reason and expect in r.fail_reason
    assert env.stack.halts
    assert env.stack.submits == []


def test_non_demo_at_start_fails_closed(env):
    env.stack.set_account(is_demo=False)
    r = env.build()
    assert r.run(max_cycles=2) == 7


def test_clock_anomaly_backwards(env):
    r = env.build()
    _cycle(r)
    env.clock.advance(minutes=-30)
    r.run_cycle()
    assert "clock_anomaly" in (r.fail_reason or "")


def test_server_time_skew_is_clock_anomaly(env):
    env.stack.set_account(server_time_utc=env.clock() + timedelta(hours=2))
    r = env.build()
    _cycle(r)
    assert "clock_anomaly" in (r.fail_reason or "")


def _advancing_cycles(env, r, lag_s, n=4, step_s=5):
    """n cycles in which the broker tick ADVANCES each cycle while it sits ``lag_s`` behind the local clock
    (negative lag = tick ahead of the local clock). A live, moving tick is a usable clock reference."""
    r.start()
    for _ in range(n):
        env.clock.advance(seconds=step_s)
        env.stack.set_account(server_time_utc=env.clock() - timedelta(seconds=lag_s))
        r.run_cycle()
        if r.fail_reason:
            break


def _hb_clock_reference(r):
    return json.loads(r.cfg.heartbeat_path.read_text())["clock_reference"]


def test_clock_a_stale_tick_305s_behind_is_not_clock_anomaly(env):
    # A paused market freezes its tick time far behind the local clock: quote staleness, not clock skew.
    env.stack.set_account(server_time_utc=env.clock() - timedelta(seconds=305))
    r = env.build()
    r.start()
    for _ in range(4):
        env.clock.advance(seconds=5)
        r.run_cycle()
    assert "clock_anomaly" not in (r.fail_reason or "")
    assert _hb_clock_reference(r) == "UNAVAILABLE"


def test_clock_b_fresh_tick_local_clock_ahead_is_anomaly(env):
    r = env.build()
    _advancing_cycles(env, r, lag_s=400)  # moving tick, local clock 400 s ahead of it
    assert "clock_anomaly" in (r.fail_reason or "")


def test_clock_c_fresh_tick_local_clock_behind_is_anomaly(env):
    r = env.build()
    _advancing_cycles(env, r, lag_s=-400)  # moving tick, 400 s ahead of the local clock
    assert "clock_anomaly" in (r.fail_reason or "")


def test_clock_e_all_markets_stale_no_false_anomaly_and_staleness_stays_reported(env):
    env.stack.set_account(server_time_utc=env.clock() - timedelta(hours=9))  # weekend-old, frozen
    r = env.build()
    r.start()
    for _ in range(4):
        env.clock.advance(seconds=30)
        r.run_cycle()
    assert "clock_anomaly" not in (r.fail_reason or "")
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    assert hb["clock_reference"] == "UNAVAILABLE"
    assert "market_state" in hb and "idle_all_markets_closed" in hb  # stale/closed logic keeps running


def test_clock_f_frozen_weekend_tick_keeps_runner_healthy_and_opens_no_exposure(env):
    env.stack.set_account(server_time_utc=env.clock() - timedelta(days=1))
    r = env.build()
    r.start()
    for _ in range(3):
        env.clock.advance(seconds=30)
        r.run_cycle()
    assert r.fail_reason is None
    assert env.stack.submits == []


def test_clock_g_normal_fresh_quotes_unchanged(env):
    r = env.build()
    _advancing_cycles(env, r, lag_s=1)
    assert r.fail_reason is None
    assert _hb_clock_reference(r) == "OK"


def test_clock_static_tick_far_ahead_is_still_anomaly(env):
    # A stale quote can never lie in the future: a tick far AHEAD of the local clock is fatal even if frozen.
    env.stack.set_account(server_time_utc=env.clock() + timedelta(hours=2))
    r = env.build()
    _cycle(r)
    assert "clock_anomaly" in (r.fail_reason or "")


def test_disk_low_fail_closed(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(disk_free=lambda: 1 * GIB, min_disk_free_bytes=2 * GIB)
    r.start()
    r.run_cycle()
    assert r.fail_reason and r.fail_reason.startswith("disk_low")
    assert env.stack.submits == [] and env.stack.halts
    assert json.loads(r.cfg.heartbeat_path.read_text())["disk_free_bytes"] == GIB


def test_single_stale_market_is_skipped_all_stale_halts(env):
    a = make_pair(market="GER40")
    b = make_pair(market="NAS100", tag="n")
    env.engine.push("GER40", a)
    env.engine.push("NAS100", b)
    env.stack.bar_source.frozen["NAS100"] = env.clock()
    env.clock.advance(minutes=30)  # NAS100 frozen 30 min ago -> stale; GER40 live
    r = env.build(markets=("GER40", "NAS100"), all_stale_grace_s=900.0, all_stale_exit_s=900.0)
    _cycle(r)
    assert {m for m, _ in env.engine.calls} == {"GER40"}
    assert r.fail_reason is None
    assert json.loads(r.cfg.heartbeat_path.read_text())["stale_markets"] == ["NAS100"]
    env.stack.bar_source.frozen["GER40"] = env.clock()
    env.clock.advance(minutes=30)
    r.run_cycle()  # both stale, grace not over
    assert r.fail_reason is None
    env.clock.advance(minutes=20)
    r.run_cycle()
    assert r.fail_reason == "all_feeds_stale"


def test_stack_failclosed_from_poll(env):
    r = env.build()
    r.start()

    def boom():
        raise StackFailClosed("feed died")

    env.stack.poll_events = boom
    r.run_cycle()
    assert "feed died" in (r.fail_reason or "")


def test_exit_managed_after_fail_closed_then_stop(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(transient_grace_s=0.0)
    _cycle(r)
    assert env.store.get_state(intent.intent_id) == PROTECTED
    env.stack.set_account(reconciliation="MISMATCH")
    r.run_cycle()
    assert r.fail_reason
    assert not r._should_exit(env.clock())  # position open -> keep managing exits
    env.stack.close_position(intent.intent_id)
    env.clock.advance(minutes=35)
    r.run_cycle()
    assert env.store.get_outcome(intent.intent_id) is not None  # exit still processed
    assert r._should_exit(env.clock())


# ------------------------------------------------------------------ milestones / reports
def test_milestone_report_at_ten_trades_once(env):
    r = env.build()
    r.start()
    def nxt(k):
        return make_pair(signal_ts=floor5(env.clock()), tag=f"t{k}", direction=1)

    cur = nxt(0)
    env.engine.push("GER40", cur)
    r.run_cycle()
    for k in range(10):
        env.stack.close_position(cur[2].intent_id, exit_price=103.0 if k % 2 == 0 else 98.5,
                                 reason="TARGET" if k % 2 == 0 else "STOP")
        env.clock.advance(minutes=40)
        cur = nxt(k + 1)
        env.engine.push("GER40", cur)  # next bar's opportunity; the close is managed first
        r.run_cycle()
        if k < 9:
            assert r.milestones == []
    assert env.store.count_trades("DISCOVERY") == 10
    assert r.milestones == [10]
    r.check_milestones()
    r.run_cycle()
    assert r.milestones == [10]
    reports = sorted(p.name for p in r.cfg.reports_dir.iterdir())
    assert reports == ["report-DISCOVERY-n10.json", "report-DISCOVERY-n10.md"]


def test_analyze_exports(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    _cycle(r)
    art = env.tmp / "art"
    s = monitor.analyze(env.store, None, art / "reports", art / "export")
    assert s["n_decisions"] == 1 and s["phase"] == "ALL"
    assert (art / "reports" / "report-ALL-analyze.md").exists()
    assert any(s["export"].values())
    s2 = monitor.analyze(env.store, "FROZEN", art / "reports", art / "export")
    assert s2["n_decisions"] == 0


# ----------------------------------------------------------------- heartbeat / status
def test_heartbeat_contents_and_staleness(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    _cycle(r)
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    for key in (
        "mode", "runner_mode", "phase", "equity", "balance", "floating_pl", "cumulative_r", "reconciliation",
        "open_positions", "open_orders", "protection_state", "opportunities_today", "trades_today",
        "last_signal", "last_fill", "last_error", "learning_samples", "champion", "challengers",
        "process_alive", "pid", "started_utc", "mt5_connected", "feed", "last_persistence_write",
        "disk_free_bytes", "git_commit", "calendar_status",
    ):
        assert key in hb, key
    assert hb["mode"] == "DEMO MODE" and hb["champion"] == "static-demo-policy-v1"
    assert hb["last_signal"]["accepted"] is True and hb["last_fill"]["market"] == "GER40"
    assert hb["feed"]["GER40"]["bar_age_s"] == pytest.approx(10.0)
    assert hb["calendar_status"]["GER40"] in ("provisional", "verified_current_constants")
    assert hb["trades_today"] == 1
    now = env.clock()
    assert monitor.heartbeat_verdict(r.cfg.heartbeat_path, now)["verdict"] == "RUNNING"
    assert monitor.heartbeat_verdict(r.cfg.heartbeat_path, now + timedelta(seconds=91))["verdict"] == "NOT RUNNING"
    assert monitor.heartbeat_verdict(env.tmp / "nope.json", now)["verdict"] == "NOT RUNNING"


def test_orderly_stop_file(env):
    r = env.build()
    r.start()
    r.cfg.stop_file.parent.mkdir(parents=True, exist_ok=True)
    r.cfg.stop_file.write_text("stop")
    assert r.run(max_cycles=5) == 0
    assert env.stack.stopped
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    assert hb["process_alive"] is False and hb["stop_reason"] == "stop_file"
    assert (r.cfg.reports_dir / "report-DISCOVERY-final.md").exists()
    assert monitor.heartbeat_verdict(r.cfg.heartbeat_path, env.clock())["verdict"] == "NOT RUNNING"


# --------------------------------------------------- separation / phases / learning
class SpyPredictor:
    def __init__(self, store):
        self.store, self.seen = store, []

    def predict(self, snapshot, predicted_utc=None):
        # at prediction time: snapshot persisted, decision and outcome NOT yet
        self.seen.append((
            self.store.get_snapshot(snapshot.opportunity_id) is not None,
            self.store.get_decision(snapshot.opportunity_id) is None,
            self.store.get_outcome(snapshot.opportunity_id) is None,
        ))
        return {"spy": {"status": "ok", "p_target_before_stop": 0.5}}


def test_predictions_before_decision_and_outcome_after_close(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    spy = SpyPredictor(env.store)
    r = env.build(predictor=spy)
    _cycle(r)
    assert spy.seen == [(True, True, True)]
    preds = env.store.list_shadow_predictions(snap.opportunity_id)
    assert len(preds) == 1
    assert env.store.get_decision(snap.opportunity_id).shadow["spy"]["status"] == "ok"
    assert env.store.get_outcome(intent.intent_id) is None  # still open: no outcome
    env.stack.close_position(intent.intent_id)
    env.clock.advance(minutes=35)
    r.run_cycle()
    ev = env.store.intent_events(intent.intent_id)
    assert ev[-1]["to_state"] == "CLOSED"
    assert env.store.get_outcome(intent.intent_id) is not None
    assert r.status()["challengers"] == {"spy": "ok"}


def test_predictor_failure_does_not_stop_trading(env):
    class Boom:
        def predict(self, *a, **k):
            raise RuntimeError("model exploded")

    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(predictor=Boom())
    _cycle(r)
    assert env.store.get_state(intent.intent_id) == PROTECTED
    assert "model exploded" in r._last_error["text"] and r.fail_reason is None


def test_trainer_is_throttled_and_contained(env):
    calls = []

    class Trainer:
        def update(self, store):
            calls.append(1)
            raise RuntimeError("train boom")

    r = env.build(trainer=Trainer(), trainer_every_s=600.0)
    _cycle(r)
    assert calls == []
    env.clock.advance(minutes=11)
    r.run_cycle()
    r.join_training(5)
    r.run_cycle()
    r.join_training(5)
    assert len(calls) == 1 and r.fail_reason is None
    assert "train boom" in r._last_error["text"]


def test_phase_tagging_and_separation(env):
    d = make_pair(phase="DISCOVERY", tag="d")
    env.engine.push("GER40", d)
    r1 = env.build(phase="DISCOVERY")
    _cycle(r1)
    env.stack.close_position(d[2].intent_id)
    env.clock.advance(minutes=35)
    r1.run_cycle()

    env.clock.advance(minutes=5)
    f = make_pair(phase="FROZEN", tag="f", signal_ts=floor5(env.clock()))
    env.engine2 = ScriptedEngine()
    env.engine2.push("GER40", f)
    r2 = env.build(phase="FROZEN", engine=env.engine2)
    _cycle(r2)
    env.stack.close_position(f[2].intent_id)
    env.clock.advance(minutes=35)
    r2.run_cycle()

    st = env.store
    assert [s.opportunity_id for s in st.list_snapshots("DISCOVERY")] == [d[0].opportunity_id]
    assert [s.opportunity_id for s in st.list_snapshots("FROZEN")] == [f[0].opportunity_id]
    assert st.count_trades("DISCOVERY") == 1 and st.count_trades("FROZEN") == 1
    assert st.get_intent(d[2].intent_id)["phase"] == "DISCOVERY"
    assert st.get_intent(f[2].intent_id)["phase"] == "FROZEN"
    assert all(st.get_risk(i).approved for i in (d[2].intent_id, f[2].intent_id))
    assert monitor_milestone_keys(st) == set()  # 2 trades: no milestone in either phase


def test_phase_mismatch_between_engine_and_runner_fails_closed(env):
    env.engine.push("GER40", make_pair(phase="FROZEN"))
    r = env.build(phase="DISCOVERY")
    _cycle(r)
    assert "phase_mismatch" in (r.fail_reason or "")
    assert env.stack.submits == []


def test_learning_import_failure_does_not_stop_runner(env, monkeypatch):
    monkeypatch.setitem(sys.modules, "demo.learning.shadow", None)  # import raises
    pred, trainer, err = rn.load_learning(None, None)
    assert pred is None and trainer is None and err
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(predictor=pred, trainer=trainer, learning_error=err)
    _cycle(r)
    assert env.store.get_state(intent.intent_id) == PROTECTED
    assert r.status()["challengers"]["status"] == err


def test_learning_disabled_imports_nothing(monkeypatch):
    monkeypatch.delitem(sys.modules, "demo.learning.shadow", raising=False)
    assert rn.load_learning(None, False)[2] == "disabled"
    assert "demo.learning.shadow" not in sys.modules


# ------------------------------------------------------------------------ clock chain
def test_clock_chain_real_markets_provisional_preserved(env):
    rows = rn.verify_clock_chain(("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD"), now=env.clock())
    assert all(r.ok for r in rows), [r for r in rows if not r.ok]
    assert {r.calendar_status for r in rows} <= {"provisional", "verified_current_constants"}
    assert "GER40" in rn.render_clock_table(rows)


def test_failed_clock_chain_disables_market(env):
    real = None

    def loader(m):
        nonlocal real
        from markets.spec import load_market_spec

        real = load_market_spec(m)
        if m == "NAS100":
            import dataclasses as dc

            cal = dc.replace(real.calendar, tz="Not/AZone")
            return dc.replace(real, calendar=cal)
        return real

    env.engine.push("GER40", make_pair(market="GER40"))
    env.engine.push("NAS100", make_pair(market="NAS100", tag="n"))
    r = env.build(markets=("GER40", "NAS100"), spec_loader=loader)
    _cycle(r)
    assert "NAS100" in r.disabled and "GER40" not in r.disabled
    assert {m for m, _ in env.engine.calls} == {"GER40"}
    assert r.fail_reason is None


def test_all_markets_failing_clock_chain_fails_closed(env):
    def loader(m):
        raise KeyError(m)

    r = env.build(spec_loader=loader)
    assert r.run(max_cycles=1) == 7


# ------------------------------------------------------------------------ bar adapter
def test_stack_bar_source_is_the_engine_bar_source_without_adapter(env):
    from demo.opportunity.bar_source import BarSource, validate_frame

    src = env.stack.bar_source  # the LiveBarSource surface itself: no adapter, no frame() assumption
    assert isinstance(src, BarSource) and not hasattr(rn, "StackBarAdapter")
    fr = src.m5_frame("GER40", 30)
    validate_frame(fr, "GER40")
    assert len(fr) == 30
    q = src.latest_quote("GER40")
    assert q is not None and q.valid
    assert src.last_closed_bar_close_utc("GER40") == fr["ts"].iloc[-1].to_pydatetime() + M5


def test_planned_intent_is_persisted_before_submit(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    seen_states = []
    orig = env.stack.submit

    def spy(i, context=None):
        seen_states.append(env.store.get_state(i.intent_id))
        return orig(i, context)

    env.stack.submit = spy
    _cycle(env.build())
    assert seen_states == [PLANNED]


def test_expired_intent_is_cancelled_not_sent(env):
    snap, dec, intent = make_pair(valid_s=5)  # valid 5 s, runner now = signal + 10 s
    env.engine.push("GER40", (snap, dec, intent))
    _cycle(env.build())
    assert env.stack.submits == []
    assert env.store.get_state(intent.intent_id) == CANCELLED


def test_no_global_one_position_rule_second_same_market_intent_reaches_the_stack(env):
    """Same-symbol handling is the STACK's job (broker netting / ADDON_* codes), not the runner's."""
    from decimal import Decimal

    from demo.execution.events import Accepted, Rejected

    a = make_pair(tag="a")
    env.engine.push("GER40", a)
    r = env.build()
    _cycle(r)
    env.clock.advance(minutes=5)
    b = make_pair(tag="b", signal_ts=floor5(env.clock()))
    env.stack.script[b[2].intent_id] = [
        Accepted(b[2].intent_id, Decimal("1")),
        Rejected(b[2].intent_id, "ADDON_EXPOSURE_NOT_SUPPORTED_V1", {"otherwise_valid": True}),
    ]
    env.engine.push("GER40", b)
    r.run_cycle()
    assert len(env.stack.submits) == 2  # the runner did NOT swallow the second same-market intent
    assert env.store.get_state(b[2].intent_id) == CANCELLED
    rd = env.store.get_risk_detail(b[2].intent_id, "REJECTED")
    assert rd["reject_code"] == "ADDON_EXPOSURE_NOT_SUPPORTED_V1" and rd["gate_reject_class"] == "TEMPORARY"


def test_fill_states_walk(env):
    # FILLED without protection stays FILLED
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    env.stack.mode = "accept_only"
    _cycle(env.build())
    assert env.store.get_state(intent.intent_id) == "SENT"
    assert FILLED == "FILLED"


def test_clock_object_helpers():
    c = FakeClock()
    assert c() == T0
    assert isinstance(FakeStack(c).default_account().equity, float)
    assert ScriptedEngine().on_m5_close("X", T0) == []
    assert monitor.git_commit()
