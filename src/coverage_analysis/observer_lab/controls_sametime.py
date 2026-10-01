# ruff: noqa: E501
"""SAME-CLOCK-TIME / OTHER-DAY controls (``observer-controls-3``; OFFLINE research, OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

``observer-controls-2`` (+-48-bar exclusion around EVERY opportunity) is practically empty and censoring-biased for the five core markets (the
opportunity bars cover the whole session). ``observer-controls-3`` drops the exclusion radius and matches on the clock instead:

* For every event the candidates are the bars at the EXACT same Europe/Berlin bar clock time (minute of day of the bar open) on the
  ``max_day_offset`` (default 10) nearest trading days before / after the event day. "Trading day" = a Berlin calendar date that has bars of
  the market (weekends, holidays and data gaps simply do not exist as days, so the walk is gap / segment aware: a candidate day without a bar at
  that clock time yields no candidate).
* Same PARTITION as the event (TRAIN / VALIDATION / OOS), taken from ``controls.bar_partitions`` with the label horizon: a bar whose 48-bar horizon
  reaches into another partition is PURGED, the first horizon after a boundary EMBARGO; neither can ever be a control, so the control's complete
  label horizon lies inside the partition. FORWARD bars are never candidates; a FORWARD event (or a control on a day >= the forward start) is an
  ERROR (``ForwardHoldoutError``), never silently skipped.
* The control bar is not an opportunity bar (events + ``exclude_idx`` = every generator candidate of the market). There is NO neighbourhood rule;
  the distance in bars to the nearest opportunity is stored as a diagnostic column (``dist_to_opportunity_bars``) only.
* Selection: nearest trading day (|offset|, ties resolved by a seeded coin per event) whose bar is inside the same session bucket and the ATR /
  spread percentile bands (partition-internal ranks as in controls-2; an unknown spread matches only unknown). The control inherits the direction
  of its event. Controls are drawn without replacement (a bar is never reused) in a seeded random event order: same seed => identical controls.
* ``n_controls`` per event (default 1). ``match_controls_sametime(..., avoid_idx=<set A bars>, seed=<other seed>)`` draws the DISJOINT second set
  ``controls_b`` of the A/A test.
* Every control carries the position of its event (``event_pos``), hence its event id: the bootstrap can put it in the day block of the EVENT.

Matching never looks at outcomes (labels / features are not read here).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from alpha.common.market_data import FORWARD_HOLDOUT_START, ForwardHoldoutError
from coverage_analysis.observer_lab import splits as SP
from coverage_analysis.observer_lab.controls import (
    ControlSet,
    _report,
    bar_covariates,
    bar_partitions,
)
from market_observer.schema import ObserverBars

CONTROL_METHOD_VERSION = "observer-controls-3"
CONTROL_MATCHING_REVISION = "same Berlin bar clock time on the +-10 nearest trading days, same partition (full horizon inside), no exclusion radius, opportunity bars never controls"
USABLE = (SP.TRAIN, SP.VALIDATION, SP.OOS)
B_SEED_OFFSET = 1_000_003


@dataclass(frozen=True)
class SameTimeSpec:
    max_day_offset: int = 10
    atr_pct_band: float = 0.10
    spread_pct_band: float = 0.15
    n_controls: int = 1
    horizon_bars: int = 48
    control_set: str = "a"  # "a" | "b": b = disjoint A/A set, drawn with seed + B_SEED_OFFSET


def berlin_clock(bars: ObserverBars) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(day_id, minute_of_day) of every bar OPEN in Europe/Berlin. ``day_id`` indexes the sorted distinct Berlin dates that have bars."""
    idx = pd.to_datetime(np.asarray(bars.ts_ns, dtype="int64"), utc=True).tz_convert(SP.BERLIN)
    dates = np.asarray(idx.strftime("%Y-%m-%d"), dtype=object)
    minute = (idx.hour * 60 + idx.minute).to_numpy(np.int64)
    uniq, inv = np.unique(dates, return_inverse=True)
    return inv.astype(np.int64), minute, uniq


