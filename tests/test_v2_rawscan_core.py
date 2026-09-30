# ruff: noqa: E501
"""Raw edge scan: forced-entry causality, day clustering, planted edge vs pure random walk,
null construction, holdout guard, ledger."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alpha.common.market_data import ForwardHoldoutError
from alpha.rawscan import (
    FLAT,
    CostParams,
    DataDisciplineError,
    TrialLedger,
    WindowSpec,
    bh_qvalues,
    build_day_grid,
    build_pairs,
    causal_atr,
    cluster_t,
    draw_shifts,
    scan_train,
)
from alpha.rawscan.engine import net_matrix
from alpha.rawscan.ledger import V1_CUMULATIVE_TRIALS, V1_CUMULATIVE_UNIQUE

TZ = "Europe/Berlin"
POINT = 0.01
WIN = WindowSpec(entry_start=108, entry_end=240, flat=258, cash_open=108, cash_close=210)
COSTS = {
    "BASE": CostParams("BASE", 1.0, 0.01),
    "COMBINED_ADVERSE": CostParams("COMBINED_ADVERSE", 2.0, 0.03, commission_price=0.01),
    "EXIT_SHOCK": CostParams("EXIT_SHOCK", 1.0, 0.01, extra_spread=1.0),
}
PLANT_SLOT = 120  # 10:00 local


def make_frame(n_days: int = 300, seed: int = 1, plant: float = 0.0, start: str = "2024-09-02"):
    """M5 bars 08:00-22:00 Berlin on business days; ``plant`` = extra return PER BAR added to the
    three bars starting at 10:00 (so the 10:00 -> 10:15 LONG has a drift of 3*plant)."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, periods=n_days)
    slots = np.arange(96, 264)
    r = rng.normal(0.0, 1.0, size=(n_days, len(slots)))
    r[:, [PLANT_SLOT - 96 + i for i in range(3)]] += plant
    close = 5000.0 + np.cumsum(r.ravel()).reshape(r.shape)
    flat = close.ravel()
    open_ = np.concatenate([[flat[0] - r.ravel()[0]], flat[:-1]]).reshape(r.shape)
    hi = np.maximum(open_, close) + np.abs(rng.normal(0, 0.3, r.shape))
    lo = np.minimum(open_, close) - np.abs(rng.normal(0, 0.3, r.shape))
    ts = []
    for d in days:
        base = pd.Timestamp(d.date(), tz=TZ)
        ts.extend(base + pd.Timedelta(minutes=5 * int(s)) for s in slots)
    frame = pd.DataFrame({
        "ts": pd.DatetimeIndex(ts).tz_convert("UTC"),
        "open": open_.ravel(), "high": hi.ravel(), "low": lo.ravel(), "close": close.ravel(),
        "tick_volume": 100.0, "spread_pts": 2.0,
    })
    return frame


@pytest.fixture(scope="module")
def planted():
    return make_frame(400, seed=3, plant=0.16)


@pytest.fixture(scope="module")
def planted_scan(planted):
    return scan_train(planted, tz=TZ, point_size=POINT, win=WIN, costs=COSTS, n_rep=40, seed=5)


