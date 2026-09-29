"""Causality + hand-computed tests for the price-action features (FeatureStore additions)."""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from alpha.fast.spec import FEATURE_NAMES
from alpha.fast.store import (
    FEATURE_SET_VERSION,
    NEW_FEATURE_NAMES,
    FeatureConfig,
    FeatureStore,
)
from tests._fast_equiv import DATA, ROOT, requires_dataset


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    return a.dtype == b.dtype and np.array_equal(a, b, equal_nan=True)


def _flat(periods: int = 200, start: str = "2024-03-28 20:00") -> pd.DataFrame:
    """Flat 100/101/99 bars; Berlin (CET) day boundary lies at 23:00 UTC = bar index 36."""
    ts = pd.date_range(start, periods=periods, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "ts": ts,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "spread_pts": 1.0,
        }
    )


def _set(df: pd.DataFrame, i: int, **values: float) -> None:
    for key, value in values.items():
        df.loc[i, key] = value


def test_names_registered_and_version_in_cache_key(tmp_path, monkeypatch) -> None:
    assert len(NEW_FEATURE_NAMES) == len(set(NEW_FEATURE_NAMES)) == 29
    assert set(NEW_FEATURE_NAMES) <= FEATURE_NAMES
    frame = _flat(120)
    first = FeatureStore.load_or_build(frame, None, tmp_path)
    assert first.metadata["feature_set_version"] == FEATURE_SET_VERSION
    assert set(NEW_FEATURE_NAMES) <= set(first)
    monkeypatch.setattr("alpha.fast.store.FEATURE_SET_VERSION", FEATURE_SET_VERSION + 1)
    second = FeatureStore.load_or_build(frame, None, tmp_path)
    assert second.metadata["cache_hit"] is False
    assert second.metadata["cache_key"] != first.metadata["cache_key"]


def test_session_high_low_are_session_so_far() -> None:
    df = _flat(80)
    _set(df, 50, high=110.0, low=90.0)
    f = FeatureStore.build(df, FeatureConfig())
    assert f["berlin_day_id"][35] == 0 and f["berlin_day_id"][36] == 1
    assert f["session_high"][49] == 101.0 and f["session_low"][49] == 99.0
    assert f["session_high"][50] == 110.0 and f["session_low"][50] == 90.0
    assert f["session_high"][36] == 101.0  # day 2 restarts, not carried from day 1
    assert f["dist_sess_high_atr"][49] == (100.0 - 101.0) / f["m5_atr14"][49]


def test_brk_up_20_hand_computed_and_day_boundary_nan() -> None:
    df = _flat()
    _set(df, 60, open=100.0, high=103.5, low=100.0, close=103.0)
    f = FeatureStore.build(df, FeatureConfig())
    atr = f["m5_atr14"]
    assert atr[59] == 2.0
    assert f["brk_up_20"][60] == (103.0 - 101.0) / atr[60]
    assert f["brk_dn_20"][60] == (99.0 - 103.0) / atr[60]
    # day 1 (idx 0-35) is its own run; day 2 starts at idx 36: a 20-bar look-back needs idx >= 56
    assert np.isnan(f["brk_up_20"][36:56]).all()
    assert np.isfinite(f["brk_up_20"][56]) and np.isfinite(f["brk_up_20"][30])
    assert np.isnan(f["brk_up_20"][:20]).all()  # warm-up on the first day
    assert f["brk_up_20"][58] == (100.0 - 101.0) / atr[58]
    # 48-bar look-back needs idx >= 84
    assert np.isnan(f["brk_up_48"][36:84]).all() and np.isfinite(f["brk_up_48"][84])


def test_sweep_hi_20_and_pdh_and_gap() -> None:
    df = _flat()
    _set(df, 90, high=102.0, close=100.5)  # takes out 101, closes back below
    _set(df, 100, high=102.0, close=101.5)  # breaks out and holds above -> no sweep
    _set(df, 36, open=101.5, close=101.5, high=101.5, low=101.5)
    f = FeatureStore.build(df, FeatureConfig())
    assert f["sweep_hi_20"][90] == 1.0
    assert f["sweep_hi_20"][100] == 0.0
    assert f["sweep_hi_20"][91] == 0.0 and f["sweep_lo_20"][90] == 0.0
    assert np.isnan(f["sweep_hi_20"][36:56]).all()
    # previous_day_high = 101 (day 1); NaN on day 1
    assert np.isnan(f["sweep_pdh"][:36]).all()
    assert f["sweep_pdh"][90] == 1.0 and f["sweep_pdh"][100] == 0.0
    # gap: session_open (101.5, bar 36) - previous_day_close (100), known from the open bar
    assert f["gap_atr"][36] == (101.5 - 100.0) / f["m5_atr14"][36]
    assert np.isnan(f["gap_atr"][:36]).all()


