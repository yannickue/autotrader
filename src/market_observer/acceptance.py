# ruff: noqa: E501
"""ACCEPTANCE / REJECTION feature group (OBSERVATION ONLY / NOT ALPHA VALIDATED): continuous descriptors of a level break at the decision bar.

``acceptance_features(bars, i, level, direction)``: ``level`` is the explicit :class:`~market_observer.schema.LevelRef` handed over by the level
group (``None`` => every value None), ``direction`` = +1 (break above) / -1 (break below). Everything below uses bars ``<= i`` only; nothing is
thresholded into an "accepted" flag, and later movement belongs to the post-event labels.

Definitions (s = direction, edge = ``zone_high`` for s=+1 / ``zone_low`` for s=-1, "beyond" = ``s*(close - edge) > 0``, strict, on CLOSES):

* Break bar k = the FIRST bar in the scan window that is a FRESH cross (beyond, previous close not beyond). The scan window is: inside the
  segment of bar i (the previous close must be in the same segment), at most ``max_break_lookback_bars`` before i, and only bars OPENING at or after
  ``level.confirmed_at_ts_ns`` (a break must happen after the level existed). A level confirmed after the decision time => all None.
  No fresh cross => ``break_found`` False, the rest None. A later re-break after a reclaim does not replace the first break; it shows up as
  ``reclaim_occurred`` with ``closes_beyond_level_count`` > ``time_held_beyond_level_bars``.
* ATR normalisation: the three break-bar descriptors use ``atr[k]``; everything describing the state at the decision bar uses ``atr[i]``
  (None if not finite / <= 0). Counts, times and the close location need no ATR.
* ``break_close_distance_atr`` s*(c_k - edge)/atr_k (>0); ``break_body_atr`` s*(c_k - o_k)/atr_k (signed, + = body in break direction);
  ``break_true_range_atr`` TR_k/atr_k with TR from the previous close of the same segment.
* ``closes_beyond_level_count`` closes beyond the edge over k..i; ``bars_since_break`` = i - k.
* ``max_reentry_depth_atr`` deepest wick back through the edge after the break bar: max(0, s*(edge - extreme)) with extreme = min low (s=+1) /
  max high (s=-1) over k+1..i; 0 if k == i.
* ``followthrough_high_atr`` / ``followthrough_low_atr``: (max high - edge)/atr_i and (min low - edge)/atr_i over k..i in RAW price orientation
  (mirrored prices therefore swap them with a sign flip); ``followthrough_dir_atr`` = the favourable one (high for s=+1, -low for s=-1).
* ``close_location_in_bar`` of the BREAK bar, direction-relative in [0,1]: 1 = closed at the extreme in the break direction. None if h == l.
* ``time_held_beyond_level_bars`` = trailing run of consecutive closes beyond the edge ending at i (0 if bar i closed back inside);
  ``..._minutes`` = bars * bar_seconds / 60.
* WARM-UP / HISTORY INVARIANCE: the value at T depends only on bars [T - L - 1 .. T] (L = ``max_break_lookback_bars``) clipped at the segment start,
  plus the explicit level and the ATR array. If fewer than L + 2 bars are loaded AND no segment break proves that the data really starts there,
  every value is None (``min_history_bars``/``MIN_HISTORY_BARS`` = L + 2 = 98). From that point the result is EXACTLY independent of how much
  earlier history was loaded (tested with 120 / 240 / 500 / full windows). Live/research consequence: after a restart the first 98 bars of a
  loaded history yield None, never a truncated-lookback value; ATR is an input (its own recursion warm-up is the adapter's job).
* ``reclaim_occurred`` True if any close in k+1..i is at/inside the edge. ``break_bar_ts_ns`` = open timestamp of bar k (<= decision time).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np

from market_observer.schema import GROUP_VERSIONS, FeatureResult, JsonScalar, LevelRef, ObserverBars

GROUP = "acceptance"
FEATURE_NAMES: tuple[str, ...] = (
    "break_found", "break_bar_ts_ns", "break_close_distance_atr", "break_body_atr", "break_true_range_atr", "closes_beyond_level_count",
    "bars_since_break", "max_reentry_depth_atr", "followthrough_high_atr", "followthrough_low_atr", "followthrough_dir_atr",
    "close_location_in_bar", "time_held_beyond_level_bars", "time_held_beyond_level_minutes", "reclaim_occurred",
)


@dataclass(frozen=True)
class AcceptanceConfig:
    max_break_lookback_bars: int = 96  # a break older than this many bars is not tracked (= STRUCT swing scan length, 8 h of M5)

    def __post_init__(self) -> None:
        if self.max_break_lookback_bars < 1:
            raise ValueError("max_break_lookback_bars must be >= 1")


def min_history_bars(config: AcceptanceConfig | None = None) -> int:
    return (config or AcceptanceConfig()).max_break_lookback_bars + 2


def definition_hash(config: AcceptanceConfig | None = None) -> str:
    cfg = config or AcceptanceConfig()
    payload = {"group": GROUP, "version": GROUP_VERSIONS[GROUP], "features": list(FEATURE_NAMES), "config": asdict(cfg), "min_history_bars": min_history_bars(cfg)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


DEFINITION_HASH = definition_hash()
MIN_HISTORY_BARS = min_history_bars()


def _div(num: float, atr: float) -> float | None:
    return num / atr if np.isfinite(atr) and atr > 0 and np.isfinite(num) else None


def acceptance_features(bars: ObserverBars, i: int, level: LevelRef | None, direction: int, config: AcceptanceConfig | None = None) -> FeatureResult:
    if direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    cfg = config or AcceptanceConfig()
    out: dict[str, JsonScalar] = {k: None for k in FEATURE_NAMES}
    result = FeatureResult(GROUP, GROUP_VERSIONS[GROUP], out)
    if level is None or level.confirmed_at_ts_ns > bars.decision_ts_ns(i):
        return result
    s = direction
    edge = level.zone_high if s > 0 else level.zone_low
    seg_start = int(np.searchsorted(bars.segment_id[: i + 1], bars.segment_id[i], side="left"))
    k0 = int(np.searchsorted(bars.ts_ns[: i + 1], level.confirmed_at_ts_ns, side="left"))  # first bar opening at/after confirmation
    if i - cfg.max_break_lookback_bars - 1 < 0 and seg_start == 0:  # lookback not fully loaded and no proven segment break: warm-up, unknown
        return result
    j0 = max(seg_start, i - cfg.max_break_lookback_bars - 1)
    beyond = s * (bars.c[j0: i + 1] - edge) > 0
    fresh = np.flatnonzero(beyond[1:] & ~beyond[:-1]) + j0 + 1
    fresh = fresh[fresh >= k0]
    out["break_found"] = bool(len(fresh))
    if not len(fresh):
        return result
    k = int(fresh[0])
    o, h, lo, c = bars.o, bars.h, bars.l, bars.c
    atr_k, atr_i = float(bars.atr[k]), float(bars.atr[i])
    out["break_bar_ts_ns"] = int(bars.ts_ns[k])
    out["break_close_distance_atr"] = _div(s * (float(c[k]) - edge), atr_k)
    out["break_body_atr"] = _div(s * (float(c[k]) - float(o[k])), atr_k)
    prev_c = float(c[k - 1])  # k-1 >= seg_start by construction
    tr = max(float(h[k] - lo[k]), abs(float(h[k]) - prev_c), abs(float(lo[k]) - prev_c))
    out["break_true_range_atr"] = _div(tr, atr_k)
    after = s * (c[k: i + 1] - edge) > 0
    out["closes_beyond_level_count"] = int(after.sum())
    out["bars_since_break"] = i - k
    if k < i:
        ext = (edge - float(lo[k + 1: i + 1].min())) if s > 0 else (float(h[k + 1: i + 1].max()) - edge)
        out["max_reentry_depth_atr"] = _div(max(0.0, ext), atr_i)
        out["reclaim_occurred"] = bool(np.any(~after[1:]))
    else:
        out["max_reentry_depth_atr"] = _div(0.0, atr_i)
        out["reclaim_occurred"] = False
    fh = _div(float(h[k: i + 1].max()) - edge, atr_i)
    fl = _div(float(lo[k: i + 1].min()) - edge, atr_i)
    out["followthrough_high_atr"], out["followthrough_low_atr"] = fh, fl
    out["followthrough_dir_atr"] = fh if s > 0 else (None if fl is None else -fl)
    rng = float(h[k] - lo[k])
    if rng > 0:
        out["close_location_in_bar"] = (float(c[k] - lo[k]) if s > 0 else float(h[k] - c[k])) / rng
    run = 0
    for flag in after[::-1]:
        if not flag:
            break
        run += 1
    out["time_held_beyond_level_bars"] = run
    out["time_held_beyond_level_minutes"] = run * bars.bar_seconds / 60.0
    return result
