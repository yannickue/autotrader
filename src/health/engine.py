"""Standalone clock/latency/health monitoring engine.

Every function here is pure and deterministic: it reads externally supplied,
already-observed timestamps and samples and returns a typed result. Nothing
in this module performs I/O, calls a venue API, or depends on an LLM or
remote reasoning service, matching `src/costs`, `src/margin`,
`src/persistence`, and `src/exits`. `HealthEngine.evaluate` does not call
`RiskEngine` and does not halt anything itself -- it only produces a
`HealthAssessment` a caller can act on (see `health.models` module
docstring and `docs/ARCHITECTURE.md` "Safety state machine").
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from health.models import (
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
    SignalAge,
    StageTransitionSample,
    StalenessPolicy,
    _require_utc_aware,
)


def evaluate_clock_drift(
    sample: ClockDriftSample, policy: ClockDriftPolicy
) -> ClockDriftEvaluation:
    """Compare one clock-drift sample against the configured threshold."""
    drift = sample.drift
    return ClockDriftEvaluation(
        source=sample.source,
        drift=drift,
        max_drift=policy.max_drift,
        exceeded=drift > policy.max_drift,
    )


def is_market_data_stale(age: MarketDataAge, policy: StalenessPolicy) -> bool:
    """True when the instrument's most recent snapshot is too old to trust."""
    return age.age > policy.max_market_data_age


def is_signal_stale(age: SignalAge, policy: StalenessPolicy) -> bool:
    """True when a signal is too old to still be safely acted on."""
    return age.age > policy.max_signal_age


def evaluate_heartbeat(
    heartbeat: Heartbeat, policy: HeartbeatPolicy, *, as_of: datetime
) -> ComponentHealth:
    """Classify one component's liveness as of `as_of` (UTC-aware datetime)."""
    _require_utc_aware("as_of", as_of)
    if heartbeat.component != policy.component:
        raise ValueError(
            f"heartbeat component {heartbeat.component!r} does not match "
            f"policy component {policy.component!r}"
        )
    age = as_of - heartbeat.last_seen_at
    if age < timedelta(0):
        raise ValueError("as_of must not precede heartbeat.last_seen_at")
    if age <= policy.max_healthy_age:
        status = ComponentStatus.HEALTHY
    elif age <= policy.max_disconnected_age:
        status = ComponentStatus.STALE
    else:
        status = ComponentStatus.DISCONNECTED
    return ComponentHealth(component=heartbeat.component, status=status, age=age)


def _percentile_micros(sorted_samples: list[int], fraction: float) -> int:
    """Nearest-rank percentile: index = ceil(fraction * n) - 1, clamped.

    This selects an actual recorded sample value rather than interpolating
    between two samples, matching the "exact, verifiable" requirement for
    latency percentiles documented in `health.models`.
    """
    n = len(sorted_samples)
    rank = max(1, min(math.ceil(fraction * n), n))
    return sorted_samples[rank - 1]


def compute_latency_stats(samples: list[StageTransitionSample]) -> LatencyStats:
    """Compute p50/p95/p99 latency from a rolling window of stage samples.

    All samples must share the same `(from_stage, to_stage)` pair -- mixing
    transitions would make the percentiles meaningless. Uses the
    nearest-rank method (see `_percentile_micros`), so every returned
    percentile is one of the recorded sample durations, never an
    interpolation.
    """
    if not samples:
        raise ValueError("samples must be non-empty")
    from_stage = samples[0].from_stage
    to_stage = samples[0].to_stage
    for sample in samples:
        if sample.from_stage != from_stage or sample.to_stage != to_stage:
            raise ValueError("all samples must share the same from_stage/to_stage pair")
    durations = sorted(sample.duration_micros for sample in samples)
    return LatencyStats(
        from_stage=from_stage,
        to_stage=to_stage,
        sample_count=len(durations),
        p50=timedelta(microseconds=_percentile_micros(durations, 0.50)),
        p95=timedelta(microseconds=_percentile_micros(durations, 0.95)),
        p99=timedelta(microseconds=_percentile_micros(durations, 0.99)),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthEngine:
    """Aggregates clock-drift, staleness, and heartbeat checks into one
    deterministic `HealthAssessment`.

    Pure and stateless: every `evaluate` call takes its full input as
    arguments and returns a fresh result, so this class holds no mutable
    state and is safe to share across callers.
    """

    def evaluate(
        self,
        *,
        as_of: datetime,
        clock_drift_evaluations: tuple[ClockDriftEvaluation, ...] = (),
        market_data_ages: tuple[MarketDataAge, ...] = (),
        staleness_policy: StalenessPolicy | None = None,
        signal_ages: tuple[SignalAge, ...] = (),
        component_health: tuple[ComponentHealth, ...] = (),
    ) -> HealthAssessment:
        """Combine already-evaluated inputs into one aggregate assessment.

        This method does not itself fetch heartbeats, samples, or compute
        drift/staleness -- callers run `evaluate_clock_drift`,
        `is_market_data_stale`/`is_signal_stale`, and `evaluate_heartbeat`
        first and pass the results in, keeping this aggregation step a
        simple, auditable reduction with no hidden I/O or timing coupling.
        """
        reasons: list[HealthReason] = []

        for evaluation in clock_drift_evaluations:
            if evaluation.exceeded:
                reasons.append(HealthReason.CLOCK_DRIFT_EXCEEDED)
                break

        if staleness_policy is not None:
            if any(is_market_data_stale(age, staleness_policy) for age in market_data_ages):
                reasons.append(HealthReason.MARKET_DATA_STALE)
            if any(is_signal_stale(age, staleness_policy) for age in signal_ages):
                reasons.append(HealthReason.SIGNAL_STALE)

        if any(c.status is ComponentStatus.STALE for c in component_health):
            reasons.append(HealthReason.COMPONENT_STALE)
        if any(c.status is ComponentStatus.DISCONNECTED for c in component_health):
            reasons.append(HealthReason.COMPONENT_DISCONNECTED)

        return HealthAssessment(
            as_of=as_of,
            reasons=tuple(reasons),
            component_health=component_health,
        )
