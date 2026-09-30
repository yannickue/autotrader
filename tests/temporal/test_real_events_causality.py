# ruff: noqa: E501
"""End-to-end causality of the temporal engine on the REAL 75,837-bar GER40 dev frame.

Every cut / perturbation REBUILDS features + events + frame from the modified bars (not just array
slicing), then evaluates ~40 hand-written specs (section-2 example structure + variants + mirrors).
Thresholds are fixed from the full frame's TRAIN bars so that only bar data can change results.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd
import pytest

from alpha.temporal.evaluate import evaluate_temporal_full, evaluate_temporal_many
from alpha.temporal.frame import (
    TrainThresholds,
    build_market_frame,
    frame_for_specs,
)
from alpha.temporal.reference import evaluate_reference
from scripts.bench_temporal import random_specs
from scripts.bench_temporal_real import build_pipeline, hand_specs, load_real
from tests.events._helpers import diff_names, same_array

FIELDS = ("decision_idx", "direction", "stop", "target", "target_r", "exit_kind")


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    return a.dtype == b.dtype and a.shape == b.shape and np.array_equal(a, b, equal_nan=True)


@lru_cache(maxsize=1)
def _full():
    real = load_real()
    specs = hand_specs()
    frame = frame_for_specs(real.frame, specs)
    res = evaluate_temporal_many(specs, frame)
    return real, specs, dict(frame.thresholds), res


def _bday(real) -> np.ndarray:
    ts = pd.DatetimeIndex(real.df["ts"]).tz_convert("Europe/Berlin").normalize().tz_localize(None)
    return ts.to_numpy().astype("datetime64[D]")


def _dst_cut_points(real) -> dict[str, int]:
    d = _bday(real)
    out = {}
    for label, day in (("dst_mar", "2025-03-30"), ("dst_oct", "2025-10-26")):
        end = int(np.searchsorted(d, np.datetime64(day), side="right"))  # bars with date <= day
        assert 1000 < end < len(d) - 200
        out[f"{label}_dayend"] = end
        out[f"{label}_mid_after"] = end + 60  # inside the first session after the change
    return out


def _cut_points(real) -> dict[str, int]:
    n = len(real.df)
    rng = np.random.default_rng(20260930)
    cuts = _dst_cut_points(real)
    cuts["random_a"] = int(rng.integers(8000, n - 2000))
    cuts["random_b"] = int(rng.integers(8000, n - 2000))
    return cuts


def test_full_frame_produces_enough_candidates():
    _, specs, _, res = _full()
    counts = [len(r.candidates.decision_idx) for r in res]
    assert len(specs) == 40
    assert sum(c > 0 for c in counts) >= 25 and sum(counts) > 3000
    ex = [r for s, r in zip(specs, res, strict=True) if s.strategy_id.startswith("h_ex_")]
    assert sum(len(r.candidates.decision_idx) for r in ex) >= 3  # the section-2 chain does fire


@pytest.mark.parametrize("label", ["random_a", "random_b", "dst_mar_dayend", "dst_mar_mid_after",
                                   "dst_oct_dayend", "dst_oct_mid_after"])
def test_truncation_invariance_rebuilt_prefix(label):
    real, specs, thr, full = _full()
    t = _cut_points(real)[label]
    feats, events = build_pipeline(real.df.iloc[:t])
    frame = frame_for_specs(build_market_frame(feats, events, thresholds=thr), specs)
    assert len(frame) == t
    pre = evaluate_temporal_many(specs, frame)
    checked = 0
    for s, f, p in zip(specs, full, pre, strict=True):
        keep = f.candidates.decision_idx < t
        for fld in FIELDS:
            assert _same(np.asarray(getattr(f.candidates, fld))[keep], np.asarray(getattr(p.candidates, fld))), (label, s.strategy_id, fld)
        checked += int(keep.sum())
    assert checked > 100  # not vacuous


@pytest.mark.parametrize("label", ["random_a", "random_b", "dst_oct_mid_after"])
def test_future_perturbation_invariance_rebuilt(label):
    real, specs, thr, full = _full()
    t = _cut_points(real)[label]  # bars with index > t are randomised
    df = real.df.reset_index(drop=True).copy()
    n = len(df)
    rng = np.random.default_rng(t)
    m = n - (t + 1)
    step = rng.normal(0, 8.0, m)
    close = df["close"].iloc[t] + np.cumsum(step)
    opn = np.r_[df["close"].iloc[t], close[:-1]] + rng.normal(0, 1.0, m)
    high = np.maximum(opn, close) + np.abs(rng.normal(0, 4.0, m))
    low = np.minimum(opn, close) - np.abs(rng.normal(0, 4.0, m))
    df.loc[t + 1:, "open"] = opn
    df.loc[t + 1:, "high"] = high
    df.loc[t + 1:, "low"] = low
    df.loc[t + 1:, "close"] = close
    df.loc[t + 1:, "spread_pts"] = rng.integers(1, 60, m)
    df.loc[t + 1:, "tick_volume"] = rng.integers(1, 5000, m)
    feats, events = build_pipeline(df)
    frame = frame_for_specs(build_market_frame(feats, events, thresholds=thr), specs)
    assert not _same(np.asarray(frame.c[t + 1:]), np.asarray(real.frame.c[t + 1:]))  # perturbation is real
    # events and features up to t are untouched ...
    assert diff_names(real.events, events, t + 1) == []
    assert same_array(np.asarray(real.frame.c[: t + 1]), np.asarray(frame.c[: t + 1]))
    # ... hence every candidate decided at or before t is identical
    per = evaluate_temporal_many(specs, frame)
    seen = 0
    for s, f, p in zip(specs, full, per, strict=True):
        keep = f.candidates.decision_idx <= t
        keep_p = p.candidates.decision_idx <= t
        for fld in FIELDS:
            assert _same(np.asarray(getattr(f.candidates, fld))[keep], np.asarray(getattr(p.candidates, fld))[keep_p]), (label, s.strategy_id, fld)
        seen += int(keep.sum())
    assert seen > 100


@pytest.fixture(scope="module")
def slices():
    """Two ~3000-bar real slices (each spans a DST change), features + events rebuilt from scratch."""
    real, *_ = _full()
    d = _bday(real)
    out = []
    for start in ("2025-03-24", "2025-10-20"):
        a = int(np.searchsorted(d, np.datetime64(start), side="left"))
        feats, events = build_pipeline(real.df.iloc[a:a + 3000])
        th = TrainThresholds(build_market_frame(feats, events).arrays, np.ones(3000, dtype=bool))
        out.append(build_market_frame(feats, events, thresholds=th))
    return out


def test_oracle_parity_on_real_slices(slices):
    specs = [*hand_specs(), *random_specs(40, seed=5)]
    total = 0
    with_c = 0
    for frame in slices:
        sub = frame_for_specs(frame, specs)
        for s in specs:
            k = evaluate_temporal_full(s, sub)
            r = evaluate_reference(s, sub)
            for f in FIELDS:
                assert _same(getattr(k.candidates, f), getattr(r.candidates, f)), (s.strategy_id, f)
            kt, rt = k.trails.to_instance_trails(), r.trails
            assert len(kt) == len(rt)
            for x, y in zip(kt, rt, strict=True):
                assert x.anchor_idx == y.anchor_idx and x.step_idx == y.step_idx
                assert x.event_ids == y.event_ids
                assert np.array_equal(x.registers, y.registers, equal_nan=True)
            total += len(rt)
            with_c += len(rt) > 0
    assert with_c >= 20 and total >= 100  # the slices exercise the machinery