def distance_to_set(n: int, idx_sorted: np.ndarray, query: np.ndarray) -> np.ndarray:
    """Distance in bars from each ``query`` index to the nearest index of the sorted array ``idx_sorted`` (large when empty)."""
    if len(idx_sorted) == 0:
        return np.full(len(query), n, dtype=np.int64)
    pos = np.searchsorted(idx_sorted, query)
    left = idx_sorted[np.clip(pos - 1, 0, len(idx_sorted) - 1)]
    right = idx_sorted[np.clip(pos, 0, len(idx_sorted) - 1)]
    return np.minimum(np.abs(query - left), np.abs(right - query)).astype(np.int64)


def match_controls_sametime(
    bars: ObserverBars, event_idx: Sequence[int], *, spec: SameTimeSpec | None = None, seed: int = 0, eligible: np.ndarray | None = None,
    partition: np.ndarray | Sequence[str] | str | None = "auto", exclude_idx: Sequence[int] | None = None, avoid_idx: Sequence[int] | None = None,
) -> ControlSet:
    """Same-clock-time / other-day controls (see module docstring). ``ControlSet.extra`` carries one dict per control: ``day_offset`` (signed trading
    days control minus event), ``dist_to_opportunity_bars``, ``control_set``. ``partition='auto'`` (default) or a per-bar label array is REQUIRED
    (controls-3 is partition bound); ``avoid_idx``: bars that may not be drawn (the A set when drawing B)."""
    spec = spec or SameTimeSpec()
    if partition is None:
        raise ValueError("observer-controls-3 is partition bound: partition=None is not supported")
    ev = np.asarray(event_idx, dtype=np.int64)
    n = len(bars)
    if len(ev) and (ev.min() < 0 or ev.max() >= n):
        raise ValueError("event index outside the bars")
    day_id, minute, dates = berlin_clock(bars)
    if len(ev) and str(dates[day_id[ev]].max()) >= FORWARD_HOLDOUT_START:
        raise ForwardHoldoutError(f"event on a Berlin day >= {FORWARD_HOLDOUT_START}: the forward period is never read")
    if isinstance(partition, str):
        if partition != "auto":
            raise ValueError("partition must be 'auto' or a per-bar label array")
        part = bar_partitions(bars, horizon_bars=spec.horizon_bars)
    else:
        part = np.asarray(partition, dtype=object)
        if len(part) != n:
            raise ValueError(f"partition has {len(part)} labels for {n} bars")
    extra_block = np.asarray([] if exclude_idx is None else exclude_idx, dtype=np.int64)
    if len(extra_block) and (extra_block.min() < 0 or extra_block.max() >= n):
        raise ValueError("exclude_idx outside the bars")
    blockers = np.unique(np.concatenate([ev, extra_block]))
    cov = bar_covariates(bars, rank_mode="partition", partition=part)
    ok = ~np.isnan(cov["atr_pct"]) & (np.arange(n) < n - 1) & np.isin(part, USABLE)
    ok &= np.asarray(dates, dtype=object)[day_id] < FORWARD_HOLDOUT_START  # the forward period is never a candidate
    if eligible is not None:
        ok &= np.asarray(eligible, bool)
    ok[blockers] = False
    used: set[int] = {int(x) for x in (avoid_idx if avoid_idx is not None else [])}
    # (day_id, clock minute) -> bar index; a clock time that occurs twice on one Berlin date (autumn DST hour) is ambiguous and never a candidate
    key = day_id * 1440 + minute
    order = np.argsort(key, kind="stable")
    ks = key[order]
    dup = np.zeros(n, dtype=bool)
    if n > 1:
        same = ks[1:] == ks[:-1]
        dup[order[1:][same]] = True
        dup[order[:-1][same]] = True
    lookup = {int(k): int(i) for k, i, d in zip(key.tolist(), range(n), dup.tolist(), strict=True) if not d}
    n_days = int(day_id.max()) + 1 if n else 0
    sub_seed = int(seed) + (B_SEED_OFFSET if spec.control_set == "b" else 0)
    rng = np.random.default_rng(sub_seed)
    pairs: list[tuple[int, int, int]] = []
    for pos in rng.permutation(len(ev)):
        i = int(ev[pos])
        if part[i] not in USABLE or np.isnan(cov["atr_pct"][i]):
            continue  # event in a purged / embargoed bar or without a volatility rank: reported unmatched
        erng = np.random.default_rng([sub_seed, i])
        sp_i = cov["spread_pct"][i]
        taken = 0
        for k in range(1, spec.max_day_offset + 1):
            signs = [-1, 1] if erng.random() < 0.5 else [1, -1]
            for sgn in signs:
                d = int(day_id[i]) + sgn * k
                if not 0 <= d < n_days:
                    continue
                j = lookup.get(d * 1440 + int(minute[i]))
                if j is None or not ok[j] or j in used or part[j] != part[i] or cov["session"][j] != cov["session"][i]:
                    continue
                if abs(cov["atr_pct"][j] - cov["atr_pct"][i]) > spec.atr_pct_band + 1e-12:
                    continue
                sp_j = cov["spread_pct"][j]
                if np.isnan(sp_i) != np.isnan(sp_j) or (not np.isnan(sp_i) and abs(sp_j - sp_i) > spec.spread_pct_band + 1e-12):
                    continue
                used.add(j)
                pairs.append((int(pos), j, sgn * k))
                taken += 1
                if taken >= spec.n_controls:
                    break
            if taken >= spec.n_controls:
                break
    pairs.sort()
    event_pos = np.array([p for p, _, _ in pairs], dtype=np.int64)
    control_idx = np.array([c for _, c, _ in pairs], dtype=np.int64)
    if len(control_idx) and str(dates[day_id[control_idx]].max()) >= FORWARD_HOLDOUT_START:
        raise ForwardHoldoutError("a control lies on a Berlin day in the forward period")  # unreachable by construction; hard assertion
    if len(control_idx) and np.isin(control_idx, blockers).any():
        raise RuntimeError("a control is an opportunity bar")  # unreachable by construction; hard assertion
    dist = distance_to_set(n, blockers, control_idx) if len(control_idx) else np.empty(0, dtype=np.int64)
    rep = _report(cov, ev, event_pos, control_idx, part=part, rank_mode="partition", n_blocked=len(blockers))
    rep = replace(rep, method=CONTROL_METHOD_VERSION)
    extra = tuple({"day_offset": off, "dist_to_opportunity_bars": int(dd), "control_set": spec.control_set} for (_, _, off), dd in zip(pairs, dist.tolist(), strict=True))
    return ControlSet(event_pos, control_idx, rep, extra, tuple(str(part[j]) for j in control_idx.tolist()))


