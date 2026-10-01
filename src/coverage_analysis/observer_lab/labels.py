# ruff: noqa: E501
"""Post-event labels of the Market Structure Observer (OFFLINE, RETROSPECTIVE, OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

Conventions (they mirror ``demo.labeling.simulate_hypothetical`` and ``demo.entry_exit_quality.walk_entry_path``, and the
equality is pinned by ``tests/unit/observer_lab/test_ol_labels.py``):

* The decision bar ``i`` is CLOSED at ``decision_ts_ns``; the hypothetical fill is AT ``event.entry``. Only bars ``j > i`` are used,
  bar ``i`` itself and everything before are never read.
* Bar resolution, STOP-FIRST: inside one bar the adverse side is checked before the favourable side (same-bar tie = adverse first).
* The excursion path runs under a hypothetical 1R stop (``entry -/+ 1 R``): the stop bar updates MAE but NOT MFE, a bar opening
  through the stop is a gap (same truncation as the repo). ``mfe_r`` / ``mae_r`` are in R of the event's own initial risk distance.
* ``y_fav<a>_before_adv<b>``: 1 = +a R reached first, 0 = -b R reached first (incl. same-bar tie), None = neither inside the horizon
  (censored). ``y_mfe_before_mae`` is the 0.5 R / 0.5 R touch order of ``entry_exit_quality`` (True/False/None).
* times: seconds from the decision time to the OPEN of the bar that set the extreme (the first bar after the decision has t = 0:
  resolution = one bar, as in the repo); None while the excursion is 0.
* ``y_post_stop_favorable_excursion_r``: max favourable excursion (R, from the entry) over the bars STRICTLY AFTER the hypothetical
  1R stop bar inside the horizon, floored at 0; None when the stop was not hit or no bar follows it.
* ``y_structural_target_reached``: caller-supplied price level; True when a bar BEFORE the stop bar reaches it (stop-first),
  False otherwise, None without a target. ``y_reclaim_or_followthrough``: caller-supplied level; True when a bar before the stop bar
  CLOSES at/over it on the favourable side (>= for long, <= for short), None without a level.
* HORIZON = the earlier of ``max_bars`` bars (default 48 = 4 h of M5), the first bar whose ``local_minute >= flat_local_minute``
  (market-local session end / flat; defaults to ``bars.session.cash_close_min`` when it is defined, else off), the first bar of a
  different ``segment_id`` (data break) or of a different ``local_day``. ``horizon_end_ts_ns`` = close of the last used bar.
* COSTS are EXCLUDED by default (bid bars, fill at ``entry``, no fee/slippage). ``spread_adjusted=True`` reproduces the repo's
  exit-side convention (a short exits at ask = bid + spread of the bar; long at bid).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from market_observer.schema import (
    FIRST_PASSAGE_LABELS,
    ObserverBars,
    PostEventLabels,
    all_label_columns,
    first_passage_label_name,
    label_key,
)

LABEL_CONVENTION_VERSION = "obs-labels-1"
DEFAULT_MAX_BARS = 48
STOP_R = 1.0
TOUCH_R = 0.5  # mfe_before_mae touch level (== demo.entry_exit_quality.FIRST_TOUCH_R)


@dataclass(frozen=True)
class EventSpec:
    """An event at decision bar ``i`` (index of the last CLOSED bar), with its own initial risk distance ``risk`` in price units."""

    i: int
    direction: int
    entry: float
    risk: float
    target: float | None = None
    reclaim_level: float | None = None


def horizon_bars(bars: ObserverBars, i: int, *, max_bars: int = DEFAULT_MAX_BARS, flat_local_minute: int | None = None) -> int:
    """Index one past the last bar of the horizon (bars ``i+1 .. end-1`` are labelled)."""
    if flat_local_minute is None:
        flat_local_minute = bars.session.cash_close_min
    end = min(len(bars), i + 1 + max_bars)
    seg, day = bars.segment_id[i], bars.local_day[i]
    for j in range(i + 1, end):
        if bars.segment_id[j] != seg or bars.local_day[j] != day:
            return j
        if flat_local_minute is not None and bars.local_minute[j] >= flat_local_minute:
            return j
    return end


def label_event(
    bars: ObserverBars,
    event: EventSpec,
    *,
    max_bars: int = DEFAULT_MAX_BARS,
    flat_local_minute: int | None = None,
    spread_adjusted: bool = False,
) -> PostEventLabels:
    n = len(bars)
    if event.direction not in (1, -1):
        raise ValueError("direction must be +1/-1")
    if not (math.isfinite(event.risk) and event.risk > 0) or not math.isfinite(event.entry):
        raise ValueError("initial risk distance must be finite and > 0")
    if not 0 <= event.i < n:
        raise ValueError(f"event index {event.i} outside the bars (n={n})")
    if event.target is not None and (event.target - event.entry) * event.direction <= 0:
        raise ValueError("target must be on the favourable side of the entry")
    long = event.direction > 0
    risk, entry = event.risk, event.entry
    decision_ns = bars.decision_ts_ns(event.i)
    end = horizon_bars(bars, event.i, max_bars=max_bars, flat_local_minute=flat_local_minute)
    names = {pair: label_key(first_passage_label_name(*pair)) for pair in FIRST_PASSAGE_LABELS}
    fp: dict[tuple[float, float], int | None] = {pair: None for pair in FIRST_PASSAGE_LABELS}

    mfe = mae = 0.0
    t_mfe: float | None = None
    t_mae: float | None = None
    touch: bool | None = None
    stop_bar: int | None = None
    post_fav: float | None = None
    target_hit = False
    reclaimed = False
    for j in range(event.i + 1, end):
        sp = 0.0 if (long or not spread_adjusted) else float(bars.spread[j])
        o, hi, lo, cl = bars.o[j] + sp, bars.h[j] + sp, bars.l[j] + sp, bars.c[j] + sp
        adv = ((entry - lo) if long else (hi - entry)) / risk
        fav = ((hi - entry) if long else (entry - lo)) / risk
        t = max((int(bars.ts_ns[j]) - decision_ns) / 1e9, 0.0)
        if stop_bar is not None:  # beyond the hypothetical stop only the post-stop excursion is tracked
            post_fav = max(0.0 if post_fav is None else post_fav, fav)
            continue
        # first-passage pairs, stop-first inside the bar
        for pair in FIRST_PASSAGE_LABELS:
            if fp[pair] is None:
                a, b = pair
                if adv >= b:
                    fp[pair] = 0
                elif fav >= a:
                    fp[pair] = 1
        if touch is None:
            if adv >= TOUCH_R:
                touch = False
            elif fav >= TOUCH_R:
                touch = True
        if adv > mae:
            mae, t_mae = adv, t
        gap = (o <= entry - event.direction * STOP_R * risk) if long else (o >= entry + STOP_R * risk)
        if gap or adv >= STOP_R:
            stop_bar = j
            continue
        if event.target is not None and not target_hit and ((hi >= event.target) if long else (lo <= event.target)):
            target_hit = True
        if event.reclaim_level is not None and not reclaimed and ((cl >= event.reclaim_level) if long else (cl <= event.reclaim_level)):
            reclaimed = True
        if fav > mfe:
            mfe, t_mfe = fav, t
    horizon_end = int(bars.ts_ns[end - 1]) + bars.bar_seconds * 1_000_000_000 if end > event.i + 1 else decision_ns
    if end <= event.i + 1:
        return PostEventLabels(horizon_end, {k: None for k in all_label_columns()})
    cols: dict[str, float | int | bool | None] = {names[p]: fp[p] for p in FIRST_PASSAGE_LABELS}
    cols.update({
        label_key("mfe_r"): mfe, label_key("mae_r"): mae, label_key("time_to_mfe_s"): t_mfe, label_key("time_to_mae_s"): t_mae,
        label_key("mfe_before_mae"): touch, label_key("post_stop_favorable_excursion_r"): post_fav,
        label_key("structural_target_reached"): None if event.target is None else target_hit,
        label_key("reclaim_or_followthrough"): None if event.reclaim_level is None else reclaimed,
    })
    return PostEventLabels(horizon_end, cols)

