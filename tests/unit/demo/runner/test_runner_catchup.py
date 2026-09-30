# ruff: noqa: E501
"""Closed-bar catch-up (Lane R2): every closed M5 bar since the persisted pointer is evaluated in
chronological order; past bars are recorded but NEVER traded late; the newest bar stays tradable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from demo.contracts import Decision
from demo.store import DemoStore
from demo.testing import M5, T0, FakeClock, FakeStack, ScriptedEngine, floor5, make_pair


def _missed(snap, *, extra=()):
    return Decision(
        opportunity_id=snap.opportunity_id, phase=snap.phase, decided_utc=snap.created_utc, accepted=False,
        reasons=("CATCHUP_MISSED", "EXPIRED_ENTRY", *extra), policy_id="static-demo-policy-v1",
    )


def _close_of_last_bar(clock):
    return floor5(clock())  # close of the newest closed bar == floor to the grid


def test_first_run_processes_only_the_newest_bar_and_persists_pointer(env):
    r = env.build()
    r.start()
    r.run_cycle()
    assert len(env.engine.calls) == 1 and env.engine.catchup_calls == []
    assert env.store.get_bar_pointer("GER40") == _close_of_last_bar(env.clock).isoformat()


def test_three_bar_gap_is_evaluated_chronologically_and_never_traded(env):
    r = env.build()
    r.start()
    r.run_cycle()  # pointer = 09:00
    env.clock.advance(minutes=15)  # 09:15:10 -> closes 09:05, 09:10, 09:15 are pending
    # the past bars would have produced an accepted trade; the newest (live) bar has a fresh accepted one
    s1, _, _ = make_pair(signal_ts=T0 + M5, tag="p1")
    s2, _, _ = make_pair(signal_ts=T0 + 2 * M5, tag="p2")
    env.engine.push_catchup("GER40", (s1, _missed(s1), None))
    env.engine.push_catchup("GER40", (s2, _missed(s2, extra=("ALREADY_MOVED",)), None))
    live_snap, live_dec, live_intent = make_pair(signal_ts=T0 + 3 * M5, tag="live", valid_s=300)
    env.engine.push("GER40", (live_snap, live_dec, live_intent))
    r.run_cycle()
    cu = env.engine.catchup_calls
    assert [c[1] for c in cu] == [T0 + M5, T0 + 2 * M5]  # chronological, evaluated AT their own close
    assert all(c[2].live_now == env.clock() for c in cu)
    # recorded + explained, never an intent
    for s in (s1, s2):
        assert env.store.get_decision(s.opportunity_id).reasons[0] == "CATCHUP_MISSED"
        assert env.store.intent_for_opportunity(s.opportunity_id) is None
    assert {i.opportunity_id for i in env.stack.submits} == {live_snap.opportunity_id}
    assert env.store.get_bar_pointer("GER40") == (T0 + 3 * M5).isoformat()


def test_newest_bar_still_tradable_after_catchup(env):
    r = env.build()
    r.start()
    r.run_cycle()
    env.clock.advance(minutes=10)
    snap, dec, intent = make_pair(signal_ts=T0 + 2 * M5, tag="n")
    env.engine.push("GER40", (snap, dec, intent))
    r.run_cycle()
    assert len(env.engine.calls) == 2 and len(env.engine.catchup_calls) == 1
    assert [i.intent_id for i in env.stack.submits] == [intent.intent_id]


def test_accepted_decision_on_a_past_bar_is_never_submitted_defence_in_depth(env):
    r = env.build()
    r.start()
    r.run_cycle()
    env.clock.advance(minutes=10, seconds=-10)
    snap, dec, intent = make_pair(signal_ts=T0 + M5, tag="bad")
    env.engine.push_catchup("GER40", (snap, dec, intent))  # a buggy engine returns an ACCEPTED past decision
    r.run_cycle()
    assert env.stack.submits == []
    assert env.store.intent_for_opportunity(snap.opportunity_id) is None
    assert "catchup_accepted_not_submitted" in r.status()["last_error"]["text"]


def test_restart_mid_stream_continues_from_persisted_pointer(env):
    r1 = env.build()
    r1.start()
    r1.run_cycle()
    env.clock.advance(minutes=5)
    r1.run_cycle()
    assert env.store.get_bar_pointer("GER40") == (T0 + M5).isoformat()
    # process dies; 3 more bars close while down; a NEW runner on the same store
    env.clock.advance(minutes=15)
    r2 = env.build()
    r2.start()
    r2.run_cycle()
    assert [c[1] for c in env.engine.catchup_calls] == [T0 + 2 * M5, T0 + 3 * M5]
    assert env.store.get_bar_pointer("GER40") == (T0 + 4 * M5).isoformat()
    # a further restart without new bars evaluates nothing
    calls = len(env.engine.calls) + len(env.engine.catchup_calls)
    r3 = env.build()
    r3.start()
    r3.run_cycle()
    assert len(env.engine.calls) + len(env.engine.catchup_calls) == calls


def test_catchup_is_bounded_by_age_and_bar_count(env):
    env.stack.bar_source.n = 400
    r = env.build(catchup_max_bars=4, catchup_max_age_s=3600.0)
    r.start()
    r.run_cycle()
    env.clock.advance(hours=3)  # 36 bars missed; only the last 4 (within 1 h and <= 4) are evaluated
    r.run_cycle()
    closes = [c[1] for c in env.engine.catchup_calls]
    assert len(closes) == 3 and closes == sorted(closes)  # 4 kept = 3 catch-up + the live newest
    assert r._catchup["GER40"]["skipped_old"] == 36 - 4
    assert env.store.get_bar_pointer("GER40") == _close_of_last_bar(env.clock).isoformat()


def test_weekend_gap_closed_market_bars_are_skipped_not_evaluated(tmp_path):
    friday = datetime(2026, 10, 2, 13, 0, tzinfo=UTC)  # Friday
    clock = FakeClock(friday + timedelta(seconds=10))
    store = DemoStore(tmp_path / "w.sqlite", clock=lambda: clock().isoformat())
    stack = FakeStack(clock, markets=("GER40",))
    stack.bar_source.n = 1000
    eng = ScriptedEngine()
    from demo.runner import DemoRunner, RunnerConfig

    cfg = RunnerConfig(mode="demo-auto", markets=("GER40",), artifacts_dir=tmp_path / "a", poll_interval_s=1.0,
                       min_disk_free_bytes=1, catchup_max_age_s=4 * 86400.0, catchup_max_bars=1000)
    r = DemoRunner(stack, eng, store, config=cfg, clock=clock, sleep=lambda s: None, disk_free=lambda: 10**12)
    r.start()
    r.run_cycle()
    clock.now = datetime(2026, 10, 5, 7, 2, tzinfo=UTC)  # Monday, before/around the Berlin cash open
    r.run_cycle()
    st = r._catchup["GER40"]
    assert st["skipped_closed"] > 0
    evaluated_closes = [c[1] for c in eng.catchup_calls]
    # nothing inside Saturday/Sunday was ever evaluated
    assert all(c.weekday() < 5 for c in evaluated_closes)
    assert store.get_bar_pointer("GER40") == floor5(clock()).isoformat()
    store.close()


# ---------------------------------------------------------------- audit items A and B
def test_bars_are_recorded_while_halted_and_the_intent_is_cancelled_halted(env):
    snap, dec, intent = make_pair(signal_ts=T0, tag="h")
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    env.stack.set_account(reconciliation="RECONCILING")  # transient condition -> halt-new-exposure
    r.run_cycle()
    assert not r.can_trade()
    assert env.store.get_decision(snap.opportunity_id) is not None  # recorded, not silently dropped
    assert env.store.get_state(intent.intent_id) == "CANCELLED"
    ev = env.store.intent_events(intent.intent_id)[-1]
    assert ev["to_state"] == "CANCELLED" and "halted" in str(ev["detail"])
    assert env.stack.submits == []
    assert env.store.get_bar_pointer("GER40") == T0.isoformat()


def test_scan_exception_is_retried_once_then_recorded_as_scan_error(env):
    class Boom(ScriptedEngine):
        def __init__(self, fail_times):
            super().__init__()
            self.fail_times, self.attempts = fail_times, 0

        def on_m5_close(self, market, now, *, catchup=None):
            self.attempts += 1
            if self.attempts <= self.fail_times:
                raise ValueError("boom")
            return super().on_m5_close(market, now, catchup=catchup)

    eng = Boom(1)
    snap, dec, intent = make_pair(signal_ts=T0, tag="r")
    eng.push("GER40", (snap, dec, intent))
    r = env.build(engine=eng)
    r.start()
    r.run_cycle()
    assert eng.attempts == 2 and env.store.scan_errors() == []  # one failure: retried, nothing lost
    assert env.store.get_decision(snap.opportunity_id) is not None

    eng2 = Boom(2)
    env2_store = env.store
    r2 = env.build(engine=eng2)
    env.clock.advance(minutes=5)
    r2.start()
    r2.run_cycle()
    errs = env2_store.scan_errors()
    assert len(errs) == 1 and errs[0]["market"] == "GER40" and "boom" in errs[0]["error"]
    assert env2_store.get_bar_pointer("GER40") == (T0 + M5).isoformat()  # auditable, then moved on
    assert any(w.startswith("SCAN_ERROR") for w in r2.status()["warnings"])


def test_seen_id_is_not_committed_before_the_snapshot(env):
    from demo.runner import StoreSeenAdapter

    snap, _dec, _ = make_pair(tag="s")
    ad = StoreSeenAdapter(env.store)
    assert ad.add_if_new(snap.opportunity_id) is True
    assert ad.add_if_new(snap.opportunity_id) is False  # in-cycle dedupe
    # an exception between build and persist: NOTHING durable
    assert env.store.seen(snap.opportunity_id) is False
    assert StoreSeenAdapter(env.store).add_if_new(snap.opportunity_id) is True  # e.g. after a restart
    ad.release_all()
    assert ad.add_if_new(snap.opportunity_id) is True  # retry of the same bar works
    env.store.record_snapshot(snap)  # the snapshot commits the seen id in the same transaction
    assert env.store.seen(snap.opportunity_id) is True
    assert StoreSeenAdapter(env.store).add_if_new(snap.opportunity_id) is False
