# ruff: noqa: E501
"""FeatureSet + EventSet -> MarketFrame adapter (alpha.temporal.frame) on the REAL GER40 dev frame."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pytest

from alpha.events import schema as ev
from alpha.temporal.evaluate import evaluate_temporal_full, evaluate_temporal_many
from alpha.temporal.frame import (
    FEATURE_ALIASES,
    LEVEL_SOURCES,
    TrainThresholds,
    _dist_twap_atr,
    berlin_dates,
    build_market_frame,
    frame_for_specs,
    needed_names,
)
from alpha.temporal.reference import MarketFrame
from scripts.bench_temporal_real import hand_specs, load_real


@pytest.fixture(scope="module")
def real():
    return load_real()


class Recording(Mapping):
    def __init__(self, inner: Mapping) -> None:
        self.inner, self.log = inner, set()

    def __getitem__(self, k):
        self.log.add(k)
        return self.inner[k]

    def __contains__(self, k):
        return k in self.inner

    def __iter__(self):
        return iter(self.inner)

    def __len__(self):
        return len(self.inner)


def test_all_registry_feature_and_level_names_resolve(real):
    arrays = real.frame.arrays
    n = len(real.frame)
    names = [*ev.all_array_names(), *ev.FEATURE_MIRROR, *ev.TARGET_LEVELS, *real.features]
    assert all(nm in arrays for nm in names)
    assert "no_such_array" not in arrays
    for nm in (*ev.FEATURE_MIRROR, *ev.TARGET_LEVELS, "ev_m5_bos_up", "range_ratio_12_48"):
        assert arrays[nm].shape == (n,)
    assert set(LEVEL_SOURCES) == set(ev.TARGET_LEVELS)


def test_core_arrays_and_lazy_loading(real):
    f, fr = real.features, real.frame
    assert np.array_equal(fr.c, f["c"]) and np.array_equal(fr.atr, f["m5_atr14"], equal_nan=True)
    assert fr.run_start.dtype == np.int64 and (fr.run_start <= np.arange(len(fr))).all()
    assert np.array_equal(fr.ts_close_ns, f["ts_ns"] + 300 * 10**9)
    # a fresh frame loads nothing until asked, and event arrays stay memory-mapped
    from alpha.events.store import load_or_build_events
    from scripts.bench_temporal_real import CACHE_DIR

    events = load_or_build_events(f, None, CACHE_DIR / "events")  # cache hit -> nothing loaded yet
    assert events.metadata["cache_hit"] and not any(events.is_loaded(k) for k in events)
    fresh = build_market_frame(f, events, plan=real.plan)
    assert fresh.arrays.loaded() == ()
    a = fresh.arrays["ev_m5_bos_up"]
    assert fresh.arrays.loaded() == ("ev_m5_bos_up",)
    assert isinstance(a, np.memmap) or isinstance(getattr(a, "base", None), np.memmap)
    assert sum(events.is_loaded(k) for k in events) == 1


def test_level_and_feature_mapping(real):
    a, f = real.frame.arrays, real.features
    assert np.array_equal(a["pdh"], f["previous_day_high"], equal_nan=True)
    assert np.array_equal(a["pdl"], f["previous_day_low"], equal_nan=True)
    assert np.array_equal(a["session_high"], f["session_high"], equal_nan=True)
    assert np.array_equal(a["h1_swing_low"], real.events["lv_h1_swing_low_lvl"], equal_nan=True)
    assert np.array_equal(a["m5_swing_high"], real.events["lv_m5_swing_high_lvl"], equal_nan=True)
    for k, src in FEATURE_ALIASES.items():
        assert np.array_equal(a[k], f[src], equal_nan=True)
    exp = 100.0 * f["m5_atr14"] / f["c"]
    assert np.allclose(a["atr_pct"], exp, equal_nan=True)


def test_derived_twap_feature_is_causal(real):
    """dist_vwap_atr at bar i equals the value computed from bars <= i only."""
    f = real.features
    keys = ("h", "l", "c", "berlin_day_id", "m5_atr14")
    full = _dist_twap_atr({k: f[k] for k in keys})
    for t in (5000, 30001, 60000):
        pre = _dist_twap_atr({k: f[k][:t] for k in keys})
        assert np.allclose(pre, full[:t], equal_nan=True, rtol=1e-9, atol=1e-9)


def test_thresholds_use_train_bars_only(real):
    f, plan = real.features, real.plan
    dates = berlin_dates(f)
    train = plan.mask(dates, plan.train)
    assert 0 < train.sum() < len(train)  # Validation (and gap) bars exist and are excluded
    base = build_market_frame(f, real.events, plan=plan)
    qs = (0.1, 0.5, 0.9)
    want = {(k, q): base.thresholds[(k, q)] for k in ev.FEATURE_MIRROR for q in qs}
    # manual TRAIN quantile of the mapped array
    for (k, q), v in want.items():
        vals = np.asarray(base.arrays[k], dtype=float)[train]
        assert v == round(float(np.quantile(vals[np.isfinite(vals)], q)), 6)
    # scramble EVERY non-train input bar (Validation + OOS-side + gap): thresholds must not move
    rng = np.random.default_rng(5)
    g = dict(f)
    for name in ("o", "h", "l", "c", "m5_atr14", "mom_12_atr", "range_ratio_12_48", "m5_ema_slope"):
        arr = np.array(f[name], dtype=float)
        arr[~train] = arr[~train] * rng.uniform(0.2, 5.0, (~train).sum()) + rng.normal(0, 50, (~train).sum())
        g[name] = arr
    mutated = build_market_frame(g, real.events, plan=plan)
    for key, v in want.items():
        assert mutated.thresholds[key] == v, key
    # ... and scrambling TRAIN bars does move them (the test is not vacuous)
    h = dict(f)
    arr = np.array(f["mom_12_atr"], dtype=float)
    arr[train] += 3.0
    h["mom_12_atr"] = arr
    moved = build_market_frame(h, real.events, plan=plan)
    assert moved.thresholds[("ret_12", 0.5)] != want[("ret_12", 0.5)]
    # ... and Validation-only mutation of a spec-relevant frame leaves compiled candidates unchanged
    specs = [s for s in hand_specs() if any(c.kind == "feature" for c in (*s.anchor, *s.context, *(x for t in s.states for x in t.guards)))]
    assert specs
    fa, fb = frame_for_specs(base, specs), frame_for_specs(mutated, specs)
    assert fa.thresholds == fb.thresholds


def test_train_thresholds_mask_and_missing_fail_closed(real):
    n = len(real.frame)
    mask = np.zeros(n, dtype=bool)
    mask[1000:5000] = True
    th = TrainThresholds(real.frame.arrays, mask)
    vals = np.asarray(real.frame.arrays["ret_12"], dtype=float)[1000:5000]
    assert th[("ret_12", 0.25)] == round(float(np.quantile(vals[np.isfinite(vals)], 0.25)), 6)
    bare = build_market_frame(real.features, real.events)  # neither thresholds nor plan
    spec = next(s for s in hand_specs() if s.strategy_id == "h_zone_feat")
    with pytest.raises(KeyError):
        evaluate_temporal_full(spec, bare)


def test_needed_names_cover_every_read_and_lazy_equals_frozen(real):
    """Kernel reads only what needed_names announces; frozen batch frame == lazy full frame."""
    specs = hand_specs()
    for s in specs:
        need_a, need_t = needed_names(s)
        rec_a, rec_t = Recording(real.frame.arrays), Recording(real.frame.thresholds)
        fr = MarketFrame(real.frame.o, real.frame.h, real.frame.l, real.frame.c, real.frame.atr,
                         real.frame.run_start, real.frame.berlin_minute, rec_a, rec_t,
                         real.frame.ts_close_ns)
        rec_a.log.clear()  # MarketFrame.__post_init__ iterates the (loaded) arrays
        evaluate_temporal_full(s, fr)
        assert rec_a.log <= need_a, (s.strategy_id, rec_a.log - need_a)
        assert rec_t.log == need_t, (s.strategy_id, rec_t.log ^ need_t)
    lazy = evaluate_temporal_many(specs, real.frame, use_cache=False)
    sub = frame_for_specs(real.frame, specs)
    assert len(sub.arrays) < 0.4 * len(real.events)  # a 40-spec batch needs a small slice of 385
    frozen = evaluate_temporal_many(specs, sub)
    assert all(x.to_bytes() == y.to_bytes() for x, y in zip(lazy, frozen, strict=True))
    assert sum(len(x.candidates.decision_idx) > 0 for x in frozen) >= 25


def test_dist_twap_atr_is_slice_stable_bitwise(real):
    """Per-Berlin-day cumsum: a frame that starts at any day boundary (or ends anywhere) reproduces the
    full-frame values of the days it contains BIT-IDENTICALLY (a global cumsum differed at ~1e-7)."""
    f = real.features
    keys = ("h", "l", "c", "berlin_day_id", "m5_atr14")
    full = _dist_twap_atr({k: f[k] for k in keys})
    day = np.asarray(f["berlin_day_id"])
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]])
    checked = 0
    for s in (starts[40], starts[len(starts) // 2], starts[-30]):
        e = min(s + 4000, len(day))
        part = _dist_twap_atr({k: np.asarray(f[k])[s:e] for k in keys})
        assert np.array_equal(part, full[s:e], equal_nan=True)
        checked += e - s
    assert checked > 5000
    assert "dist_twap_atr" in real.frame.arrays  # honest alias of the registry feature dist_vwap_atr
    assert np.array_equal(real.frame.arrays["dist_twap_atr"], real.frame.arrays["dist_vwap_atr"], equal_nan=True)


def test_workers_gt1_on_lazy_frame_materialises_specs_automatically(real):
    """SharedFrame dumps only LOADED arrays: a lazy build_market_frame frame used to raise KeyError in workers."""
    specs = hand_specs()[:4]
    fresh = build_market_frame(real.features, real.events, thresholds=dict(real.frame.thresholds) or None, plan=real.plan)
    assert fresh.arrays.loaded() == ()
    par = evaluate_temporal_many(specs, fresh, workers=2)
    seq = evaluate_temporal_many(specs, frame_for_specs(real.frame, specs), workers=1)
    assert sum(len(r.candidates.decision_idx) for r in seq) > 0
    for a, b in zip(par, seq, strict=True):
        for fld in ("decision_idx", "direction", "stop", "target", "target_r"):
            assert np.array_equal(getattr(a.candidates, fld), getattr(b.candidates, fld), equal_nan=True)
