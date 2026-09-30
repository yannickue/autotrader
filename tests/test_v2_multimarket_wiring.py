# ruff: noqa: E501
"""Spec-correct multi-market wiring: SimWindow through screen / evaluator / search, market_costs in the runner."""

from __future__ import annotations

import functools

import numpy as np

from alpha.common.sim import COST_SCENARIOS, SizingSpec
from alpha.discovery import temporal_archetypes as ta
from alpha.discovery import temporal_genome as tg
from alpha.discovery import temporal_search as ts
from alpha.fast.screen import light_screen, light_screen_many
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays, SimWindow, simulate_fast

COST = COST_SCENARIOS["BASE"]
SIZING = SizingSpec(equity_eur=10_000.0, min_risk_pts=1.0, max_risk_pts=50.0, contract_size=1.0)
N = 60


def _market(start_minute: int) -> MarketArrays:
    """One day, M5 bars whose minute-of-day starts at ``start_minute`` (local-calendar minutes)."""
    o = np.full(N, 100.0)
    contig = np.ones(N, dtype=bool)
    contig[-1] = False
    return MarketArrays(o, o + 1.0, o - 1.0, o.copy(), np.full(N, 0.1), start_minute + 5 * np.arange(N),
                        np.zeros(N, dtype=np.int64), contig)


def _cand() -> CandidateArrays:
    return CandidateArrays(np.array([4], dtype=np.int64), np.array([1], dtype=np.int8), np.array([95.0]),
                           np.array([np.nan]), np.array([1.0]), np.array([EXIT_FIXED_R], dtype=np.int8))


def _split_dates():
    from alpha.common.protocol import Partition, SplitPlan

    d = np.full(N, np.datetime64("2024-01-02", "D"))
    plan = SplitPlan(Partition("train", "2024-01-01", "2024-01-03"), Partition("validation", "2024-01-04", "2024-01-05"),
                     Partition("oos", "2024-01-06", "2024-01-07"))
    return plan, d


def test_light_screen_passes_the_window_through():
    # New-York-like market: entry bars at 09:30-ish local (570+), GER40 window (540..1200) would admit it,
    # so use a market at 03:00 local (180): outside the GER40 window, inside a 02:00-08:00 window.
    m = _market(180)
    plan, dates = _split_dates()
    win = SimWindow(120, 480, 600)
    assert len(simulate_fast(m, _cand(), COST, SIZING)) == 0  # default GER40 window: outside_window
    assert len(simulate_fast(m, _cand(), COST, SIZING, window=win)) == 1
    default = light_screen(m, _cand(), COST, plan, dates=dates, sizing=SIZING)
    windowed = light_screen(m, _cand(), COST, plan, dates=dates, sizing=SIZING, window=win)
    assert default.train.n_trades == 0 and windowed.train.n_trades == 1
    many = light_screen_many(m, [_cand()], COST, plan, dates=dates, sizing=SIZING, window=win)
    assert many[0].train.n_trades == 1


def test_op_window_uses_supplied_windows_and_keeps_default_stream():
    pool = tg.EventPool.full()
    g = ta.random_genome(np.random.default_rng(0), pool)
    allowed = ta.windows_for(120, 480)
    seen = set()
    rng = np.random.default_rng(1)
    for _ in range(200):
        seen.add(ts._op_window(g, rng, pool, allowed).window)
    assert seen and all(w is None or w in allowed for w in seen) and not (seen - {None}) - set(allowed)
    # default (no windows): identical to the legacy WINDOWS stream
    a = [ts._op_window(g, np.random.default_rng(5), pool).window for _ in range(3)]
    b = [ts._op_window(g, np.random.default_rng(5), pool, None).window for _ in range(3)]
    assert a == b and all(w is None or w in ta.WINDOWS for w in a)


def test_mutate_structure_threads_windows_to_the_window_operator(monkeypatch):
    pool = tg.EventPool.full()
    g = ta.random_genome(np.random.default_rng(2), pool)
    got = []
    real = ts._op_window

    def spy(genome, rng, pool_, windows=None):
        got.append(windows)
        return real(genome, rng, pool_, windows)

    spy = functools.wraps(real)(spy)
    monkeypatch.setattr(ts, "_OPS", tuple((spy if op is real else op, w) for op, w in ts._OPS))
    allowed = ta.windows_for(120, 480)
    rng = np.random.default_rng(3)
    for _ in range(400):
        ts.mutate_structure(g, rng, pool, windows=allowed)
    assert got and all(w == allowed for w in got)


def test_evolve_derives_search_windows_from_window_argument():
    class _Ev:
        window = SimWindow(120, 480, 600)

    assert ts.search_windows(SimWindow(120, 480, 600)) == ta.windows_for(120, 480)
    assert ts.search_windows(None) is None
    assert ts.search_windows(None, _Ev()) == ta.windows_for(120, 480)  # falls back to the evaluator's window
