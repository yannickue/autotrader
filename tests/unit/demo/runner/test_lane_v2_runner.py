# ruff: noqa: E501
"""Lane V2 HIGH-2 gap B: a wedged MT5 lane (stack ``lane_wedged``) ends the runner at once with exit 7 whatever fatal came first
and even while a stop is being deferred for the flatten sweep."""

from __future__ import annotations

from tests.unit.demo.runner.test_runner_operating import _rig, berlin


def _exposed(tmp_path, hh=21, mm=40):
    clock, store, stack, _eng, r = _rig(tmp_path, berlin(hh, mm), daily=True)
    r.start()
    store.recover_open_intents = lambda: [{"intent_id": "i1"}]  # own exposure
    return clock, store, stack, r


def test_earlier_fatal_then_lane_wedge_still_exits_7(tmp_path):
    clock, store, stack, r = _exposed(tmp_path)
    r.fail_reason, r._halted_since = "persistence_failure: earlier", clock()  # fail_reason keeps only the FIRST reason
    assert not r._should_exit(clock())  # keeps managing while it can still act
    stack.lane_wedged = True  # the lane timed out AFTER the earlier fail-closed
    assert r._should_exit(clock())
    assert r.shutdown() == 7
    store.close()


def test_stop_requested_in_the_flatten_window_with_a_wedged_lane_exits(tmp_path):
    clock, store, stack, r = _exposed(tmp_path, 21, 58)
    r.request_stop("stop_file")
    r._flatten_pending = lambda: True  # own exposure still pending in the window: a normal stop is DEFERRED
    assert r._stop_must_finish_flatten(clock())
    stack.lane_wedged = True
    assert r._should_exit(clock())  # was: the deferral ("finish the sweep first") kept a dead lane alive
    assert r.shutdown() == 7
    store.close()


def test_healthy_lane_is_unchanged(tmp_path):
    clock, store, stack, r = _exposed(tmp_path)
    assert getattr(stack, "lane_wedged", False) is False
    assert not r._should_exit(clock())
    store.close()
