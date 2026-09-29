"""C3 MT5 historical read path -- fake client + synthetic numpy arrays only.

Array dtypes are the ones OBSERVED from the live ActivTrades demo terminal.
No MetaTrader5 import, no real MT5 access.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import numpy as np
import pytest

from adapters.activtrades_mt5.history import (
    AmbiguousServerTime,
    MT5HistoryError,
    MT5SchemaError,
    MT5SymbolResolutionError,
    ServerTimePolicy,
    fetch_rates_range,
    fetch_ticks_range,
    rates_to_bar_records,
    resolve_broker_symbol,
    ticks_to_tick_records,
)
from adapters.activtrades_mt5.testing import FakeMT5Client
from data.provenance import SemanticType

RATES_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<u8"),
        ("spread", "<i4"),
        ("real_volume", "<u8"),
    ]
)
TICKS_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("bid", "<f8"),
        ("ask", "<f8"),
        ("last", "<f8"),
        ("volume", "<u8"),
        ("time_msc", "<i8"),
        ("flags", "<u4"),
        ("volume_real", "<f8"),
    ]
)
POLICY = ServerTimePolicy()
RETRIEVED = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
# Server clock 2026-09-22 10:00 (CEST = UTC+2) -> true UTC 08:00.
SERVER_EPOCH = int(datetime(2026, 9, 22, 10, 0, tzinfo=UTC).timestamp())
WINDOW = {
    "start_utc": RETRIEVED - timedelta(days=1),
    "end_utc": RETRIEVED,
}


def make_rates(n: int = 3, step: int = 300) -> np.ndarray:
    arr = np.zeros(n, dtype=RATES_DTYPE)
    for i in range(n):
        arr[i] = (SERVER_EPOCH + i * step, 100.0, 101.0, 99.0, 100.5, 10 + i, 155, 0)
    return arr


def make_ticks() -> np.ndarray:
    arr = np.zeros(2, dtype=TICKS_DTYPE)
    arr[0] = (SERVER_EPOCH, 100.0, 100.5, 0.0, 0, SERVER_EPOCH * 1000 + 185, 6, 0.0)
    arr[1] = (SERVER_EPOCH, 100.1, 100.6, 0.0, 0, SERVER_EPOCH * 1000 + 300, 6, 0.0)
    return arr


def _bars(arr, **kw):
    return rates_to_bar_records(
        arr,
        canonical_symbol="GER40",
        broker_symbol="Ger40",
        mt5_timeframe=kw.pop("mt5_timeframe", 5),
        retrieved_at=RETRIEVED,
        policy=POLICY,
        **kw,
    )


def test_server_time_converted_to_true_utc():
    assert POLICY.server_epoch_to_utc(SERVER_EPOCH) == datetime(2026, 9, 22, 8, 0, tzinfo=UTC)


def test_request_datetime_is_server_epoch_shifted():
    req = POLICY.utc_to_request_datetime(datetime(2026, 9, 22, 8, 0, tzinfo=UTC))
    assert req == datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        POLICY.utc_to_request_datetime(datetime(2026, 9, 22, 8, 0))


def test_dst_ambiguous_and_nonexistent_hours_fail_closed():
    ambiguous = int(datetime(2026, 10, 25, 2, 30, tzinfo=UTC).timestamp())
    missing = int(datetime(2026, 3, 29, 2, 30, tzinfo=UTC).timestamp())
    with pytest.raises(AmbiguousServerTime):
        POLICY.server_epoch_to_utc(ambiguous)
    with pytest.raises(AmbiguousServerTime):
        POLICY.server_epoch_to_utc(missing)


def test_bars_normalized_with_tick_activity_semantics_and_spread():
    bars = _bars(make_rates())
    b = bars[0]
    assert b.timestamp == datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
    assert (b.canonical_symbol, b.broker_symbol, b.timeframe) == ("GER40", "Ger40", "5m")
    assert b.volume == Decimal(10)
    assert b.spread_points == 155
    assert b.volume_semantic_type is SemanticType.BROKER_TICK_ACTIVITY
    assert b.open == Decimal("100.0")


def test_incomplete_last_bar_dropped():
    end = datetime(2026, 9, 22, 8, 7, tzinfo=UTC)  # 08:05 bar still open until 08:10
    assert len(_bars(make_rates(2), end_utc=end)) == 1


def test_unmapped_timeframe_fails_closed():
    with pytest.raises(KeyError):
        _bars(make_rates(), mt5_timeframe=99)


def test_ticks_use_millis_and_last_zero_becomes_none():
    ticks = ticks_to_tick_records(
        make_ticks(),
        canonical_symbol="GER40",
        broker_symbol="Ger40",
        retrieved_at=RETRIEVED,
        policy=POLICY,
    )
    assert ticks[0].timestamp == datetime(2026, 9, 22, 8, 0, 0, 185000, tzinfo=UTC)
    assert ticks[0].last is None
    assert ticks[0].volume_semantic_type is SemanticType.BROKER_TICK_ACTIVITY


def test_schema_change_rejected():
    bad = np.zeros(2, dtype=[("time", "<i8"), ("open", "<f8")])
    with pytest.raises(MT5SchemaError):
        _bars(bad)
    with pytest.raises(MT5SchemaError):
        ticks_to_tick_records(
            bad,
            canonical_symbol="GER40",
            broker_symbol="Ger40",
            retrieved_at=RETRIEVED,
            policy=POLICY,
        )


def test_nan_price_preserved_for_quality_layer():
    arr = make_rates(1)
    arr[0]["high"] = float("nan")
    assert not _bars(arr)[0].high.is_finite()


def test_fetch_none_and_empty_raise_with_last_error():
    client = FakeMT5Client()
    client.set_rates(None)
    client.set_last_error(-2, "Terminal: Invalid params")
    with pytest.raises(MT5HistoryError, match="Invalid params"):
        fetch_rates_range(client, broker_symbol="Ger40", mt5_timeframe=5, policy=POLICY, **WINDOW)
    client.set_rates(np.zeros(0, dtype=RATES_DTYPE))
    with pytest.raises(MT5HistoryError, match="no rows"):
        fetch_rates_range(client, broker_symbol="Ger40", mt5_timeframe=5, policy=POLICY, **WINDOW)
    client.set_ticks_history(None)
    with pytest.raises(MT5HistoryError):
        fetch_ticks_range(client, broker_symbol="Ger40", policy=POLICY, **WINDOW)


def test_fetch_returns_validated_array_and_unmapped_tf_rejected():
    client = FakeMT5Client()
    client.set_rates(make_rates())
    rates = fetch_rates_range(
        client, broker_symbol="Ger40", mt5_timeframe=5, policy=POLICY, **WINDOW
    )
    assert len(rates) == 3
    with pytest.raises(MT5HistoryError):
        fetch_rates_range(client, broker_symbol="Ger40", mt5_timeframe=77, policy=POLICY, **WINDOW)


def _info(name: str, path: str):
    return SimpleNamespace(name=name, path=path)


def test_symbol_resolution_distinguishes_canonical_from_broker_symbol():
    client = FakeMT5Client()
    client.set_symbol_info("Ger40", _info("Ger40", "Cash Indices\\Ger40"))
    client.set_symbol_info("Ger40Dec26", _info("Ger40Dec26", "Futures\\Ger40Dec26"))
    assert resolve_broker_symbol(client, "GER40") == "Ger40"


def test_symbol_resolution_fails_closed():
    client = FakeMT5Client()
    with pytest.raises(MT5SymbolResolutionError):
        resolve_broker_symbol(client, "GER40")
    with pytest.raises(MT5SymbolResolutionError):
        resolve_broker_symbol(client, "NOPE")
    client.set_symbol_info("Ger40", _info("Ger40", "Futures\\Ger40"))
    with pytest.raises(MT5SymbolResolutionError):
        resolve_broker_symbol(client, "GER40")
