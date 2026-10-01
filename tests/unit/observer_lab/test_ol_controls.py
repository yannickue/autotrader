from __future__ import annotations

import numpy as np
import pytest

from coverage_analysis.observer_lab import controls as CT

BARS_PER_DAY = 100


@pytest.fixture(scope="module")
def market(make_bars):
    build_bars = make_bars
    rng = np.random.default_rng(0)
    days = 40
    n = days * BARS_PER_DAY
    minute = np.tile(540 + 5 * np.arange(BARS_PER_DAY), days)
    day = np.repeat(np.arange(days), BARS_PER_DAY)
    atr = np.exp(rng.normal(0, 0.5, n))
    spread = rng.uniform(0.01, 0.05, n)
    close = 100 + np.cumsum(rng.normal(0, 0.1, n))
    return build_bars(close, close + 0.2, close - 0.2, close, spread=spread, atr=atr, local_minute=minute, local_day=day)


def pick_events(bars, seed=1, k=15, high_atr=False):
    rng = np.random.default_rng(seed)
    pct = CT.bar_covariates(bars)["atr_pct"]
    ok = np.where((np.arange(len(bars)) > 60) & (np.arange(len(bars)) < len(bars) - 60) & ((pct > 0.7) if high_atr else True))[0]
    ev = np.sort(rng.choice(ok, size=k, replace=False))
    # keep events apart so their exclusion zones are disjoint enough to leave candidates
    return ev[np.r_[True, np.diff(ev) > 48]]


def test_covariates_session_buckets_and_percentiles(market):
    cov = CT.bar_covariates(market)
    assert set(np.unique(cov["session"])) <= {"PRE", "OPEN_HOUR", "MID", "CLOSE_HOUR", "POST"}
    assert cov["session"][0] == "OPEN_HOUR" and cov["session"][BARS_PER_DAY - 1] in {"POST", "CLOSE_HOUR"}
    assert np.nanmin(cov["atr_pct"]) > 0 and np.nanmax(cov["atr_pct"]) <= 1.0
    assert len(CT.bar_covariates(market.prefix(0))["atr_pct"]) == 0


def test_same_seed_same_controls_different_seed_differs(market):
    ev = pick_events(market)
    a = CT.match_controls(market, ev, seed=5)
    b = CT.match_controls(market, ev, seed=5)
    c = CT.match_controls(market, ev, seed=6)
    assert np.array_equal(a.control_idx, b.control_idx) and np.array_equal(a.event_pos, b.event_pos)
    assert not np.array_equal(a.control_idx, c.control_idx)


def test_controls_match_variables_and_exclude_event_neighbourhoods(market):
    ev = pick_events(market)
    spec = CT.MatchSpec(minute_tol=30, atr_pct_band=0.10, spread_pct_band=0.15, n_controls=2, exclusion_bars=48)
    cs = CT.match_controls(market, ev, spec=spec, seed=2)
    cov = CT.bar_covariates(market)
    assert len(cs.control_idx) > 0
    for pos, j in zip(cs.event_pos, cs.control_idx, strict=True):
        i = ev[pos]
        assert cov["session"][j] == cov["session"][i]
        assert abs(cov["minute"][j] - cov["minute"][i]) <= 30
        assert abs(cov["atr_pct"][j] - cov["atr_pct"][i]) <= 0.10 + 1e-12
        assert abs(cov["spread_pct"][j] - cov["spread_pct"][i]) <= 0.15 + 1e-12
        assert j < len(market) - 1
    # no control inside ANY event's exclusion zone (=> no overlap with an event's own outcome window), none equals an event bar
    far = np.abs(cs.control_idx[:, None] - ev[None, :]).min(axis=1)
    assert far.min() > spec.exclusion_bars
    assert len(set(cs.control_idx.tolist())) == len(cs.control_idx)  # no control reused
    assert cs.report.n_controls == len(cs.control_idx) and cs.control_idx.size <= 2 * len(ev)


