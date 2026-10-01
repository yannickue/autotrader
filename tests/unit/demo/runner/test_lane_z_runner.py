# ruff: noqa: E501
"""Lane Z runner semantics (H1): exposure while disconnected keeps the runner alive; the flatten attempt is made even when
the stack's poll path raises; a STOP inside the flatten window finishes the sweep first (or alerts loudly)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from demo.execution.stack_port import StackFailClosed
from tests.unit.demo.runner.test_runner_operating import EodStack, _rig, berlin


@dataclass
class PollFailsStack(EodStack):
    poll_error: str | None = None

    def poll_events(self):
        if self.poll_error:
            raise StackFailClosed(self.poll_error)
        return super().poll_events()


def _rig_with(tmp_path, start, stack_cls=EodStack, **cfg):
    clock, store, stack, eng, r = _rig(tmp_path, start, **cfg)
    if stack_cls is not EodStack:  # rebuild with the subclass (same wiring as _rig)
        stack = stack_cls(clock, markets=("GER40",))
        stack.bar_source.n = 200
        r.stack = stack
    return clock, store, stack, eng, r


def test_H1_open_intents_count_as_exposure_even_while_disconnected(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(21, 58), daily=True)
    r.start()
    store.recover_open_intents = lambda: [{"intent_id": "i1"}]  # an open / protected intent
    stack.set_account(connected=False)
    r.run_cycle()
    assert r._has_open_exposure() is True  # was False while disconnected -> the runner exited 7 immediately
    r.fail_reason, r._halted_since = "broker_disconnect (persisted 301s)", clock()
    assert not r._should_exit(clock())  # keeps trying (bounded by manage_after_halt_s)
    clock.advance(seconds=r.cfg.manage_after_halt_s + 1)
    assert r._should_exit(clock())  # ... but it is bounded
    store.close()


def test_H1_no_intents_and_disconnected_is_still_not_exposure(tmp_path):
    _clock, store, stack, _eng, r = _rig(tmp_path, berlin(21, 58))
    r.start()
    stack.set_account(connected=False)
    r.run_cycle()
    assert r._has_open_exposure() is False
    store.close()


def test_H1_the_flatten_attempt_is_made_even_when_poll_events_raises(tmp_path):
    _clock, store, stack, _eng, r = _rig_with(tmp_path, berlin(21, 56), stack_cls=PollFailsStack)
    stack.poll_error = "flatten_failed:GER40"  # a latched stack fatal: poll_events raises every cycle
    r.start()
    before = stack.clock_calls
    r.run_cycle()
    assert stack.clock_calls == before + 1  # on_clock (-> sweep) still ran
    assert r.fail_reason is not None and "flatten_failed" in r.fail_reason  # ... and the failure is still reported
    store.close()


def test_H1_stop_inside_the_flatten_window_finishes_the_sweep_before_exit(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(21, 57))
    stack.eod, stack.eod_detail = "WINDOW", "1 own position(s) still open; closing"
    r.start()
    r.request_stop("stop_file")
    assert not r._should_exit(clock())  # exposure pending inside the window: keep sweeping
    r.run_cycle()
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    assert "stop_deferred_flatten_pending" in json.dumps(hb["last_error"])  # loud
    stack.eod, stack.eod_confirmed, stack.eod_detail = "FLAT_CONFIRMED", clock().isoformat(), None
    assert r._should_exit(clock())  # flat confirmed: the stop may proceed
    store.close()


def test_H1_stop_deferral_is_bounded_by_the_grace_after_the_deadline(tmp_path):
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(22, 0))
    stack.eod = "OVERDUE"
    r.start()
    r.request_stop("stop_file")
    assert not r._should_exit(clock())
    clock.advance(seconds=r.cfg.stop_flatten_grace_s + 60)
    assert r._should_exit(clock())  # bounded; the exit carries a loud alert (see shutdown test)
    store.close()


def test_H1_stop_outside_the_flatten_window_exits_at_once(tmp_path):
    clock, store, _stack, _eng, r = _rig(tmp_path, berlin(14, 0))
    r.start()
    r.request_stop("stop_file")
    assert r._should_exit(clock())
    store.close()


def test_H1_shutdown_with_exposure_left_writes_a_loud_alert(tmp_path):
    _clock, store, stack, _eng, r = _rig(tmp_path, berlin(22, 40))
    stack.eod, stack.eod_detail = "OVERDUE", "1 own position(s) still open; close_failed:GER40:x9"
    r.start()
    store.recover_open_intents = lambda: [{"intent_id": "i1"}]
    r.request_stop("signal_15")
    r.shutdown()
    hb = json.loads(r.cfg.heartbeat_path.read_text())
    assert any("overnight_exposure_at_shutdown" in w for w in hb["warnings"])
    store.close()
