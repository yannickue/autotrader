"""Standalone clock/latency/health monitoring module.

Public API:
    `health.models.ClockDriftPolicy`, `ClockDriftSample`,
        `ClockDriftEvaluation` -- clock-drift configuration, one drift
        observation, and its evaluated result.
    `health.models.StalenessPolicy`, `MarketDataAge`, `SignalAge` --
        market-data/signal age configuration and observations.
    `health.models.PipelineStage`, `PIPELINE_STAGE_ORDER`,
        `StageTransitionSample`, `LatencyStats` -- named pipeline stages and
        p50/p95/p99 latency between consecutive stages.
    `health.models.HeartbeatPolicy`, `Heartbeat`, `ComponentStatus`,
        `ComponentHealth` -- per-component heartbeat tracking and
        healthy/stale/disconnected classification.
    `health.models.HealthReason`, `HealthAssessment` -- the aggregate
        health/HALT-propagation result.
    `health.engine.evaluate_clock_drift`, `is_market_data_stale`,
        `is_signal_stale`, `evaluate_heartbeat`, `compute_latency_stats` --
        pure evaluation functions.
    `health.engine.HealthEngine.evaluate(...) -> HealthAssessment` --
        aggregates already-evaluated inputs into one typed result.

This module has no dependency on `src/pipeline`, `src/risk`, `src/execution`,
or any other `src/` package and performs no venue I/O; it is independently
testable and is wired into the pipeline by a later integration step. It does
not itself call `RiskEngine` or halt anything -- see `health.models` and
`health.engine` module docstrings.
"""

from health.engine import (
    HealthEngine,
    compute_latency_stats,
    evaluate_clock_drift,
    evaluate_heartbeat,
    is_market_data_stale,
    is_signal_stale,
)
from health.models import (
    PIPELINE_STAGE_ORDER,
    ClockDriftEvaluation,
    ClockDriftPolicy,
    ClockDriftSample,
    ComponentHealth,
    ComponentStatus,
    HealthAssessment,
    HealthReason,
    Heartbeat,
    HeartbeatPolicy,
    LatencyStats,
    MarketDataAge,
    PipelineStage,
    SignalAge,
    StageTransitionSample,
    StalenessPolicy,
)

__all__ = [
    "PIPELINE_STAGE_ORDER",
    "ClockDriftEvaluation",
    "ClockDriftPolicy",
    "ClockDriftSample",
    "ComponentHealth",
    "ComponentStatus",
    "HealthAssessment",
    "HealthEngine",
    "HealthReason",
    "Heartbeat",
    "HeartbeatPolicy",
    "LatencyStats",
    "MarketDataAge",
    "PipelineStage",
    "SignalAge",
    "StageTransitionSample",
    "StalenessPolicy",
    "compute_latency_stats",
    "evaluate_clock_drift",
    "evaluate_heartbeat",
    "is_market_data_stale",
    "is_signal_stale",
]
