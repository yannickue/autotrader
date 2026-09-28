"""Tests for `data.parquet_store.ParquetStore`."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from data.parquet_store import BarRecord, ParquetStore, TickRecord
from data.provenance import SemanticType, Source


def _tick(
    *,
    ts: datetime,
    bid: Decimal = Decimal("1.23456"),
    ask: Decimal = Decimal("1.23458"),
) -> TickRecord:
    return TickRecord(
        canonical_symbol="DAX",
        broker_symbol="GER40.cash.fixture",
        timestamp=ts,
        bid=bid,
        ask=ask,
        last=Decimal("1.23457"),
        volume=Decimal("3"),
        volume_semantic_type=SemanticType.BROKER_TICK_ACTIVITY,
        source=Source.ACTIVTRADES_MT5_CFD,
        quality_flags=("UNVERIFIED",),
        ingested_at=datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC),
    )


def _bar(*, ts: datetime) -> BarRecord:
    return BarRecord(
        canonical_symbol="DAX",
        broker_symbol="GER40.cash.fixture",
        timestamp=ts,
        timeframe="1m",
        open=Decimal("100.1"),
        high=Decimal("100.5"),
        low=Decimal("99.9"),
        close=Decimal("100.3"),
        volume=Decimal("42"),
        volume_semantic_type=SemanticType.BROKER_TICK_ACTIVITY,
        source=Source.ACTIVTRADES_MT5_CFD,
        ingested_at=datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC),
    )


def test_tick_round_trip_write_read(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    ts = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)
    written = store.write_ticks([_tick(ts=ts)])
    assert written == 1

    result = store.read_ticks(
        source=Source.ACTIVTRADES_MT5_CFD,
        canonical_symbol="DAX",
        start=datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 29, 23, 59, 59, tzinfo=UTC),
    )
    assert len(result) == 1
    got = result[0]
    assert got.canonical_symbol == "DAX"
    assert got.bid == Decimal("1.23456")
    assert got.ask == Decimal("1.23458")
    assert got.quality_flags == ("UNVERIFIED",)
    assert got.volume_semantic_type is SemanticType.BROKER_TICK_ACTIVITY
    assert got.source is Source.ACTIVTRADES_MT5_CFD


def test_decimal_precision_preserved_with_many_fractional_digits(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    ts = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)
    precise_bid = Decimal("1.234567890123456789")
    precise_ask = Decimal("1.234567890123456790")
    store.write_ticks([_tick(ts=ts, bid=precise_bid, ask=precise_ask)])

    result = store.read_ticks(
        source=Source.ACTIVTRADES_MT5_CFD,
        canonical_symbol="DAX",
        start=ts,
        end=ts,
    )
    assert result[0].bid == precise_bid
    assert result[0].ask == precise_ask
    # Exact string round trip, not just numeric equality after quantization.
    assert str(result[0].bid) == str(precise_bid)


def test_second_write_of_same_data_does_not_duplicate(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    ts = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)
    record = _tick(ts=ts)

    first = store.write_ticks([record])
    second = store.write_ticks([record])
    assert first == 1
    assert second == 0

    result = store.read_ticks(
        source=Source.ACTIVTRADES_MT5_CFD,
        canonical_symbol="DAX",
        start=ts,
        end=ts,
    )
    assert len(result) == 1


def test_bar_round_trip_write_read(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    ts = datetime(2026, 9, 29, 10, 5, 0, tzinfo=UTC)
    store.write_bars([_bar(ts=ts)])

    result = store.read_bars(
        source=Source.ACTIVTRADES_MT5_CFD,
        canonical_symbol="DAX",
        start=datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 29, 23, 59, 59, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].timeframe == "1m"
    assert result[0].open == Decimal("100.1")
    assert result[0].close == Decimal("100.3")


def test_read_date_range_spans_multiple_partitions(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    ts_day1 = datetime(2026, 9, 28, 23, 0, 0, tzinfo=UTC)
    ts_day2 = datetime(2026, 9, 29, 1, 0, 0, tzinfo=UTC)
    store.write_ticks([_tick(ts=ts_day1), _tick(ts=ts_day2)])

    result = store.read_ticks(
        source=Source.ACTIVTRADES_MT5_CFD,
        canonical_symbol="DAX",
        start=datetime(2026, 9, 28, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 29, 23, 59, 59, tzinfo=UTC),
    )
    assert len(result) == 2
    assert result[0].timestamp < result[1].timestamp


def test_read_range_excludes_data_outside_window(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    inside = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)
    outside = datetime(2026, 9, 30, 10, 0, 0, tzinfo=UTC)
    store.write_ticks([_tick(ts=inside), _tick(ts=outside)])

    result = store.read_ticks(
        source=Source.ACTIVTRADES_MT5_CFD,
        canonical_symbol="DAX",
        start=datetime(2026, 9, 29, 0, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 29, 23, 59, 59, tzinfo=UTC),
    )
    assert len(result) == 1
    assert result[0].timestamp == inside
