"""Small declarative alpha specification evaluated against causal feature arrays."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import numpy as np

from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays
from alpha.fast.store import NEW_FEATURE_NAMES, FeatureSet

SPEC_SCHEMA_VERSION = 1

Direction = Literal["LONG", "SHORT", "BOTH"]
Operator = Literal[">", ">=", "<", "<=", "==", "!=", "crosses_above", "crosses_below"]

_BASE_FEATURES = {
    "ts_ns",
    "o",
    "h",
    "l",
    "c",
    "spread",
    "berlin_minute",
    "berlin_day_id",
    "phase_code",
    "contig",
    "previous_day_high",
    "previous_day_low",
    "previous_day_close",
    "session_open",
    "session_high",
    "session_low",
    "last_swing_high",
    "last_swing_low",
    "structure_state",
    "bos",
    "compression_expansion_ratio",
    "regime_direction",
    "regime_trend_strength",
    "regime_volatility",
    "regime_vol_state",
}
_TA_FEATURES = {
    f"{timeframe}_{name}"
    for timeframe in ("m5", "m15", "h1")
    for name in (
        "atr14",
        "adx14",
        "rsi14",
        "ema_slope",
        "sma_slope",
        "bollinger_width",
        "normalized_return",
        "efficiency_ratio",
        "range_position",
        "volatility_percentile",
        "cdlengulfing",
        "cdlhammer",
        "cdldoji",
    )
}
_HTF_PRICES = {f"{timeframe}_{name}" for timeframe in ("m15", "h1") for name in "ohlc"}
_HTF_RANGES = {"m15_range", "h1_range"}
_CONTEXT_FEATURES = {
    f"context_{name}"
    for name in (
        "trend_continuation",
        "pullback",
        "consolidation",
        "compression",
        "range_extreme",
        "breakout_setup",
        "retest",
        "failed_breakout",
        "momentum_continuation",
        "reversal_context",
    )
}
FEATURE_NAMES = frozenset(
    _BASE_FEATURES
    | _TA_FEATURES
    | _HTF_PRICES
    | _HTF_RANGES
    | _CONTEXT_FEATURES
    | set(NEW_FEATURE_NAMES)
)
_OPS = {">", ">=", "<", "<=", "==", "!=", "crosses_above", "crosses_below"}
_LEVELS = {
    "session_open",
    "session_high",
    "session_low",
    "previous_day_high",
    "previous_day_low",
    "previous_day_close",
}


def _feature(name: str) -> None:
    if name not in FEATURE_NAMES:
        raise ValueError(f"unknown feature: {name}")


@dataclass(frozen=True)
class Rule:
    feature: str
    op: Operator
    threshold: float | int | bool | None = None
    other_feature: str | None = None

    def __post_init__(self) -> None:
        _feature(self.feature)
        if self.op not in _OPS:
            raise ValueError(f"unsupported operator: {self.op}")
        if (self.threshold is None) == (self.other_feature is None):
            raise ValueError("rule requires exactly one of threshold or other_feature")
        if self.other_feature is not None:
            _feature(self.other_feature)


@dataclass(frozen=True)
class StopSpec:
    kind: Literal["atr_multiple", "last_swing", "session_level"]
    feature: str | None = None
    multiple: float | None = None
    level: str | None = None
    offset: float = 0.0

    def __post_init__(self) -> None:
        if self.kind == "atr_multiple":
            if self.feature is None or self.multiple is None or self.multiple <= 0:
                raise ValueError("atr_multiple requires a feature and positive multiple")
            _feature(self.feature)
        elif self.kind == "last_swing":
            if self.feature not in {None, "last_swing_low", "last_swing_high"}:
                raise ValueError("last_swing feature must be last_swing_low/high")
        elif self.kind == "session_level":
            if self.level not in _LEVELS or self.offset < 0:
                raise ValueError("session_level requires a known level and non-negative offset")
        else:
            raise ValueError(f"unknown stop kind: {self.kind}")


@dataclass(frozen=True)
class TargetSpec:
    kind: Literal["fixed_r"]
    r: float

    def __post_init__(self) -> None:
        if self.kind != "fixed_r" or not np.isfinite(self.r) or self.r <= 0:
            raise ValueError("fixed_r target requires positive finite r")


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    version: str
    direction: Direction
    entry_rules: tuple[Rule, ...]
    stop: StopSpec
    target: TargetSpec
    regime_filters: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    context_filters: tuple[str, ...] = ()
    or_groups: tuple[tuple[Rule, ...], ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.version:
            raise ValueError("strategy_id and version are required")
        if self.direction not in {"LONG", "SHORT", "BOTH"}:
            raise ValueError("direction must be LONG, SHORT, or BOTH")
        if not self.entry_rules:
            raise ValueError("at least one entry rule is required")
        dimensions = {"DIRECTION", "TREND_STRENGTH", "VOLATILITY", "VOL_STATE"}
        unknown_dimensions = set(self.regime_filters).difference(dimensions)
        if unknown_dimensions:
            raise ValueError(f"unknown regime dimensions: {sorted(unknown_dimensions)}")
        unknown_context = [
            name for name in self.context_filters if f"context_{name.lower()}" not in FEATURE_NAMES
        ]
        if unknown_context:
            raise ValueError(f"unknown context flags: {unknown_context}")
        if any(not group for group in self.or_groups):
            raise ValueError("OR groups must not be empty")
        try:
            json.dumps(self._payload(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("spec fields must be finite JSON values") from exc

    def _payload(self) -> dict[str, Any]:
        return {"schema_version": SPEC_SCHEMA_VERSION, **asdict(self)}

    def spec_hash(self) -> str:
        encoded = json.dumps(
            self._payload(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def _rule_mask(features: FeatureSet, rule: Rule) -> np.ndarray:
    left = np.asarray(features[rule.feature])
    right: Any = (
        rule.threshold if rule.other_feature is None else np.asarray(features[rule.other_feature])
    )
    comparison = {
        ">": np.greater,
        ">=": np.greater_equal,
        "<": np.less,
        "<=": np.less_equal,
        "==": np.equal,
        "!=": np.not_equal,
    }
    if rule.op in comparison:
        return comparison[rule.op](left, right)
    current = np.greater(left, right) if rule.op == "crosses_above" else np.less(left, right)
    previous = np.zeros(len(left), dtype=bool)
    if len(left) > 1:
        prior_right = right if np.ndim(right) == 0 else right[:-1]
        previous[1:] = (
            np.less_equal(left[:-1], prior_right)
            if rule.op == "crosses_above"
            else np.greater_equal(left[:-1], prior_right)
        )
    return current & previous


def _regime_mask(features: FeatureSet, filters: Mapping[str, tuple[str, ...]]) -> np.ndarray:
    n = len(features["c"])
    result = np.ones(n, dtype=bool)
    maps = features.metadata.get("maps", {}).get("regime", {})
    for dimension, allowed in filters.items():
        labels = maps.get(dimension, {})
        codes = [int(code) for code, label in labels.items() if label in allowed]
        result &= np.isin(features[f"regime_{dimension.lower()}"], codes)
    return result


def _directions(features: FeatureSet, spec: StrategySpec) -> np.ndarray:
    n = len(features["c"])
    if spec.direction != "BOTH":
        return np.full(n, 1 if spec.direction == "LONG" else -1, dtype=np.int8)
    if "m5_normalized_return" in features:
        return np.where(features["m5_normalized_return"] >= 0, 1, -1).astype(np.int8)
    return np.where(features["c"] >= features["o"], 1, -1).astype(np.int8)


def _stops(features: FeatureSet, spec: StrategySpec, direction: np.ndarray) -> np.ndarray:
    close = np.asarray(features["c"], dtype=float)
    stop = spec.stop
    if stop.kind == "atr_multiple":
        return close - direction * float(stop.multiple) * np.asarray(
            features[stop.feature], dtype=float
        )
    if stop.kind == "last_swing":
        low = np.asarray(features["last_swing_low"], dtype=float)
        high = np.asarray(features["last_swing_high"], dtype=float)
        return np.where(direction > 0, low, high)
    level = np.asarray(features[stop.level], dtype=float)
    return level - direction * stop.offset


class RuleMaskCache:
    """Bounded LRU of per-rule boolean masks for ONE feature set.

    Keyed by ``(feature, op, threshold, other_feature)``; a mask is a pure function of the
    (immutable) feature arrays and the rule, so a hit is bit-identical to recomputation.
    Cached arrays are only ever read (``mask &= cached``), never mutated.
    """

    def __init__(self, features: FeatureSet, max_entries: int = 768) -> None:
        self._features = features
        self._max = max_entries
        self._data: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, rule: Rule) -> np.ndarray:
        key = (rule.feature, rule.op, rule.threshold, rule.other_feature)
        hit = self._data.get(key)
        if hit is not None:
            self._data.move_to_end(key)
            self.hits += 1
            return hit
        self.misses += 1
        value = _rule_mask(self._features, rule)
        value.flags.writeable = False
        self._data[key] = value
        if len(self._data) > self._max:
            self._data.popitem(last=False)
        return value


def evaluate_spec(features: FeatureSet, spec: StrategySpec,
                  rule_masks: RuleMaskCache | None = None) -> CandidateArrays:
    """Evaluate one spec at M5 closes without constructing per-bar Python objects.

    ``rule_masks`` (optional) memoises per-rule masks across calls; results are identical.
    """
    n = len(features["c"])
    if any(len(value) != n for value in features.values()):
        raise ValueError("feature arrays must be aligned")
    rule_mask = _rule_mask if rule_masks is None else (lambda _f, r: rule_masks.get(r))
    mask = _regime_mask(features, spec.regime_filters)
    for name in spec.context_filters:
        mask &= np.asarray(features[f"context_{name.lower()}"], dtype=bool)
    for rule in spec.entry_rules:
        mask &= rule_mask(features, rule)
    for group in spec.or_groups:
        group_mask = np.zeros(n, dtype=bool)
        for rule in group:
            group_mask |= rule_mask(features, rule)
        mask &= group_mask
    direction = _directions(features, spec)
    stop = _stops(features, spec, direction)
    close = np.asarray(features["c"], dtype=float)
    valid_stop = (
        np.isfinite(stop) & np.isfinite(close) & np.where(direction > 0, stop < close, stop > close)
    )
    evaluate_spec.invalid_stop_count = int(np.count_nonzero(mask & ~valid_stop))
    indices = np.flatnonzero(mask & valid_stop)
    count = len(indices)
    return CandidateArrays(
        indices,
        direction[indices],
        stop[indices],
        np.full(count, np.nan),
        np.full(count, spec.target.r),
        np.full(count, EXIT_FIXED_R, dtype=np.int8),
    )


evaluate_spec.invalid_stop_count = 0

__all__ = (
    "FEATURE_NAMES",
    "SPEC_SCHEMA_VERSION",
    "Rule",
    "RuleMaskCache",
    "StopSpec",
    "StrategySpec",
    "TargetSpec",
    "evaluate_spec",
)
