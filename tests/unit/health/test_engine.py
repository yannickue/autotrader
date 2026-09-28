"""Unit tests for `health.engine`."""

from datetime import UTC, datetime, timedelta

import pytest

from health.engine import (
    HealthEngine,
    compute_latency_stats,
    evaluate_clock_drift,
    evaluate_heartbeat,
    is_market_data_stale,
    is_signal_stale,
)
from health.models import (
    ClockDriftPolicy,
    ClockDriftSample,
    ComponentHealth,
    ComponentStatus,
    HealthReason,
    Heartbeat,
    HeartbeatPolicy,
    MarketDataAge,
    PipelineStage,
    SignalAge,
    StageTransitionSample,
    StalenessPolicy,
)

UTC_NOW = datetime(2026, 1, 1, tzinfo=UTC)


class TestClockDrift:
    def test_drift_under_threshold_not_exceeded(self):
        policy = ClockDriftPolicy(max_drift=timedelta(milliseconds=500))
        sample = ClockDriftSample(
            source="venue",
            local_observed_at=UTC_NOW,
            external_reported_at=UTC_NOW - timedelta(milliseconds=100),
        )
        result = evaluate_clock_drift(sample, policy)
        assert result.exceeded is False
        assert result.drift == timedelta(milliseconds=100)

    def test_drift_at_threshold_not_exceeded(self):
        policy = ClockDriftPolicy(max_drift=timedelta(milliseconds=500))
        sample = ClockDriftSample(
            source="venue",
            local_observed_at=UTC_NOW,
            external_reported_at=UTC_NOW - timedelta(milliseconds=500),
        )
        result = evaluate_clock_drift(sample, policy)
        assert result.exceeded is False

    def test_drift_over_threshold_exceeded(self):
        policy = ClockDriftPolicy(max_drift=timedelta(milliseconds=500))
        sample = ClockDriftSample(
            source="venue",
            local_observed_at=UTC_NOW,
            external_reported_at=UTC_NOW - timedelta(milliseconds=501),
        )
        result = evaluate_clock_drift(sample, policy)
        assert result.exceeded is True

    def test_drift_is_symmetric_absolute_value(self):
        policy = ClockDriftPolicy(max_drift=timedelta(milliseconds=500))
        sample = ClockDriftSample(
            source="venue",
            local_observed_at=UTC_NOW - timedelta(milliseconds=501),
            external_reported_at=UTC_NOW,
        )
        result = evaluate_clock_drift(sample, policy)
        assert result.exceeded is True
        assert result.drift == timedelta(milliseconds=501)


class TestStaleness:
    def test_market_data_stale_over_threshold(self):
        policy = StalenessPolicy(
            max_market_data_age=timedelta(seconds=5), max_signal_age=timedelta(seconds=5)
        )
        age = MarketDataAge(
            instrument="BTCUSDT",
            snapshot_observed_at=UTC_NOW - timedelta(seconds=6),
            as_of=UTC_NOW,
        )
        assert is_market_data_stale(age, policy) is True

    def test_market_data_not_stale_at_threshold(self):
        policy = StalenessPolicy(
            max_market_data_age=timedelta(seconds=5), max_signal_age=timedelta(seconds=5)
        )
        age = MarketDataAge(
            instrument="BTCUSDT",
            snapshot_observed_at=UTC_NOW - timedelta(seconds=5),
            as_of=UTC_NOW,
        )
        assert is_market_data_stale(age, policy) is False

    def test_market_data_not_stale_under_threshold(self):
        policy = StalenessPolicy(
            max_market_data_age=timedelta(seconds=5), max_signal_age=timedelta(seconds=5)
        )
        age = MarketDataAge(
            instrument="BTCUSDT",
            snapshot_observed_at=UTC_NOW - timedelta(seconds=1),
            as_of=UTC_NOW,
        )
        assert is_market_data_stale(age, policy) is False

    def test_signal_stale_over_threshold(self):
        policy = StalenessPolicy(
            max_market_data_age=timedelta(seconds=5), max_signal_age=timedelta(seconds=2)
        )
        age = SignalAge(
            signal_id="sig-1", generated_at=UTC_NOW - timedelta(seconds=3), as_of=UTC_NOW
        )
        assert is_signal_stale(age, policy) is True

    def test_signal_not_stale_under_threshold(self):
        policy = StalenessPolicy(
            max_market_data_age=timedelta(seconds=5), max_signal_age=timedelta(seconds=2)
        )
        age = SignalAge(
            signal_id="sig-1", generated_at=UTC_NOW - timedelta(seconds=1), as_of=UTC_NOW
        )
        assert is_signal_stale(age, policy) is False


