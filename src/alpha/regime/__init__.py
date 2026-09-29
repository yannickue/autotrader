"""Causal H1 market-regime classification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from alpha.timeframe import MtfView

UNDEFINED = "UNDEFINED"
REGIME_DIMENSIONS = ("DIRECTION", "TREND_STRENGTH", "VOLATILITY", "VOL_STATE")


@dataclass(frozen=True)
class RegimeConfig:
    """Fixed, interpretable H1 regime thresholds and lookbacks."""

    reference_window: int = 8
    slope_lookback: int = 3
    atr_short_window: int = 4
    atr_long_window: int = 12
    efficiency_window: int = 8
    realized_vol_window: int = 8
    volatility_history: int = 20
    direction_threshold: float = 0.10
    trending_efficiency: float = 0.55
    range_efficiency: float = 0.30
    volatility_low_quantile: float = 0.25
    volatility_high_quantile: float = 0.75
    compression_ratio_threshold: float = 0.85

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


def _h1_features(bars: pd.DataFrame, config: RegimeConfig) -> pd.DataFrame:
    close = bars["close"].astype(float)
    true_range = _true_range(bars)
    atr_short = true_range.rolling(
        config.atr_short_window, min_periods=config.atr_short_window
    ).mean()
    atr_long = true_range.rolling(config.atr_long_window, min_periods=config.atr_long_window).mean()
    reference = close.rolling(config.reference_window, min_periods=config.reference_window).mean()
    normalized_slope = (reference - reference.shift(config.slope_lookback)) / (
        atr_long * config.slope_lookback
    )
    path = (
        close.diff()
        .abs()
        .rolling(config.efficiency_window, min_periods=config.efficiency_window)
        .sum()
    )
    efficiency = (close - close.shift(config.efficiency_window)).abs() / path.replace(0.0, np.nan)
    log_return = np.log(close).diff()
    realized_vol = log_return.rolling(
        config.realized_vol_window, min_periods=config.realized_vol_window
    ).std(ddof=0) * np.sqrt(config.realized_vol_window)
    past = realized_vol.shift(1).rolling(
        config.volatility_history, min_periods=config.volatility_history
    )
    result = pd.DataFrame(index=bars.index)
    result["normalized_slope"] = normalized_slope
    result["efficiency_ratio"] = efficiency
    result["atr_short"] = atr_short
    result["atr_long"] = atr_long
    result["realized_vol"] = realized_vol
    result["volatility_past_low"] = past.quantile(config.volatility_low_quantile)
    result["volatility_past_high"] = past.quantile(config.volatility_high_quantile)
    result["compression_ratio"] = atr_short / atr_long.replace(0.0, np.nan)
    return result


def classify_regime(frame: pd.DataFrame, config: RegimeConfig | None = None) -> pd.DataFrame:
    """Return H1 regime dimensions aligned to each M5 bar close.

    Only complete H1 bars exposed by :class:`MtfView` participate. Volatility
    thresholds use the preceding ``volatility_history`` observations and never
    the current or future realized-volatility observation.
    """

    config = config or RegimeConfig()
    view = MtfView(frame)
    completed = view.h1[view.h1["complete"]].copy()
    features = _h1_features(completed, config)
    labels = features.copy()
    labels["DIRECTION"] = np.where(
        features["normalized_slope"] > config.direction_threshold,
        "UP",
        np.where(features["normalized_slope"] < -config.direction_threshold, "DOWN", "NEUTRAL"),
    )
    labels["TREND_STRENGTH"] = np.where(
        features["efficiency_ratio"] >= config.trending_efficiency,
        "TRENDING",
        np.where(features["efficiency_ratio"] <= config.range_efficiency, "RANGE_LIKE", "WEAK"),
    )
    labels["VOLATILITY"] = np.where(
        features["realized_vol"] <= features["volatility_past_low"],
        "LOW",
        np.where(features["realized_vol"] >= features["volatility_past_high"], "HIGH", "NORMAL"),
    )
    labels["VOL_STATE"] = np.where(
        features["compression_ratio"] <= config.compression_ratio_threshold,
        "COMPRESSION",
        "EXPANSION",
    )
    required = [
        "normalized_slope",
        "efficiency_ratio",
        "atr_short",
        "atr_long",
        "realized_vol",
        "volatility_past_low",
        "volatility_past_high",
        "compression_ratio",
    ]
    labels["defined"] = labels[required].notna().all(axis=1)
    labels.loc[~labels["defined"], list(REGIME_DIMENSIONS)] = UNDEFINED

    position_to_stamp = {
        position: view.h1.index[position] for position in np.flatnonzero(view.h1["complete"])
    }
    records: list[dict[str, object]] = []
    rows: dict[pd.Timestamp, dict[str, object]] = {}
    for position in view.h1_alignment:
        if position < 0:
            record = {column: np.nan for column in features.columns}
            record.update({dimension: UNDEFINED for dimension in REGIME_DIMENSIONS})
            record.update({"defined": False, "higher_bar_ts": None})
        else:
            stamp = position_to_stamp[int(position)]
            if stamp not in rows:  # one lookup per higher bar, reused by its M5 bars
                rows[stamp] = labels.loc[stamp].to_dict()
            record = dict(rows[stamp])
            record["higher_bar_ts"] = stamp
        records.append(record)
    return pd.DataFrame(records, index=view.m5.index)


__all__ = ["REGIME_DIMENSIONS", "UNDEFINED", "RegimeConfig", "classify_regime"]
