# ruff: noqa: E501
"""Lane V2 HIGH-2 gap A: a runner that ended on a wedged MT5 lane releases its lock and then terminates the process with
``os._exit(7)`` (the wedged C-call thread would otherwise block the interpreter exit forever); healthy paths exit orderly."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from demo import runner as rn
from scripts import demo_trader


class _Store:
    def close(self):
        pass


class _Runner:
    def __init__(self, wedged, code):
        self.store, self.lane_wedged, self.code = _Store(), wedged, code

    def run(self, install_signals=True):
        return self.code


def _reset():
    demo_trader._LANE_WEDGED = False


def _args(tmp_path: Path):
    return argparse.Namespace(
        artifacts=tmp_path, markets=None, phase=None, db=None, learning=None, forced_flat_on_shutdown=False, account_phase=None,
        exit_policy="fixed_1_5r", out_of_window_shadow=None, shadow_exit_lab=None, shadow_universe=None, market_observer=None, geometry_source="family",
        daily=False, flatten_only=False,
    )


@pytest.fixture
def exits(monkeypatch):
    _reset()
    seen: list[int] = []
    monkeypatch.setattr(demo_trader, "_hard_exit", lambda code: seen.append(code))
    return seen


def test_wedged_lane_releases_the_lock_then_hard_exits_7(tmp_path, monkeypatch, exits):
    monkeypatch.setattr(rn, "build_live_runner", lambda *a, **k: _Runner(True, 7))
    code = demo_trader._run_single_instance(_args(tmp_path), "demo-auto")
    assert exits == [7] and code == 7
    assert not (tmp_path / "runner.lock").exists()  # released BEFORE the hard exit


def test_healthy_runner_exits_orderly(tmp_path, monkeypatch, exits):
    monkeypatch.setattr(rn, "build_live_runner", lambda *a, **k: _Runner(False, 0))
    assert demo_trader._run_single_instance(_args(tmp_path), "demo-auto") == 0
    assert exits == []
