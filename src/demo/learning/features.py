# ruff: noqa: E501
"""PRE-TRADE feature extraction from an `OpportunitySnapshot` ONLY (numpy-free, no ML imports).

Leakage guard (hard):
  * `extract_features` accepts nothing but an `OpportunitySnapshot`; a Decision, an outcome, a
    counterfactual label or a `TradeLearningRecord` is rejected with `LeakageGuardError`.
  * Only the dotted snapshot paths in `ALLOWED_SNAPSHOT_FIELDS` are ever read (a flat "view" is built
    first, features are computed from that view alone). Unknown keys inside the free-form dicts
    (`signal`, `structure`, `context`) are therefore invisible to the models.
  * `assert_no_forbidden_tokens` rejects any whitelist path / feature name containing an outcome-like
    token (outcome, decision, counterfactual, label, mfe, mae, pnl, net_r, ...).
  * `FEATURE_VERSION` changes whenever the feature set or its semantics change; models and shadow
    predictions carry it and a mismatching model is treated as untrained.
"""

from __future__ import annotations

import math
from typing import Any

from demo.contracts import OpportunitySnapshot

FEATURE_VERSION = "demo-features-1"

FAMILIES: tuple[str, ...] = (
    "ORB", "GAP", "EOD", "LEADLAG", "OVERNIGHT", "ROUNDNUM", "VOLREV",
)
SESSION_BUCKETS: tuple[str, ...] = (
    "PRE", "OPEN_09_10", "MORNING_10_12", "MIDDAY_12_1530", "US_OVERLAP_1530_1730", "LATE_1730_2130",
)
TIMEFRAMES: tuple[str, ...] = ("D1", "H4", "H1", "M15", "M5", "M1")
STRUCTURE_FLAGS: tuple[str, ...] = ("breakout", "retest", "sweep", "bos", "choch")

FORBIDDEN_TOKENS: tuple[str, ...] = (
    "outcome", "decision", "counterfactual", "label", "mfe", "mae", "pnl", "net_r", "gross_r",
    "hypothetical", "exit_reason", "closed", "fill", "slippage", "shadow", "accepted", "reason",
    "intent", "execution", "target_before_stop", "holding", "risk_record",
)

ALLOWED_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "direction",
    "geometry.intended_entry",
    "geometry.risk_distance",
    "geometry.target",
    "geometry.min_space_r",
    "geometry.space_to_opposition_r",
    "geometry.expected_horizon_s",
    "geometry.exit_kind",
    "geometry.exit_r",
    "market_state.spread",
    "market_state.atr",
    "market_state.realized_vol",
    "market_state.tick_activity",
    "market_state.clock.local_minute",
    "market_state.clock.session_bucket",
    "market_state.clock.in_entry_window",
    "market_state.clock.minutes_to_forced_flat",
    "signal.family",
    "signal.confluence",
    "signal.independent_clusters",
    "signal.quality",
    *(f"context.{tf}.trend" for tf in TIMEFRAMES),
    *(f"structure.{flag}" for flag in STRUCTURE_FLAGS),
)

NAN = float("nan")


class LeakageGuardError(ValueError):
    """Raised when something other than a pre-decision snapshot (or a forbidden field) is used."""


def assert_no_forbidden_tokens(names: tuple[str, ...] | list[str]) -> None:
    for n in names:
        low = n.lower()
        for tok in FORBIDDEN_TOKENS:
            if tok in low:
                raise LeakageGuardError(f"forbidden token {tok!r} in field/feature {n!r}")


def _feature_names() -> tuple[str, ...]:
    return (
        "direction", "risk_atr", "rr_plan", "exit_r", "min_space_r", "space_to_opp_r",
        "spread_risk", "spread_atr", "atr_rel", "realized_vol_atr", "tick_activity",
        "horizon_min", "local_minute", "minutes_to_flat", "in_entry_window",
        "confluence", "independent_clusters", "quality", "quality_min", "quality_max",
        "exit_kind_trail",
        *(f"session_{s}" for s in SESSION_BUCKETS), "session_other",
        *(f"family_{f}" for f in FAMILIES), "family_other",
        *(f"struct_{f}" for f in STRUCTURE_FLAGS),
        *(f"ctx_{tf}_trend" for tf in TIMEFRAMES),
    )


FEATURE_NAMES: tuple[str, ...] = _feature_names()
assert_no_forbidden_tokens(ALLOWED_SNAPSHOT_FIELDS)
assert_no_forbidden_tokens(FEATURE_NAMES)


