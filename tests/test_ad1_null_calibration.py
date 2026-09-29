"""Null-frame construction for AD1 null calibration (research only)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.runners.ad1_null_calibration import make_null_frame


def _frame(days: int = 6, bars: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    ts = []
    for d in range(days):
        start = pd.Timestamp("2025-03-03 00:15", tz="UTC") + pd.Timedelta(days=d)
        ts += list(pd.date_range(start, periods=bars, freq="5min"))
    # a data hole inside day 2
    ts = pd.DatetimeIndex(ts)
    keep = np.ones(len(ts), dtype=bool)
    keep[2 * bars + 20: 2 * bars + 30] = False
    ts = ts[keep]
    n = len(ts)
    c = 20000 * np.exp(np.cumsum(rng.normal(0, 4e-4, n)))
    o = np.round(c + rng.normal(0, 2, n), 2)
    c = np.round(c, 2)
    h = np.round(np.maximum(o, c) + rng.uniform(0, 3, n), 2)
    lo = np.round(np.minimum(o, c) - rng.uniform(0, 3, n), 2)
    return pd.DataFrame({"ts": ts, "open": o, "high": h, "low": lo, "close": c,
                         "tick_volume": rng.integers(1, 100, n).astype(float),
                         "spread_pts": rng.uniform(5, 30, n).round(0)})


def test_shape_and_untouched_columns() -> None:
    dev = _frame()
    null = make_null_frame(dev, 1)
    assert len(null) == len(dev)
    assert (null["ts"] == dev["ts"]).all()
    assert (null["spread_pts"] == dev["spread_pts"]).all()
    assert (null["tick_volume"] == dev["tick_volume"]).all()
    assert list(null.columns) == list(dev.columns)


def test_ohlc_consistency() -> None:
    null = make_null_frame(_frame(), 2)
    assert (null["high"] >= null[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (null["low"] <= null[["open", "close"]].min(axis=1) + 1e-9).all()
    assert (null["close"] > 0).all()


def test_seed_behaviour() -> None:
    dev = _frame()
    a, b, a2 = make_null_frame(dev, 1), make_null_frame(dev, 2), make_null_frame(dev, 1)
    assert a.equals(a2)
    assert not a["close"].equals(b["close"])
    assert not a["close"].equals(dev["close"])


def test_return_distribution_preserved() -> None:
    dev = _frame()
    null = make_null_frame(dev, 3)
    r0 = np.diff(np.log(dev["close"].to_numpy()))
    r1 = np.diff(np.log(null["close"].to_numpy()))
    # same multiset of returns up to price-tick rounding of the rebuilt closes
    assert r1.mean() == pytest.approx(r0.mean(), abs=2e-6)
    assert r1.std() == pytest.approx(r0.std(), rel=0.01)
    np.testing.assert_allclose(np.sort(r1), np.sort(r0), atol=2e-6)
    # start and end price kept (bridge), gap bars keep their own return
    assert null["close"].iloc[0] == dev["close"].iloc[0]
    assert null["close"].iloc[-1] == pytest.approx(dev["close"].iloc[-1], abs=0.05)


def test_gaps_preserved() -> None:
    dev = _frame()
    null = make_null_frame(dev, 4)
    ts_ns = pd.DatetimeIndex(dev["ts"]).asi8
    gap = np.diff(ts_ns) != np.diff(ts_ns).min()
    r0 = np.diff(np.log(dev["close"].to_numpy()))
    r1 = np.diff(np.log(null["close"].to_numpy()))
    np.testing.assert_allclose(r1[gap], r0[gap], atol=2e-6)
