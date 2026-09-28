"""Configuration, inputs, and outputs for the clock/latency/health module.

This module is intentionally standalone: it has no dependency on
`src/pipeline`, `src/risk`, `src/execution`, or any other `src/` package, and
nothing here performs I/O or calls a venue API. It never calls an LLM or
remote reasoning service. It does not call `RiskEngine` and does not halt
anything itself -- `HealthEngine.evaluate` (see `health.engine`) only
produces a typed `HealthAssessment` that a caller (a later integration step)
can use to decide whether to signal "no new exposure" upstream, the same
standalone-module pattern already used by `src/costs`, `src/margin`,
`src/persistence`, and `src/exits`.

Duration/age precision policy (explicit, documented per this module's task,
"Decimal for any numeric latency/duration values that need exact
arithmetic ... percentiles can reasonably use Decimal or int microseconds --
your call, document it"): all *durations and ages* that participate in
staleness/drift comparisons are `timedelta` (matching
`ExitPolicy.max_market_data_age` and `ExitPolicy.max_holding_duration` in
`src/exits/models.py`), since callers already build thresholds this way and
`timedelta` has exact microsecond resolution with no floating-point error.
Latency *samples* recorded into the rolling window are stored as integer
microseconds (`int`) rather than `Decimal`: percentiles only ever select one
of the recorded sample values (nearest-rank method, see
`health.engine.compute_latency_stats`), never interpolate or average, so
integer microseconds keep that selection exact while staying cheap to store
and sort in a rolling window -- `Decimal` would add no precision here since
no arithmetic (only comparison/selection) is performed on the samples. The
percentile *result* type is `timedelta` again, for symmetry with every other
duration in this module's public API.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


def _require_utc_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC-aware")


def _require_non_negative_timedelta(name: str, value: timedelta) -> None:
    if value < timedelta(0):
        raise ValueError(f"{name} must be non-negative")


def _require_positive_timedelta(name: str, value: timedelta) -> None:
    if value <= timedelta(0):
        raise ValueError(f"{name} must be positive")


def _require_non_empty(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} must be non-empty")


class ComponentStatus(StrEnum):
    """Liveness classification of a single heartbeat-tracked component."""

    HEALTHY = "healthy"
    STALE = "stale"
    DISCONNECTED = "disconnected"


class HealthReason(StrEnum):
    """Machine-readable reason code contributing to an unhealthy assessment."""

    CLOCK_DRIFT_EXCEEDED = "CLOCK_DRIFT_EXCEEDED"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    SIGNAL_STALE = "SIGNAL_STALE"
    COMPONENT_STALE = "COMPONENT_STALE"
    COMPONENT_DISCONNECTED = "COMPONENT_DISCONNECTED"


class PipelineStage(StrEnum):
    """Named stage in the production hot path (see `docs/ARCHITECTURE.md`).

    Latency is measured between consecutive stages in this declared order:
    `MARKET_EVENT -> FEATURE -> SIGNAL -> RISK -> EXECUTION_REQUEST ->
    VENUE_ACK -> FILL`.
    """

    MARKET_EVENT = "market_event"
    FEATURE = "feature"
    SIGNAL = "signal"
    RISK = "risk"
    EXECUTION_REQUEST = "execution_request"
    VENUE_ACK = "venue_ack"
    FILL = "fill"


# Declared pipeline order; latency is only ever computed between a stage and
# the one immediately following it in this tuple.
PIPELINE_STAGE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.MARKET_EVENT,
    PipelineStage.FEATURE,
    PipelineStage.SIGNAL,
    PipelineStage.RISK,
    PipelineStage.EXECUTION_REQUEST,
    PipelineStage.VENUE_ACK,
    PipelineStage.FILL,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClockDriftPolicy:
    """Configured threshold for clock-drift detection.

    `max_drift` is the maximum absolute difference tolerated between a
    locally observed timestamp and an externally reported one (e.g. venue
    server time vs local wall clock) before drift is flagged.
    """

    max_drift: timedelta

    def __post_init__(self) -> None:
        _require_positive_timedelta("max_drift", self.max_drift)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClockDriftSample:
    """One clock-drift observation: a local timestamp vs an external one."""

    source: str
    local_observed_at: datetime
    external_reported_at: datetime

    def __post_init__(self) -> None:
        _require_non_empty("source", self.source)
        _require_utc_aware("local_observed_at", self.local_observed_at)
        _require_utc_aware("external_reported_at", self.external_reported_at)

    @property
    def drift(self) -> timedelta:
        """Absolute difference between the local and external timestamps."""
        delta = self.local_observed_at - self.external_reported_at
        return -delta if delta < timedelta(0) else delta


@dataclass(frozen=True, slots=True, kw_only=True)
class ClockDriftEvaluation:
    """Result of comparing a `ClockDriftSample` against a `ClockDriftPolicy`."""

    source: str
    drift: timedelta
    max_drift: timedelta
    exceeded: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class StalenessPolicy:
    """Configured max-age thresholds for market data and signal staleness."""

    max_market_data_age: timedelta
    max_signal_age: timedelta

    def __post_init__(self) -> None:
        _require_positive_timedelta("max_market_data_age", self.max_market_data_age)
        _require_positive_timedelta("max_signal_age", self.max_signal_age)


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketDataAge:
    """Age of the most recent market-data snapshot for one instrument."""

    instrument: str
    snapshot_observed_at: datetime
    as_of: datetime

    def __post_init__(self) -> None:
        _require_non_empty("instrument", self.instrument)
        _require_utc_aware("snapshot_observed_at", self.snapshot_observed_at)
        _require_utc_aware("as_of", self.as_of)
        if self.as_of < self.snapshot_observed_at:
            raise ValueError("as_of must not precede snapshot_observed_at")

    @property
    def age(self) -> timedelta:
        return self.as_of - self.snapshot_observed_at


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalAge:
    """Age of a signal by the time it would be acted on."""

    signal_id: str
    generated_at: datetime
    as_of: datetime

    def __post_init__(self) -> None:
        _require_non_empty("signal_id", self.signal_id)
        _require_utc_aware("generated_at", self.generated_at)
        _require_utc_aware("as_of", self.as_of)
        if self.as_of < self.generated_at:
            raise ValueError("as_of must not precede generated_at")

    @property
    def age(self) -> timedelta:
        return self.as_of - self.generated_at


@dataclass(frozen=True, slots=True, kw_only=True)
class StageTransitionSample:
    """One observed timing pair between two consecutive pipeline stages.

    `duration_micros` is the elapsed wall-clock time from `from_stage` to
    `to_stage` for a single event traversing the pipeline, in integer
    microseconds (see module docstring for the precision rationale).
    """

    from_stage: PipelineStage
    to_stage: PipelineStage
    duration_micros: int

    def __post_init__(self) -> None:
        if PIPELINE_STAGE_ORDER.index(self.to_stage) != (
            PIPELINE_STAGE_ORDER.index(self.from_stage) + 1
        ):
            raise ValueError(
                f"{self.to_stage} must immediately follow {self.from_stage} "
                "in PIPELINE_STAGE_ORDER"
            )
        if self.duration_micros < 0:
            raise ValueError("duration_micros must be non-negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class LatencyStats:
    """p50/p95/p99 latency for one stage transition over a sample window."""

    from_stage: PipelineStage
    to_stage: PipelineStage
    sample_count: int
    p50: timedelta
    p95: timedelta
    p99: timedelta

    def __post_init__(self) -> None:
        if self.sample_count <= 0:
            raise ValueError("sample_count must be positive")
        for name in ("p50", "p95", "p99"):
            _require_non_negative_timedelta(name, getattr(self, name))


@dataclass(frozen=True, slots=True, kw_only=True)
class HeartbeatPolicy:
    """Configured max-age thresholds for one heartbeat-tracked component.

    A component is `HEALTHY` when its last heartbeat age is within
    `max_healthy_age`, `STALE` when older than that but within
    `max_disconnected_age`, and `DISCONNECTED` once older than
    `max_disconnected_age`.
    """

    component: str
    max_healthy_age: timedelta
    max_disconnected_age: timedelta

    def __post_init__(self) -> None:
        _require_non_empty("component", self.component)
        _require_positive_timedelta("max_healthy_age", self.max_healthy_age)
        _require_positive_timedelta("max_disconnected_age", self.max_disconnected_age)
        if self.max_disconnected_age <= self.max_healthy_age:
            raise ValueError("max_disconnected_age must exceed max_healthy_age")


@dataclass(frozen=True, slots=True, kw_only=True)
class Heartbeat:
    """Last-seen timestamp for one named component/feed."""

    component: str
    last_seen_at: datetime

    def __post_init__(self) -> None:
        _require_non_empty("component", self.component)
        _require_utc_aware("last_seen_at", self.last_seen_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentHealth:
    """Evaluated liveness of one component as of a given moment."""

    component: str
    status: ComponentStatus
    age: timedelta


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthAssessment:
    """Aggregate health/HALT-propagation result for a single evaluation.

    This is the only output a caller needs to decide whether to signal
    "NO NEW EXPOSURE" upstream (see `docs/ARCHITECTURE.md` "Safety state
    machine" -- new exposure is allowed only in `READY`; this module does
    not itself read or write that state machine, it only reports facts).
    """

    as_of: datetime
    reasons: tuple[HealthReason, ...]
    component_health: tuple[ComponentHealth, ...]

    def __post_init__(self) -> None:
        _require_utc_aware("as_of", self.as_of)

    @property
    def is_healthy(self) -> bool:
        return len(self.reasons) == 0

    @property
    def block_new_exposure(self) -> bool:
        """True whenever any unhealthy condition is present.

        Named to mirror `docs/ARCHITECTURE.md`'s "NEW_EXPOSURE" language so a
        caller can read this field name directly at the integration point
        without translating "is_healthy" into halt semantics itself.
        """
        return not self.is_healthy