def _get(d: dict[str, Any], path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def snapshot_view(snap: OpportunitySnapshot) -> dict[str, Any]:
    """Flat {dotted_path: value} of the whitelisted fields ONLY."""
    if not isinstance(snap, OpportunitySnapshot):
        raise LeakageGuardError(
            f"features are computed from OpportunitySnapshot only, got {type(snap).__name__}"
        )
    d = snap.to_dict()
    return {p: _get(d, p) for p in ALLOWED_SNAPSHOT_FIELDS}


def _num(v: Any) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)) and math.isfinite(v):
        return float(v)
    return NAN


def _ratio(a: float, b: float) -> float:
    if math.isnan(a) or math.isnan(b) or b == 0.0:
        return NAN
    return a / b


def _quality(q: Any) -> tuple[float, float, float]:
    if isinstance(q, dict):
        vals = [_num(v) for v in q.values()]
        vals = [v for v in vals if not math.isnan(v)]
        if not vals:
            return NAN, NAN, NAN
        return sum(vals) / len(vals), min(vals), max(vals)
    v = _num(q)
    return v, v, v


def extract_features(snap: OpportunitySnapshot) -> dict[str, float]:
    """Deterministic pre-trade feature dict in `FEATURE_NAMES` order (NaN = unknown)."""
    v = snapshot_view(snap)
    entry = _num(v["geometry.intended_entry"])
    risk = _num(v["geometry.risk_distance"])
    target = _num(v["geometry.target"])
    atr = _num(v["market_state.atr"])
    spread = _num(v["market_state.spread"])
    exit_r = _num(v["geometry.exit_r"])
    kind = v["geometry.exit_kind"]
    if not math.isnan(target) and not math.isnan(entry) and risk and not math.isnan(risk):
        rr = abs(target - entry) / risk
    else:
        rr = exit_r if kind == "fixed_r" and exit_r > 0 else NAN
    q_mean, q_min, q_max = _quality(v["signal.quality"])
    bucket = v["market_state.clock.session_bucket"]
    family = str(v["signal.family"]).upper() if v["signal.family"] is not None else ""
    horizon = _num(v["geometry.expected_horizon_s"])
    out: dict[str, float] = {
        "direction": float(snap.direction),
        "risk_atr": _ratio(risk, atr),
        "rr_plan": rr,
        "exit_r": exit_r,
        "min_space_r": _num(v["geometry.min_space_r"]),
        "space_to_opp_r": _num(v["geometry.space_to_opposition_r"]),
        "spread_risk": _ratio(spread, risk),
        "spread_atr": _ratio(spread, atr),
        "atr_rel": _ratio(atr, entry),
        "realized_vol_atr": _ratio(_num(v["market_state.realized_vol"]), atr),
        "tick_activity": _num(v["market_state.tick_activity"]),
        "horizon_min": horizon / 60.0 if not math.isnan(horizon) else NAN,
        "local_minute": _num(v["market_state.clock.local_minute"]),
        "minutes_to_flat": _num(v["market_state.clock.minutes_to_forced_flat"]),
        "in_entry_window": _num(v["market_state.clock.in_entry_window"]),
        "confluence": _num(v["signal.confluence"]),
        "independent_clusters": _num(v["signal.independent_clusters"]),
        "quality": q_mean,
        "quality_min": q_min,
        "quality_max": q_max,
        "exit_kind_trail": 1.0 if kind == "trail" else 0.0,
    }
    for s in SESSION_BUCKETS:
        out[f"session_{s}"] = 1.0 if bucket == s else 0.0
    out["session_other"] = 0.0 if bucket in SESSION_BUCKETS else 1.0
    for f in FAMILIES:
        out[f"family_{f}"] = 1.0 if family == f else 0.0
    out["family_other"] = 0.0 if family in FAMILIES else 1.0
    for flag in STRUCTURE_FLAGS:
        out[f"struct_{flag}"] = _num(v[f"structure.{flag}"])
    for tf in TIMEFRAMES:
        out[f"ctx_{tf}_trend"] = _num(v[f"context.{tf}.trend"])
    return {k: out[k] for k in FEATURE_NAMES}


def feature_vector(features: dict[str, float]) -> list[float]:
    return [features[k] for k in FEATURE_NAMES]


def group_keys(snap: OpportunitySnapshot) -> dict[str, str]:
    """Cluster keys for the promotion gate (market / session / family). Descriptive only, not features."""
    return {
        "market": snap.market,
        "session": str(snap.market_state.clock.session_bucket),
        "family": str(snap.signal.get("family", "unknown")).upper(),
    }
