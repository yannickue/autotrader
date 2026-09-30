# ruff: noqa: RUF059
"""HTF mapping: a pulse is written to exactly one M5 bar; incomplete HTF bars emit nothing."""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from alpha.events import htf
from alpha.events.store import build_events
from tests.events._helpers import build_features, diff_names, slice_frame

M5_NS = 300 * 10**9


def _features(n: int, drop: tuple[int, ...] = (), start="2025-06-02 08:00") -> dict:
    """Synthetic M5 arrays starting on an hour boundary (10:00 Berlin), 1 day, optional holes."""
    ts = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    keep = np.array([i for i in range(n) if i not in drop])
    ts_ns = ts.as_unit("ns").asi8[keep]
    m = len(keep)
    price = 100.0 + np.arange(m, dtype=float)
    return {
        "ts_ns": ts_ns,
        "o": price,
        "h": price + 1,
        "l": price - 1,
        "c": price + 0.5,
        "berlin_day_id": np.zeros(m, np.int32),
        "berlin_minute": np.arange(m, dtype=np.int16) * 5 + 600,
    }


def test_m15_pulse_stamped_on_the_closing_bar_only():
    s = htf.build_series(_features(12), "M15")
    assert s.n == 4
    assert s.stamp.tolist() == [2, 5, 8, 11]  # last M5 bar of each bucket
    assert s.vis.tolist() == [-1, -1, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3]
    out = s.pulse_to_m5(np.ones(s.n, np.uint8))
    assert out.sum() == s.n and np.flatnonzero(out).tolist() == [2, 5, 8, 11]


def test_data_hole_drops_the_incomplete_bucket_and_never_moves_a_pulse_earlier():
    # M5 bar 4 is missing: the M15 bucket [3,4,5] is incomplete and emits nothing
    s = htf.build_series(_features(12, drop=(4,)), "M15")
    assert s.n == 3
    assert s.stamp.tolist() == [2, 7, 10]  # positions after the hole shift with the data
    out = s.pulse_to_m5(np.ones(s.n, np.uint8))
    assert np.flatnonzero(out).tolist() == [2, 7, 10] and out.sum() == 3
    # the bars of the broken bucket keep seeing the previous complete bar (forward fill)
    assert s.vis[3] == 0 and s.vis[4] == 0


def test_open_htf_bar_is_not_in_the_series():
    s = htf.build_series(_features(17), "M15")  # last bucket has 2 of 3 bars
    assert s.n == 5 and s.stamp.tolist() == [2, 5, 8, 11, 14]
    h1 = htf.build_series(_features(17), "H1")
    assert h1.n == 1 and h1.stamp.tolist() == [11]
    assert h1.pulse_to_m5(np.ones(1, np.uint8))[12:].sum() == 0


def test_value_arrays_are_written_on_the_stamp_bar_only():
    s = htf.build_series(_features(12), "M15")
    vals = np.array([10.0, 20.0, 30.0, 40.0])
    pulse = np.array([0, 1, 0, 1], np.uint8)
    out = s.values_to_m5(vals, pulse)
    assert np.flatnonzero(np.isfinite(out)).tolist() == [5, 11]
    assert out[5] == 20.0 and out[11] == 40.0
    org = s.values_to_m5(np.array([0, 1, 2, 3], np.int64), pulse)
    assert org[5] == 1 and org[11] == 3 and (org[[0, 1, 2]] == -1).all()
    # state/level arrays are forward-filled from the closing bar on
    lvl = s.ffill_to_m5(vals)
    assert np.isnan(lvl[:2]).all() and lvl[2] == 10.0 and lvl[4] == 10.0 and lvl[5] == 20.0


def test_so_far_arrays_include_only_bars_up_to_i():
    f = _features(12)
    s = htf.build_series(f, "M15")
    sf = htf.so_far_arrays(f, s)
    assert sf["progress"][:6].tolist() == pytest.approx([1 / 3, 2 / 3, 1.0, 1 / 3, 2 / 3, 1.0])
    assert sf["partial_h"][2] == f["h"][:3].max() and sf["partial_h"][1] == f["h"][:2].max()
    assert sf["partial_l"][4] == f["l"][3:5].min()  # restarts with the new bucket