# ---------------------------------------------------------------------------------------------- balance gate (counts / covariates only)
GATE_VERSION = "observer-controls-3-balance-1"
GATE_MATCH_RATE_MIN = 0.90
GATE_SMD_MAX = 0.10
GATE_SESSION_DIFF_MAX = 0.02
GATE_CENSOR_DIFF_MAX = 0.05
GATE_MIN_EVENTS = 20  # fewer usable events in a partition: INSUFFICIENT_N (the partition is descriptive only, not a pass)
GATE_PARTITIONS = (SP.TRAIN, SP.VALIDATION, "FROZEN_OOS")  # backfill writes OOS as FROZEN_OOS
SMD_VARS = (("local_minute", "minute"), ("atr_pct", "atr_pct"), ("spread_pct", "spread_pct"))


def _smd(a: np.ndarray, b: np.ndarray) -> float:
    from coverage_analysis.observer_lab.controls import standardised_mean_difference

    return standardised_mean_difference(a, b)


def balance_gate(pairs: pd.DataFrame, n_events_by_partition: dict[str, int], partitions: Sequence[str] = GATE_PARTITIONS) -> dict:
    """BLOCKING balance gate per partition of ONE market. ``pairs``: one row per control with columns ``partition`` (the event's), ``ev_minute``,
    ``ev_atr_pct``, ``ev_spread_pct``, ``ev_session``, ``ev_censored``, ``c_minute``, ``c_atr_pct``, ``c_spread_pct``, ``c_session``, ``c_censored``.
    ``n_events_by_partition``: events of the market per partition (usable partitions; the match-rate denominator).
    Checks: match rate >= 0.9; |SMD| <= 0.1 (local_minute, atr_pct, spread_pct); max session-share difference <= 0.02; |censored share control - event| <= 0.05
    (over the matched events / their controls). Reads covariates and label AVAILABILITY only, never a feature-vs-label distribution.
    Verdict per partition: PASS | FAIL | INSUFFICIENT_N (< 20 events) | NO_EVENTS; market status ``descriptive_only`` as soon as one partition with events is not PASS."""
    out: dict = {"gate_version": GATE_VERSION, "thresholds": {"match_rate_min": GATE_MATCH_RATE_MIN, "smd_abs_max": GATE_SMD_MAX, "session_share_diff_max": GATE_SESSION_DIFF_MAX,
                                                              "censored_share_diff_max": GATE_CENSOR_DIFF_MAX, "min_events": GATE_MIN_EVENTS}, "partitions": {}}
    any_not_pass = False
    any_pass = False
    for p in partitions:
        n_ev = int(n_events_by_partition.get(p, 0))
        sub = pairs[pairs["partition"] == p] if len(pairs) else pairs
        n_ctl = len(sub)
        rec: dict = {"n_events": n_ev, "n_controls": n_ctl}
        if n_ev == 0:
            rec["verdict"] = "NO_EVENTS"
            out["partitions"][p] = rec
            continue
        n_matched_events = n_ctl  # lower bound; refined below when the event ids are present
        if "control_of" in sub.columns:
            n_matched_events = int(sub["control_of"].nunique())
        rec["n_matched_events"] = n_matched_events
        rec["match_rate"] = n_matched_events / n_ev
        reasons = []
        if rec["match_rate"] < GATE_MATCH_RATE_MIN:
            reasons.append(f"match_rate {rec['match_rate']:.3f} < {GATE_MATCH_RATE_MIN}")
        if n_ctl:
            rec["smd"] = {name: _smd(sub[f"ev_{col}"].to_numpy(float), sub[f"c_{col}"].to_numpy(float)) for name, col in SMD_VARS}
            for name, v in rec["smd"].items():
                if not np.isfinite(v) or abs(v) > GATE_SMD_MAX:
                    reasons.append(f"|SMD {name}| {v:.3f} > {GATE_SMD_MAX}")
            sess = set(sub["ev_session"].tolist()) | set(sub["c_session"].tolist())
            rec["session_share_diff_max"] = max(abs(float((sub["ev_session"] == s).mean()) - float((sub["c_session"] == s).mean())) for s in sess)
            if rec["session_share_diff_max"] > GATE_SESSION_DIFF_MAX:
                reasons.append(f"session share diff {rec['session_share_diff_max']:.3f} > {GATE_SESSION_DIFF_MAX}")
            ce, cc = float(sub["ev_censored"].astype(float).mean()), float(sub["c_censored"].astype(float).mean())
            rec["censored_share"] = {"events": ce, "controls": cc, "diff": cc - ce}
            if abs(cc - ce) > GATE_CENSOR_DIFF_MAX:
                reasons.append(f"|censored share diff| {abs(cc - ce):.3f} > {GATE_CENSOR_DIFF_MAX}")
        else:
            reasons.append("no controls")
        rec["fail_reasons"] = reasons
        if n_ev < GATE_MIN_EVENTS:
            rec["verdict"] = "INSUFFICIENT_N"
        else:
            rec["verdict"] = "PASS" if not reasons else "FAIL"
        any_pass |= rec["verdict"] == "PASS"
        any_not_pass |= rec["verdict"] != "PASS"
        out["partitions"][p] = rec
    out["market_status"] = "analysis_eligible" if (any_pass and not any_not_pass) else "descriptive_only"
    out["market_pass"] = out["market_status"] == "analysis_eligible"
    return out
