"""C3 validation + deterministic Parquet dataset roundtrip (synthetic data, tmp fs)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from data.historical import (
    DatasetIntegrityError,
    DatasetRejected,
    read_bar_dataset,
    read_tick_dataset,
    write_bar_dataset,
    write_tick_dataset,
)
from data.historical_quality import (
    HistoricalIssue,
    ValidationStatus,
    validate_bars,
    validate_ticks,
)
from data.parquet_store import BarRecord, TickRecord
from data.provenance import SemanticType, Source

RETRIEVED = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
TZ_POLICY = {"zone": "Europe/Berlin", "basis": "test"}
# 2026-09-22 (Tue) 08:00 UTC = 10:00 server (CEST).
T0 = datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


def bar(i: int, **kw) -> BarRecord:
    base = {
        "canonical_symbol": "GER40",
        "broker_symbol": "Ger40",
        "timestamp": T0 + timedelta(minutes=5 * i),
        "timeframe": "5m",
        "open": Decimal("100.00"),
        "high": Decimal("101.00"),
        "low": Decimal("99.00"),
        "close": Decimal("100.50"),
        "volume": Decimal(10),
        "volume_semantic_type": SemanticType.BROKER_TICK_ACTIVITY,
        "source": Source.ACTIVTRADES_MT5_CFD,
        "ingested_at": RETRIEVED,
        "spread_points": 155,
    }
    base.update(kw)
    return BarRecord(**base)


def tick(i: int, **kw) -> TickRecord:
    base = {
        "canonical_symbol": "GER40",
        "broker_symbol": "Ger40",
        "timestamp": T0 + timedelta(milliseconds=200 * i),
        "bid": Decimal("100.00"),
        "ask": Decimal("100.50"),
        "last": None,
        "volume": Decimal(0),
        "volume_semantic_type": SemanticType.BROKER_TICK_ACTIVITY,
        "source": Source.ACTIVTRADES_MT5_CFD,
        "ingested_at": RETRIEVED,
    }
    base.update(kw)
    return TickRecord(**base)


def clean_bars(n: int = 20) -> list[BarRecord]:
    return [bar(i) for i in range(n)]


def types_of(report) -> set[str]:
    return {str(v.violation_type) for v in (*report.base.violations, *report.issues)}


def test_clean_bars_pass():
    report = validate_bars(clean_bars(), timeframe_seconds=300)
    assert report.status is ValidationStatus.PASSED
    assert report.row_count == 20


def test_duplicates_and_non_monotonic_fail():
    bars = clean_bars(5)
    bars.append(bars[2])
    report = validate_bars(bars, timeframe_seconds=300)
    assert report.status is ValidationStatus.FAILED
    assert {"DUPLICATE_RECORD", "NON_MONOTONIC_TIMESTAMP"} <= types_of(report)


@pytest.mark.parametrize(
    "override",
    [
        {"high": Decimal("99.50")},  # high < open/close
        {"low": Decimal("100.80")},  # low > close
        {"open": Decimal("0")},
        {"close": Decimal("-1")},
        {"high": Decimal("NaN")},
        {"low": Decimal("Infinity")},
    ],
)
def test_invalid_ohlc_prices_fail(override):
    bars = clean_bars(5)
    bars[2] = replace(bars[2], **override)
    assert validate_bars(bars, timeframe_seconds=300).status is ValidationStatus.FAILED


def test_off_grid_bar_fails():
    bars = clean_bars(5)
    bars[3] = replace(bars[3], timestamp=bars[3].timestamp + timedelta(seconds=7))
    report = validate_bars(bars, timeframe_seconds=300)
    assert report.status is ValidationStatus.FAILED
    assert str(HistoricalIssue.OFF_GRID_BAR) in types_of(report)


def test_negative_spread_fails_and_outlier_warns():
    bars = clean_bars(20)
    bars[4] = replace(bars[4], spread_points=-1)
    assert validate_bars(bars, timeframe_seconds=300).status is ValidationStatus.FAILED
    bars = clean_bars(20)
    bars[4] = replace(bars[4], spread_points=5000)
    report = validate_bars(bars, timeframe_seconds=300)
    assert report.status is ValidationStatus.PASSED_WITH_WARNINGS
    assert str(HistoricalIssue.SPREAD_ANOMALY) in types_of(report)


def test_suspicious_intraday_gap_warns_but_session_gap_is_expected():
    bars = clean_bars(10)
    gap = [bars[i] if i < 5 else bar(i + 30) for i in range(10)]  # 30-bar hole, same day
    report = validate_bars(gap, timeframe_seconds=300)
    assert report.status is ValidationStatus.PASSED_WITH_WARNINGS
    assert len(report.suspicious_gaps) == 1

    # Friday 21:55 server (19:55 UTC) -> Monday 02:15 server (00:15 UTC): expected weekend gap.
    fri = datetime(2026, 9, 25, 19, 55, tzinfo=UTC)
    mon = datetime(2026, 9, 28, 0, 15, tzinfo=UTC)
    week = [bar(0, timestamp=fri), bar(1, timestamp=mon)]
    report = validate_bars(week, timeframe_seconds=300)
    assert report.status is ValidationStatus.PASSED
    assert report.expected_gaps == 1


def test_missing_weekday_is_not_hidden_as_expected():
    tue_evening = datetime(2026, 9, 22, 19, 55, tzinfo=UTC)
    thu_morning = datetime(2026, 9, 24, 0, 15, tzinfo=UTC)  # Wednesday fully missing
    report = validate_bars(
        [bar(0, timestamp=tue_evening), bar(1, timestamp=thu_morning)], timeframe_seconds=300
    )
    assert report.status is ValidationStatus.PASSED_WITH_WARNINGS
    assert len(report.suspicious_gaps) == 1


def test_empty_and_mixed_datasets_fail():
    assert validate_bars([], timeframe_seconds=300).status is ValidationStatus.FAILED
    mixed = [*clean_bars(3), bar(3, broker_symbol="Ger40Dec26")]
    assert validate_bars(mixed, timeframe_seconds=300).status is ValidationStatus.FAILED
    assert validate_ticks([]).status is ValidationStatus.FAILED


def test_tick_checks():
    assert validate_ticks([tick(i) for i in range(5)]).status is ValidationStatus.PASSED
    crossed = [tick(0), tick(1, bid=Decimal("101"), ask=Decimal("100"))]
    assert validate_ticks(crossed).status is ValidationStatus.FAILED
    dup = [tick(0), tick(0)]
    assert validate_ticks(dup).status is ValidationStatus.FAILED
    same_ms_different_price = [tick(0), tick(0, bid=Decimal("100.1"), ask=Decimal("100.6"))]
    assert validate_ticks(same_ms_different_price).status is ValidationStatus.PASSED
    jump = [tick(0), tick(1, bid=Decimal("110"), ask=Decimal("110.5"))]
    report = validate_ticks(jump)
    assert report.status is ValidationStatus.PASSED_WITH_WARNINGS
    assert str(HistoricalIssue.TICK_JUMP) in types_of(report)
    nan = [tick(0, bid=Decimal("NaN"))]
    assert validate_ticks(nan).status is ValidationStatus.FAILED


def _write_bars(root, bars, retrieved=RETRIEVED):
    report = validate_bars(bars, timeframe_seconds=300)
    return write_bar_dataset(
        root,
        bars,
        report=report,
        requested_start=T0,
        requested_end=T0 + timedelta(days=1),
        retrieved_at=retrieved,
        timezone_policy=TZ_POLICY,
        broker_account_kind="DEMO",
    )


def test_bar_roundtrip_preserves_records_and_provenance(tmp_path):
    bars = clean_bars(12)
    path, prov = _write_bars(tmp_path, bars)
    back, prov2 = read_bar_dataset(path)
    assert prov == prov2
    assert back == bars  # includes Decimal precision, spread_points, ingested_at
    assert prov.source == "ACTIVTRADES_MT5_CFD"
    assert (prov.canonical_instrument, prov.broker_symbol) == ("GER40", "Ger40")
    assert prov.timeframe == "5m" and prov.row_count == 12 and prov.data_type == "BARS"
    assert prov.volume_semantic_type == "BROKER_TICK_ACTIVITY"
    assert prov.timezone_policy == TZ_POLICY and prov.broker_account_kind == "DEMO"
    assert prov.schema_version and prov.normalization_policy and prov.retrieved_at
    assert prov.requested_start != "" and prov.actual_start == T0.isoformat()


def test_parquet_is_byte_deterministic(tmp_path):
    bars = clean_bars(12)
    p1, _ = _write_bars(tmp_path / "a", bars)
    p2, _ = _write_bars(tmp_path / "b", list(bars))
    assert p1.read_bytes() == p2.read_bytes()


def test_failed_validation_is_never_persisted(tmp_path):
    bars = clean_bars(5)
    bars[2] = replace(bars[2], high=Decimal("50"))
    report = validate_bars(bars, timeframe_seconds=300)
    with pytest.raises(DatasetRejected):
        write_bar_dataset(
            tmp_path,
            bars,
            report=report,
            requested_start=T0,
            requested_end=T0,
            retrieved_at=RETRIEVED,
            timezone_policy=TZ_POLICY,
            broker_account_kind="DEMO",
        )
    assert not list(tmp_path.rglob("*.parquet"))


def test_report_must_match_records(tmp_path):
    report = validate_bars(clean_bars(5), timeframe_seconds=300)
    with pytest.raises(DatasetRejected):
        write_bar_dataset(
            tmp_path,
            clean_bars(6),
            report=report,
            requested_start=T0,
            requested_end=T0,
            retrieved_at=RETRIEVED,
            timezone_policy=TZ_POLICY,
            broker_account_kind="DEMO",
        )


def test_cfd_volume_cannot_be_labelled_exchange_volume(tmp_path):
    bars = clean_bars(3)
    bars[1] = replace(bars[1], volume_semantic_type=SemanticType.REAL_EXCHANGE_VOLUME)
    with pytest.raises(DatasetRejected):
        _write_bars(tmp_path, bars)


def test_tampered_content_detected(tmp_path):
    import pyarrow.parquet as pq

    path, _ = _write_bars(tmp_path, clean_bars(4))
    table = pq.read_table(path)
    idx = table.schema.get_field_index("close")
    values = table.column("close").to_pylist()
    values[1] = "999.99"
    import pyarrow as pa

    tampered = table.set_column(idx, "close", pa.array(values, type=pa.string()))
    pq.write_table(tampered, path)
    with pytest.raises(DatasetIntegrityError):
        read_bar_dataset(path)


def test_tick_roundtrip_with_millis_and_null_last(tmp_path):
    ticks = [tick(i) for i in range(6)]
    report = validate_ticks(ticks)
    path, prov = write_tick_dataset(
        tmp_path,
        ticks,
        report=report,
        requested_start=T0,
        requested_end=T0 + timedelta(seconds=2),
        retrieved_at=RETRIEVED,
        timezone_policy=TZ_POLICY,
        broker_account_kind="DEMO",
    )
    back, prov2 = read_tick_dataset(path)
    assert back == ticks and prov == prov2
    assert back[1].timestamp.microsecond == 200000
    assert back[0].last is None
    with pytest.raises(DatasetIntegrityError):
        read_bar_dataset(path)
