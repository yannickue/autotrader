# ruff: noqa: E501
"""Lane P runner semantics: broker-session aware feed states (N, O), entry gate in the flatten window, --daily shutdown."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from demo.opportunity.operating_policy import load_operating_policy
from demo.runner import DemoRunner, RunnerConfig
from demo.store import CANCELLED, DemoStore
from demo.testing import FakeClock, FakeStack, ScriptedEngine, make_pair

BERLIN = ZoneInfo("Europe/Berlin")
POL = load_operating_policy()


def berlin(hh: int, mm: int = 0, day=(2026, 7, 15)) -> datetime:  # Wed 2026-07-15 (CEST)
    return datetime(*day, hh, mm, tzinfo=BERLIN).astimezone(UTC)


@dataclass
class EodStack(FakeStack):
    """FakeStack + the optional ``eod_status`` port of Mt5DemoStack (state is scripted by the test)."""

    eod: str = "IDLE"
    eod_confirmed: str | None = None
    eod_detail: str | None = None

    def eod_status(self):
        return {"flatten_state": self.eod, "eod_flat_confirmed_utc": self.eod_confirmed,
                "eod_detail": self.eod_detail, "eod_own_positions_open": 0}


def _rig(tmp_path, start, markets=("GER40",), policy=POL, daily=False, **cfg):
    clock = FakeClock(start)
    store = DemoStore(tmp_path / "w.sqlite", clock=lambda: clock().isoformat())
    stack = EodStack(clock, markets=markets)
    stack.bar_source.n = 200
    eng = ScriptedEngine()
    kw = dict(mode="demo-auto", markets=tuple(markets), artifacts_dir=tmp_path / "a", poll_interval_s=5.0,
              min_disk_free_bytes=1, all_stale_grace_s=900.0, all_stale_exit_s=7200.0,
              operating_policy=policy, daily=daily)
    kw.update(cfg)
    r = DemoRunner(stack, eng, store, config=RunnerConfig(**kw), clock=clock, sleep=lambda s: clock.advance(seconds=s),
                   disk_free=lambda: 10**12)
    return clock, store, stack, eng, r


# ------------------------------------------------------------------------------------ N: pause
def test_N_known_broker_pause_is_not_a_stale_fault_and_healthy_markets_keep_trading(tmp_path):
    markets = ("NAS100", "XAUUSD", "EURUSD")
    clock, store, stack, eng, r = _rig(tmp_path, berlin(22, 40), markets=markets)
    r.start()
    r.run_cycle()
    assert set(r._market_state.values()) == {"FRESH"}  # 22:40 Berlin: cash closed but the broker session is OPEN
    # 23:00-00:00 Berlin: NAS100 / XAUUSD are paused by the broker, their feeds freeze at the break
    for m in ("NAS100", "XAUUSD"):
        stack.bar_source.frozen[m] = berlin(23, 0)
    clock.now = berlin(23, 40)
    for _ in range(30):  # 23:40 .. 23:55 in 30 s steps: far beyond stale_feed_s / all_stale_grace_s
        clock.advance(seconds=30)
        r.run_cycle()
        assert r.fail_reason is None and r.halt_reason is None and r._stale_halt_since is None and not r._transient
    st = r.status()
    assert st["market_state"] == {"NAS100": "KNOWN_SESSION_PAUSE", "XAUUSD": "KNOWN_SESSION_PAUSE", "EURUSD": "FRESH"}
    assert r.can_trade() and any(m == "EURUSD" for m, _ in eng.calls)  # the healthy market keeps being scanned
    assert "NAS100" in st["stale_markets"] and st["idle_all_markets_closed"] is False
    assert stack.clock_calls > 0  # open-position management (on_clock) runs every cycle, pause or not
    store.close()


def test_N_the_pause_is_idle_not_fault_even_when_every_enabled_market_is_paused(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(22, 40), markets=("NAS100", "XAUUSD"))
    r.start()
    r.run_cycle()
    for m in ("NAS100", "XAUUSD"):
        stack.bar_source.frozen[m] = berlin(23, 0)
    clock.now = berlin(23, 50)
    r.run_cycle()
    assert r._market_state == {"NAS100": "KNOWN_SESSION_PAUSE", "XAUUSD": "KNOWN_SESSION_PAUSE"}
    assert r._idle_all and r.fail_reason is None and r._stale_halt_since is None
    store.close()


def test_O_genuine_stale_inside_an_open_broker_session_is_a_real_fault(tmp_path):
    # GER40 cash closed at 17:30 Berlin but the broker session runs to 22:00: silence at 19:30 is a fault
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(19, 0), all_stale_grace_s=600.0, all_stale_exit_s=1800.0)
    r.start()
    r.run_cycle()
    assert r._market_state["GER40"] == "FRESH"
    stack.bar_source.frozen["GER40"] = clock()
    clock.advance(minutes=30)
    r.run_cycle()
    assert r._market_state["GER40"] == "STALE_FAULT" and not r._idle_all
    clock.advance(minutes=11)
    r.run_cycle()
    assert r._stale_halt_since is not None and not r.can_trade()
    clock.advance(minutes=31)
    r.run_cycle()
    assert r.fail_reason == "all_feeds_stale"
    store.close()


def test_O_without_the_policy_the_same_silence_was_hidden_as_closed_idle(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(19, 0), policy=None)
    r.start()
    r.run_cycle()
    stack.bar_source.frozen["GER40"] = clock()
    clock.advance(minutes=30)
    r.run_cycle()
    assert r._market_state["GER40"] == "CLOSED_IDLE"  # the old cash-session-only semantics (default unchanged)
    store.close()


def test_weekend_stays_closed_idle_under_the_policy(tmp_path):
    _clock, store, stack, _eng, r = _rig(tmp_path, berlin(10, 0, day=(2026, 7, 18)))  # Saturday
    r.start()
    stack.bar_source.frozen["GER40"] = berlin(23, 0, day=(2026, 7, 17)) - timedelta(hours=8)
    r.run_cycle()
    assert r._market_state["GER40"] == "CLOSED_IDLE" and r._idle_all and r.fail_reason is None
    store.close()


# ----------------------------------------------------------------------- entry gate in the runner
def test_runner_cancels_an_accepted_intent_inside_the_flatten_window(tmp_path):
    clock, store, stack, eng, r = _rig(tmp_path, berlin(21, 56))
    now = clock()
    snap, dec, intent = make_pair(signal_ts=now - timedelta(minutes=5), valid_s=900)
    eng.push("GER40", (snap, dec, intent))
    r.start()
    r.run_cycle()
    assert stack.submits == []
    assert store.get_state(intent.intent_id) == CANCELLED
    events = [e for e in store.intent_events(intent.intent_id)]
    assert events and "flatten_window_active" in json.dumps(events[-1], default=str)
    store.close()


def test_daily_mode_refuses_entries_outside_the_operating_day(tmp_path):
    clock, store, stack, eng, r = _rig(tmp_path, berlin(10, 0, day=(2026, 7, 18)), daily=True)  # Saturday
    snap, dec, intent = make_pair(signal_ts=clock() - timedelta(minutes=5), valid_s=900)
    eng.push("GER40", (snap, dec, intent))
    r.start()
    r.run_cycle()
    assert stack.submits == [] and store.get_state(intent.intent_id) == CANCELLED
    assert "outside_operating_day" in json.dumps(list(store.intent_events(intent.intent_id)), default=str)
    store.close()


# ----------------------------------------------------------------------------- --daily shutdown
def _confirmed(stack, clock):
    stack.eod, stack.eod_confirmed = "FLAT_CONFIRMED", clock().isoformat()


def test_daily_exits_zero_with_eod_flat_shutdown_only_after_deadline_when_flat_and_reconciled(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(21, 57), daily=True)
    _confirmed(stack, clock)
    code = r.run()
    assert code == 0 and r.exit_code == 0 and r.stop_reason == "eod_flat_shutdown"
    assert r.fail_reason is None and stack.stopped
    assert clock() >= berlin(22, 0)  # it did NOT exit before the deadline although flat was already confirmed
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    assert hb["stop_reason"] == "eod_flat_shutdown" and hb["process_alive"] is False
    assert hb["flatten_state"] == "FLAT_CONFIRMED" and hb["eod_flat_confirmed_utc"] and hb["daily_mode"] is True
    assert hb["operating_policy"]["version"] == "live-op-3"
    store.close()


def test_daily_never_exits_while_not_flat_or_not_reconciled_and_reports_overdue_loudly(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(22, 5), daily=True)
    stack.eod, stack.eod_detail = "OVERDUE", "1 own position(s) still open; close_failed:GER40:x3"
    r.start()
    for _ in range(40):
        r.run_cycle()
        assert not r._should_exit(clock())
        clock.advance(seconds=10)
    assert r.stop_reason is None and r.exit_code is None
    assert r.status()["flatten_state"] == "OVERDUE"
    assert "eod_flat_overdue" in (r._last_error or {}).get("text", json.dumps(r._last_error, default=str))
    # flat confirmed by the stack, but the account is not reconciled / still shows a position: still no exit
    _confirmed(stack, clock)
    stack.set_account(reconciliation="MISMATCH")
    r._last_account_at = None
    r.run_cycle()
    assert not r._should_exit(clock())
    stack.set_account(reconciliation="RECONCILED")
    r.run_cycle()
    assert r._should_exit(clock()) and r.stop_reason == "eod_flat_shutdown"
    store.close()


def test_daily_off_by_default_never_shuts_down_at_the_deadline(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(22, 5), daily=False)
    _confirmed(stack, clock)
    r.start()
    for _ in range(5):
        r.run_cycle()
        clock.advance(seconds=10)
        assert not r._should_exit(clock())
    assert RunnerConfig().daily is False and RunnerConfig().operating_policy is None
    store.close()
