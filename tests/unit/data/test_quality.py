"""Tests for `data.quality` violation detection.

Detection only: these tests assert violations are *reported*, never that the
input records are mutated or "fixed".
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from data.parquet_store import BarRecord, TickRecord
from data.provenance import SemanticType, Source
from data.quality import QualityConfig, ViolationType, check_bars, check_ticks

T0 = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)


def _tick(
    *,
    ts: datetime,
    bid: Decimal = Decimal("1.0"),
    ask: Decimal = Decimal("1.001"),
    last: Decimal | None = Decimal("1.0005"),
    volume: Decimal = Decimal("1"),
) -> TickRecord:
    return TickRecord(
        canonical_symbol="DAX",
        broker_symbol="GER40.cash.fixture",
        timestamp=ts,
        bid=bid,
        ask=ask,
        last=last,
        volume=volume,
        volume_semantic_type=SemanticType.BROKER_TICK_ACTIVITY,
        source=Source.ACTIVTRADES_MT5_CFD,
        ingested_at=T0,
    )


def _bar(
    *,
    ts: datetime,
    open_: Decimal = Decimal("100"),
    high: Decimal = Decimal("101"),
    low: Decimal = Decimal("99"),
    close: Decimal = Decimal("100.5"),
    volume: Decimal = Decimal("10"),
) -> BarRecord:
    return BarRecord(
        canonical_symbol="DAX",
        broker_symbol="GER40.cash.fixture",
        timestamp=ts,
        timeframe="1m",
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        volume_semantic_type=SemanticType.BROKER_TICK_ACTIVITY,
        source=Source.ACTIVTRADES_MT5_CFD,
        ingested_at=T0,
    )


def test_clean_tick_batch_reports_zero_violations() -> None:
    records = [_tick(ts=T0), _tick(ts=T0 + timedelta(seconds=1))]
    report = check_ticks(records, now=T0 + timedelta(seconds=2))
    assert report.is_clean
    assert report.violations == ()


def test_clean_bar_batch_reports_zero_violations() -> None:
    records = [_bar(ts=T0), _bar(ts=T0 + timedelta(minutes=1))]
    report = check_bars(records, now=T0 + timedelta(minutes=2))
    assert report.is_clean


def test_non_monotonic_timestamp_detected() -> None:
    records = [_tick(ts=T0), _tick(ts=T0 - timedelta(seconds=5))]
    report = check_ticks(records)
    assert not report.is_clean
    assert len(report.of_type(ViolationType.NON_MONOTONIC_TIMESTAMP)) == 1


def test_duplicate_timestamp_detected() -> None:
    records = [_tick(ts=T0), _tick(ts=T0)]
    report = check_ticks(records)
    assert len(report.of_type(ViolationType.DUPLICATE_RECORD)) == 1


def test_gap_detected() -> None:
    config = QualityConfig(max_gap=timedelta(seconds=10))
    records = [_tick(ts=T0), _tick(ts=T0 + timedelta(minutes=5))]
    report = check_ticks(records, config=config)
    assert len(report.of_type(ViolationType.GAP_DETECTED)) == 1


def test_stale_data_detected() -> None:
    config = QualityConfig(max_age=timedelta(minutes=1))
    records = [_tick(ts=T0)]
    report = check_ticks(records, config=config, now=T0 + timedelta(hours=1))
    assert len(report.of_type(ViolationType.STALE_DATA)) == 1


def test_zero_or_negative_price_detected() -> None:
    records = [_tick(ts=T0, bid=Decimal("0"))]
    report = check_ticks(records)
    assert len(report.of_type(ViolationType.ZERO_OR_NEGATIVE_PRICE)) >= 1


def test_non_finite_value_detected() -> None:
    records = [_tick(ts=T0, bid=Decimal("NaN"))]
    report = check_ticks(records)
    assert len(report.of_type(ViolationType.NON_FINITE_VALUE)) >= 1


def test_crossed_quote_detected() -> None:
    records = [_tick(ts=T0, bid=Decimal("1.01"), ask=Decimal("1.00"))]
    report = check_ticks(records)
    assert len(report.of_type(ViolationType.CROSSED_QUOTE)) == 1


def test_absurd_spread_detected() -> None:
    config = QualityConfig(max_spread_ratio=Decimal("0.01"))
    records = [_tick(ts=T0, bid=Decimal("1.0"), ask=Decimal("1.5"))]
    report = check_ticks(records, config=config)
    assert len(report.of_type(ViolationType.ABSURD_SPREAD)) == 1


def test_malformed_volume_detected() -> None:
    records = [_tick(ts=T0, volume=Decimal("-1"))]
    report = check_ticks(records)
    assert len(report.of_type(ViolationType.MALFORMED_VOLUME)) == 1


def test_bar_crossed_high_low_detected() -> None:
    records = [_bar(ts=T0, high=Decimal("90"), low=Decimal("100"))]
    report = check_bars(records)
    assert len(report.of_type(ViolationType.CROSSED_QUOTE)) == 1


def test_bar_open_outside_range_detected() -> None:
    records = [_bar(ts=T0, open_=Decimal("200"), high=Decimal("101"), low=Decimal("99"))]
    report = check_bars(records)
    assert len(report.of_type(ViolationType.CROSSED_QUOTE)) == 1


def test_detection_does_not_mutate_input_records() -> None:
    records = [_tick(ts=T0, bid=Decimal("0"))]
    original_bid = records[0].bid
    check_ticks(records)
    assert records[0].bid == original_bid
