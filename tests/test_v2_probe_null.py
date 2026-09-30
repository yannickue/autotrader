# ruff: noqa: E501
"""Null-frame builders keep their invariants; the null/stat pipeline runs on a synthetic ProbeContext."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec
from alpha.discovery.folds import SearchSplitPlan, fold_report, make_folds
from alpha.discovery.temporal_evaluate import market_from_frame
from alpha.discovery.temporal_genome import EventPool
from research.runners import v2_probe
from research.runners import v2_probe_null as vn
from scripts.bench_temporal import synth_frame

TICK = 0.01


def _dev(n_days: int = 30, bars: int = 60, seed: int = 1, ar: float = 0.0) -> pd.DataFrame:
    """M5 bars: one 5h session per Berlin day (Mon-Fri, 09:00 Berlin), price ~10000, optional AR(1) returns."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2025-02-03", periods=n_days, tz="Europe/Berlin")
    parts = []
    for i, d in enumerate(days):
        b = bars - (i % 3)  # varying layouts: 60/59/58 bars
        parts.append(pd.date_range(d + pd.Timedelta(hours=9), periods=b, freq="5min"))
    ts = parts[0].append(parts[1:]).tz_convert("UTC")
    n = len(ts)
    e = rng.normal(0, 1e-3, n)
    r = np.zeros(n)
    for k in range(1, n):
        r[k] = ar * r[k - 1] + e[k]
    c = np.round(10000.0 * np.exp(np.cumsum(r)), 2)
    o = np.round(np.r_[c[0], c[:-1]] + rng.normal(0, 0.5, n), 2)
    hi = np.round(np.maximum(o, c) + np.abs(rng.normal(0, 1.0, n)), 2)
    lo = np.round(np.minimum(o, c) - np.abs(rng.normal(0, 1.0, n)), 2)
    return pd.DataFrame({"ts": ts, "open": o, "high": hi, "low": lo, "close": c,
                         "tick_volume": rng.integers(1, 100, n), "spread_pts": rng.integers(5, 30, n)})


def _lr(df: pd.DataFrame) -> np.ndarray:
    c = df["close"].to_numpy(float)
    return np.r_[0.0, np.log(c[1:] / c[:-1])]


def _day_ids(df: pd.DataFrame) -> np.ndarray:
    d = pd.DatetimeIndex(df["ts"]).tz_convert("Europe/Berlin").normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
    return np.unique(d, return_inverse=True)[1]


