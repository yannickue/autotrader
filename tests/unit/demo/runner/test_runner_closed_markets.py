# ruff: noqa: E501
"""Overnight / weekend: closed is IDLE, not a fault; resume with bar catch-up; a market that should be
open and is silent IS the fault."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from demo.runner import DemoRunner, RunnerConfig
from demo.store import DemoStore
from demo.testing import FakeClock, FakeStack, ScriptedEngine

FRI_CLOSE = datetime(2026, 10, 2, 15, 30, tzinfo=UTC)  # Friday, GER40 cash close (17:30 Berlin, CEST)
MON_OPEN = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)  # Monday, GER40 cash open (09:00 Berlin)


class CountingStack(FakeStack):
    snaps = 0

    def account_snapshot(self):
        type(self).snaps += 1
        return super().account_snapshot()


def _rig(tmp_path, start, **cfg):
    clock = FakeClock(start)
    store = DemoStore(tmp_path / "w.sqlite", clock=lambda: clock().isoformat())
    CountingStack.snaps = 0
    stack = CountingStack(clock, markets=("GER40",))
    stack.bar_source.n = 200
    eng = ScriptedEngine()
    kw = dict(mode="demo-auto", markets=("GER40",), artifacts_dir=tmp_path / "a", poll_interval_s=5.0,
              min_disk_free_bytes=1, all_stale_grace_s=900.0, all_stale_exit_s=7200.0)
    kw.update(cfg)
    r = DemoRunner(stack, eng, store, config=RunnerConfig(**kw), clock=clock, sleep=lambda s: None, disk_free=lambda: 10**12)
    return clock, store, stack, eng, r


def test_friday_close_to_monday_open_idles_cleanly_and_resumes_with_catchup(tmp_path):
    clock, store, stack, eng, r = _rig(tmp_path, FRI_CLOSE - timedelta(minutes=20))
    r.start()
    r.run_cycle()  # trading normally shortly before the close
    assert r._market_state["GER40"] == "FRESH"
    # the feed stops: bars and quotes freeze at the Friday close
    stack.bar_source.frozen["GER40"] = FRI_CLOSE
    clock.now = FRI_CLOSE + timedelta(minutes=30)
    for _ in range(12):  # well beyond stale_feed_s and all_stale_grace_s, with the calendar closed
        clock.advance(hours=4)
        r.run_cycle()
        assert r.fail_reason is None and r.halt_reason is None
        assert r._stale_halt_since is None and not r._transient
        st = r.status()
        assert st["market_state"]["GER40"] == "CLOSED_IDLE" and st["idle_all_markets_closed"] is True
        if clock.now >= MON_OPEN - timedelta(hours=4):
            break
    assert stack.submits == [] and stack.halts == []
    assert r._sleep_s() == r.cfg.idle_poll_interval_s  # lowered cadence, heartbeat still < 90 s
    # Monday: the broker streams again -> fresh bars, automatic resume, catch-up of the bars since the pointer
    pointer_before = store.get_bar_pointer("GER40")
    stack.bar_source.frozen.pop("GER40")
    clock.now = MON_OPEN + timedelta(minutes=12, seconds=10)
    r.run_cycle()
    assert r.fail_reason is None and r._market_state["GER40"] == "FRESH" and not r._idle_all
    assert store.get_bar_pointer("GER40") > pointer_before
    st = r._catchup["GER40"]
    assert st["skipped_closed"] > 0  # the closed hours were skipped, not evaluated
    assert all(c[1].weekday() == 0 and c[1] >= MON_OPEN for c in eng.catchup_calls)  # only Monday session bars
    assert len(eng.calls) >= 2 and r.can_trade()
    store.close()


def test_account_checks_are_lowered_not_stopped_while_idle(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, datetime(2026, 10, 3, 10, 0, tzinfo=UTC))  # Saturday
    stack.bar_source.frozen["GER40"] = FRI_CLOSE
    r.start()
    r.run_cycle()  # first cycle classifies the market as idle
    assert r._idle_all
    base = CountingStack.snaps
    for _ in range(12):  # 12 cycles in 2 minutes
        clock.advance(seconds=10)
        r.run_cycle()
    assert 1 <= CountingStack.snaps - base <= 3  # ~ one snapshot per idle_account_check_s (60 s)
    assert r.status()["updated_utc"] is not None  # heartbeat still written every cycle
    store.close()


def test_broker_disconnect_while_all_markets_closed_is_tolerated_then_recovers(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, datetime(2026, 10, 3, 10, 0, tzinfo=UTC))
    stack.bar_source.frozen["GER40"] = FRI_CLOSE
    r.start()
    r.run_cycle()
    stack.set_account(connected=False)
    for _ in range(8):  # 8 h disconnected over the weekend: far beyond transient_grace_s (300 s)
        clock.advance(hours=1)
        r.run_cycle()
        r._last_account_at = None  # force the idle cadence to re-check the account on every cycle
    assert r.fail_reason is None and "disconnected" in r._transient
    stack.set_account(connected=True)
    clock.advance(minutes=5)
    r._last_account_at = None
    r.run_cycle()
    assert "disconnected" not in r._transient and r.fail_reason is None
    store.close()


def test_a_market_that_should_be_open_and_is_silent_is_a_fault(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, datetime(2026, 10, 5, 8, 0, tzinfo=UTC), all_stale_grace_s=600.0, all_stale_exit_s=1800.0)
    r.start()
    r.run_cycle()
    stack.bar_source.frozen["GER40"] = clock()  # Monday 08:00 UTC: GER40 cash session is OPEN
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


def test_closed_calendar_with_live_quotes_is_not_idle(tmp_path):
    """Calendar says closed (Saturday) but the broker still delivers quotes: the calendar is wrong, the
    market trades -> stale bars there are a fault, not idle."""
    clock, store, stack, _eng, r = _rig(tmp_path, datetime(2026, 10, 3, 10, 0, tzinfo=UTC))
    r.start()
    stack.bar_source.frozen["GER40"] = clock() - timedelta(minutes=20)  # bars stale ...
    orig = stack.bar_source.latest_quote

    def live_quote(m):
        q = orig(m)
        from dataclasses import replace

        return replace(q, ts_utc=clock())  # ... but the quote is fresh

    stack.bar_source.latest_quote = live_quote  # type: ignore[method-assign]
    r.run_cycle()
    assert r._market_state["GER40"] == "STALE_FAULT" and not r._idle_all
    assert r.status()["feed"]["GER40"].get("calendar_closed_but_quotes_live") is True
    store.close()
