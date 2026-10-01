# ruff: noqa: E501
"""Gate-B audit helpers: the leakage harness works on a toy series (a deliberately leaky toy feature is DETECTED = negative control), the real-data
recomputation audits pass on the synthetic backfill and fail when a stored value is tampered, plausibility flags impossible values."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from ol_backfill_support import MARKET, built

from coverage_analysis.observer_lab import backfill as BF
from coverage_analysis.observer_lab.backfill_audit import (
    audit_buffer_pass,
    audit_shallow_depths,
    audit_window_recompute,
    diff_keys,
    plausibility,
    prefix_invariance_audit,
    values_equal,
)
from market_observer import bars_adapter as BA
from market_observer.schema import ObserverBars, SessionSpec


def toy_bars(n: int = 120) -> ObserverBars:
    rng = np.random.default_rng(3)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    return BA.build_observer_bars(MARKET, (np.arange(n) * 300 + 1_780_000_000) * 10**9, c, c + 0.5, c - 0.5, c, np.full(n, 10.0), np.zeros(n), tick_size=0.01, session=SessionSpec("UTC", None, None))


def honest(b: ObserverBars, i: int) -> dict:
    return {"dev": float(b.c[i] - b.c[max(0, i - 4): i + 1].mean()), "hi": float(b.h[: i + 1].max())}


def leaky_next_close(b: ObserverBars, i: int) -> dict:
    return {"x": float(b.c[i + 1])}  # reads a bar after the decision bar -> IndexError on the prefix


def leaky_centered_mean(b: ObserverBars, i: int) -> dict:
    return {"x": float(b.c[max(0, i - 2): i + 3].mean())}  # centred window: silently shorter on the prefix


def test_harness_passes_an_honest_feature_and_detects_both_kinds_of_leak():
    bars = toy_bars()
    idx = list(range(5, 100, 7))
    ok = prefix_invariance_audit(honest, bars, idx)
    assert ok.passed and ok.n_pass == len(idx) and ok.n_fail == 0
    leak1 = prefix_invariance_audit(leaky_next_close, bars, idx)
    assert not leak1.passed and leak1.n_fail == len(idx) and "beyond the prefix" in str(leak1.failures[0]["detail"])
    leak2 = prefix_invariance_audit(leaky_centered_mean, bars, idx)
    assert not leak2.passed and leak2.n_fail >= 1 and leak2.to_dict()["verdict"] == "FAIL"


def test_values_equal_treats_none_and_nan_alike_and_is_exact():
    assert values_equal(None, float("nan")) and values_equal(3, 3.0) and values_equal("a", "a")
    assert not values_equal(1.0, 1.0 + 1e-12) and not values_equal(None, 0.0) and not values_equal(True, 1)
    assert diff_keys({"a": 1, "b": 2}, {"a": 1, "b": 3}) == ["b"]


def _stored():
    b = built()
    f = pd.read_parquet(b["mdir"] / "features.parquet")
    e = pd.read_parquet(b["mdir"] / "events.parquet")[["event_id", "decision_idx"]]
    return b, f.merge(e, on="event_id")


def test_real_recomputation_audits_pass_on_the_synthetic_backfill_and_detect_tampering():
    b, stored = _stored()
    cfg = BF.observer_config_for(b["mspec"])
    spec = b["mspec"]
    mk = lambda: BA.BarBuffer(MARKET, tick_size=0.01, session=BF.build_bars(b["frame"].iloc[:50].reset_index(drop=True), spec, MARKET).session, max_bars=10**9)  # noqa: E731
    sample = stored.iloc[[0, 3, 6, len(stored) - 1]]
    buf = audit_buffer_pass(b["frame"], mk, cfg, 0.01, sample, window=6000)
    assert buf.passed and buf.n_pass == len(sample), buf.to_dict()
    build = lambda fr: BF.build_bars(fr, spec, MARKET)  # noqa: E731
    pre = audit_window_recompute(b["frame"], build, cfg, sample, depth=None, extension=0, name="prefix")
    ext = audit_window_recompute(b["frame"], build, cfg, sample, depth=None, extension=150, name="extended")
    assert pre.passed and ext.passed, (pre.to_dict(), ext.to_dict())
    # tampering with one stored value is detected by every audit
    bad = sample.copy()
    col = "f_balance__range_width_atr_w24"
    bad.iloc[1, bad.columns.get_loc(col)] = float(bad.iloc[1][col]) + 0.5
    assert not audit_buffer_pass(b["frame"], mk, cfg, 0.01, bad).passed
    assert not audit_window_recompute(b["frame"], build, cfg, bad, depth=None, name="t").passed
    sh = audit_shallow_depths(b["frame"], build, cfg, sample, depths=(120, 240))
    assert all(v["rule_ok"] and v["flagged_not_warm"] == v["n"] for v in sh.values())


def test_plausibility_flags_impossible_values_and_constants():
    df = pd.DataFrame({
        "decision_ts_ns": [1000, 2000, 3000], "f_levels__zone_width_atr": [1.0, -0.5, 2.0], "f_balance__bar_overlap_ratio_w24": [0.2, 1.4, 0.5],
        "f_levels__last_touch_ts_ns": [900, 2500, None], "f_levels__role": ["SUPPORT", "BOGUS", None], "f_swings__m5_sequence": ["UP_SEQUENCE"] * 3,
        "f_levels__touch_count": [1, 2, 3],
    })
    p = plausibility(df)
    flagged = {x["column"]: x["flags"] for x in p["impossible_value_flags"]}
    assert set(flagged) == {"f_levels__zone_width_atr", "f_balance__bar_overlap_ratio_w24", "f_levels__last_touch_ts_ns", "f_levels__role"}
    assert p["columns"]["f_swings__m5_sequence"]["constant"] and p["columns"]["f_levels__touch_count"]["constant"] is False
    assert p["columns"]["f_levels__last_touch_ts_ns"]["missing_share"] == pytest.approx(1 / 3, abs=1e-5)
    assert p["n_flagged"] == 4 and p["groups"]["levels"]["n_columns"] == 4
