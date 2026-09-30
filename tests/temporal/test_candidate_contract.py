"""Candidate contract of the numba path: dtypes, ordering, sides, simulate_fast compatibility."""

from __future__ import annotations

import random

import numpy as np
import pytest

from alpha.common.sim import COST_SCENARIOS
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays, simulate_fast
from alpha.temporal.evaluate import evaluate_temporal, evaluate_temporal_full
from scripts.bench_temporal import freeze_frame, golden_like, random_specs, synth_frame


@pytest.fixture(scope="module")
def corpus():
    specs = random_specs(160, seed=31)
    return specs, freeze_frame(specs, synth_frame(1500, 5, 8.0, (60, 250)))


def _market(frame) -> MarketArrays:
    n = len(frame)
    minute = np.asarray(frame.berlin_minute, dtype=np.int64)
    day = np.arange(n, dtype=np.int64) // 288
    contig = np.ones(n, dtype=bool)
    contig[-1] = False
    return MarketArrays(frame.o, frame.h, frame.l, frame.c, np.full(n, 1.0), minute, day, contig)


def test_dtypes_ordering_sides_and_sim(corpus):
    specs, frame = corpus
    market = _market(frame)
    cost = COST_SCENARIOS["BASE"]
    seen = simulated = 0
    for s in specs:
        cand = evaluate_temporal(s, frame)
        assert isinstance(cand, CandidateArrays)
        assert cand.decision_idx.dtype == np.int64 and cand.direction.dtype == np.int8
        assert cand.stop.dtype == np.float64 and cand.target.dtype == np.float64
        assert cand.target_r.dtype == np.float64 and cand.exit_kind.dtype == np.int8
        m = len(cand.decision_idx)
        assert all(
            len(a) == m for a in (cand.direction, cand.stop, cand.target, cand.target_r,
                                  cand.exit_kind)
        )
        if m == 0:
            continue
        seen += 1
        assert np.all(np.diff(cand.decision_idx) > 0)  # strictly increasing
        assert cand.decision_idx[0] >= 0 and cand.decision_idx[-1] < len(frame)
        want = 1 if s.direction == "LONG" else -1
        assert np.all(cand.direction == want)
        assert np.all(cand.exit_kind == EXIT_FIXED_R)
        close = frame.c[cand.decision_idx]
        assert np.all(np.isfinite(cand.stop))
        assert np.all(cand.stop < close) if want == 1 else np.all(cand.stop > close)
        fin = np.isfinite(cand.target)
        if fin.any():  # a finite target lies strictly beyond the decision close
            assert (
                np.all(cand.target[fin] > close[fin]) if want == 1
                else np.all(cand.target[fin] < close[fin])
            )
        assert np.all(cand.target_r > 0)
        trades = simulate_fast(market, cand, cost)  # must not raise
        simulated += len(trades.decision_idx)
    assert seen >= 25 and simulated > 0


def test_trails_are_consistent_with_candidates(corpus):
    specs, frame = corpus
    n_checked = 0
    for s in specs[:80]:
        r = evaluate_temporal_full(s, frame)
        t = r.trails
        m = len(r.candidates.decision_idx)
        assert len(t) == m and t.step_idx.shape == (m, len(s.states) + 1)
        if m:
            assert np.array_equal(t.step_idx[:, -1], r.candidates.decision_idx)
            assert np.array_equal(t.step_idx[:, 0], t.anchor_idx)
            assert np.all(np.diff(t.step_idx, axis=1) >= 1)  # min_gap 1: strictly later bars
            assert np.all(t.step_idx[:, -1] - t.anchor_idx <= s.expires_after)
            assert t.registers.shape == (m, 4)
            n_checked += m
    assert n_checked > 20


def test_empty_result_is_a_valid_contract():
    spec = golden_like(random.Random(0), 0)
    frame = freeze_frame([spec], synth_frame(60, 1, 0.0))  # density 0: no pulses at all
    cand = evaluate_temporal(spec, frame)
    assert len(cand.decision_idx) == 0 and cand.decision_idx.dtype == np.int64
