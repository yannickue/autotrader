# ruff: noqa: E501
"""Validation of the capital-growth simulator: it scales a stream, it never creates edge."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.growth import (
    DrawdownThrottle,
    FixedFraction,
    FractionalKelly,
    RampAfterWins,
    RStream,
    SimConfig,
    growth_rate,
    kelly_fraction,
    lot_sizing_for,
    required_fraction,
    simulate,
    summarize,
    synthetic_stream,
)

P = 3000


def _coin(n: int = 2000) -> RStream:
    r = np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
    return RStream(r=r, day=np.arange(n), total_days=n, label="coin")


def _run(stream, sched, **kw):
    cfg = SimConfig(n_paths=kw.pop("n_paths", P), **kw)
    return simulate(stream, sched, cfg)


def test_fixed_coin_median_matches_theory():
    res = _run(_coin(), FixedFraction(0.01), scheme="iid_trade", horizons=(250,), seed=3)
    theory = 500.0 * np.exp(250 * 0.5 * (np.log(1.01) + np.log(0.99)))
    med = np.median(res.equity[-1])
    assert abs(med / theory - 1.0) < 0.02
    assert abs(np.mean(np.log(res.equity[-1] / 500.0)) - np.log(theory / 500.0)) < 0.02


def test_zero_edge_decays_and_ruin_rises_with_f():
    s = synthetic_stream(mean_r=0.0)
    ruin, med = [], []
    for f in (0.02, 0.05, 0.10):
        h = summarize(_run(s, FixedFraction(f), horizons=(250,)))["horizons"]["250"]
        ruin.append(h["p_ruin"])
        med.append(h["ending_capital"]["p50"])
    assert all(m < 500.0 for m in med)
    assert ruin[0] < ruin[1] < ruin[2]
    assert med[0] > med[1] > med[2]


def test_negative_edge_never_grows():
    s = synthetic_stream(mean_r=-0.10)
    h = summarize(_run(s, FixedFraction(0.02), horizons=(250,)))["horizons"]["250"]
    assert h["ending_capital"]["p50"] < 500.0
    assert kelly_fraction(s.r) == 0.0
    assert required_fraction(s.r, trades_per_day=1.2, days=250, start=500, target=5000)["f_needed"] is None


def test_kelly_matches_closed_form_and_ruin_rises_past_it():
    n = 1000
    r = np.where(np.arange(n) < 400, 1.875, -1.0)  # p=0.4, b=1.875 -> f* = (pb - q)/b = 0.08
    assert kelly_fraction(r) == pytest.approx(0.08, abs=1e-3)
    fs = kelly_fraction(r)
    assert growth_rate(r, fs) > growth_rate(r, 0.6 * fs)
    assert growth_rate(r, fs) > growth_rate(r, 1.6 * fs)
    s = synthetic_stream(mean_r=0.15)
    at = summarize(_run(s, FixedFraction(fs), horizons=(250,)))["horizons"]["250"]
    past = summarize(_run(s, FixedFraction(3 * fs), horizons=(250,)))["horizons"]["250"]
    assert past["p_ruin"] > at["p_ruin"]
    assert past["ending_capital"]["p50"] < at["ending_capital"]["p50"]


def test_growth_rate_theory_vs_mc_positive_edge():
    s = synthetic_stream(mean_r=0.15)
    f = 0.01
    res = _run(s, FixedFraction(f), scheme="iid_trade", horizons=(250,), seed=5)
    n_tr = res.n_exec.mean()
    got = np.mean(np.log(res.equity[-1] / 500.0)) / n_tr
    assert got == pytest.approx(growth_rate(s.r, f), rel=0.08)


def test_determinism_and_seed_sensitivity():
    s = synthetic_stream()
    a = _run(s, FixedFraction(0.02), seed=11, n_paths=500)
    b = _run(s, FixedFraction(0.02), seed=11, n_paths=500)
    c = _run(s, FixedFraction(0.02), seed=12, n_paths=500)
    assert np.array_equal(a.equity, b.equity)
    assert not np.array_equal(a.equity, c.equity)


def _intraday_clustered(n_days: int, seed: int) -> RStream:
    """4 trades/day; 25% 'bad' days (win prob 0.05), else 0.517: overall win rate 0.4."""
    rng = np.random.default_rng(seed)
    bad = rng.random(n_days) < 0.25
    pw = np.where(bad, 0.05, (0.4 - 0.25 * 0.05) / 0.75)
    wins = rng.random((n_days, 4)) < pw[:, None]
    r = np.where(wins, 1.875, -1.0).ravel()
    return RStream(r=r, day=np.repeat(np.arange(n_days), 4), total_days=n_days)


def _max_run(x: np.ndarray) -> int:
    best = cur = 0
    for v in x:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def test_day_block_preserves_streak_structure_better_than_iid():
    truth_src = _intraday_clustered(40 * 250, seed=1)  # ground truth: 40 chunks of 250 days
    chunk = 250 * 4
    truth = np.mean([_max_run(truth_src.r[i : i + chunk]) for i in range(0, 40 * chunk, chunk)])
    sample = _intraday_clustered(1500, seed=2)
    tiny = FixedFraction(0.0005)
    mean_streak = {}
    for scheme in ("day_block", "iid_trade"):
        res = _run(sample, tiny, scheme=scheme, horizons=(250,), n_paths=1500, seed=4)
        mean_streak[scheme] = res.streak[-1].mean()
    assert abs(mean_streak["day_block"] - truth) < abs(mean_streak["iid_trade"] - truth)


def test_stationary_block_and_daily_cap_run():
    s = synthetic_stream(trades_per_day=2.5)
    res = _run(s, FixedFraction(0.01), scheme="stationary_block", horizons=(60,),
               max_trades_per_day=1, n_paths=300)
    assert res.n_exec.max() <= 60
    res2 = _run(s, FixedFraction(0.01), scheme="iid_trade", horizons=(60,),
                max_trades_per_day=1, n_paths=300)
    assert res2.n_exec.max() <= 60


def test_forced_min_lot_inflates_realised_risk_on_ger40_like_stream():
    lots = lot_sizing_for("GER40", leverage_cap=30.0)
    s = synthetic_stream(mean_r=0.15, stop_pts=25.0)
    out = {}
    for pol in ("skip", "forced_min_lot"):
        cfg = SimConfig(n_paths=800, horizons=(60,), policy=pol, lots=lots, leverage_cap=30.0)
        out[pol] = simulate(s, FixedFraction(0.01), cfg)
    skip, forced = out["skip"], out["forced_min_lot"]
    assert skip.realised["mean_realised_over_target"] <= 1.0 + 1e-9  # rounded DOWN never exceeds
    assert forced.realised["mean_realised_over_target"] > 1.2  # 1% of 500 = 5 EUR < min lot risk
    assert forced.realised["max_realised_risk_frac"] > 0.011
    assert skip.n_skip_min.mean() > 0 and forced.n_skip_min.sum() == 0


def test_leverage_cap_10x_blocks_min_lot_at_500_eur_ger40():
    lots = lot_sizing_for("GER40", leverage_cap=10.0)  # 0.25 lot ~ 12.7x at 500 EUR
    s = synthetic_stream(mean_r=0.15, stop_pts=25.0)
    cfg = SimConfig(n_paths=200, horizons=(60,), policy="forced_min_lot", lots=lots)
    res = simulate(s, FixedFraction(0.05), cfg)
    assert res.n_skip_lev.sum() > 0


def test_edge_uncertainty_widens_distribution():
    s = synthetic_stream(n_trades=500, mean_r=0.15, seed=9)
    common = {"horizons": (250,), "n_paths": 3000, "seed": 2}
    a = _run(s, FixedFraction(0.03), edge_uncertainty=False, **common)
    b = _run(s, FixedFraction(0.03), edge_uncertainty=True, **common)
    spread = lambda r: np.log(np.percentile(r.equity[-1], 95) / np.percentile(r.equity[-1], 5))  # noqa: E731
    assert spread(b) > spread(a) * 1.05


def test_no_negative_equity_and_percentiles_monotone():
    s = synthetic_stream(mean_r=0.05)
    for sched in (FixedFraction(0.9), DrawdownThrottle(0.5), RampAfterWins(0.2, 0.2, 0.9)):
        res = _run(s, sched, n_paths=500, horizons=(60, 250))
        assert res.equity.min() >= 0.0
        summ = summarize(res)["horizons"]
        for h in summ.values():
            v = list(h["ending_capital"].values())
            assert v == sorted(v)
        assert (res.equity[1] <= res.equity[0] * 1e9).all()


def test_dynamic_schedules_behave():
    s = synthetic_stream(mean_r=0.15)
    fixed = _run(s, FixedFraction(0.05), horizons=(250,))
    thr = _run(s, DrawdownThrottle(0.05), horizons=(250,))
    assert np.median(thr.maxdd[-1]) <= np.median(fixed.maxdd[-1])
    kel = _run(s, FractionalKelly(0.5, se=0.02), horizons=(250,))
    assert np.median(kel.equity[-1]) > 500.0
    ramp = summarize(_run(s, RampAfterWins(), horizons=(60,)))
    assert ramp["horizons"]["60"]["ending_capital"]["p50"] > 0


def test_required_fraction_and_survival_reading():
    s = synthetic_stream(mean_r=0.15)
    rq = required_fraction(s.r, trades_per_day=1.2, days=250, start=500.0, target=1000.0)
    assert rq["f_needed"] is not None and 0 < rq["f_needed"] < rq["f_star"]
    unreachable = required_fraction(s.r, trades_per_day=1.2, days=250, start=500.0, target=5e7)
    assert unreachable["f_needed"] is None