def test_dist_pdh_bar_structure_and_momentum() -> None:
    df = _flat()
    _set(df, 60, open=100.0, high=103.5, low=100.0, close=103.0)
    _set(df, 61, open=100.0, high=100.0, low=100.0, close=100.0)  # flat bar h == l
    f = FeatureStore.build(df, FeatureConfig())
    atr = f["m5_atr14"]
    assert f["dist_pdh_atr"][60] == (103.0 - 101.0) / atr[60]
    assert f["dist_pdl_atr"][60] == (103.0 - 99.0) / atr[60]
    assert f["dist_pdc_atr"][60] == (103.0 - 100.0) / atr[60]
    assert np.isnan(f["dist_pdh_atr"][:36]).all()
    assert f["bar_close_loc"][60] == 3.0 / 3.5
    assert f["bar_body_ratio"][60] == 3.0 / 3.5
    assert f["upper_wick_ratio"][60] == 0.5 / 3.5 and f["lower_wick_ratio"][60] == 0.0
    assert f["bar_range_atr"][60] == 3.5 / atr[60] and f["bar_dir"][60] == 1.0
    assert f["bar_close_loc"][61] == 0.5 and f["bar_body_ratio"][61] == 0.0
    assert f["bar_dir"][61] == 0.0
    assert f["mom_3_atr"][63] == (100.0 - 103.0) / atr[63]
    assert np.isnan(f["mom_3_atr"][36:39]).all() and np.isfinite(f["mom_3_atr"][39])
    assert np.isnan(f["mom_12_atr"][36:48]).all() and np.isfinite(f["mom_12_atr"][48])


def test_from_high_24_atr() -> None:
    df = _flat()
    _set(df, 60, open=100.0, high=103.5, low=100.0, close=103.0)
    f = FeatureStore.build(df, FeatureConfig())
    atr = f["m5_atr14"]
    # window h[i-23..i] must lie in day 2 (idx >= 36) -> first valid idx 59
    assert np.isnan(f["from_high_24_atr"][36:59]).all()
    assert f["from_high_24_atr"][59] == (101.0 - 100.0) / atr[59]
    assert f["from_high_24_atr"][62] == (103.5 - 100.0) / atr[62]
    assert f["from_low_24_atr"][62] == (100.0 - 99.0) / atr[62]
    assert f["from_high_24_atr"][60] == (103.5 - 103.0) / atr[60]


# ------------------------------------------------------------------ causality (truncation)


def _synthetic_multi_day(start: str, days: int, seed: int = 7) -> pd.DataFrame:
    """Weekday 07:00-21:00 UTC M5 bars with overnight/weekend gaps, spanning a DST change."""
    rng = np.random.default_rng(seed)
    stamps = []
    for day in pd.date_range(start, periods=days, freq="D", tz="UTC"):
        if day.dayofweek >= 5:
            continue
        stamps.append(pd.date_range(day + pd.Timedelta(hours=7), periods=168, freq="5min"))
    ts = stamps[0].append(stamps[1:])
    n = len(ts)
    close = 100.0 + np.cumsum(rng.normal(0, 0.4, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, 0.1, n)
    high = np.maximum(open_, close) + rng.uniform(0, 0.5, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.5, n)
    return pd.DataFrame(
        {"ts": ts, "open": open_, "high": high, "low": low, "close": close, "spread_pts": 1.0}
    )


def _assert_truncation_invariant(frame: pd.DataFrame, cuts: list[int]) -> None:
    full = FeatureStore.build(frame, FeatureConfig())
    for cut in cuts:
        short = FeatureStore.build(frame.iloc[:cut].copy().reset_index(drop=True), FeatureConfig())
        for name in NEW_FEATURE_NAMES:
            assert _same(full[name][:cut], short[name]), (name, cut)


def test_truncation_invariance_synthetic_gaps_and_dst() -> None:
    frame = _synthetic_multi_day("2024-03-25", 14)  # DST change 2024-03-31 inside
    full = FeatureStore.build(frame, FeatureConfig())
    boundaries = np.flatnonzero(np.diff(full["berlin_day_id"]) != 0) + 1
    ts = pd.DatetimeIndex(frame["ts"])
    dst_bar = int(np.searchsorted(ts, pd.Timestamp("2024-04-01", tz="UTC")))
    b = int(boundaries[1])
    cuts = [300, 500, b - 1, b, b + 1, dst_bar - 1, dst_bar + 30, len(frame) - 7]
    assert np.isnan(full["brk_up_20"][b : b + 20]).all()
    _assert_truncation_invariant(frame, cuts)


@requires_dataset
def test_truncation_invariance_dev_frame() -> None:
    sys.path.insert(0, str(ROOT / "research" / "runners"))
    import ar2_compare

    from alpha.common.dataset import load_research_dataset
    from alpha.common.protocol import Partition, SplitPlan

    cfg = json.loads((ROOT / "research/configs/ar2_phase2.json").read_text(encoding="utf-8"))
    plan = SplitPlan(**{k: Partition(k, *v) for k, v in cfg["splits"].items()})
    dev = ar2_compare.dev_frame(load_research_dataset(DATA).frame, plan)
    ts = pd.DatetimeIndex(dev["ts"])
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    dst = int(np.searchsorted(ts, pd.Timestamp("2025-03-30", tz="UTC")))
    assert 0 < dst < len(dev), "DST week not inside the dev frame"
    window = dev.iloc[max(0, dst - 1500) : dst + 1500].reset_index(drop=True)
    full = FeatureStore.build(window, FeatureConfig())
    boundary = int(np.flatnonzero(np.diff(full["berlin_day_id"]) != 0)[10]) + 1
    cuts = [700, boundary - 1, boundary, boundary + 1, 1500, 1540, len(window) - 3]
    _assert_truncation_invariant(window, cuts)
