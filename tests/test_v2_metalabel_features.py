# ruff: noqa: E501
"""Meta-label features: causality by future perturbation, direction handling, confluence counts."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.metalabel import features as F

N = 2400
BARS_PER_DAY = 100


def make_inputs(n: int = N, seed: int = 0) -> F.FeatureInputs:
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 0.3, n))
    day = np.arange(n) // BARS_PER_DAY
    minute = 540 + 5 * (np.arange(n) % BARS_PER_DAY)
    a: dict[str, np.ndarray] = {}
    for name in F.REQUIRED_ARRAYS:
        a[name] = rng.normal(0, 1, n)
    a["c"] = c
    a["m5_atr14"] = 0.5 + np.abs(rng.normal(0, 0.1, n))
    a["spread"] = 0.2 + np.abs(rng.normal(0, 0.05, n))
    a["berlin_day_id"] = day.astype(np.float64)
    a["previous_day_close"] = np.r_[np.full(BARS_PER_DAY, np.nan), c[:-BARS_PER_DAY]]
    for lv in F.LEVEL_ARRAYS:
        a[lv] = c + rng.normal(0, 2.0, n)
    for tf in ("m15", "h1", "d1"):
        a[f"st_{tf}_trend_up"] = (rng.random(n) < 0.4).astype(float)
        a[f"st_{tf}_trend_dn"] = (rng.random(n) < 0.3).astype(float)
    dow = ((day + 3) % 5).astype(np.int64)
    return F.FeatureInputs(a, minute.astype(np.int64), dow, 540, 1040)


def make_trade_info(i: np.ndarray, rng: np.random.Generator, inp: F.FeatureInputs) -> F.TradeInfo:
    m = len(i)
    d = rng.choice(np.array([-1.0, 1.0]), m)
    c = inp.a["c"][i]
    stop = c - d * (1.0 + rng.random(m))
    tgt = np.where(rng.random(m) < 0.3, c + d * 2.0, np.nan)
    z = np.full(m, np.nan)
    return F.TradeInfo(i, d, stop, tgt, np.full(m, 1.5), z, z, rng.integers(0, 9, m), rng.integers(0, 4, m).astype(float),
                       rng.integers(0, 2, m).astype(float), rng.integers(1, 3, m).astype(float))


def _equal(a: np.ndarray, b: np.ndarray) -> bool:
    return np.array_equal(a, b, equal_nan=True)


@pytest.mark.parametrize("i0", [400, 950, 1503, 2100])
def test_bar_and_trade_features_ignore_the_future(i0):
    inp = make_inputs()
    rng = np.random.default_rng(i0)
    raw1 = F.bar_features(inp)
    inp2 = F.perturb_future(inp, i0, np.random.default_rng(1))
    assert not _equal(inp.a["c"], inp2.a["c"])  # the perturbation really changes the future
    raw2 = F.bar_features(inp2)
    for k in raw1:
        assert _equal(raw1[k][: i0 + 1], raw2[k][: i0 + 1]), f"bar feature {k} leaks the future"
    i = np.sort(rng.choice(np.arange(300, i0 + 1), 60, replace=False))
    ti = make_trade_info(i, rng, inp)
    x1, names = F.trade_matrix(inp, raw1, ti)
    x2, _ = F.trade_matrix(inp2, raw2, ti)
    bad = [names[j] for j in range(len(names)) if not _equal(x1[:, j], x2[:, j])]
    assert not bad, f"trade features depend on bars after the decision bar: {bad}"


def test_perturbing_the_past_does_change_features():
    """Power check of the causality test: a modification of bars <= i does alter the row."""
    inp = make_inputs()
    i0 = 1500
    rng = np.random.default_rng(3)
    i = np.array([i0])
    ti = make_trade_info(i, rng, inp)
    x1, names = F.trade_matrix(inp, F.bar_features(inp), ti)
    a = {k: v.copy() for k, v in inp.a.items()}
    a["m5_atr14"][i0 - 50:i0 + 1] *= 3.0
    a["spread"][i0 - 50:i0 + 1] *= 3.0
    inp2 = F.FeatureInputs(a, inp.minute, inp.dow, 540, 1040)
    x2, _ = F.trade_matrix(inp2, F.bar_features(inp2), ti)
    changed = {names[j] for j in range(len(names)) if not _equal(x1[:, j], x2[:, j])}
    assert {"atr_pctl", "atr_rel_mean", "spread_rel_med", "spread_atr", "risk_atr"} <= changed


def test_direction_adjustment_flips_signed_features():
    inp = make_inputs()
    raw = F.bar_features(inp)
    i = np.array([700, 800])
    rng = np.random.default_rng(0)
    ti = make_trade_info(i, rng, inp)
    long = F.TradeInfo(i, np.ones(2), *(getattr(ti, f) for f in ("stop", "target", "target_r", "zone_lo", "zone_hi", "family", "k_same", "k_opp", "n_fam_same")))
    short = F.TradeInfo(i, -np.ones(2), *(getattr(ti, f) for f in ("stop", "target", "target_r", "zone_lo", "zone_hi", "family", "k_same", "k_opp", "n_fam_same")))
    xl, names = F.trade_matrix(inp, raw, long)
    xs, _ = F.trade_matrix(inp, raw, short)
    col = {n: j for j, n in enumerate(names)}
    for name in ("dir_mom_3_atr", "trend_h1", "ahead_dist_pdh_atr", "dir_m15_ema_slope"):
        assert np.allclose(xl[:, col[name]], -xs[:, col[name]], equal_nan=True), name
    assert np.allclose(xl[:, col["room_ahead_24"]], xs[:, col["room_behind_24"]])
    # level 'ahead' distance: long sees the level above as positive, short sees it as negative
    assert np.allclose(xl[:, col["lvl_pdh"]], -xs[:, col["lvl_pdh"]], equal_nan=True)


def test_confluence_counts_same_opposite_families():
    dec = np.array([10, 10, 10, 20, 20, 30])
    dr = np.array([1, 1, -1, 1, 1, -1])
    fam = np.array([0, 1, 2, 3, 3, 4])
    same, opp, nf = F.confluence_counts(fam, dec, dr)
    assert same.tolist() == [1, 1, 0, 1, 1, 0]
    assert opp.tolist() == [1, 1, 2, 0, 0, 0]
    assert nf.tolist() == [2, 2, 1, 1, 1, 1]


def test_d1_features_use_prior_days_only():
    inp = make_inputs()
    raw = F.bar_features(inp)
    c, day = inp.a["c"], inp.a["berlin_day_id"].astype(int)
    k, i = 10, 10 * BARS_PER_DAY + 37
    closes = np.array([c[(day == kk)][-1] for kk in range(k)])
    expect = (closes[-1] - closes[-5:].mean()) / inp.a["m5_atr14"][i]
    assert raw["d1_sma_dist_atr"][i] == pytest.approx(expect)
    assert np.isnan(raw["d1_sma_dist_atr"][3 * BARS_PER_DAY + 5])  # fewer than 5 prior closes
