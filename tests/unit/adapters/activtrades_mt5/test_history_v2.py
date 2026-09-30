"""V2 additions to the history path: observed-symbol resolution and server-grid H4/D1 bars."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from adapters.activtrades_mt5 import history, history_download
from adapters.activtrades_mt5.testing import FakeMT5Client
from data.historical import read_bar_dataset

RATES = np.dtype(
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


def test_h4_is_a_mapped_timeframe():
    assert history.TIMEFRAME_LABELS[16388] == "4h"
    assert history.TIMEFRAME_SECONDS["4h"] == 14400


def _client(name: str, path: str) -> FakeMT5Client:
    c = FakeMT5Client()
    c.set_symbol_info(name, SimpleNamespace(name=name, path=path))
    return c


def test_resolve_with_observed_symbol_and_path_guard():
    c = _client("UsaTec", "Cash Indices\\UsaTec")
    assert history.resolve_broker_symbol(c, "NAS100", broker_symbol="UsaTec") == "UsaTec"
    assert (
        history.resolve_broker_symbol(
            c, "NAS100", broker_symbol="UsaTec", path_prefix="Cash Indices\\"
        )
        == "UsaTec"
    )
    with pytest.raises(history.MT5SymbolResolutionError, match="not under"):
        history.resolve_broker_symbol(c, "NAS100", broker_symbol="UsaTec", path_prefix="Metals\\")
    with pytest.raises(history.MT5SymbolResolutionError):
        history.resolve_broker_symbol(c, "NAS100", broker_symbol="Nas100")  # not observed
    with pytest.raises(history.MT5SymbolResolutionError):
        history.resolve_broker_symbol(c, "NAS100")  # legacy path has no NAS100 mapping


def test_legacy_ger40_resolution_unchanged():
    c = _client("Ger40", "Cash Indices\\Ger40")
    assert history.resolve_broker_symbol(c, "GER40") == "Ger40"


def test_h4_bars_validate_on_server_grid_and_store_true_utc(tmp_path):
    # Server wall clock 00:00,04:00,...20:00 on Mon 2026-01-12 (CET, UTC+1), encoded as epoch.
    base = datetime(2026, 1, 12, tzinfo=UTC)
    rows = [
        (int((base + timedelta(hours=4 * i)).timestamp()), 100.0, 101.0, 99.0, 100.5, 10, 20, 0)
        for i in range(6)
    ]
    c = _client("UsaTec", "Cash Indices\\UsaTec")
    c.set_rates(np.array(rows, dtype=RATES))
    path, prov, report = history_download.download_bars(
        c,
        root=tmp_path,
        canonical="NAS100",
        mt5_timeframe=16388,
        start_utc=datetime(2026, 1, 11, tzinfo=UTC),
        end_utc=datetime(2026, 1, 14, tzinfo=UTC),
        retrieved_at=datetime(2026, 9, 30, tzinfo=UTC),
        broker_account_kind="DEMO",
        broker_symbol="UsaTec",
        path_prefix="Cash Indices\\",
    )
    assert report.status.value != "FAILED"
    bars, _ = read_bar_dataset(path)
    assert len(bars) == 6 and prov.timeframe == "4h"
    # server 00:00 (UTC+1) is 23:00 UTC of the previous day: off the UTC 4h grid but valid.
    assert bars[0].timestamp == datetime(2026, 1, 11, 23, tzinfo=UTC)
    assert "OFF_GRID_BAR" not in report.summary