def test_forced_entry_is_next_open_and_features_are_past_only(planted):
    grid = build_day_grid(planted, TZ, POINT)
    pairs = build_pairs(grid, WIN, COSTS)
    col = int(np.flatnonzero((pairs.slot == PLANT_SLOT) & (pairs.h == 3))[0])
    d = 200
    exp = (grid.O[d, PLANT_SLOT + 3] - grid.O[d, PLANT_SLOT]) / grid.ATR[d, PLANT_SLOT]
    assert pairs.G[d, col] == pytest.approx(exp)  # entry at the entry slot OPEN, exit at the h-th next OPEN
    # ATR at the entry open is the causal ATR of bars strictly before the entry bar
    i = int(np.flatnonzero(pd.DatetimeIndex(planted["ts"]).tz_convert(TZ)
                           == pd.Timestamp(grid.dates[d]).tz_localize(TZ) + pd.Timedelta(minutes=5 * PLANT_SLOT))[0])
    atr = causal_atr(planted["high"].to_numpy(), planted["low"].to_numpy(), planted["close"].to_numpy())
    assert grid.ATR[d, PLANT_SLOT] == pytest.approx(atr[i])
    # wrecking every bar from the entry bar on (H/L/C) leaves the entry-time features untouched
    hacked = planted.copy()
    hacked.loc[i:, ["high", "low", "close"]] *= 1.5
    hacked.loc[i:, "high"] += 100.0
    atr2 = causal_atr(hacked["high"].to_numpy(), hacked["low"].to_numpy(), hacked["close"].to_numpy())
    assert atr2[i] == pytest.approx(atr[i])
    assert hacked.loc[i, "open"] == planted.loc[i, "open"]
    # prefix stability of the causal ATR
    short = causal_atr(*(planted[k].to_numpy()[: i + 50] for k in ("high", "low", "close")))
    assert np.allclose(short[: i + 1], atr[: i + 1], equal_nan=True)


def test_flat_horizon_clips_at_forced_flat_and_duplicate_horizons_are_dropped(planted):
    grid = build_day_grid(planted, TZ, POINT)
    pairs = build_pairs(grid, WIN, COSTS)
    late = pairs.slot == WIN.flat - 2
    assert (pairs.exit_slot[late] == WIN.flat).all()
    assert late.sum() == 1  # every horizon collapses onto the flat slot -> one unique trade
    assert pairs.n_duplicate_horizons > 0
    assert (pairs.h[pairs.slot == PLANT_SLOT] == np.array([3, 6, 12, 24, 48, FLAT])).all()


def test_cluster_t_and_engine_agree(planted_scan):
    v = np.array([1.0, 3.0, 2.0, 2.0, -1.0, 5.0])
    days = np.array([0, 0, 1, 1, 2, 2])  # day means 2, 2, 2 -> zero variance -> nan t
    m, t, n = cluster_t(v, days)
    assert (m, n) == (2.0, 3) and np.isnan(t)
    v2 = np.array([1.0, 3.0, 4.0, 2.0, -1.0, 5.0])  # day means 2, 3, 2
    _m2, t2, _n2 = cluster_t(v2, days)
    dm = np.array([2.0, 3.0, 2.0])
    assert t2 == pytest.approx(dm.mean() / (dm.std(ddof=1) / np.sqrt(3)))
    tab, cs = planted_scan.table, planted_scan.cs
    row = tab[(tab.sigma == "long") & (tab.slot == PLANT_SLOT) & (tab.h == 3)].index[0]
    x = net_matrix(cs, "BASE", cols=np.array([row]))[:, 0]
    ok = np.isfinite(x)
    _, t_c, n_c = cluster_t(x[ok], np.flatnonzero(ok))
    assert tab.loc[row, "t"] == pytest.approx(t_c) and tab.loc[row, "n"] == n_c


def test_planted_edge_is_recovered_rank_one(planted_scan):
    tab = planted_scan.table
    top = tab.sort_values("t", ascending=False).iloc[0]
    assert top["sigma"] == "long" and PLANT_SLOT - 12 < top["slot"] <= PLANT_SLOT + 2
    assert top["t"] > 4.0 and top["q_bh"] < 0.05
    cell = tab[(tab.sigma == "long") & (tab.slot == PLANT_SLOT) & (tab.h == 3)].iloc[0]
    assert cell["t"] > 4.0 and cell["mean_net"] > 0.2  # ~0.3 ATR planted minus cost
    assert planted_scan.null_boot["max_t"]["observed"] > planted_scan.null_boot["max_t"]["p99"]
    assert planted_scan.null_flip["max_t"]["observed"] > planted_scan.null_flip["max_t"]["p99"]


