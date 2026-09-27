from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from data.binance_usdm import (
    BinanceUsdMDiscovery,
    BinanceUsdMSnapshotFactory,
    SnapshotPolicy,
    parse_exchange_info,
)
from data.models import DataQuality

EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "pair": "BTCUSDT",
            "contractType": "PERPETUAL",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "marginAsset": "USDT",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.00100000",
                    "stepSize": "0.00100000",
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "minQty": "0.00100000",
                    "stepSize": "0.00100000",
                },
                {"filterType": "MIN_NOTIONAL", "notional": "5.00000000"},
            ],
        },
        {
            "symbol": "BTCUSDT_260327",
            "pair": "BTCUSDT",
            "contractType": "CURRENT_QUARTER",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "marginAsset": "USDT",
            "filters": [],
        },
    ]
}


def test_exchange_info_parser_keeps_only_perpetuals_and_preserves_rule_precision() -> None:
    # Catches accepting dated futures and converting exchange decimals through float.
    (instrument,) = parse_exchange_info(EXCHANGE_INFO)

    assert instrument.instrument == "BTCUSDT-PERP"
    assert instrument.venue_symbol == "BTCUSDT"
    assert instrument.price_tick.as_tuple().exponent == -8
    assert instrument.quantity_step.as_tuple().exponent == -8
    assert instrument.min_quantity.as_tuple().exponent == -8
    assert instrument.min_notional.as_tuple().exponent == -8
    assert instrument.price_tick == Decimal("0.01000000")
    assert instrument.quantity_step == Decimal("0.00100000")


def test_instrument_rules_floor_price_and_quantity_to_exchange_increments() -> None:
    # Catches rounding up an order beyond its requested price or quantity.
    (instrument,) = parse_exchange_info(EXCHANGE_INFO)

    assert instrument.floor_price(Decimal("101.239")) == Decimal("101.23000000")
    assert instrument.floor_quantity(Decimal("0.0049")) == Decimal("0.00400000")
    assert instrument.meets_minimums(price=Decimal("2000"), quantity=Decimal("0.003"))
    assert not instrument.meets_minimums(price=Decimal("1000"), quantity=Decimal("0.003"))


def test_discovery_uses_injected_public_json_reader_without_credentials() -> None:
    # Catches coupling public discovery to authenticated/network-only infrastructure.
    seen_urls: list[str] = []

    def read_json(url: str) -> object:
        seen_urls.append(url)
        return EXCHANGE_INFO

    instruments = BinanceUsdMDiscovery(read_json=read_json).discover_instruments()

    assert [item.venue_symbol for item in instruments] == ["BTCUSDT"]
    assert seen_urls == ["https://fapi.binance.com/fapi/v1/exchangeInfo"]


def test_snapshot_factory_normalizes_book_ticker_and_marks_fresh_data_live() -> None:
    # Catches wrong timestamp units, spread calculation, or volume semantics.
    (instrument,) = parse_exchange_info(EXCHANGE_INFO)
    event_time = datetime(2026, 1, 1, tzinfo=UTC)
    factory = BinanceUsdMSnapshotFactory(
        SnapshotPolicy(live_after=timedelta(seconds=1), stale_after=timedelta(seconds=5))
    )

    snapshot = factory.create(
        instrument=instrument,
        book_ticker={
            "s": "BTCUSDT",
            "b": "100.00",
            "a": "100.10",
            "E": int(event_time.timestamp() * 1000),
            "u": 42,
        },
        ticker={"s": "BTCUSDT", "c": "100.05", "v": "12.5", "q": "1250.625"},
        receipt_time=event_time + timedelta(milliseconds=250),
        volatility=Decimal("0.025"),
        volatility_window=30,
    )

    assert snapshot.instrument == "BTCUSDT-PERP"
    assert snapshot.timestamp == event_time
    assert snapshot.bid == Decimal("100.00")
    assert snapshot.ask == Decimal("100.10")
    assert snapshot.last == Decimal("100.05")
    assert snapshot.volume == Decimal("12.5")
    assert snapshot.volatility == Decimal("0.025")
    assert snapshot.liquidity == Decimal("1250.625")
    assert snapshot.quality is DataQuality.LIVE
    assert snapshot.spread == Decimal("0.10")
    assert snapshot.metadata == {
        "schema_version": 1,
        "venue_symbol": "BTCUSDT",
        "sequence": 42,
        "receipt_timestamp": (event_time + timedelta(milliseconds=250)).isoformat(),
        "spread": "0.10",
        "spread_bps": "9.995002498750624687656171914",
        "volume_semantics": "rolling_24h_base",
        "liquidity_semantics": "rolling_24h_quote_volume",
        "volatility_method": "sqrt_sum_log_returns",
        "volatility_window": 30,
    }


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(seconds=2), DataQuality.DELAYED),
        (timedelta(seconds=6), DataQuality.STALE),
    ],
)
def test_snapshot_factory_downgrades_quality_using_event_and_receipt_time(
    age: timedelta, expected: DataQuality
) -> None:
    # Catches reconnect/freshness logic that labels old events live.
    (instrument,) = parse_exchange_info(EXCHANGE_INFO)
    event_time = datetime(2026, 1, 1, tzinfo=UTC)
    factory = BinanceUsdMSnapshotFactory(
        SnapshotPolicy(live_after=timedelta(seconds=1), stale_after=timedelta(seconds=5))
    )

    snapshot = factory.create(
        instrument=instrument,
        book_ticker={
            "s": "BTCUSDT",
            "b": "100",
            "a": "101",
            "E": int(event_time.timestamp() * 1000),
            "u": 42,
        },
        ticker={"s": "BTCUSDT", "c": "100.5", "v": "12", "q": "1206"},
        receipt_time=event_time + age,
        volatility=Decimal("0.02"),
        volatility_window=30,
    )

    assert snapshot.quality is expected


def test_snapshot_factory_rejects_mismatched_symbol() -> None:
    # Catches publication of a quote under another instrument identifier.
    (instrument,) = parse_exchange_info(EXCHANGE_INFO)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="book ticker symbol does not match instrument"):
        BinanceUsdMSnapshotFactory().create(
            instrument=instrument,
            book_ticker={"s": "ETHUSDT", "b": "100", "a": "101", "E": 1, "u": 1},
            ticker={"s": "BTCUSDT", "c": "100.5", "v": "12", "q": "1206"},
            receipt_time=now,
            volatility=Decimal("0.02"),
        )
