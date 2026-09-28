"""Unit tests for `health.models` validation."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from health.models import (
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

UTC_NOW = datetime(2026, 1, 1, tzinfo=UTC)
NAIVE_NOW = datetime(2026, 1, 1)
NON_UTC_NOW = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))


class TestUtcValidation:
    """Every timestamp-bearing model must reject naive/non-UTC datetimes."""

    def test_clock_drift_sample_rejects_naive_local(self):
        with pytest.raises(ValueError, match="UTC-aware"):
            ClockDriftSample(
                source="venue", local_observed_at=NAIVE_NOW, external_reported_at=UTC_NOW
            )

    def test_clock_drift_sample_rejects_non_utc_external(self):
        with pytest.raises(ValueError, match="UTC-aware"):
            ClockDriftSample(
                source="venue", local_observed_at=UTC_NOW, external_reported_at=NON_UTC_NOW
            )

    def test_clock_drift_sample_accepts_utc_aware(self):
        sample = ClockDriftSample(
            source="venue", local_observed_at=UTC_NOW, external_reported_at=UTC_NOW
        )
        assert sample.drift == timedelta(0)

    def test_market_data_age_rejects_naive(self):
        with pytest.raises(ValueError, match="UTC-aware"):
            MarketDataAge(instrument="BTCUSDT", snapshot_observed_at=NAIVE_NOW, as_of=UTC_NOW)

    def test_market_data_age_rejects_as_of_before_snapshot(self):
        with pytest.raises(ValueError, match="must not precede"):
            MarketDataAge(
                instrument="BTCUSDT",
                snapshot_observed_at=UTC_NOW,
                as_of=UTC_NOW - timedelta(seconds=1),
            )

    def test_signal_age_rejects_naive(self):
        with pytest.raises(ValueError, match="UTC-aware"):
            SignalAge(signal_id="sig-1", generated_at=NAIVE_NOW, as_of=UTC_NOW)

    def test_signal_age_rejects_as_of_before_generated(self):
        with pytest.raises(ValueError, match="must not precede"):
            SignalAge(
                signal_id="sig-1", generated_at=UTC_NOW, as_of=UTC_NOW - timedelta(seconds=1)
            )

    def test_heartbeat_rejects_non_utc(self):
        with pytest.raises(ValueError, match="UTC-aware"):
            Heartbeat(component="feed", last_seen_at=NON_UTC_NOW)

    def test_health_assessment_rejects_naive_as_of(self):
        with pytest.raises(ValueError, match="UTC-aware"):
            HealthAssessment(as_of=NAIVE_NOW, reasons=(), component_health=())

    def test_health_assessment_accepts_utc_as_of(self):
        assessment = HealthAssessment(as_of=UTC_NOW, reasons=(), component_health=())
        assert assessment.is_healthy


class TestPolicyValidation:
    def test_clock_drift_policy_rejects_non_positive_max_drift(self):
        with pytest.raises(ValueError, match="positive"):
            ClockDriftPolicy(max_drift=timedelta(0))

    def test_staleness_policy_rejects_non_positive_thresholds(self):
        with pytest.raises(ValueError, match="positive"):
            StalenessPolicy(
                max_market_data_age=timedelta(0), max_signal_age=timedelta(seconds=1)
            )

    def test_heartbeat_policy_requires_disconnected_after_healthy(self):
        with pytest.raises(ValueError, match="must exceed"):
            HeartbeatPolicy(
                component="feed",
                max_healthy_age=timedelta(seconds=10),
                max_disconnected_age=timedelta(seconds=10),
            )

    def test_heartbeat_policy_rejects_empty_component(self):
        with pytest.raises(ValueError, match="non-empty"):
            HeartbeatPolicy(
                component="",
                max_healthy_age=timedelta(seconds=10),
                max_disconnected_age=timedelta(seconds=30),
            )


class TestStageTransitionSample:
    def test_rejects_non_consecutive_stages(self):
        with pytest.raises(ValueError, match="must immediately follow"):
            StageTransitionSample(
                from_stage=PipelineStage.MARKET_EVENT,
                to_stage=PipelineStage.SIGNAL,
                duration_micros=100,
            )

    def test_rejects_negative_duration(self):
        with pytest.raises(ValueError, match="non-negative"):
            StageTransitionSample(
                from_stage=PipelineStage.MARKET_EVENT,
                to_stage=PipelineStage.FEATURE,
                duration_micros=-1,
            )

    def test_accepts_consecutive_stages(self):
        sample = StageTransitionSample(
            from_stage=PipelineStage.SIGNAL, to_stage=PipelineStage.RISK, duration_micros=50
        )
        assert sample.duration_micros == 50


class TestLatencyStats:
    def test_rejects_non_positive_sample_count(self):
        with pytest.raises(ValueError, match="positive"):
            LatencyStats(
                from_stage=PipelineStage.MARKET_EVENT,
                to_stage=PipelineStage.FEATURE,
                sample_count=0,
                p50=timedelta(0),
                p95=timedelta(0),
                p99=timedelta(0),
            )


class TestHealthAssessmentProperties:
    def test_block_new_exposure_true_when_reasons_present(self):
        assessment = HealthAssessment(
            as_of=UTC_NOW, reasons=(HealthReason.MARKET_DATA_STALE,), component_health=()
        )
        assert assessment.block_new_exposure
        assert not assessment.is_healthy

    def test_block_new_exposure_false_when_no_reasons(self):
        assessment = HealthAssessment(as_of=UTC_NOW, reasons=(), component_health=())
        assert not assessment.block_new_exposure
        assert assessment.is_healthy

    def test_component_health_status_values(self):
        healthy = ComponentHealth(
            component="feed", status=ComponentStatus.HEALTHY, age=timedelta(seconds=1)
        )
        assert healthy.status is ComponentStatus.HEALTHY