def test_random_walk_is_inside_the_null_band_and_fails_fdr():
    fr = make_frame(400, seed=11, plant=0.0)
    res = scan_train(fr, tz=TZ, point_size=POINT, win=WIN, costs=COSTS, n_rep=100, seed=2)
    tab = res.table
    assert np.nanmin(tab["q_bh"]) > 0.05
    nb = res.null_boot["max_abs_t"]
    assert nb["observed"] <= nb["p99"]
    assert res.null_boot["max_t"]["observed"] <= res.null_boot["max_t"]["p99"]
    assert res.null_flip["max_t"]["observed"] <= res.null_flip["max_t"]["p99"]
    assert res.null_flip["max_abs_t"]["observed"] <= res.null_flip["max_abs_t"]["p99"]


def test_day_shift_null_shape_and_invariance(planted_scan):
    rng = np.random.default_rng(0)
    for d in (5, 40, 199):
        ks = draw_shifts(d, 200, rng)
        assert len(ks) == 200 and ks.min() >= 1 and ks.max() <= d - 1
    cs = planted_scan.cs
    tab = planted_scan.table
    ucol = np.array([tab[(tab.sigma == "long") & (tab.slot == PLANT_SLOT) & (tab.h == 6)].index[0]])
    a, b = net_matrix(cs, "BASE", cols=ucol)[:, 0], net_matrix(cs, "BASE", cols=ucol, shift=17)[:, 0]
    assert not np.array_equal(a, b)
    assert np.allclose(np.sort(a[np.isfinite(a)]), np.sort(b[np.isfinite(b)]))  # same multiset
    assert np.allclose(np.roll(a, 17), b, equal_nan=True)  # whole days move, intraday untouched
    # an unconditional cell has the same t under a whole-day shift; a DOW slice does not
    lcol = np.array([tab[(tab.sigma == "dow1_long") & (tab.slot == PLANT_SLOT) & (tab.h == 6)].index[0]])
    assert np.nansum(net_matrix(cs, "BASE", cols=ucol, shift=17)[:, 0]) == pytest.approx(np.nansum(a))
    assert not np.allclose(
        np.nan_to_num(net_matrix(cs, "BASE", cols=lcol)), np.nan_to_num(net_matrix(cs, "BASE", cols=lcol, shift=17))
    )
    assert planted_scan.null_shift["n_rep"] == 40 and planted_scan.null_shift["n_cells"] > 0


def test_holdout_and_train_only_guards(planted):
    late = planted.copy()
    late.loc[late.index[-10:], "ts"] = pd.date_range("2026-09-02", periods=10, freq="5min", tz="UTC")
    with pytest.raises(ForwardHoldoutError):
        build_day_grid(late, TZ, POINT)
    with pytest.raises(ForwardHoldoutError):
        scan_train(late, tz=TZ, point_size=POINT, win=WIN, costs=COSTS, run_nulls=False)
    with pytest.raises(DataDisciplineError):
        scan_train(planted, tz=TZ, point_size=POINT, win=WIN, costs=COSTS, train_end="2025-06-30",
                   run_nulls=False)


def test_ledger_counts_every_examined_cell(planted_scan):
    led = TrialLedger()
    led.add_table("SYN", planted_scan.table, planted_scan.duplicate_cells)
    d = led.to_dict()
    assert d["v1_cumulative_header_only"] == {
        "trials": V1_CUMULATIVE_TRIALS, "unique": V1_CUMULATIVE_UNIQUE,
    }
    assert V1_CUMULATIVE_TRIALS == 30310 and V1_CUMULATIVE_UNIQUE == 29798
    n = len(planted_scan.table)
    assert d["rawscan_unique"] == n
    assert d["rawscan_trials"] == n + planted_scan.duplicate_cells
    assert d["per_market"]["SYN"]["trials"] == d["rawscan_trials"]
    fam_sum = sum(v["unique"] for v in d["per_market_family"]["SYN"].values())
    assert fam_sum == n


def test_bh_qvalues_basic():
    q = bh_qvalues(np.array([0.001, 0.02, 0.5, np.nan]))
    assert q[0] == pytest.approx(0.003) and q[1] == pytest.approx(0.03) and q[2] == pytest.approx(0.5)
    assert np.isnan(q[3])
