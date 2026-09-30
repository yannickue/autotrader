# ruff: noqa: E501
"""Purged chronological folds: no overlap, embargo, monotone time, purge, DST safety, determinism,
forward-holdout refusal, and the SearchSplitPlan mapping onto the evaluator split interface."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from alpha.common.market_data import ForwardHoldoutError
from alpha.discovery.folds import (
    FoldError,
    SearchSplitPlan,
    berlin_dates_from_ts_ns,
    fold_report,
    fold_test_masks,
    make_folds,
    verify_folds,
)

BARS_PER_DAY = 12


def _dates(start="2025-02-03", n_days=400, bars=BARS_PER_DAY):
    """Weekday calendar dates, ``bars`` bars per trading day."""
    days = pd.bdate_range(start, periods=n_days).to_numpy().astype("datetime64[D]")
    return np.repeat(days, bars)


def test_no_overlap_monotone_and_embargo():
    d = _dates()
    folds = make_folds(d, 4, 5)
    assert len(folds) == 4
    prev_end = None
    for f in folds:
        assert not (f.train_mask & f.test_mask).any()
        assert d[f.train_mask].max() < d[f.test_mask].min()
        assert f.embargo_trading_days >= 5
        if prev_end is not None:
            assert d[f.test_mask].min() > prev_end  # tests chronological and disjoint
        prev_end = d[f.test_mask].max()
    # expanding window: each later train side contains the previous one
    for a, b in pairwise(folds):
        assert b.train_mask[a.train_mask].all()
        assert b.train_mask.sum() > a.train_mask.sum()
    assert folds[-1].test_mask[-1]  # last fold reaches the last development day


def test_each_test_fold_has_at_least_40_days_and_report_proves_checks():
    d = _dates()
    folds = make_folds(d, 4, 5)
    rep = fold_report(d, folds, 5)
    assert all(f["test_days"] >= 40 for f in rep["folds"])
    assert all(f["train_test_bar_overlap"] == 0 for f in rep["folds"])
    assert all(v is True for v in rep["checks"].values())
    assert rep["n_folds"] == 4 and rep["digest"]


def test_embargo_below_five_refused_and_too_short_series_refused():
    d = _dates()
    with pytest.raises(FoldError):
        make_folds(d, 4, 4)
    with pytest.raises(FoldError):
        make_folds(_dates(n_days=90), 4, 5)  # < 40 test days per fold
    with pytest.raises(FoldError):
        make_folds(d, 0, 5)


def test_purge_removes_bars_in_front_of_the_cut():
    d = _dates()
    plain = make_folds(d, 4, 5, 0)
    purged = make_folds(d, 4, 5, 7)
    for a, b in zip(plain, purged, strict=True):
        assert b.n_purged_bars == 7 and a.n_purged_bars == 0
        assert b.train_mask.sum() == a.train_mask.sum() - 7
        assert (b.train_mask <= a.train_mask).all()
        assert (b.test_mask == a.test_mask).all()
        assert b.embargo_trading_days >= 5


def test_deterministic():
    d = _dates()
    a, b = make_folds(d, 4, 5, 3), make_folds(d, 4, 5, 3)
    assert fold_report(d, a, 5)["digest"] == fold_report(d, b, 5)["digest"]
    assert all((x.train_mask == y.train_mask).all() and (x.test_mask == y.test_mask).all()
               for x, y in zip(a, b, strict=True))


def test_refuses_forward_holdout_dates():
    days = pd.bdate_range("2025-11-03", "2026-09-04").to_numpy().astype("datetime64[D]")
    d = np.repeat(days, BARS_PER_DAY)
    with pytest.raises(ForwardHoldoutError):
        make_folds(d, 3, 5)
    ok = d[d <= np.datetime64("2026-08-31")]
    folds = make_folds(ok, 3, 5)
    assert all(ok[f.test_mask].max() <= np.datetime64("2026-08-31") for f in folds)
    with pytest.raises(ForwardHoldoutError):
        SearchSplitPlan.from_folds(d, folds)


def test_dst_safe_berlin_dates():
    # 2026-03-28 23:30 UTC = 2026-03-29 00:30 CET (before the change), 2026-03-29 22:30 UTC = 00:30 CEST on the 30th
    ts = pd.to_datetime(["2026-03-28 22:30", "2026-03-28 23:30", "2026-03-29 21:30", "2026-03-29 22:30",
                         "2026-10-24 21:30", "2026-10-24 22:30", "2026-10-25 22:30", "2026-10-25 23:30"], utc=True)
    got = berlin_dates_from_ts_ns(ts.as_unit("ns").asi8).astype(str).tolist()
    assert got == ["2026-03-28", "2026-03-29", "2026-03-29", "2026-03-30",
                   "2026-10-24", "2026-10-25", "2026-10-25", "2026-10-26"]
    # a UTC-hourly series across the spring change yields non-decreasing Berlin dates and folds still work
    idx = pd.date_range("2025-02-01", "2026-08-30", freq="1h", tz="UTC")
    dates = berlin_dates_from_ts_ns(idx.as_unit("ns").asi8)
    assert (np.diff(dates.astype("int64")) >= 0).all()
    folds = make_folds(dates, 4, 5)
    verify_folds(dates, folds, 5)


def test_verify_folds_detects_tampering():
    d = _dates()
    folds = make_folds(d, 4, 5)
    bad = folds[0]
    tr = bad.train_mask.copy()
    tr[np.flatnonzero(bad.test_mask)[0]] = True
    from dataclasses import replace

    with pytest.raises(AssertionError):
        verify_folds(d, [replace(bad, train_mask=tr), *folds[1:]], 5)


def test_search_split_plan_maps_fold0_train_and_sealed_union_of_tests():
    d = _dates()
    folds = make_folds(d, 4, 5, 3)
    plan = SearchSplitPlan.from_folds(d, folds)
    tr = plan.mask(d, plan.train)
    te = plan.mask(d, plan.validation)
    assert (tr == folds[0].train_mask).all()
    union = np.zeros(len(d), bool)
    for m in fold_test_masks(folds):
        union |= m
    assert (te == union).all()
    assert not (tr & te).any()
    assert not plan.mask(d, plan.oos).any()  # forward holdout has no dev bar
    # search train never includes a fold-test day, so it is contained in every fold's train side
    assert all(f.train_mask[tr].all() for f in folds)
    assert plan.to_dict()["fold_digest"] == fold_report(d, folds, 5)["digest"]
    assert plan.embargo_days == 0
