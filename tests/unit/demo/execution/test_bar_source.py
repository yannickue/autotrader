from datetime import timedelta

import numpy as np

from adapters.activtrades_mt5.fake_broker import FakeBrokerConfig, FakeMT5Broker
from adapters.activtrades_mt5.history import RATES_DTYPES, RATES_FIELDS, ServerTimePolicy
from demo.execution.bar_source import Mt5BarSource
from tests.unit.nautilus_mt5.conftest import real_symbol_info


def test_frame_returns_only_closed_m5_bars_and_quote_uses_server_time_policy():
    broker = FakeMT5Broker(real_symbol_info(), FakeBrokerConfig())
    dtype = np.dtype(list(zip(RATES_FIELDS, RATES_DTYPES, strict=True)))
    rows = np.array(
        [
            (1_790_000_000, 1.0, 2.0, 0.5, 1.5, 11, 100, 0),
            (1_790_000_300, 1.5, 2.5, 1.0, 2.0, 22, 120, 0),
            (1_790_000_600, 2.0, 3.0, 1.5, 2.5, 33, 130, 0),  # forming
        ],
        dtype=dtype,
    )
    broker.rates[5] = rows
    source = Mt5BarSource(broker, lookback=10)

    frame = source.frame("GER40")
    assert list(frame["close"]) == [1.5, 2.0]
    assert list(frame["tick_activity"]) == [11, 22]
    expected_open = ServerTimePolicy().server_epoch_to_utc(1_790_000_300)
    assert source.last_bar_close_utc("GER40") == expected_open + timedelta(minutes=5)
    bid, ask, quote_ts = source.quote("GER40")
    assert (bid, ask) == (broker.bid, broker.ask)
    assert quote_ts == ServerTimePolicy().server_epoch_to_utc(broker.server_time)
