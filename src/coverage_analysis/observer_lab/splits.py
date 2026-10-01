# ruff: noqa: E501
"""Temporal splits of the observer lab (OFFLINE). Chronological, grouped by Berlin trading date, NEVER shuffled.

Boundaries are NOT invented; they are the repo's existing freeze constants:

* ``DEFAULT_FIT_END`` = 2026-06-30 (``demo.opportunity.production_spec``): the core thresholds were fitted on Berlin dates <= this day.
* ``DEV_END`` = 2026-08-31 and ``FORWARD_HOLDOUT_START`` = 2026-09-01 (``alpha.common.market_data``): nothing after DEV_END may ever be
  used for development; the forward period is observed only (``FORWARD_NEVER_USED_FOR_FITTING``).

Partitions: TRAIN | VALIDATION (the last ``validation_frac`` of the distinct trading days up to the core fit end - a day-count fraction, not
a new date constant) | OOS (core-fit end < date <= DEV_END, "frozen out-of-sample" relative to the core and to the observer's own fits:
touched once for a final verdict) | FORWARD (>= 2026-09-01). Around every boundary the label horizon is purged (an event whose horizon
reaches into another partition is PURGED) and the first ``embargo_s`` (= the label horizon) after a boundary are EMBARGOed.

Additional robustness check: ROLLING / EXPANDING walk-forward windows (``walk_forward_windows`` + ``window_masks``), with the same
purge and an embargo gap in trading days. They never reach past DEV_END.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from alpha.common.market_data import DEV_END, FORWARD_HOLDOUT_START, ForwardHoldoutError

BERLIN = "Europe/Berlin"
FORWARD_NEVER_USED_FOR_FITTING = True
TRAIN, VALIDATION, OOS, FORWARD = "TRAIN", "VALIDATION", "OOS", "FORWARD"
PURGED, EMBARGO, UNASSIGNED = "PURGED", "EMBARGO", "UNASSIGNED"
_ALLOWED_USE = {
    "fit": {TRAIN},
    "select": {TRAIN, VALIDATION},
    "final_oos": {OOS},
    "observe_forward": {FORWARD},
    # enrichment / statistics entry points (``enrichment``): rows of ONE partition per purpose
    "validate": {VALIDATION},
    "oos_test": {OOS},
    "forward_monitor": {FORWARD},
}
FORWARD_PURPOSES = frozenset({"observe_forward", "forward_monitor"})
PURPOSES = tuple(_ALLOWED_USE)


def core_fit_end() -> str:
    from demo.opportunity.production_spec import DEFAULT_FIT_END  # lazy: pulls the family registry

    return DEFAULT_FIT_END


def berlin_dates(ts_ns: np.ndarray) -> np.ndarray:
    """ISO Berlin dates (``YYYY-MM-DD``) of UTC-ns timestamps."""
    idx = pd.to_datetime(np.asarray(ts_ns, dtype="int64"), utc=True).tz_convert(BERLIN)
    return np.asarray(idx.strftime("%Y-%m-%d"), dtype=object)


def _berlin_midnight_ns(date: str) -> int:
    return int(pd.Timestamp(date, tz=BERLIN).value)


def guard_dev_only(ts_ns: np.ndarray) -> None:
    """Refuse any timestamp whose Berlin date is after DEV_END (the forward holdout is never developed on)."""
    d = berlin_dates(ts_ns)
    if len(d) and str(d.max()) > DEV_END:
        raise ForwardHoldoutError(f"data reaches {d.max()} but development ends {DEV_END}; {FORWARD_HOLDOUT_START}+ is reserved")


def assert_partition_use(partitions: Iterable[str], purpose: str) -> None:
    """``fit`` -> TRAIN only; ``select`` -> TRAIN/VALIDATION; ``final_oos`` / ``oos_test`` -> OOS only; ``validate`` -> VALIDATION only;
    ``observe_forward`` / ``forward_monitor`` -> FORWARD only. The forward period is refused for every other purpose."""
    if purpose not in _ALLOWED_USE:
        raise ValueError(f"unknown purpose {purpose!r}")
    used = set(partitions)
    if FORWARD in used and purpose not in FORWARD_PURPOSES:
        raise ForwardHoldoutError(f"the forward period is never used for {purpose}")
    bad = used - _ALLOWED_USE[purpose]
    if bad:
        raise ValueError(f"partitions {sorted(bad)} are not allowed for {purpose}")


@dataclass(frozen=True)
class ObserverSplitPlan:
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    oos_start: str
    oos_end: str
    forward_start: str
    core_fit_end: str

    def ranges(self) -> list[tuple[str, str, str]]:
        return [
            (TRAIN, self.train_start, self.train_end), (VALIDATION, self.validation_start, self.validation_end),
            (OOS, self.oos_start, self.oos_end), (FORWARD, self.forward_start, "9999-12-31"),
        ]


def build_plan(trading_days: Sequence[str], *, validation_frac: float = 0.25, fit_end: str | None = None) -> ObserverSplitPlan:
    fit_end = fit_end or core_fit_end()
    fit_days = sorted({d for d in trading_days if d <= fit_end})
    if len(fit_days) < 4 or not 0 < validation_frac < 1:
        raise ValueError("need >= 4 trading days up to the core fit end and 0 < validation_frac < 1")
    n_val = max(1, math.ceil(validation_frac * len(fit_days)))
    train_days, val_days = fit_days[:-n_val], fit_days[-n_val:]
    if not train_days:
        raise ValueError("validation fraction leaves no training days")
    oos_start = (pd.Timestamp(fit_end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    return ObserverSplitPlan(
        train_days[0], train_days[-1], val_days[0], val_days[-1], oos_start, DEV_END, FORWARD_HOLDOUT_START, fit_end,
    )


def _partition_of(dates: np.ndarray, plan: ObserverSplitPlan) -> np.ndarray:
    out = np.full(len(dates), UNASSIGNED, dtype=object)
    for name, lo, hi in plan.ranges():
        out[(dates >= lo) & (dates <= hi)] = name
    return out


def assign_partitions(
    decision_ts_ns: np.ndarray, horizon_end_ts_ns: np.ndarray, plan: ObserverSplitPlan, *, embargo_s: float | None = None,
) -> np.ndarray:
    """Partition per event by the Berlin date of its decision; PURGED if its label horizon ends in another partition, EMBARGO inside the
    first ``embargo_s`` seconds after the start of a (non-first) partition. Default embargo = the longest label horizon in the data."""
    dec = np.asarray(decision_ts_ns, dtype="int64")
    hor = np.asarray(horizon_end_ts_ns, dtype="int64")
    if embargo_s is None:
        embargo_s = float(np.max(hor - dec) / 1e9) if len(dec) else 0.0
    part = _partition_of(berlin_dates(dec), plan)
    hpart = _partition_of(berlin_dates(np.maximum(hor - 1, dec)), plan)  # horizon end is exclusive
    out = part.copy()
    out[(hpart != part) & (part != UNASSIGNED)] = PURGED
    starts = {VALIDATION: plan.validation_start, OOS: plan.oos_start, FORWARD: plan.forward_start}
    for name, start in starts.items():
        sel = (part == name) & (out == name) & (dec < _berlin_midnight_ns(start) + int(embargo_s * 1e9))
        out[sel] = EMBARGO
    return out


# ---------------------------------------------------------------------------------------------- rolling / expanding windows
@dataclass(frozen=True)
class Window:
    index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str


def walk_forward_windows(
    trading_days: Sequence[str], *, train_days: int, test_days: int, embargo_days: int = 0, step_days: int | None = None, mode: str = "rolling",
) -> list[Window]:
    """Time-ordered windows over distinct Berlin trading dates: train = ``train_days`` days (rolling) or everything from the start
    (expanding), then ``embargo_days`` trading days gap, then ``test_days`` test days; the start advances by ``step_days`` (default =
    ``test_days``, i.e. disjoint test windows). Refuses dates after DEV_END. Returns [] when the data is too short."""
    if mode not in ("rolling", "expanding"):
        raise ValueError("mode must be 'rolling' or 'expanding'")
    if min(train_days, test_days) < 1 or embargo_days < 0:
        raise ValueError("train_days/test_days must be >= 1 and embargo_days >= 0")
    days = sorted(set(trading_days))
    if days and days[-1] > DEV_END:
        raise ForwardHoldoutError(f"trading days reach {days[-1]} but development ends {DEV_END}")
    step = step_days or test_days
    wins: list[Window] = []
    s = 0
    while s + train_days + embargo_days + test_days <= len(days):
        t0 = s + train_days + embargo_days
        wins.append(Window(len(wins), days[0] if mode == "expanding" else days[s], days[s + train_days - 1], days[t0], days[t0 + test_days - 1]))
        s += step
    return wins


def window_masks(decision_ts_ns: np.ndarray, horizon_end_ts_ns: np.ndarray, window: Window) -> tuple[np.ndarray, np.ndarray]:
    """(train_mask, test_mask) over the given events (order preserved, nothing shuffled). A train event is kept only when its decision AND
    its label horizon end lie inside the train range (purge); a test event only when its decision lies in the test range."""
    dec = np.asarray(decision_ts_ns, dtype="int64")
    hor = np.asarray(horizon_end_ts_ns, dtype="int64")
    d, h = berlin_dates(dec), berlin_dates(np.maximum(hor - 1, dec))
    train = (d >= window.train_start) & (d <= window.train_end) & (h <= window.train_end)
    test = (d >= window.test_start) & (d <= window.test_end)
    return train, test
