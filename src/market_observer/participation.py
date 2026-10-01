# ruff: noqa: E501
"""PARTICIPATION feature group (OBSERVATION ONLY / NOT ALPHA VALIDATED). MT5 gives TICK counts per bar (``tick_volume``), NOT exchange volume:
everything here is a *tick activity / participation proxy*. No volume profile, no POC, no value area, no exchange-volume interpretation.

``participation_features(bars, i)`` uses bars ``<= i`` only. Baselines are strictly PAST-ONLY and never use a full-sample mean/std.

Validity: a tick count counts only if finite and > 0 (MT5 only forms a bar when ticks arrived; 0/NaN is a data defect). A current value that is
invalid => every feature None. Invalid baseline samples are EXCLUDED (never imputed, never replaced by later data).

Features (names without the ``f_participation__`` prefix):

* ``tick_activity_per_min``      x_i / (bar_seconds / 60) (same scaling as ``demo.opportunity.bar_source.tick_activity``: ticks per minute).
* ``tick_activity_percentile``   mid-rank of x_i inside the TOD baseline: (#below + 0.5 * #equal) / n  (constant series => 0.5).
* ``tick_activity_z``            (x_i - mean) / sample-std (ddof=1) of the TOD baseline; None if std is 0.
* ``activity_vs_same_tod``       x_i / median of the TOD baseline.
* ``activity_vs_session_baseline`` x_i / median of the valid bars of the same market-local trading day strictly BEFORE i (>= ``session_min_bars``).
* ``activity_acceleration``      x_i / median of the previous ``accel_bars`` bars (all in the same segment, all valid).
* ``tod_baseline_n`` / ``tod_baseline_oldest_ts_ns``  audit: number of valid baseline samples / open time of the oldest one.

TOD baseline: the bar with the SAME ``local_minute`` on each of the ``tod_baseline_days`` (20) most recent PREVIOUS trading days present in the
data (distinct ``local_day`` values < today's; weekends/holidays are simply absent days), valid samples only, at least ``tod_min_valid`` (10).

WARM-UP / HISTORY INVARIANCE (recursive-analysis requirement): the value at T must not depend on how much history was loaded before T.
* ``MIN_HISTORY_PREV_DAYS`` = ``tod_baseline_days + 1`` = 21 previous trading days must be present. The 21st (oldest loaded) day may be a truncated
  first day of the loaded window, so it is only used as proof of completeness; the 20 days used are all complete. With fewer previous days the
  TOD features (percentile, z, vs_same_tod, tod_baseline_*) are None — NEVER a partial baseline presented as a full one. With >= 21 days
  the result is EXACTLY independent of any further history (tested with windows of 120 / 240 / 500 bars / full: the TOD group is None for the short
  windows and exactly equal once the window covers 21 previous trading days).
* ``activity_vs_session_baseline`` needs the START of today's trading day to be loaded: at least one earlier-day bar must exist, else None.
* ``activity_acceleration`` needs only the previous ``accel_bars`` bars (history independent from 4 bars on).
Live/research consequence: after a restart/backfill the TOD features are None until 21 previous trading days (~21 * 288 M5 bars on a 24h market) are
loaded; research runs must load >= that much before the first evaluated bar (or accept None rows). A day with a missing same-minute bar lowers
``tod_baseline_n`` (data quality, not loading), and below ``tod_min_valid`` the TOD group is None.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np

from market_observer.schema import GROUP_VERSIONS, FeatureResult, JsonScalar, ObserverBars

GROUP = "participation"
FEATURE_NAMES: tuple[str, ...] = (
    "tick_activity_per_min", "tick_activity_percentile", "tick_activity_z", "activity_vs_same_tod", "activity_vs_session_baseline",
    "activity_acceleration", "tod_baseline_n", "tod_baseline_oldest_ts_ns",
)


@dataclass(frozen=True)
class ParticipationConfig:
    tod_baseline_days: int = 20  # previous trading days of same-local-minute bars (~one trading month)
    tod_min_valid: int = 10  # minimum valid samples among them (data quality)
    session_min_bars: int = 6  # minimum earlier valid bars of the same trading day for the running session baseline (30 min of M5)
    accel_bars: int = 3  # the "previous few bars" of the short-term acceleration

    def __post_init__(self) -> None:
        if self.tod_baseline_days < 2 or not 2 <= self.tod_min_valid <= self.tod_baseline_days or self.session_min_bars < 1 or self.accel_bars < 1:
            raise ValueError("invalid ParticipationConfig")


def min_history_prev_days(config: ParticipationConfig | None = None) -> int:
    return (config or ParticipationConfig()).tod_baseline_days + 1


def definition_hash(config: ParticipationConfig | None = None) -> str:
    cfg = config or ParticipationConfig()
    payload = {"group": GROUP, "version": GROUP_VERSIONS[GROUP], "features": list(FEATURE_NAMES), "config": asdict(cfg),
               "min_history_prev_days": min_history_prev_days(cfg)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


DEFINITION_HASH = definition_hash()
MIN_HISTORY_PREV_DAYS = min_history_prev_days()


def _valid(x: float) -> bool:
    return bool(np.isfinite(x) and x > 0)


def _tod_baseline(bars: ObserverBars, i: int, ds: int, cfg: ParticipationConfig) -> tuple[np.ndarray, int] | None:
    """(valid baseline samples, ts of the oldest) or None if the baseline is not fully available."""
    ld = bars.local_day
    starts: list[int] = []
    pos = ds
    for _ in range(cfg.tod_baseline_days + 1):
        if pos == 0:
            break
        st = int(np.searchsorted(ld[:pos], ld[pos - 1], side="left"))
        starts.append(st)
        pos = st
    if len(starts) < cfg.tod_baseline_days + 1:
        return None  # warm-up: not enough previous trading days loaded
    s_used = starts[cfg.tod_baseline_days - 1]  # first bar of the oldest USED day
    idx = np.flatnonzero(bars.local_minute[s_used:ds] == bars.local_minute[i]) + s_used
    if len(idx):  # at most one bar per day (keep the last if a DST fold produced two)
        days = ld[idx]
        keep = np.r_[days[1:] != days[:-1], True]
        idx = idx[keep]
    vals = bars.tick_volume[idx]
    ok = np.isfinite(vals) & (vals > 0)
    if int(ok.sum()) < cfg.tod_min_valid:
        return None
    idx = idx[ok]
    return vals[ok].astype(float), int(bars.ts_ns[idx[0]])


def participation_features(bars: ObserverBars, i: int, config: ParticipationConfig | None = None) -> FeatureResult:
    cfg = config or ParticipationConfig()
    out: dict[str, JsonScalar] = {k: None for k in FEATURE_NAMES}
    result = FeatureResult(GROUP, GROUP_VERSIONS[GROUP], out)
    x = float(bars.tick_volume[i])
    if not _valid(x):
        return result
    out["tick_activity_per_min"] = x / (bars.bar_seconds / 60.0)
    ld = bars.local_day
    ds = int(np.searchsorted(ld[: i + 1], ld[i], side="left"))  # first loaded bar of today's trading day

    base = _tod_baseline(bars, i, ds, cfg)
    if base is not None:
        samples, oldest_ts = base
        n = len(samples)
        out["tick_activity_percentile"] = float((np.count_nonzero(samples < x) + 0.5 * np.count_nonzero(samples == x)) / n)
        mean, std = float(samples.mean()), float(samples.std(ddof=1))
        out["tick_activity_z"] = (x - mean) / std if std > 1e-12 * max(1.0, abs(mean)) else None
        out["activity_vs_same_tod"] = x / float(np.median(samples))
        out["tod_baseline_n"] = n
        out["tod_baseline_oldest_ts_ns"] = oldest_ts

    if ds > 0:  # today's trading day is fully loaded from its first bar
        today = bars.tick_volume[ds:i]
        today = today[np.isfinite(today) & (today > 0)]
        if len(today) >= cfg.session_min_bars:
            out["activity_vs_session_baseline"] = x / float(np.median(today))

    k = cfg.accel_bars
    if i >= k and int(bars.segment_id[i - k]) == int(bars.segment_id[i]):
        prev = bars.tick_volume[i - k: i]
        if bool(np.all(np.isfinite(prev) & (prev > 0))):
            out["activity_acceleration"] = x / float(np.median(prev))
    return result
