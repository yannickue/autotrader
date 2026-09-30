"""Causality: prefix equality and future perturbation of the event arrays."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alpha.events.store import build_events
from tests.events._helpers import build_features, diff_names, slice_frame

WINDOWS = {
    "spring_dst": ("2025-03-24", "2025-04-04"),  # DST starts Sunday 2025-03-30
    "autumn_dst": ("2025-10-20", "2025-10-31"),  # DST ends Sunday 2025-10-26
}


def _cut_points(frame: pd.DataFrame, seed: int) -> list[int]:
    """~20 cut points: day ends, day starts, mid-HTF-bar cuts and random bars (t = bars kept)."""
    local = pd.DatetimeIndex(frame["ts"]).tz_convert("Europe/Berlin")
    day = np.asarray(local.normalize().tz_localize(None).asi8)
    n = len(frame)
    last_of_day = np.flatnonzero(np.r_[day[1:] != day[:-1], True])
    rng = np.random.default_rng(seed)
    cuts: set[int] = set()
    cuts.update(int(x) + 1 for x in rng.choice(last_of_day[3:-1], 3, replace=False))  # day ends
    cuts.update(int(x) + 2 for x in rng.choice(last_of_day[3:-1], 3, replace=False))  # 1st bar+1
    minute = (local.hour * 60 + local.minute).to_numpy()
    mid_bucket = np.flatnonzero((minute % 60) == 35)  # inside an M15 and an H1 bucket
    cuts.update(int(x) + 1 for x in rng.choice(mid_bucket[30:], 4, replace=False))
    cuts.update(int(x) + 1 for x in rng.choice(np.arange(700, n - 1), 10, replace=False))
    return sorted(c for c in cuts if 300 < c < n)


@pytest.fixture(scope="module", params=list(WINDOWS))
def window(request):
    frame = slice_frame(*WINDOWS[request.param])
    feats = build_features(frame)
    return request.param, frame, feats, build_events(feats)


def test_prefix_equality(window):
    """events built on the raw prefix [:t] equal events[:t] of the full frame, byte for byte."""
    name, frame, _, full = window
    cuts = _cut_points(frame, seed=7)
    assert len(cuts) >= 18
    for t in cuts:
        prefix = build_events(build_features(frame.iloc[:t]))
        bad = diff_names(full, prefix, t)
        assert not bad, f"{name}: prefix t={t} differs in {bad[:5]}"


def _perturb_after(frame: pd.DataFrame, t: int, seed: int) -> pd.DataFrame:
    """Random OHLC/spread for bars >= t (timestamps kept)."""
    rng = np.random.default_rng(seed)
    out = frame.copy()
    m = len(out) - t
    price = float(out["close"].iloc[t - 1]) + np.cumsum(rng.normal(0, 25, m))
    o = price + rng.normal(0, 10, m)
    c = price + rng.normal(0, 10, m)
    out.loc[out.index[t:], "open"] = o
    out.loc[out.index[t:], "close"] = c
    out.loc[out.index[t:], "high"] = np.maximum(o, c) + rng.uniform(0, 30, m)
    out.loc[out.index[t:], "low"] = np.minimum(o, c) - rng.uniform(0, 30, m)
    out.loc[out.index[t:], "spread_pts"] = rng.uniform(1, 50, m)
    return out


def test_future_perturbation(window):
    """Randomising bars > t leaves events[:t+1] unchanged (t = last bar that must stay)."""
    name, frame, _, full = window
    cuts = _cut_points(frame, seed=11)[:14]
    for cut in cuts:
        t = cut - 1  # keep bars <= t
        perturbed = _perturb_after(frame, t + 1, seed=cut)
        assert not perturbed["close"].iloc[t + 1 :].equals(frame["close"].iloc[t + 1 :])
        ev = build_events(build_features(perturbed))
        bad = diff_names(full, ev, t + 1)
        assert not bad, f"{name}: perturbation after t={t} changed {bad[:5]}"
