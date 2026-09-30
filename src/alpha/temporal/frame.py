# ruff: noqa: E501
"""Adapter: real ``FeatureSet`` + ``EventSet`` -> the temporal ``MarketFrame`` contract.

``build_market_frame(features, events, thresholds=None, *, plan=None)`` returns a MarketFrame whose
``arrays`` mapping is LAZY (nothing is copied or loaded until the kernel asks for a name; EventSet
arrays stay memory-mapped) and whose ``thresholds`` mapping resolves feature quantiles from
TRAIN-partition bars only (``alpha.discovery.compile.ThresholdResolver`` + ``SplitPlan.mask``).

``needed_names(spec)`` / ``frame_for_specs(frame, specs)`` implement the memory discipline: the
kernel's ArrayPool stacks every array a batch touches, so a batch frame is materialised with just
those arrays (and the thresholds its feature clauses need) as a plain, picklable/dumpable frame.

Name mapping (``MarketFrame.arrays`` key -> source)
---------------------------------------------------
core        o,h,l,c                 features o,h,l,c
            atr                     features m5_atr14
            run_start               ``alpha.fast.store._run_start(berlin_day_id, contig)``
            berlin_minute           features berlin_minute
            ts_close_ns             features ts_ns + 5 min (ts_ns is the bar OPEN stamp)
events      every EventSet name     ev_/evl_/evx_/evo_/st_/lv_/zid_/sf_/valid_* (mmap, lazy)
features    every FeatureSet name   by its own name (e.g. ``range_ratio_12_48``)
feature     atr_pct                 100 * m5_atr14 / c                      (derived)
clause      range_ratio             range_ratio_12_48                        (alias)
names       ret_12                  mom_12_atr                               (alias; signed, ATR units)
(registry   slope_20                m5_ema_slope                             (alias; EMA20 slope / ATR)
FEATURE_    dist_vwap_atr           (c - mean_since_day_start((h+l+c)/3)) / atr   (derived; a causal
MIRROR)                             session TWAP proxy: the FeatureSet carries no volume, so no VWAP)
levels      m5_swing_high/low       EventSet lv_m5_swing_{high,low}_lvl   (confirmed at p+order)
(TARGET_    m15_swing_high/low      EventSet lv_m15_swing_{high,low}_lvl
LEVELS)     h1_swing_high/low       EventSet lv_h1_swing_{high,low}_lvl
            session_high/low        features session_high/session_low (running Berlin-day extreme,
                                    includes bar u, known at the close of u)
            pdh / pdl               features previous_day_high / previous_day_low (Berlin day)
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any

import numpy as np

from alpha.discovery.compile import ThresholdResolver
from alpha.events import schema as ev
from alpha.fast.store import _run_start
from alpha.temporal.reference import MarketFrame, level_arrays
from alpha.temporal.spec import Capture, Clause, StateMachineStrategySpec

BAR_NS = 5 * 60 * 10**9

# TARGET_LEVELS name -> source array name
LEVEL_SOURCES: dict[str, str] = {
    "m5_swing_high": "lv_m5_swing_high_lvl",
    "m5_swing_low": "lv_m5_swing_low_lvl",
    "m15_swing_high": "lv_m15_swing_high_lvl",
    "m15_swing_low": "lv_m15_swing_low_lvl",
    "h1_swing_high": "lv_h1_swing_high_lvl",
    "h1_swing_low": "lv_h1_swing_low_lvl",
    "session_high": "session_high",
    "session_low": "session_low",
    "pdh": "previous_day_high",
    "pdl": "previous_day_low",
}
# FEATURE_MIRROR name -> FeatureSet name (pure alias); derived ones are built in ``_derived``
FEATURE_ALIASES: dict[str, str] = {
    "range_ratio": "range_ratio_12_48",
    "ret_12": "mom_12_atr",
    "slope_20": "m5_ema_slope",
}
_DERIVED = ("atr_pct", "dist_vwap_atr")

assert set(LEVEL_SOURCES) == set(ev.TARGET_LEVELS)
assert set(FEATURE_ALIASES) | set(_DERIVED) == set(ev.FEATURE_MIRROR)


def berlin_dates(features: Mapping[str, np.ndarray]) -> np.ndarray:
    """Berlin calendar date per bar (datetime64[D]), as ``research.runners.ar2_fast._dates``."""
    import pandas as pd

    utc = pd.to_datetime(np.asarray(features["ts_ns"]), utc=True)
    return (
        utc.tz_convert("Europe/Berlin").normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
    )


def _dist_twap_atr(features: Mapping[str, np.ndarray]) -> np.ndarray:
    """Causal per-day running mean of the typical price, as distance from close in ATR."""
    h, low, c = (np.asarray(features[k], dtype=np.float64) for k in ("h", "l", "c"))
    day = np.asarray(features["berlin_day_id"], dtype=np.int64)
    atr = np.asarray(features["m5_atr14"], dtype=np.float64)
    tp = (h + low + c) / 3.0
    csum = np.cumsum(tp)
    first = np.r_[True, day[1:] != day[:-1]]
    start = np.maximum.accumulate(np.where(first, np.arange(len(day)), 0))
    base = np.where(start > 0, csum[np.maximum(start - 1, 0)], 0.0)
    mean = (csum - base) / (np.arange(len(day)) - start + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(atr > 0, (c - mean) / atr, np.nan)


def _derived(name: str, features: Mapping[str, np.ndarray]) -> np.ndarray:
    if name == "atr_pct":
        c = np.asarray(features["c"], dtype=np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            return 100.0 * np.asarray(features["m5_atr14"], dtype=np.float64) / np.where(c != 0, c, np.nan)
    if name == "dist_vwap_atr":
        return _dist_twap_atr(features)
    raise KeyError(name)


class LazyArrays(Mapping):
    """name -> array, materialised on first access.  ``in`` answers for every KNOWN name; iteration,
    ``len`` and ``items`` cover only names already loaded (what ``SharedFrame`` dumps)."""

    def __init__(self, resolvers: Mapping[str, Callable[[], np.ndarray]], n: int) -> None:
        self._res = dict(resolvers)
        self._cache: dict[str, np.ndarray] = {}
        self._n = n

    def __getitem__(self, name: str) -> np.ndarray:
        a = self._cache.get(name)
        if a is None:
            try:
                fn = self._res[name]
            except KeyError:
                raise KeyError(name) from None
            a = np.asarray(fn())
            if a.shape != (self._n,):
                raise ValueError(f"array {name} has shape {a.shape}, expected ({self._n},)")
            self._cache[name] = a
        return a

    def __contains__(self, name: object) -> bool:
        return name in self._res

    def __iter__(self) -> Iterator[str]:
        return iter(self._cache)

    def __len__(self) -> int:
        return len(self._cache)

    def known(self) -> tuple[str, ...]:
        return tuple(self._res)

    def loaded(self) -> tuple[str, ...]:
        return tuple(self._cache)


class TrainThresholds(Mapping):
    """``thresholds[(name, q)]`` = TRAIN quantile via ``ThresholdResolver`` (train bars only).

    Iteration / ``len`` cover the thresholds resolved so far (``dict(thresholds)`` is a snapshot).
    """

    def __init__(self, arrays: Mapping[str, np.ndarray], train_mask: np.ndarray) -> None:
        self._resolver = ThresholdResolver(arrays, np.asarray(train_mask, dtype=bool))
        self._done: dict[tuple[str, float], float] = {}

    def __getitem__(self, key: tuple[str, float]) -> float:
        name, q = key
        v = self._done.get((name, q))
        if v is None:
            v = self._done[(name, q)] = self._resolver.value(name, float(q))
        return v

    def __iter__(self) -> Iterator[tuple[str, float]]:
        return iter(self._done)

    def __len__(self) -> int:
        return len(self._done)

    def __contains__(self, key: object) -> bool:
        try:
            self[key]  # type: ignore[index]
        except (KeyError, ValueError, TypeError):
            return False
        return True


class _NoThresholds(Mapping):
    def __getitem__(self, key):
        raise KeyError(f"no thresholds supplied for {key}: pass thresholds= or plan= (train-only)")

    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0


def build_market_frame(
    features: Mapping[str, np.ndarray],
    events: Mapping[str, np.ndarray],
    thresholds: Mapping[tuple[str, float], float] | None = None,
    *,
    plan: Any | None = None,
) -> MarketFrame:
    """Adapt (FeatureSet, EventSet) to the reference/kernel ``MarketFrame``.

    ``thresholds``: an explicit mapping ``(feature, q) -> value`` (used as given), or None ->
    lazily resolved from TRAIN bars of ``plan`` (a ``SplitPlan``; embargo aware via
    ``SplitPlan.mask``).  With neither, feature clauses fail closed (KeyError).
    """
    n = len(features["c"])
    day = np.asarray(features["berlin_day_id"], dtype=np.int64)
    contig = np.asarray(features["contig"], dtype=bool)
    resolvers: dict[str, Callable[[], np.ndarray]] = {}
    for name in features:
        resolvers[name] = lambda name=name: features[name]
    for name, src in FEATURE_ALIASES.items():
        resolvers[name] = lambda src=src: features[src]
    for name in _DERIVED:
        resolvers[name] = lambda name=name: _derived(name, features)
    for name in events:  # events win on a (currently non-existent) name clash
        resolvers[name] = lambda name=name: events[name]
    for name, src in LEVEL_SOURCES.items():
        resolvers[name] = lambda src=src: (events[src] if src in events else features[src])
    arrays = LazyArrays(resolvers, n)
    if thresholds is None:
        if plan is None:
            thresholds = _NoThresholds()
        else:
            thresholds = TrainThresholds(arrays, plan.mask(berlin_dates(features), plan.train))
    return MarketFrame(
        o=np.asarray(features["o"]), h=np.asarray(features["h"]), l=np.asarray(features["l"]),
        c=np.asarray(features["c"]), atr=np.asarray(features["m5_atr14"]),
        run_start=_run_start(day, contig).astype(np.int64),
        berlin_minute=np.asarray(features["berlin_minute"]),
        arrays=arrays, thresholds=thresholds,
        ts_close_ns=np.asarray(features["ts_ns"], dtype=np.int64) + BAR_NS,
    )


# ------------------------------------------------------------------------------ spec -> names
def _clause_names(c: Clause) -> list[str]:
    if c.kind == "feature":
        return [c.name]
    if c.kind == "bound":
        return []
    return [ev.array_names(c.name, c.tf, c.variant)[0]]


def _capture_names(cap: Capture, clauses: Sequence[Clause]) -> list[str]:
    if cap.source in ("evl", "evx"):
        cl = next(c for c in clauses if c.kind == "event" and c.name == cap.of and c.op == "IS")
        return [next(n for n in ev.array_names(cl.name, cl.tf, cl.variant) if n.startswith(cap.source + "_"))]
    if cap.source == "lv":
        cl = next(c for c in clauses if c.kind == "event" and c.op == "IS" and cap.of in ev.get(c.name).exposes)
        arr, zid = level_arrays(cap.of, cl)
        return [arr] if zid is None else [arr, zid]
    return []


def needed_names(spec: StateMachineStrategySpec) -> tuple[set[str], set[tuple[str, float]]]:
    """(array names, feature thresholds) one spec reads from ``MarketFrame.arrays/thresholds``."""
    arrays: set[str] = set()
    thr: set[tuple[str, float]] = set()

    def clause(c: Clause) -> None:
        arrays.update(_clause_names(c))
        if c.kind == "feature":
            thr.add((c.name, c.q))

    for c in (*spec.anchor, *spec.context):
        clause(c)
    for cap in spec.anchor_capture:
        arrays.update(_capture_names(cap, spec.anchor))
    for c in spec.anchor:
        if c.kind == "event" and c.op == "IS":
            ex = ev.get(c.name).exposes
            if "ZONE_LO" in ex and "ZONE_HI" in ex:
                arrays.update((level_arrays("ZONE_LO", c)[0], level_arrays("ZONE_HI", c)[0]))
                break
    for t in spec.states:
        same = (t.trigger, *t.guards)
        for c in (*same, *t.invalidate):
            clause(c)
        for cap in t.capture:
            arrays.update(_capture_names(cap, same))
    if spec.stop.kind == "swing":
        arrays.add(ev.array_names(spec.stop.of, spec.stop.tf, "")[0])
    if spec.target.kind == "next_structure":
        arrays.update(spec.target.levels)
    return arrays, thr


def needed_for_batch(specs: Iterable[StateMachineStrategySpec]) -> tuple[set[str], set[tuple[str, float]]]:
    arrays: set[str] = set()
    thr: set[tuple[str, float]] = set()
    for s in specs:
        a, t = needed_names(s)
        arrays |= a
        thr |= t
    return arrays, thr


def frame_for_specs(frame: MarketFrame, specs: Iterable[StateMachineStrategySpec]) -> MarketFrame:
    """Plain (dict-backed) frame holding ONLY the arrays/thresholds ``specs`` need; suitable for
    ``SharedFrame`` / ``prefix`` / multi-worker evaluation.  Loads lazily-mapped arrays on demand."""
    arrays, thr = needed_for_batch(specs)
    return MarketFrame(
        o=frame.o, h=frame.h, l=frame.l, c=frame.c, atr=frame.atr, run_start=frame.run_start,
        berlin_minute=frame.berlin_minute,
        arrays={k: np.asarray(frame.arrays[k]) for k in sorted(arrays)},
        thresholds={k: float(frame.thresholds[k]) for k in sorted(thr)},
        ts_close_ns=frame.ts_close_ns,
    )


def assert_finite_thresholds(frame: MarketFrame) -> None:
    for k, v in frame.thresholds.items():
        if not math.isfinite(v):
            raise ValueError(f"non-finite threshold {k}")
