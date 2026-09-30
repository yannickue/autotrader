"""Causal M15 setup-context classification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from alpha.timeframe import MtfView

UNDEFINED = "UNDEFINED"
CONTEXT_LABELS = (
    "TREND_CONTINUATION",
    "PULLBACK",
    "CONSOLIDATION",
    "COMPRESSION",
    "RANGE_EXTREME",
    "BREAKOUT_SETUP",
    "RETEST",
    "FAILED_BREAKOUT",
    "MOMENTUM_CONTINUATION",
    "REVERSAL_CONTEXT",
)

# V2 directional context.  Suffix semantics: ``_up`` / ``_down`` (``_bottom`` / ``_top`` for range
# extremes) name the direction of the trade the context FAVOURS, so a LONG strategy reads the
# ``_up`` flag and its SHORT mirror the ``_down`` flag:
#   pullback_up          dip below the M15 reference inside an H1 UP trend (buy the dip)
#   breakout_setup_up    close parked just under the M15 breakout high (upside break pending)
#   retest_up            retest of a broken-out M15 high from above (after an UP break)
#   momentum_continuation_up  M15 up-impulse inside an H1 UP trend
#   reversal_up          bullish turn AGAINST the prior trend: failed breakdown or bullish
#                        reference cross, while H1 was DOWN or the previous M15 slope was down
#   failed_breakout_up   trapped sellers: failed BREAKDOWN below the M15 breakout low
#   range_extreme_bottom / _top   close in the lower / upper fraction of the M15 range
DIRECTIONAL_LABELS = (
    "PULLBACK_UP",
    "PULLBACK_DOWN",
    "BREAKOUT_SETUP_UP",
    "BREAKOUT_SETUP_DOWN",
    "RETEST_UP",
    "RETEST_DOWN",
    "MOMENTUM_CONTINUATION_UP",
    "MOMENTUM_CONTINUATION_DOWN",
    "REVERSAL_UP",
    "REVERSAL_DOWN",
    "RANGE_EXTREME_TOP",
    "RANGE_EXTREME_BOTTOM",
    "FAILED_BREAKOUT_UP",
    "FAILED_BREAKOUT_DOWN",
)
# direction-free M15 geometry columns (bool) the directional flags are assembled from
GEOMETRY_COLUMNS = (
    "geo_pullback_up",
    "geo_pullback_down",
    "geo_breakout_up",
    "geo_breakout_down",
    "geo_retest_up",
    "geo_retest_down",
    "geo_momentum_up",
    "geo_momentum_down",
    "geo_reversal_up",
    "geo_reversal_down",
    "geo_prior_down",
    "geo_prior_up",
    "geo_extreme_top",
    "geo_extreme_bottom",
    "geo_failed_up",
    "geo_failed_down",
)


@dataclass(frozen=True)
class ContextConfig:
    """Fixed, interpretable M15 setup-context thresholds and lookbacks."""

    reference_window: int = 8
    slope_lookback: int = 3
    atr_short_window: int = 4
    atr_long_window: int = 12
    range_window: int = 12
    breakout_lookback: int = 8
    momentum_window: int = 3
    direction_threshold: float = 0.10
    pullback_atr: float = 0.75
    consolidation_atr: float = 2.50
    compression_ratio_threshold: float = 0.80
    range_extreme_fraction: float = 0.20
    breakout_buffer_atr: float = 0.10
    retest_tolerance_atr: float = 0.25
    momentum_atr: float = 0.50

    def fingerprint(self) -> str:
        """Return a stable identifier covering every configuration field."""

        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def _true_range(bars: pd.DataFrame) -> pd.Series:
    previous_close = bars["close"].shift(1)
    return pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - previous_close).abs(),
            (bars["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _m15_context(bars: pd.DataFrame, config: ContextConfig) -> pd.DataFrame:
    close = bars["close"].astype(float)
    true_range = _true_range(bars)
    atr_short = true_range.rolling(
        config.atr_short_window, min_periods=config.atr_short_window
    ).mean()
    atr_long = true_range.rolling(config.atr_long_window, min_periods=config.atr_long_window).mean()
    reference = close.rolling(config.reference_window, min_periods=config.reference_window).mean()
    slope = (reference - reference.shift(config.slope_lookback)) / (
        atr_long * config.slope_lookback
    )
    range_high = (
        bars["high"].shift(1).rolling(config.range_window, min_periods=config.range_window).max()
    )
    range_low = (
        bars["low"].shift(1).rolling(config.range_window, min_periods=config.range_window).min()
    )
    breakout_high = (
        bars["high"]
        .shift(1)
        .rolling(config.breakout_lookback, min_periods=config.breakout_lookback)
        .max()
    )
    breakout_low = (
        bars["low"]
        .shift(1)
        .rolling(config.breakout_lookback, min_periods=config.breakout_lookback)
        .min()
    )
    previous_breakout_high = breakout_high.shift(1)
    previous_breakout_low = breakout_low.shift(1)
    previous_close = close.shift(1)
    range_width = range_high - range_low
    tolerance = config.retest_tolerance_atr * atr_long
    buffer = config.breakout_buffer_atr * atr_long
    momentum = close - close.shift(config.momentum_window)
    up = slope > config.direction_threshold
    down = slope < -config.direction_threshold

    result = pd.DataFrame(index=bars.index)
    result["normalized_slope"] = slope
    result["atr_short"] = atr_short
    result["atr_long"] = atr_long
    result["reference"] = reference
    result["range_high"] = range_high
    result["range_low"] = range_low
    result["breakout_high"] = breakout_high
    result["breakout_low"] = breakout_low
    result["compression_ratio"] = atr_short / atr_long.replace(0.0, np.nan)
    result["TREND_CONTINUATION"] = (up & (close >= reference)) | (down & (close <= reference))
    result["PULLBACK"] = (
        up & close.lt(reference) & close.ge(reference - config.pullback_atr * atr_long)
    ) | (down & close.gt(reference) & close.le(reference + config.pullback_atr * atr_long))
    result["CONSOLIDATION"] = range_width <= config.consolidation_atr * atr_long
    result["COMPRESSION"] = result["compression_ratio"] <= config.compression_ratio_threshold
    lower_extreme = close <= range_low + config.range_extreme_fraction * range_width
    upper_extreme = close >= range_high - config.range_extreme_fraction * range_width
    result["RANGE_EXTREME"] = lower_extreme | upper_extreme
    result["BREAKOUT_SETUP"] = close.between(breakout_low, breakout_high) & (
        (breakout_high - close <= buffer) | (close - breakout_low <= buffer)
    )
    prior_up_break = previous_close > previous_breakout_high
    prior_down_break = previous_close < previous_breakout_low
    result["RETEST"] = (
        prior_up_break
        & bars["low"].le(previous_breakout_high + tolerance)
        & close.ge(previous_breakout_high - tolerance)
    ) | (
        prior_down_break
        & bars["high"].ge(previous_breakout_low - tolerance)
        & close.le(previous_breakout_low + tolerance)
    )
    result["FAILED_BREAKOUT"] = (prior_up_break & close.le(previous_breakout_high)) | (
        prior_down_break & close.ge(previous_breakout_low)
    )
    result["MOMENTUM_CONTINUATION"] = (up & momentum.ge(config.momentum_atr * atr_long)) | (
        down & momentum.le(-config.momentum_atr * atr_long)
    )
    crossed_reference = ((previous_close < reference.shift(1)) & (close > reference)) | (
        (previous_close > reference.shift(1)) & (close < reference)
    )
    result["REVERSAL_CONTEXT"] = result["FAILED_BREAKOUT"] | (
        crossed_reference & (momentum.abs() >= config.momentum_atr * atr_long)
    )
    # ---- V2 directional geometry (favoured-direction naming, see DIRECTIONAL_LABELS) ----------
    mom_thr = config.momentum_atr * atr_long
    cross_up = (previous_close < reference.shift(1)) & (close > reference) & (momentum >= mom_thr)
    cross_down = (previous_close > reference.shift(1)) & (close < reference) & (
        momentum <= -mom_thr
    )
    failed_favours_up = prior_down_break & close.ge(previous_breakout_low)
    failed_favours_down = prior_up_break & close.le(previous_breakout_high)
    result["geo_pullback_up"] = close.lt(reference) & close.ge(
        reference - config.pullback_atr * atr_long
    )
    result["geo_pullback_down"] = close.gt(reference) & close.le(
        reference + config.pullback_atr * atr_long
    )
    inside = close.between(breakout_low, breakout_high)
    result["geo_breakout_up"] = inside & (breakout_high - close <= buffer)
    result["geo_breakout_down"] = inside & (close - breakout_low <= buffer)
    result["geo_retest_up"] = (
        prior_up_break
        & bars["low"].le(previous_breakout_high + tolerance)
        & close.ge(previous_breakout_high - tolerance)
    )
    result["geo_retest_down"] = (
        prior_down_break
        & bars["high"].ge(previous_breakout_low - tolerance)
        & close.le(previous_breakout_low + tolerance)
    )
    result["geo_momentum_up"] = momentum.ge(mom_thr)
    result["geo_momentum_down"] = momentum.le(-mom_thr)
    result["geo_reversal_up"] = failed_favours_up | cross_up
    result["geo_reversal_down"] = failed_favours_down | cross_down
    prior_slope = slope.shift(1)
    result["geo_prior_down"] = prior_slope < -config.direction_threshold
    result["geo_prior_up"] = prior_slope > config.direction_threshold
    result["geo_extreme_top"] = upper_extreme
    result["geo_extreme_bottom"] = lower_extreme
    result["geo_failed_up"] = failed_favours_up
    result["geo_failed_down"] = failed_favours_down
    required = [
        "normalized_slope",
        "atr_short",
        "atr_long",
        "reference",
        "range_high",
        "range_low",
        "breakout_high",
        "breakout_low",
        "compression_ratio",
    ]
    result["defined"] = result[required].notna().all(axis=1)
    result.loc[~result["defined"], list(CONTEXT_LABELS) + list(GEOMETRY_COLUMNS)] = False
    result["labels"] = [
        tuple(label for label in CONTEXT_LABELS if bool(row[label]))
        if row["defined"]
        else (UNDEFINED,)
        for _, row in result.iterrows()
    ]
    return result


def classify_context(frame: pd.DataFrame, config: ContextConfig | None = None) -> pd.DataFrame:
    """Return deterministic multi-label M15 setup context aligned to M5 closes."""

    config = config or ContextConfig()
    view = MtfView(frame)
    completed = view.m15[view.m15["complete"]].copy()
    context = _m15_context(completed, config)
    position_to_stamp = {
        position: view.m15.index[position] for position in np.flatnonzero(view.m15["complete"])
    }
    records: list[dict[str, object]] = []
    rows: dict[pd.Timestamp, dict[str, object]] = {}
    for position in view.m15_alignment:
        if position < 0:
            record = {
                column: np.nan
                for column in context.columns
                if column not in CONTEXT_LABELS and column not in GEOMETRY_COLUMNS
            }
            record.update({label: False for label in CONTEXT_LABELS})
            record.update({column: False for column in GEOMETRY_COLUMNS})
            record.update({"defined": False, "labels": (UNDEFINED,), "higher_bar_ts": None})
        else:
            stamp = position_to_stamp[int(position)]
            if stamp not in rows:  # one lookup per higher bar, reused by its M5 bars
                rows[stamp] = context.loc[stamp].to_dict()
            record = dict(rows[stamp])
            record["higher_bar_ts"] = stamp
        records.append(record)
    return pd.DataFrame(records, index=view.m5.index)


def directional_context(
    context: pd.DataFrame, h1_up: np.ndarray, h1_down: np.ndarray
) -> dict[str, np.ndarray]:
    """Directional context flags from ``classify_context`` output plus the H1 regime direction.

    ``h1_up`` / ``h1_down`` are M5-aligned booleans (H1 regime direction UP / DOWN).  Pure per-bar
    combination of causal inputs, so it inherits their truncation invariance.  Keys are
    ``DIRECTIONAL_LABELS`` lower-cased.
    """

    defined = context["defined"].to_numpy(bool)
    up = np.asarray(h1_up, dtype=bool)
    down = np.asarray(h1_down, dtype=bool)

    def geo(name: str) -> np.ndarray:
        return context[f"geo_{name}"].to_numpy(bool) & defined

    flags = {
        "pullback_up": up & geo("pullback_up"),
        "pullback_down": down & geo("pullback_down"),
        "breakout_setup_up": geo("breakout_up"),
        "breakout_setup_down": geo("breakout_down"),
        "retest_up": geo("retest_up"),
        "retest_down": geo("retest_down"),
        "momentum_continuation_up": up & geo("momentum_up"),
        "momentum_continuation_down": down & geo("momentum_down"),
        "reversal_up": geo("reversal_up") & (down | geo("prior_down")),
        "reversal_down": geo("reversal_down") & (up | geo("prior_up")),
        "range_extreme_top": geo("extreme_top"),
        "range_extreme_bottom": geo("extreme_bottom"),
        "failed_breakout_up": geo("failed_up"),
        "failed_breakout_down": geo("failed_down"),
    }
    return {name: np.asarray(value, dtype=bool) for name, value in flags.items()}


__all__ = [
    "CONTEXT_LABELS",
    "DIRECTIONAL_LABELS",
    "GEOMETRY_COLUMNS",
    "UNDEFINED",
    "ContextConfig",
    "classify_context",
    "directional_context",
]
