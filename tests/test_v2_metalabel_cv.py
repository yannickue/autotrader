# ruff: noqa: E501
"""Purged rolling-origin CV by day blocks: no overlap, embargo, purge of overlapping horizons, chronology."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from alpha.metalabel import cv


def _rows(n_days=160, per=20, seed=0, max_hold_days=0):
    rng = np.random.default_rng(seed)
    day = np.repeat(np.arange(n_days), per)
    hold = rng.integers(0, max_hold_days + 1, len(day)) if max_hold_days else np.zeros(len(day), dtype=int)
    return day, np.minimum(day + hold, n_days - 1)


def test_no_overlap_embargo_and_chronology():
    day, exit_day = _rows()
    folds = cv.rolling_origin_folds(day, exit_day, 160, 5, embargo_days=3, min_train_days=50)
    assert len(folds) == 5
    prev_end = 0
    for f in folds:
        assert not set(day[f.train]) & set(day[f.test])
        assert day[f.train].max() <= f.test_start - 3 - 1
        assert f.test_start - day[f.train].max() - 1 >= 3
        assert day[f.test].min() >= f.test_start and day[f.test].max() < f.test_end
        assert f.test_start >= prev_end  # chronological, disjoint blocks
        prev_end = f.test_end
    assert folds[-1].test_end == 160
    for a, b in itertools.pairwise(folds):  # expanding window
        assert set(a.train) <= set(b.train)


def test_overlapping_horizons_are_purged():
    day, exit_day = _rows(max_hold_days=6, seed=1)
    folds = cv.rolling_origin_folds(day, exit_day, 160, 4, embargo_days=3, min_train_days=50)
    for f in folds:
        assert exit_day[f.train].max() < f.test_start - 3
        early = np.flatnonzero(day < f.test_start - 3)
        dropped = np.setdiff1d(early, f.train)
        assert len(dropped) > 0 and (exit_day[dropped] >= f.test_start - 3).all()


def test_embargo_below_three_days_is_refused():
    day, exit_day = _rows()
    with pytest.raises(cv.CVError):
        cv.rolling_origin_folds(day, exit_day, 160, 5, embargo_days=2)


def test_training_never_uses_later_data_than_test_block_start():
    day, exit_day = _rows(seed=2)
    for f in cv.rolling_origin_folds(day, exit_day, 160, 6, embargo_days=5, min_train_days=40):
        assert day[f.train].max() < day[f.test].min()
        assert f.train.max() < f.test.min()  # rows are time-ordered: every training row precedes every test row


def test_assert_purged_detects_tampering():
    day, exit_day = _rows()
    folds = cv.rolling_origin_folds(day, exit_day, 160, 4)
    f = folds[1]
    bad = cv.DayFold(f.index, np.r_[f.train, f.test[:5]], f.test, f.test_start, f.test_end, f.embargo)
    with pytest.raises(AssertionError):
        cv.assert_purged([bad], day, exit_day)


def test_inner_split_is_chronological_with_embargo():
    day, exit_day = _rows(max_hold_days=3, seed=3)
    f = cv.rolling_origin_folds(day, exit_day, 160, 4)[2]
    fit, cal = cv.inner_split(day, exit_day, f.train, 0.25)
    assert exit_day[fit].max() < day[cal].min() - 3 + 1
    assert day[fit].max() <= day[cal].min() - 3
    assert not set(fit) & set(cal)
    assert set(fit) | set(cal) <= set(f.train)
    assert day[cal].max() < f.test_start - 3 + 1
