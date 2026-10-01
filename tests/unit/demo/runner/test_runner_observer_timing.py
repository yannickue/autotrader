# ruff: noqa: E501
"""Runner-level TIMING PARITY (Gate A, timing half): the observer must not delay the live scan of ANY later market of a cycle.

Several markets in ONE cycle, the REAL 0.15 s observer budget, a 6000-bar cold start (the engine's production window) and the REAL wall clock
(``time.perf_counter``, no fake clock). Per cycle the offset of every market's scan start relative to the cycle start is measured with the observer OFF
and ON; the ON-vs-OFF difference must stay within an epsilon calibrated from the run-to-run noise of two OFF runs (documented below). The observer work
(everything but the O(1) ``on_bar`` stash) must start after the LAST market's scan, and its in-cycle time must stay within budget + slack.

Calibration (measured on the development machine, 6000-bar frames, 4 markets): see the printed TIMING_PARITY line. ``eps = max(EPSILON_FLOOR_S, 2 * noise)``
with noise = max over cells of (second best - best) OFF run; ``eps`` must stay far below the 0.15 s budget (asserted: the pre-fix implementation delayed every later
market by the whole budget).
"""

from __future__ import annotations

import gc
import time
from datetime import timedelta

import pandas as pd
import pytest
from test_runner_observer import GIB, START_BAR, ReplayStackSource, world

from demo.opportunity import observer_hook as H
from demo.opportunity.bar_source import M5_SECONDS
from demo.opportunity.engine import OpportunityEngine
from demo.runner import DemoRunner, RunnerConfig, StoreSeenAdapter
from demo.store import DemoStore
from demo.testing import FakeClock, FakeStack

MARKETS = ("GER40", "XAUUSD", "EURUSD", "NAS100")
pytestmark = pytest.mark.perf  # PERFORMANCE / SERIAL test: relative wall-clock offsets (ON vs OFF, min of 3 repeats, eps from measured noise); with CPU
# contention the noise term can exceed 0.75 * BUDGET_S ("run-to-run noise ... too large") and the test fails without any observer regression. Run it ALONE.
WINDOW = 6000
BUDGET_S = 0.15
N_CYCLES = 3
EPSILON_FLOOR_S = 0.06
ENQUEUE_MAX_S = 0.01  # the in-scan hook call is an O(1) stash


def build(tmp_path, *, observer: bool):
    prod, mspecs, frames = world()
    first = pd.Timestamp(START_BAR, tz="UTC")
    clock = FakeClock(first.to_pydatetime() + timedelta(seconds=M5_SECONDS + 10))
    store = DemoStore(tmp_path / "demo.sqlite", clock=lambda: clock().isoformat())
    stack = FakeStack(clock, markets=MARKETS)
    src = ReplayStackSource(clock, frames, {m: mspecs[m].point_size for m in frames})
    src.m5_frame = lambda market, n=None, _f=src.m5_frame: _f(market, WINDOW if n is None else n)  # type: ignore[method-assign]
    stack.bar_source = src  # type: ignore[assignment]
    engine = OpportunityEngine(src, production=prod, market_specs=mspecs, window_bars=WINDOW, min_history_bars=600, commit="timing-parity",
                               seen_store=StoreSeenAdapter(store))
    cfg = RunnerConfig(mode="demo-auto", markets=MARKETS, artifacts_dir=tmp_path / "art", poll_interval_s=1.0, min_disk_free_bytes=GIB, all_stale_grace_s=0.0,
                       market_observer_enabled=observer, market_observer_budget_s=BUDGET_S)
    runner = DemoRunner(stack, engine, store, config=cfg, clock=clock, sleep=lambda s: None, disk_free=lambda: 50 * GIB)
    return clock, store, runner


def measure(tmp_path, *, observer: bool, monkeypatch) -> dict:
    """Run N_CYCLES with real-time instrumentation; returns per-cycle scan offsets + observer call windows."""
    clock, store, runner = build(tmp_path, observer=observer)
    cycles: list[dict] = []
    cur: dict = {}
    real_scan = runner._scan_market

    def scan(market, now):
        t0 = time.perf_counter()
        cur["scans"].append((market, t0 - cur["t0"], None))
        try:
            return real_scan(market, now)
        finally:
            cur["scans"][-1] = (market, cur["scans"][-1][1], time.perf_counter() - cur["t0"])

    runner._scan_market = scan  # type: ignore[method-assign]

    def wrap(name):
        real = getattr(H.ObserverShadow, name)

        def inner(self, *a, **k):
            t0 = time.perf_counter()
            try:
                return real(self, *a, **k)
            finally:
                cur["obs"].append((name, t0 - cur["t0"], time.perf_counter() - cur["t0"]))
        return inner

    with monkeypatch.context() as mp:
        for name in ("on_bar", "drain_cycle", "warm_step"):
            if hasattr(H.ObserverShadow, name):
                mp.setattr(H.ObserverShadow, name, wrap(name))
        runner.start()
        assert runner.fail_reason is None, runner.fail_reason
        for _ in range(N_CYCLES):
            gc.collect()
            cur = {"t0": time.perf_counter(), "scans": [], "obs": []}
            runner.run_cycle()
            cur["end"] = time.perf_counter() - cur["t0"]
            assert runner.fail_reason is None, (runner.fail_reason, runner._last_error)
            cycles.append(cur)
            clock.advance(minutes=5)
    status = runner.status().get("market_observer")
    store.close()
    return {"cycles": cycles, "status": status}


