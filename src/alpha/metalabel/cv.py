# ruff: noqa: E501
"""Purged rolling-origin cross-validation by DAY blocks (research only).

Rows are trades with a trading-day ordinal ``day`` (entry day, ordinal over ALL trading days of the
Train side, not only days with trades) and ``exit_day`` (day of the last bar of the trade horizon).
Fold ``k`` tests one contiguous block of days ``[s_k, e_k)``; its training rows are the rows whose
entry day AND exit day are ``< s_k - embargo`` (expanding window, strictly earlier data only).  That
purges every training trade whose horizon overlaps the embargo zone or the test block, and enforces
an embargo of ``>= 3`` trading days.  There is no shuffling and nothing after the test block is used.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MIN_EMBARGO_DAYS = 3


class CVError(ValueError):
    pass


@dataclass(frozen=True)
class DayFold:
    index: int
    train: np.ndarray  # row indices
    test: np.ndarray
    test_start: int  # first test day ordinal
    test_end: int  # exclusive
    embargo: int


def rolling_origin_folds(day: np.ndarray, exit_day: np.ndarray, n_days: int, n_folds: int,
                         embargo_days: int = MIN_EMBARGO_DAYS, min_train_days: int = 40) -> list[DayFold]:
    """Chronological expanding-window folds by day blocks with purge + embargo (see module doc)."""
    if embargo_days < MIN_EMBARGO_DAYS:
        raise CVError(f"embargo_days must be >= {MIN_EMBARGO_DAYS}")
    day = np.asarray(day, dtype=np.int64)
    exit_day = np.asarray(exit_day, dtype=np.int64)
    if len(day) != len(exit_day):
        raise CVError("day / exit_day length mismatch")
    if len(day) and np.any(exit_day < day):
        raise CVError("exit_day before entry day")
    first_test = min_train_days + embargo_days
    span = n_days - first_test
    if n_folds < 1 or span < n_folds * 3:
        raise CVError(f"{n_days} days cannot host {n_folds} folds (min train {min_train_days}, embargo {embargo_days})")
    edges = np.linspace(first_test, n_days, n_folds + 1).round().astype(int)
    folds: list[DayFold] = []
    for k in range(n_folds):
        s, e = int(edges[k]), int(edges[k + 1])
        tr = np.flatnonzero((exit_day < s - embargo_days) & (day < s - embargo_days))
        te = np.flatnonzero((day >= s) & (day < e))
        if len(tr) == 0 or len(te) == 0:
            continue
        folds.append(DayFold(k, tr, te, s, e, embargo_days))
    if not folds:
        raise CVError("no usable fold")
    assert_purged(folds, day, exit_day)
    return folds


def assert_purged(folds: list[DayFold], day: np.ndarray, exit_day: np.ndarray) -> None:
    """Raise AssertionError unless train/test never share a day or overlapping horizon, the embargo holds,
    and every training row precedes its test block."""
    day = np.asarray(day)
    exit_day = np.asarray(exit_day)
    prev_end = -1
    for f in folds:
        assert not np.intersect1d(f.train, f.test).size, "row in both train and test"
        assert not np.intersect1d(day[f.train], day[f.test]).size, "train/test share a day"
        assert exit_day[f.train].max() < f.test_start - f.embargo + 0, "training horizon reaches the embargo/test zone"
        assert day[f.train].max() <= f.test_start - f.embargo - 1, "embargo violated"
        assert f.test_start - day[f.train].max() - 1 >= f.embargo, "embargo shorter than requested"
        assert day[f.test].min() >= f.test_start and day[f.test].max() < f.test_end, "test outside block"
        # no test-row horizon overlap with training rows: training rows all end before the test block
        assert exit_day[f.train].max() < day[f.test].min(), "train horizon overlaps test"
        assert f.test_start >= prev_end, "test blocks not chronological/disjoint"
        prev_end = f.test_end


def inner_split(day: np.ndarray, exit_day: np.ndarray, train: np.ndarray, frac: float = 0.25,
                embargo_days: int = MIN_EMBARGO_DAYS) -> tuple[np.ndarray, np.ndarray]:
    """Chronological split of a fold's training rows into (fit, calibration) with the same purge/embargo:
    ``calibration`` = rows in the last ``frac`` of the training days, ``fit`` = rows entirely before
    ``cut - embargo``.  Used for Platt scaling / early stopping (never touches the outer test block)."""
    d = day[train]
    days = np.unique(d)
    if len(days) < 8:
        raise CVError("too few training days for an inner split")
    cut = int(days[int(np.floor(len(days) * (1.0 - frac)))])
    fit = train[(exit_day[train] < cut - embargo_days) & (d < cut - embargo_days)]
    cal = train[d >= cut]
    if len(fit) == 0 or len(cal) == 0:
        raise CVError("empty inner split")
    return fit, cal


__all__ = ("MIN_EMBARGO_DAYS", "CVError", "DayFold", "assert_purged", "inner_split", "rolling_origin_folds")
