"""Numba kernel == streaming oracle, exactly, on random seeded specs x synthetic frames.

Also: end-to-end truncation invariance and future-perturbation invariance of the numba path.
"""

from __future__ import annotations

import random

import numpy as np
import pytest

from alpha.temporal.evaluate import evaluate_temporal, evaluate_temporal_full
from alpha.temporal.reference import MarketFrame, evaluate_reference
from scripts.bench_temporal import freeze_frame, random_specs, synth_frame

N_SPECS = 500
FRAMES = [  # (n, seed, density, run_len): several runs per frame -> run-boundary kills happen
    (1500, 3, 6.0, (60, 200)),
    (900, 7, 10.0, (30, 120)),
]
FIELDS = ("decision_idx", "direction", "stop", "target", "target_r", "exit_kind")


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    return a.dtype == b.dtype and a.shape == b.shape and np.array_equal(a, b, equal_nan=True)


def _assert_parity(spec, frame) -> int:
    k = evaluate_temporal_full(spec, frame)
    r = evaluate_reference(spec, frame)
    for f in FIELDS:
        assert _same(getattr(k.candidates, f), getattr(r.candidates, f)), (spec.strategy_id, f)
    kt, rt = k.trails.to_instance_trails(), r.trails
    assert len(kt) == len(rt)
    for x, y in zip(kt, rt, strict=True):
        assert x.anchor_idx == y.anchor_idx and x.step_idx == y.step_idx
        assert x.event_ids == y.event_ids
        assert np.array_equal(x.registers, y.registers, equal_nan=True)
        assert np.array_equal(
            [x.entry_zone_lo, x.entry_zone_hi], [y.entry_zone_lo, y.entry_zone_hi], equal_nan=True
        )
    return len(rt)


@pytest.fixture(scope="module")
def corpus():
    """(specs, frozen frame) per frame config; specs are shared so both frames see all shapes."""
    specs = random_specs(N_SPECS, seed=11)
    return [(specs, freeze_frame(specs, synth_frame(*cfg))) for cfg in FRAMES]


def test_parity_500_random_specs_exact(corpus):
    total = 0
    with_c = 0
    for specs, frame in corpus[:1]:
        for s in specs:
            m = _assert_parity(s, frame)
            total += m
            with_c += m > 0
    # the corpus must actually exercise the machinery (not 500 empty results)
    assert with_c >= 60 and total >= 300


def test_parity_second_frame_dense_events(corpus):
    specs, frame = corpus[1]
    n = 0
    for s in specs[:250]:
        n += _assert_parity(s, frame)
    assert n > 100


def test_parity_covers_short_next_structure_and_finite_targets(corpus):
    specs, frame = corpus[0]
    short = nxt = finite = timeout_like = 0
    for s in specs:
        c = evaluate_temporal(s, frame)
        if len(c.decision_idx) == 0:
            continue
        short += s.direction == "SHORT"
        nxt += s.target.kind == "next_structure"
        finite += bool(np.isfinite(c.target).any())
        timeout_like += any(t.within is not None for t in s.states)
    assert short > 5 and nxt > 5 and finite > 3 and timeout_like > 20


# ------------------------------------------------------------------------------ truncation
def _cut(frame: MarketFrame, t: int) -> MarketFrame:
    return frame.prefix(t + 1)


def test_truncation_invariance_numba_path(corpus):
    specs, frame = corpus[0]
    rng = random.Random(5)
    checked = 0
    for s in rng.sample(specs, 80):
        full = evaluate_temporal_full(s, frame)
        for t in rng.sample(range(50, len(frame) - 1), 3):
            pre = evaluate_temporal_full(s, _cut(frame, t))
            keep = full.candidates.decision_idx <= t
            for f in FIELDS:
                assert _same(getattr(pre.candidates, f), getattr(full.candidates, f)[keep]), f
            assert np.array_equal(pre.trails.step_idx, full.trails.step_idx[keep])
            assert np.array_equal(pre.trails.registers, full.trails.registers[keep], equal_nan=True)
            checked += int(keep.sum())
    assert checked > 20


def test_future_perturbation_invariance(corpus):
    """Randomising every input after bar t leaves candidates with decision_idx <= t unchanged."""
    specs, frame = corpus[0]
    rng = np.random.default_rng(9)
    py = random.Random(9)
    checked = 0
    for s in py.sample(specs, 60):
        t = int(rng.integers(100, len(frame) - 50))
        full = evaluate_temporal_full(s, frame)

        def noisy(a: np.ndarray, t=t) -> np.ndarray:
            b = np.array(a, copy=True)
            fut = b[t + 1:]
            if b.dtype.kind == "f":
                b[t + 1:] = rng.permutation(fut) * rng.uniform(0.5, 1.5, len(fut))
            else:
                b[t + 1:] = rng.permutation(fut)
            return b

        rs = np.array(frame.run_start, copy=True)
        rs[t + 1:] = np.maximum(rs[t + 1:], t + 1)  # run_start of a future bar may not reach back
        pert = MarketFrame(
            noisy(frame.o), noisy(frame.h), noisy(frame.l), noisy(frame.c), noisy(frame.atr), rs,
            noisy(frame.berlin_minute), {k: noisy(v) for k, v in frame.arrays.items()},
            dict(frame.thresholds),
        )
        p = evaluate_temporal_full(s, pert)
        keep_f = full.candidates.decision_idx <= t
        keep_p = p.candidates.decision_idx <= t
        for f in FIELDS:
            assert _same(getattr(p.candidates, f)[keep_p], getattr(full.candidates, f)[keep_f]), f
        assert np.array_equal(p.trails.step_idx[keep_p], full.trails.step_idx[keep_f])
        checked += int(keep_f.sum())
    assert checked > 5


def test_kernel_sources_never_name_origin_arrays():
    """Confirmation-lag rule: evo_* (origin index) is informational only and never read."""
    import pathlib

    import alpha.temporal.batch as b
    import alpha.temporal.kernel as k
    import alpha.temporal.program as p

    for mod in (k, p, b):
        assert "evo_" not in pathlib.Path(mod.__file__).read_text(encoding="utf-8"), mod.__name__