REPEATS = 3


@pytest.fixture(scope="module")
def timing(tmp_path_factory):
    """One discarded warm-up run (imports, caches), then REPEATS interleaved OFF / ON runs; the per-cell statistic is the MINIMUM over the repeats
    (a systematic delay such as the whole budget survives the minimum, a scheduling hiccup of a busy machine does not)."""
    mp = pytest.MonkeyPatch()
    try:
        measure(tmp_path_factory.mktemp("warmup"), observer=False, monkeypatch=mp)
        out: dict = {"off": [], "on": []}
        for k in range(REPEATS):
            out["off"].append(measure(tmp_path_factory.mktemp(f"off{k}"), observer=False, monkeypatch=mp))
            out["on"].append(measure(tmp_path_factory.mktemp(f"on{k}"), observer=True, monkeypatch=mp))
        return out
    finally:
        mp.undo()


def offsets(run: dict) -> list[dict[str, float]]:
    return [{m: s for m, s, _e in c["scans"]} for c in run["cycles"]]


def cell_min(runs: list[dict]) -> list[dict[str, float]]:
    per = [offsets(r) for r in runs]
    return [{m: min(p[c][m] for p in per) for m in per[0][c]} for c in range(len(per[0]))]


def test_observer_does_not_delay_the_scan_start_of_any_later_market(timing):
    off, on = cell_min(timing["off"]), cell_min(timing["on"])
    assert all(len(c) >= 3 for c in off), "the test needs several markets scanned in the same cycle"
    # run-to-run noise of the OFF statistic: spread between the best and the second best OFF run per cell
    per = [offsets(r) for r in timing["off"]]
    noise = max(sorted(p[c][m] for p in per)[1] - sorted(p[c][m] for p in per)[0] for c in range(len(off)) for m in off[c])
    eps = max(EPSILON_FLOOR_S, 2 * noise)
    assert eps < 0.75 * BUDGET_S, f"run-to-run noise {noise:.3f}s is too large for a meaningful timing test"
    worst = 0.0
    for c, (a, b) in enumerate(zip(off, on, strict=True)):
        assert list(a) == list(b), (c, list(a), list(b))
        for m in a:
            worst = max(worst, b[m] - a[m])
    print(f"\nTIMING_PARITY: noise(OFF best-vs-second)={noise * 1000:.1f}ms eps={eps * 1000:.1f}ms worst(ON-OFF)={worst * 1000:.1f}ms repeats={REPEATS}")
    for c, (a, b) in enumerate(zip(off, on, strict=True)):
        print(f"  cycle {c} scan-start offsets OFF/ON (ms, min of {REPEATS}): " + ", ".join(f"{m} {a[m] * 1000:.0f}/{b[m] * 1000:.0f}" for m in a))
    assert worst <= eps, f"a later market's scan start was delayed by {worst:.3f}s with the observer on (eps {eps:.3f}s)"


def test_observer_work_happens_after_the_last_scan_and_within_the_budget(timing):
    for run in timing["on"]:
        _check_run(run)


def _check_run(run):
    for c in run["cycles"]:
        last_scan_end = max(e for _m, _s, e in c["scans"])
        heavy = [(n, s, e) for n, s, e in c["obs"] if n != "on_bar"]
        stash = [(n, s, e) for n, s, e in c["obs"] if n == "on_bar"]
        assert heavy, "the observer cycle work never ran"
        assert all(s >= last_scan_end for _n, s, _e in heavy), (last_scan_end, heavy)
        assert all(e - s <= ENQUEUE_MAX_S for _n, s, e in stash), f"on_bar is not O(1): {[(e - s) for _n, s, e in stash]}"
        in_cycle = sum(e - s for _n, s, e in heavy)
        assert in_cycle <= BUDGET_S + 0.15, f"observer in-cycle time {in_cycle:.3f}s exceeds the budget {BUDGET_S}s + slack"
    st = run["status"]
    assert st["errors"] == 0 and st["budget_s"] == BUDGET_S