def test_d1_visible_from_first_bar_of_next_day():
    f = _features(6)
    f["berlin_day_id"] = np.array([0, 0, 0, 1, 1, 2], np.int32)
    s = htf.build_series(f, "D1")
    assert s.n == 2  # the last (possibly still running) day never completes
    assert s.stamp.tolist() == [3, 5]
    assert s.vis.tolist() == [-1, -1, -1, 0, 0, 1]


# ---------------------------------------------------------------------------------------
# real data
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real():
    frame = slice_frame("2025-03-24", "2025-04-04")
    feats = build_features(frame)
    return frame, feats, build_events(feats)


def test_real_series_every_complete_bar_stamped_once_at_or_after_its_close(real):
    _, feats, _ = real
    for tf in ("M15", "H1", "D1"):
        s = htf.build_series(feats, tf)
        assert (s.stamp >= s.m5_end).all(), tf
        assert (s.stamp < s.n_m5).all(), tf
        assert (np.diff(s.stamp) > 0).all(), tf  # distinct bars -> distinct stamps: written once
        assert s.pulse_to_m5(np.ones(s.n, np.uint8)).sum() == s.n
        # visibility never runs ahead of the stamp: bar j is first visible exactly at stamp[j]
        first_visible = np.searchsorted(s.vis, np.arange(s.n), side="left")
        assert (first_visible == s.stamp).all()


def test_real_mapped_arrays_only_change_when_an_htf_bar_completes(real):
    _, feats, ev = real
    checked = 0
    for tf, name in (("M15", "m15"), ("H1", "h1"), ("D1", "d1")):
        if tf == "D1":
            new_bar = np.r_[True, np.diff(feats["berlin_day_id"]) != 0]
        else:
            new_bar = ev[f"sf_{name}_bar_progress"] >= 1.0 - 1e-12
        for key, arr in ev.items():
            if not re.match(rf"^(st|lv|zid|ev|evl|evx|evo)_{name}_", key):
                continue
            a = np.asarray(arr)
            if key.startswith(("evl_", "evx_", "evo_")):
                continue  # pulse-bound values: covered through their ev_ flag
            if key.startswith("ev_"):
                changed = np.flatnonzero(a)
            elif a.dtype.kind == "f":
                same = (a[1:] == a[:-1]) | (np.isnan(a[1:]) & np.isnan(a[:-1]))
                changed = np.flatnonzero(~same) + 1
            else:
                changed = np.flatnonzero(a[1:] != a[:-1]) + 1
            assert new_bar[changed].all(), (key, changed[~new_bar[changed]][:5])
            checked += 1
    assert checked > 100


def test_open_htf_bar_perturbation_leaves_htf_arrays_unchanged(real):
    """Randomising the rest of the currently open M15/H1 bar changes no HTF array up to t."""
    frame, feats, ev = real
    local = pd.DatetimeIndex(frame["ts"]).tz_convert("Europe/Berlin")
    minute = (local.hour * 60 + local.minute).to_numpy()
    mid = np.flatnonzero(minute % 60 == 35)  # first M5 bar of the :30 M15 bucket, mid-hour
    rng = np.random.default_rng(3)
    for t in (int(x) for x in rng.choice(mid[10:-10], 5, replace=False)):
        # bar t is the 4th... 6th M5 bar of an H1 bucket; bars t+1.. still belong to it
        pert = frame.copy()
        idx = pert.index[t + 1 :]
        m = len(idx)
        base = float(pert["close"].iloc[t])
        walk = base + np.cumsum(rng.normal(0, 30, m))
        pert.loc[idx, "open"] = walk + rng.normal(0, 5, m)
        pert.loc[idx, "close"] = walk + rng.normal(0, 5, m)
        pert.loc[idx, "high"] = np.maximum(pert.loc[idx, "open"], pert.loc[idx, "close"]) + 20
        pert.loc[idx, "low"] = np.minimum(pert.loc[idx, "open"], pert.loc[idx, "close"]) - 20
        got = build_events(build_features(pert))
        keys = [k for k in ev if re.search(r"_(m15|h1|d1)(_|$)", k)]
        assert keys
        left = {k: ev[k] for k in keys}
        right = {k: got[k] for k in keys}
        assert not diff_names(left, right, t + 1), t
