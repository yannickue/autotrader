# ruff: noqa: E501
"""Lane V HIGH-2: a latched ``mt5_lane_timeout`` fatal (a no-action fatal: nothing can flatten from this process) ends the
runner promptly with exit 7 even with open exposure, so a fresh process (supervisor restart / EOD recovery) gets a working lane."""

from __future__ import annotations

from tests.unit.demo.runner.test_runner_operating import _rig, berlin


def _exposed(tmp_path, reason):
    clock, store, _stack, _eng, r = _rig(tmp_path, berlin(21, 40), daily=True)
    r.start()
    store.recover_open_intents = lambda: [{"intent_id": "i1"}]  # own exposure
    r.fail_reason, r._halted_since = reason, clock()
    return clock, store, r


def test_lane_timeout_with_exposure_exits_at_once_exit_7(tmp_path):
    clock, store, r = _exposed(tmp_path, "stack: mt5_lane_timeout")
    assert r._has_open_exposure() is True
    assert r._should_exit(clock())  # was: kept running (4 h) although it can no longer flatten anything
    assert r.shutdown() == 7
    store.close()


def test_other_fail_reasons_with_exposure_still_keep_managing(tmp_path):
    clock, store, r = _exposed(tmp_path, "broker_disconnect (persisted 301s)")
    assert not r._should_exit(clock())
    store.close()