def test_matching_report_numbers_and_smd_vs_naive_random(market):
    ev = pick_events(market, high_atr=True, k=25)
    cs = CT.match_controls(market, ev, spec=CT.MatchSpec(exclusion_bars=12), seed=3)
    r = cs.report
    assert r.n_events == len(ev) and r.n_matched == len(set(cs.event_pos.tolist()))
    assert r.match_rate == pytest.approx(r.n_matched / r.n_events)
    assert sorted(r.unmatched_event_pos + tuple(set(cs.event_pos.tolist()))) == list(range(len(ev)))
    assert abs(r.smd["atr_pct"]) < 0.15 and abs(r.smd["local_minute"]) < 0.25
    # a NAIVE random-bar control group is far worse on volatility (events sit in the high-ATR tail)
    cov = CT.bar_covariates(market)
    naive = np.random.default_rng(0).choice(np.arange(60, len(market) - 60), size=len(ev))
    naive_smd = CT.standardised_mean_difference(cov["atr_pct"][ev], cov["atr_pct"][naive])
    assert abs(naive_smd) > 1.0 > abs(r.smd["atr_pct"]) * 4
    assert 0.0 <= r.session_share_diff_max <= 1.0


def test_unmatched_events_are_reported_not_silently_dropped(market):
    ev = pick_events(market, k=10)
    none = CT.match_controls(market, ev, spec=CT.MatchSpec(exclusion_bars=10_000), seed=0)
    assert none.control_idx.size == 0 and none.report.match_rate == 0.0
    assert none.report.unmatched_event_pos == tuple(range(len(ev)))
    assert np.isnan(none.report.smd["atr_pct"])
    tight = CT.match_controls(market, ev, spec=CT.MatchSpec(atr_pct_band=0.0, minute_tol=0), seed=0)
    assert tight.report.n_matched < tight.report.n_events  # exact atr rank only matches the event itself, which is excluded
    assert len(tight.report.unmatched_event_pos) == tight.report.n_events - tight.report.n_matched


def test_events_outside_range_rejected(market):
    with pytest.raises(ValueError):
        CT.match_controls(market, np.array([len(market)]), seed=0)


def test_placebo_hook_with_toy_generator(market):
    ev = pick_events(market, k=20)

    def toy(bars, i, rng):  # an "artificial level" placebo: some far-away bars, one deliberately inside the event neighbourhood
        far = int((i + 200 + int(rng.integers(0, 50))) % (len(bars) - 2))
        return [CT.PlaceboDraw(idx=i + 3, meta={"bad": 1}), CT.PlaceboDraw(idx=far, meta={"artificial_level": float(bars.c[far])})]

    a = CT.placebo_controls(market, ev, toy, seed=9, exclusion_bars=48)
    b = CT.placebo_controls(market, ev, toy, seed=9, exclusion_bars=48)
    c = CT.placebo_controls(market, ev, toy, seed=10, exclusion_bars=48)
    assert np.array_equal(a.control_idx, b.control_idx) and not np.array_equal(a.control_idx, c.control_idx)
    assert a.report.n_rejected_neighbourhood >= len(ev)  # every i+3 draw refused
    assert all("artificial_level" in m for m in a.extra) and len(a.extra) == len(a.control_idx)
    assert not (np.abs(a.control_idx[:, None] - ev[None, :]) <= 48).any()
    # order independence: reversing the event order gives the same control per event
    rev = CT.placebo_controls(market, ev[::-1], toy, seed=9, exclusion_bars=48)
    mine = {int(ev[p]): j for p, j in zip(a.event_pos.tolist(), a.control_idx.tolist(), strict=True)}
    theirs = {int(ev[::-1][p]): j for p, j in zip(rev.event_pos.tolist(), rev.control_idx.tolist(), strict=True)}
    assert mine == theirs