class TestHeartbeat:
    POLICY = HeartbeatPolicy(
        component="binance_ws",
        max_healthy_age=timedelta(seconds=10),
        max_disconnected_age=timedelta(seconds=30),
    )

    def test_healthy_within_max_healthy_age(self):
        heartbeat = Heartbeat(component="binance_ws", last_seen_at=UTC_NOW - timedelta(seconds=5))
        result = evaluate_heartbeat(heartbeat, self.POLICY, as_of=UTC_NOW)
        assert result.status is ComponentStatus.HEALTHY

    def test_healthy_at_exact_boundary(self):
        heartbeat = Heartbeat(
            component="binance_ws", last_seen_at=UTC_NOW - timedelta(seconds=10)
        )
        result = evaluate_heartbeat(heartbeat, self.POLICY, as_of=UTC_NOW)
        assert result.status is ComponentStatus.HEALTHY

    def test_stale_between_healthy_and_disconnected(self):
        heartbeat = Heartbeat(
            component="binance_ws", last_seen_at=UTC_NOW - timedelta(seconds=20)
        )
        result = evaluate_heartbeat(heartbeat, self.POLICY, as_of=UTC_NOW)
        assert result.status is ComponentStatus.STALE

    def test_stale_at_exact_disconnected_boundary(self):
        heartbeat = Heartbeat(
            component="binance_ws", last_seen_at=UTC_NOW - timedelta(seconds=30)
        )
        result = evaluate_heartbeat(heartbeat, self.POLICY, as_of=UTC_NOW)
        assert result.status is ComponentStatus.STALE

    def test_disconnected_past_max_disconnected_age(self):
        heartbeat = Heartbeat(
            component="binance_ws", last_seen_at=UTC_NOW - timedelta(seconds=31)
        )
        result = evaluate_heartbeat(heartbeat, self.POLICY, as_of=UTC_NOW)
        assert result.status is ComponentStatus.DISCONNECTED

    def test_mismatched_component_rejected(self):
        heartbeat = Heartbeat(component="other_feed", last_seen_at=UTC_NOW)
        with pytest.raises(ValueError, match="does not match"):
            evaluate_heartbeat(heartbeat, self.POLICY, as_of=UTC_NOW)

    def test_as_of_before_last_seen_rejected(self):
        heartbeat = Heartbeat(component="binance_ws", last_seen_at=UTC_NOW)
        with pytest.raises(ValueError, match="must not precede"):
            evaluate_heartbeat(
                heartbeat, self.POLICY, as_of=UTC_NOW - timedelta(seconds=1)
            )


class TestLatencyPercentiles:
    """Uses an exact, verifiable distribution: 100 samples, 1..100 microseconds.

    With the nearest-rank method (rank = ceil(fraction * n), 1-indexed into
    the sorted samples):
      p50 -> rank ceil(0.50 * 100) = 50 -> sorted[49] -> value 50
      p95 -> rank ceil(0.95 * 100) = 95 -> sorted[94] -> value 95
      p99 -> rank ceil(0.99 * 100) = 99 -> sorted[98] -> value 99
    """

    def _samples(self) -> list[StageTransitionSample]:
        # Intentionally unsorted insertion order to prove the engine sorts.
        durations = list(range(100, 0, -1))
        return [
            StageTransitionSample(
                from_stage=PipelineStage.SIGNAL,
                to_stage=PipelineStage.RISK,
                duration_micros=d,
            )
            for d in durations
        ]

    def test_percentiles_match_known_distribution(self):
        stats = compute_latency_stats(self._samples())
        assert stats.sample_count == 100
        assert stats.p50 == timedelta(microseconds=50)
        assert stats.p95 == timedelta(microseconds=95)
        assert stats.p99 == timedelta(microseconds=99)
        assert stats.from_stage is PipelineStage.SIGNAL
        assert stats.to_stage is PipelineStage.RISK

    def test_single_sample_all_percentiles_equal_it(self):
        samples = [
            StageTransitionSample(
                from_stage=PipelineStage.MARKET_EVENT,
                to_stage=PipelineStage.FEATURE,
                duration_micros=42,
            )
        ]
        stats = compute_latency_stats(samples)
        assert stats.p50 == stats.p95 == stats.p99 == timedelta(microseconds=42)

    def test_empty_samples_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            compute_latency_stats([])

    def test_mixed_stage_pairs_rejected(self):
        samples = [
            StageTransitionSample(
                from_stage=PipelineStage.MARKET_EVENT,
                to_stage=PipelineStage.FEATURE,
                duration_micros=1,
            ),
            StageTransitionSample(
                from_stage=PipelineStage.FEATURE,
                to_stage=PipelineStage.SIGNAL,
                duration_micros=2,
            ),
        ]
        with pytest.raises(ValueError, match="same from_stage/to_stage"):
            compute_latency_stats(samples)