def _common_invariants(dev: pd.DataFrame, out: pd.DataFrame) -> None:
    for col in ("ts", "spread_pts", "tick_volume"):
        assert (out[col].to_numpy() == dev[col].to_numpy()).all(), col
    assert len(out) == len(dev)
    vals = out[["open", "high", "low", "close"]].to_numpy(float)
    assert np.isfinite(vals).all() and (vals > 0).all()
    assert (out["high"] >= out[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (out["low"] <= out[["open", "close"]].min(axis=1) + 1e-9).all()
    assert (out["high"] >= out["low"]).all()
    assert np.allclose(np.round(vals / TICK) * TICK, vals, atol=1e-6)  # on the tick grid


def test_null_a_sign_flip_preserves_abs_returns_shape_and_flips_whole_days():
    dev = _dev()
    out = vn.null_sign_flip(dev, 7, TICK)
    _common_invariants(dev, out)
    a, b = _lr(dev), _lr(out)
    assert np.allclose(np.abs(a), np.abs(b), atol=2e-5)  # |return| per bar (tick rounding only)
    assert np.allclose((dev["high"] - dev["low"]).to_numpy(), (out["high"] - out["low"]).to_numpy(), atol=4 * TICK + 1e-6)
    day = _day_ids(dev)
    for d in np.unique(day):
        m = (day == d) & (np.abs(a) > 1e-4)
        s = np.sign(b[m] * a[m])
        assert len(set(s)) == 1  # one sign per day
    signs = {int(np.sign(b[(day == d) & (np.abs(a) > 1e-4)][0] * a[(day == d) & (np.abs(a) > 1e-4)][0])) for d in np.unique(day)}
    assert signs == {-1, 1}
    assert vn.null_sign_flip(dev, 7, TICK).equals(out)  # deterministic
    assert not vn.null_sign_flip(dev, 8, TICK).equals(out)


def test_null_b_block_shuffle_keeps_each_day_path_and_timestamps():
    dev = _dev(n_days=36)
    out = vn.null_block_shuffle(dev, 11, TICK)
    _common_invariants(dev, out)
    a, b = _lr(dev), _lr(out)
    day = _day_ids(dev)
    orig = {d: a[day == d] for d in np.unique(day)}
    new = {d: b[day == d] for d in np.unique(day)}
    # day 0 fixed
    assert np.allclose(orig[0], new[0], atol=2e-5)
    used = []
    moved = 0
    for d, vec in new.items():
        if d == 0:
            continue
        hits = [k for k, o in orig.items() if k != 0 and len(o) == len(vec) and np.allclose(o[1:], vec[1:], atol=3e-5)]
        assert hits, f"day {d}: path is not any original day's path"
        moved += int(d not in hits)
        used.append(hits)
    assert moved > 0  # something actually moved
    # multiset of paths preserved (same-layout groups are permutations)
    for length in (60, 59, 58):
        so = sorted(round(float(np.sum(np.abs(v))), 3) for v in orig.values() if len(v) == length)
        sn = sorted(round(float(np.sum(np.abs(v))), 3) for v in new.values() if len(v) == length)
        assert np.allclose(so, sn, atol=1e-3)
    assert vn.null_block_shuffle(dev, 11, TICK).equals(out)


def test_null_c_zero_drift_within_day_shuffle_kills_autocorrelation():
    dev = _dev(n_days=40, bars=120, ar=0.5)
    out = vn.null_zero_drift(dev, 3, TICK)
    _common_invariants(dev, out)
    a, b = _lr(dev), _lr(out)
    day = _day_ids(dev)

    def ac1(x):
        num = den = 0.0
        for d in np.unique(day):
            v = x[day == d][1:]
            v = v - v.mean()
            num += float((v[1:] * v[:-1]).sum())
            den += float((v * v).sum())
        return num / den

    assert ac1(a) > 0.3 and abs(ac1(b)) < 0.15
    assert abs(b[1:].mean()) < 1e-5  # zero drift (single segment)
    # within-day multiset of the continuation returns preserved (after the drift shift)
    shift = a[1:].mean()
    for d in np.unique(day)[:5]:
        va, vb = np.sort(a[day == d][1:] - shift), np.sort(b[day == d][1:])
        assert np.allclose(va, vb, atol=3e-5)
    assert (out["ts"] == dev["ts"]).all()


def test_null_c_segments_zero_drift_each():
    dev = _dev(n_days=40, bars=60)
    seg = (np.arange(len(dev)) >= len(dev) // 2).astype(np.int64)
    b = _lr(vn.null_zero_drift(dev, 5, TICK, seg))
    assert abs(b[1:len(dev) // 2].mean()) < 1e-5 and abs(b[len(dev) // 2:].mean()) < 1e-5


def test_make_null_rejects_unknown_type():
    with pytest.raises(ValueError):
        vn.make_null("Z", _dev(5), 1, TICK)


# --------------------------------------------------------------------------- pipeline on a synthetic context
N = 60_000
CFG_PATH = Path(v2_probe.DEFAULT_CONFIG)


def _cfg() -> dict:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    cfg["search"].update(deap_pop=20, deap_gens=2)
    cfg["min_train_trades"] = 15
    cfg["folds"].update(n_folds=2, purge_bars=6)
    return cfg


@pytest.fixture(scope="module")
def synth_ctx():
    cfg = _cfg()
    frame = synth_frame(N, seed=3, density=12.0, run_len=(60, 200))
    dates = (np.datetime64("2025-02-03", "D") + (np.arange(N) // 288)).astype("datetime64[D]")
    f = cfg["folds"]
    folds = make_folds(dates, f["n_folds"], f["embargo_days"], f["purge_bars"],
                       initial_train_frac=f["initial_train_frac"], min_test_days=f["min_test_days"])
    plan = SearchSplitPlan.from_folds(dates, folds)
    ctx = v2_probe.ProbeContext(
        "SYNTH", lambda: frame, market_from_frame(frame), dates, plan, fold_report(dates, folds, f["embargo_days"]),
        EventPool.full(), SizingSpec(min_risk_pts=0.5, max_risk_pts=60.0), SimRules(),
        {n: COST_SCENARIOS[n] for n in ("BASE", "COMBINED_ADVERSE")}, "synthetic", np.asarray(frame.atr), {})
    return cfg, ctx


def test_collect_stats_matches_search_and_run_stats(synth_ctx, tmp_path):
    cfg, ctx = synth_ctx
    ev = v2_probe.make_evaluator(ctx, cfg, None, None)
    v2_probe.run_search(ev, ctx, cfg, 120, 5, {})
    rows, daily, days = v2_probe.collect_stats(ev, ctx)
    assert len(rows) == len(ev.records)
    passers = [r for r in rows if r["passer"]]
    assert passers
    by = {h: rec for h, (_, rec, _) in ev.records.items()}
    for r in passers:  # the compact stats re-derive the search's own Train number
        rec = by[r["hash"]].train.adverse.screen
        assert r["n_trades"] == rec.n_trades
        assert r["e_adv"] == pytest.approx(rec.expectancy_r, abs=1e-9)
        assert r["tpd"] == pytest.approx(rec.trades_per_day, abs=1e-9)
        assert r["hash"] in daily and daily[r["hash"]].shape == days.shape
    assert all(r["e_shock"] <= r["e_base"] + 1e-9 for r in passers if r["e_shock"] is not None and r["e_base"] is not None)
    st = vn.run_stats(rows, daily)
    assert st["n_passers"] == len(passers) and st["best_e"] == pytest.approx(max(r["e_adv"] for r in passers))
    assert len(st["top5_pareto"]) <= 5 and st["n_e_pos"] == sum(r["e_adv"] > 0 for r in passers)
    # write/load round trip
    v2_probe.write_stats(tmp_path, ev, rows, daily, days)
    df, dl = v2_probe.load_stats(tmp_path)
    assert len(df) == len(rows) and set(dl) == set(daily)
    assert (tmp_path / "genomes.json.gz").exists()


def test_run_probe_writes_stats_files_and_is_deterministic(synth_ctx, tmp_path):
    cfg, ctx = synth_ctx
    s1 = v2_probe.run_probe(ctx, cfg, 100, 5, tmp_path / "a", None)
    for f in ("stats.csv.gz", "genomes.json.gz", "daily_r.npz"):
        assert (tmp_path / "a" / f).exists()
    assert s1["stats_file"]["rows"] == len(pd.read_csv(tmp_path / "a" / "stats.csv.gz"))
    v2_probe.run_probe(ctx, cfg, 100, 5, tmp_path / "b", None)
    a = pd.read_csv(tmp_path / "a" / "stats.csv.gz")
    b = pd.read_csv(tmp_path / "b" / "stats.csv.gz")
    assert a.equals(b)


def test_empirical_p_and_report_from_files(tmp_path):
    assert vn._emp_p(1.0, [0.1, 0.2, 2.0]) == pytest.approx(2 / 4)
    assert vn._emp_p(5.0, [0.1, 0.2, 2.0]) == pytest.approx(1 / 4)
    assert vn._emp_p(None, [1.0]) is None
    run = {"market": "M", "type": "A", "null_seed": 1, "stats": {"n_passers": 10, "best_e": 0.1, "best_t": 1.5,
                                                                 "n_e_pos": 3, "n_t_gt2": 0, "p95_e": 0.05}}
    (tmp_path / "null_M_A_1.json").write_text(json.dumps(run), encoding="utf-8")
    rep = vn.build_report(tmp_path)
    assert rep["markets"]["M"]["null"]["A"]["stats"]["best_t"]["null"]["max"] == 1.5
    assert "V2 probe null calibration" in vn.render_md(rep)
