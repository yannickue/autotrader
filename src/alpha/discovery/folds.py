# ruff: noqa: E501
"""Purged chronological folds for the V2 development period (research only).

V1's Validation partition was inspected repeatedly, so V2 replaces the single Train/Validation
split by an expanding-window, walk-forward style FOLD SET over the development period.

Design (all on distinct Berlin trading dates, never on bar counts)::

    days:   |<-------- initial train block -------->|<embargo>|<-- test 0 -->|<-- test 1 -->| ... |<-- test n-1 -->|
    fold k: train_k = every day before (test_k.start - embargo_days)   [expanding window]
            test_k  = a contiguous block of ``test_days`` trading days (the last fold absorbs
                      the remainder so the tests reach the last development day)

* ``embargo_days`` (>= 5, in TRADING days) separate the last train day from the first test day.
  Trades are flat at the forced-flat minute of the SAME Berlin day (no overnight), so a trade
  opened on a train day never overlaps a later test day; the embargo additionally covers feature
  / event look-back and confirmation lag (event confirmation is causal, but is stamped later).
* ``purge_bars`` additionally removes the last ``purge_bars`` train BARS in front of the embargo
  cut (belt and braces for any bar-level horizon the caller wants to be conservative about).
* NOTHING after ``DEV_END`` (2026-08-31) may ever be included: ``ForwardHoldoutError``.

Search vs. survival (how the fold set maps onto ``TemporalEvaluator``)
----------------------------------------------------------------------
``TemporalEvaluator`` needs a ``SplitPlan`` (Train mask = search fitness; "validation" mask = sealed
view that lean search evaluations never compute).  ``SearchSplitPlan.from_folds`` maps:

* ``train``      = the TRAIN side of FOLD 0 (the initial train block; it is contained in every
  later fold's train side, so a candidate selected on it has never seen ANY fold test day),
* ``validation`` = the UNION of all fold TEST days (sealed; read only in the final survival stage
  via ``fold_test_masks`` / ``evaluator.ensure_full``; never in search),
* ``oos``        = the forward holdout (2026-09-01 ..), never a dev bar.

Thresholds (``build_market_frame(..., plan=SearchSplitPlan)``) are therefore resolved from the
initial train block only and stay frozen across folds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from alpha.common.market_data import DEV_END, FORWARD_HOLDOUT_START, ForwardHoldoutError
from alpha.common.protocol import Partition, SplitPlan

MIN_EMBARGO_DAYS = 5
MIN_TEST_DAYS = 40
DEFAULT_INITIAL_TRAIN_FRAC = 0.4


class FoldError(ValueError):
    """The requested fold layout is impossible or violates an invariant."""


@dataclass(frozen=True)
class Fold:
    index: int
    train_mask: np.ndarray  # bool per bar
    test_mask: np.ndarray  # bool per bar
    train_start: str
    train_end: str  # last Berlin date that has a train bar
    test_start: str
    test_end: str
    embargo_trading_days: int  # distinct trading days strictly between train_end and test_start
    n_purged_bars: int  # bars removed by purge_bars in front of the embargo cut


def berlin_dates_from_ts_ns(ts_ns: np.ndarray) -> np.ndarray:
    """Berlin calendar date per UTC timestamp (datetime64[D]); DST-safe (tz database)."""
    import pandas as pd

    utc = pd.to_datetime(np.asarray(ts_ns, dtype=np.int64), utc=True)
    return (
        utc.tz_convert("Europe/Berlin").normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
    )


def assert_dev_only(dates: np.ndarray) -> None:
    """Refuse any date after ``DEV_END`` (September 2026+ is the reserved forward holdout)."""
    d = np.asarray(dates).astype("datetime64[D]")
    if len(d) and d.max() > np.datetime64(DEV_END):
        raise ForwardHoldoutError(
            f"dates reach {d.max()} but development ends {DEV_END}; {FORWARD_HOLDOUT_START}+ is reserved"
        )


def make_folds(
    dates: np.ndarray, n_folds: int, embargo_days: int = MIN_EMBARGO_DAYS, purge_bars: int = 0,
    *, initial_train_frac: float = DEFAULT_INITIAL_TRAIN_FRAC, min_test_days: int = MIN_TEST_DAYS,
) -> list[Fold]:
    """Expanding-window purged folds over per-bar Berlin ``dates`` (must be sorted, non-decreasing)."""
    d = np.asarray(dates).astype("datetime64[D]")
    if len(d) == 0:
        raise FoldError("no bars")
    assert_dev_only(d)
    if np.any(d[1:] < d[:-1]):
        raise FoldError("dates must be non-decreasing (chronological bars)")
    if isinstance(n_folds, bool) or not isinstance(n_folds, int) or n_folds < 1:
        raise FoldError("n_folds must be a positive integer")
    if isinstance(embargo_days, bool) or not isinstance(embargo_days, int) or embargo_days < MIN_EMBARGO_DAYS:
        raise FoldError(f"embargo_days must be an integer >= {MIN_EMBARGO_DAYS} trading days")
    if isinstance(purge_bars, bool) or not isinstance(purge_bars, int) or purge_bars < 0:
        raise FoldError("purge_bars must be a non-negative integer")
    if not 0.0 < initial_train_frac < 1.0:
        raise FoldError("initial_train_frac must be in (0, 1)")
    days, first_bar = np.unique(d, return_index=True)  # sorted distinct trading days
    n_days = len(days)
    init = round(initial_train_frac * n_days)
    test_days = (n_days - init - embargo_days) // n_folds
    if init < 1 or test_days < min_test_days:
        raise FoldError(
            f"{n_days} trading days cannot host {n_folds} folds with an initial block of {init}, "
            f"embargo {embargo_days} and >= {min_test_days} test days each (got {test_days})"
        )
    day_of_bar = np.searchsorted(days, d)  # trading-day ordinal per bar
    starts = [init + embargo_days + k * test_days for k in range(n_folds)]
    ends = [s + test_days for s in starts]
    ends[-1] = n_days  # last fold absorbs the remainder
    folds: list[Fold] = []
    for k in range(n_folds):
        cut_day = starts[k] - embargo_days  # train days: ordinal < cut_day
        cut_bar = int(first_bar[cut_day])  # first bar of day ``cut_day``
        train = day_of_bar < cut_day
        purged = 0
        if purge_bars:
            lo = max(0, cut_bar - purge_bars)
            purged = int(train[lo:cut_bar].sum())
            train[lo:cut_bar] = False
        test = (day_of_bar >= starts[k]) & (day_of_bar < ends[k])
        t_days = day_of_bar[train]
        if len(t_days) == 0:
            raise FoldError(f"fold {k} has an empty train side")
        last_train_day = int(t_days.max())
        folds.append(Fold(
            k, train, test, str(days[0]), str(days[last_train_day]), str(days[starts[k]]),
            str(days[ends[k] - 1]), starts[k] - last_train_day - 1, purged,
        ))
    verify_folds(d, folds, embargo_days)
    return folds


def verify_folds(dates: np.ndarray, folds: list[Fold], embargo_days: int = MIN_EMBARGO_DAYS) -> None:
    """Raise AssertionError unless every fold invariant holds (see ``fold_report``)."""
    d = np.asarray(dates).astype("datetime64[D]")
    assert_dev_only(d)
    days = np.unique(d)
    ordinal = np.searchsorted(days, d)
    prev_test_end = -1
    for f in folds:
        assert f.train_mask.shape == f.test_mask.shape == d.shape, "mask shape"
        assert not (f.train_mask & f.test_mask).any(), f"fold {f.index}: train/test bar overlap"
        assert f.train_mask.any() and f.test_mask.any(), f"fold {f.index}: empty side"
        tr, te = ordinal[f.train_mask], ordinal[f.test_mask]
        assert tr.max() < te.min(), f"fold {f.index}: train not strictly before test"
        assert te.min() - tr.max() - 1 >= embargo_days, f"fold {f.index}: embargo violated"
        assert te.min() > prev_test_end, f"fold {f.index}: tests not chronological/disjoint"
        prev_test_end = int(te.max())
        assert not (d[f.train_mask | f.test_mask] > np.datetime64(DEV_END)).any(), "forward holdout"


def fold_report(dates: np.ndarray, folds: list[Fold], embargo_days: int = MIN_EMBARGO_DAYS) -> dict[str, Any]:
    """JSON-ready fold summary with the overlap/embargo proofs (raises if any check fails)."""
    d = np.asarray(dates).astype("datetime64[D]")
    verify_folds(d, folds, embargo_days)
    days = np.unique(d)
    ordinal = np.searchsorted(days, d)
    out = []
    for f in folds:
        out.append({
            "fold": f.index, "train_bars": int(f.train_mask.sum()), "test_bars": int(f.test_mask.sum()),
            "train_days": len(np.unique(ordinal[f.train_mask])),
            "test_days": len(np.unique(ordinal[f.test_mask])),
            "train": [f.train_start, f.train_end], "test": [f.test_start, f.test_end],
            "embargo_trading_days": f.embargo_trading_days, "purged_bars": f.n_purged_bars,
            "train_test_bar_overlap": int((f.train_mask & f.test_mask).sum()),
        })
    return {
        "n_folds": len(folds), "n_trading_days": len(days), "first_date": str(days[0]),
        "last_date": str(days[-1]), "dev_end": DEV_END, "min_embargo_days": embargo_days,
        "checks": {"no_train_test_overlap": True, "embargo_respected": True,
                   "tests_chronological_disjoint": True, "nothing_after_dev_end": True},
        "folds": out, "digest": folds_digest(folds),
    }


def folds_digest(folds: list[Fold]) -> str:
    h = hashlib.sha256(b"folds-v1")
    for f in folds:
        h.update(np.packbits(f.train_mask).tobytes())
        h.update(np.packbits(f.test_mask).tobytes())
    return h.hexdigest()


def fold_test_masks(folds: list[Fold]) -> list[np.ndarray]:
    """Per-fold TEST bar masks: for the final survival stage only (never in search)."""
    return [f.test_mask for f in folds]


# ------------------------------------------------------------------------------ evaluator adapter
@dataclass(frozen=True)
class SearchSplitPlan(SplitPlan):
    """``SplitPlan`` whose train / "validation" masks are explicit fold-derived bar masks.

    ``mask(dates, train)`` = search train (fold 0 train side); ``mask(dates, validation)`` = union
    of all fold tests (sealed).  Any other partition falls back to the date rule.
    """

    fold_digest: str = ""
    _train: np.ndarray | None = field(default=None, repr=False, compare=False)
    _tests: np.ndarray | None = field(default=None, repr=False, compare=False)

    def mask(self, dates: np.ndarray, part: Partition) -> np.ndarray:
        if self._train is not None and len(dates) == len(self._train):
            if part == self.train:
                return self._train.copy()
            if part == self.validation and self._tests is not None:
                return self._tests.copy()
        return super().mask(dates, part)

    def to_dict(self) -> dict:
        return {**super().to_dict(), "fold_digest": self.fold_digest, "kind": "search_split_v1"}

    @classmethod
    def from_folds(cls, dates: np.ndarray, folds: list[Fold]) -> SearchSplitPlan:
        d = np.asarray(dates).astype("datetime64[D]")
        assert_dev_only(d)
        if not folds:
            raise FoldError("no folds")
        f0 = folds[0]
        tests = np.zeros(len(d), dtype=bool)
        for f in folds:
            tests |= f.test_mask
        if (f0.train_mask & tests).any():
            raise FoldError("search train overlaps a fold test")
        d_tr, d_te = d[f0.train_mask], d[tests]
        train = Partition("train", str(d_tr.min()), str(d_tr.max()))
        val = Partition("validation", str(d_te.min()), str(d_te.max()))
        oos = Partition("oos", FORWARD_HOLDOUT_START, "2099-12-31")
        return cls(train, val, oos, 0, folds_digest(folds), f0.train_mask.copy(), tests)


__all__ = (
    "DEFAULT_INITIAL_TRAIN_FRAC", "MIN_EMBARGO_DAYS", "MIN_TEST_DAYS", "Fold", "FoldError",
    "SearchSplitPlan", "assert_dev_only", "berlin_dates_from_ts_ns", "fold_report",
    "fold_test_masks", "folds_digest", "make_folds", "verify_folds",
)