class TestHealthEngineAggregate:
    ENGINE = HealthEngine()

    def test_healthy_when_no_unhealthy_conditions(self):
        assessment = self.ENGINE.evaluate(
            as_of=UTC_NOW,
            clock_drift_evaluations=(),
            market_data_ages=(),
            staleness_policy=None,
            signal_ages=(),
            component_health=(
                ComponentHealth(
                    component="binance_ws",
                    status=ComponentStatus.HEALTHY,
                    age=timedelta(seconds=1),
                ),
            ),
        )
        assert assessment.is_healthy
        assert assessment.block_new_exposure is False
        assert assessment.reasons == ()

    def test_blocks_on_clock_drift(self):
        policy = ClockDriftPolicy(max_drift=timedelta(milliseconds=100))
        sample = ClockDriftSample(
            source="venue",
            local_observed_at=UTC_NOW,
            external_reported_at=UTC_NOW - timedelta(seconds=1),
        )
        evaluation = evaluate_clock_drift(sample, policy)
        assessment = self.ENGINE.evaluate(
            as_of=UTC_NOW, clock_drift_evaluations=(evaluation,)
        )
        assert assessment.block_new_exposure is True
        assert HealthReason.CLOCK_DRIFT_EXCEEDED in assessment.reasons

    def test_blocks_on_stale_market_data(self):
        policy = StalenessPolicy(
            max_market_data_age=timedelta(seconds=5), max_signal_age=timedelta(seconds=5)
        )
        stale_age = MarketDataAge(
            instrument="BTCUSDT",
            snapshot_observed_at=UTC_NOW - timedelta(seconds=10),
            as_of=UTC_NOW,
        )
        assessment = self.ENGINE.evaluate(
            as_of=UTC_NOW,
            market_data_ages=(stale_age,),
            staleness_policy=policy,
        )
        assert assessment.block_new_exposure is True
        assert HealthReason.MARKET_DATA_STALE in assessment.reasons

    def test_blocks_on_stale_signal(self):
        policy = StalenessPolicy(
            max_market_data_age=timedelta(seconds=5), max_signal_age=timedelta(seconds=1)
        )
        stale_signal = SignalAge(
            signal_id="sig-1", generated_at=UTC_NOW - timedelta(seconds=5), as_of=UTC_NOW
        )
        assessment = self.ENGINE.evaluate(
            as_of=UTC_NOW,
            signal_ages=(stale_signal,),
            staleness_policy=policy,
        )
        assert assessment.block_new_exposure is True
        assert HealthReason.SIGNAL_STALE in assessment.reasons

    def test_blocks_on_component_stale(self):
        assessment = self.ENGINE.evaluate(
            as_of=UTC_NOW,
            component_health=(
                ComponentHealth(
                    component="binance_ws",
                    status=ComponentStatus.STALE,
                    age=timedelta(seconds=20),
                ),
            ),
        )
        assert assessment.block_new_exposure is True
        assert HealthReason.COMPONENT_STALE in assessment.reasons

    def test_blocks_on_component_disconnected(self):
        assessment = self.ENGINE.evaluate(
            as_of=UTC_NOW,
            component_health=(
                ComponentHealth(
                    component="binance_ws",
                    status=ComponentStatus.DISCONNECTED,
                    age=timedelta(seconds=60),
                ),
            ),
        )
        assert assessment.block_new_exposure is True
        assert HealthReason.COMPONENT_DISCONNECTED in assessment.reasons

    def test_multiple_reasons_all_reported(self):
        policy = StalenessPolicy(
            max_market_data_age=timedelta(seconds=5), max_signal_age=timedelta(seconds=5)
        )
        stale_age = MarketDataAge(
            instrument="BTCUSDT",
            snapshot_observed_at=UTC_NOW - timedelta(seconds=10),
            as_of=UTC_NOW,
        )
        assessment = self.ENGINE.evaluate(
            as_of=UTC_NOW,
            market_data_ages=(stale_age,),
            staleness_policy=policy,
            component_health=(
                ComponentHealth(
                    component="binance_ws",
                    status=ComponentStatus.DISCONNECTED,
                    age=timedelta(seconds=60),
                ),
            ),
        )
        assert HealthReason.MARKET_DATA_STALE in assessment.reasons
        assert HealthReason.COMPONENT_DISCONNECTED in assessment.reasons
        assert assessment.block_new_exposure is True
