"""C3 pipeline: fake client (incl. REAL captured GER40 M5 rows) -> validated Parquet.

The CSV fixture is a raw capture from the live ActivTrades DEMO terminal
(2026-09-29, week 2026-09-21..25, server-epoch `time`). Nothing here touches
MetaTrader5.
"""

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from adapters.activtrades_mt5 import history, history_download
from adapters.activtrades_mt5.history import TICKS_FIELDS
from adapters.activtrades_mt5.testing import FakeMT5Client
from data.historical import DatasetRejected, read_bar_dataset, read_tick_dataset
from data.historical_quality import ValidationStatus

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "ger40"
RETRIEVED = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
WEEK_START = datetime(2026, 9, 21, tzinfo=UTC)
WEEK_END = datetime(2026, 9, 26, tzinfo=UTC)
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


def load_real_m5() -> np.ndarray:
    with (FIXTURES / "ger40_rates_5m.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    arr = np.zeros(len(rows), dtype=RATES_DTYPE)
    for i, r in enumerate(rows):
        arr[i] = tuple(float(r[n]) for n in RATES_DTYPE.names)
    return arr


def fake_client(rates) -> FakeMT5Client:
    client = FakeMT5Client()
    client.set_symbol_info("Ger40", SimpleNamespace(name="Ger40", path="Cash Indices\\Ger40"))
    client.set_rates(rates)
    return client


def test_real_captured_week_passes_validation_and_roundtrips(tmp_path):
    client = fake_client(load_real_m5())
    path, prov, report = history_download.download_bars(
        client,
        root=tmp_path,
        canonical="GER40",
        mt5_timeframe=5,
        start_utc=WEEK_START,
        end_utc=WEEK_END,
        retrieved_at=RETRIEVED,
        broker_account_kind="DEMO",
    )
    assert report.status is ValidationStatus.PASSED
    assert prov.row_count == 1185 and report.expected_gaps == 4
    assert prov.actual_start == "2026-09-21T00:15:00+00:00"
    assert prov.actual_end == "2026-09-25T19:55:00+00:00"
    bars, prov2 = read_bar_dataset(path)
    assert prov2 == prov and len(bars) == 1185
    assert (prov.canonical_instrument, prov.broker_symbol) == ("GER40", "Ger40")
    # deterministic: same input twice -> byte-identical file
    path2, _, _ = history_download.download_bars(
        client,
        root=tmp_path / "again",
        canonical="GER40",
        mt5_timeframe=5,
        start_utc=WEEK_START,
        end_utc=WEEK_END,
        retrieved_at=RETRIEVED,
        broker_account_kind="DEMO",
    )
    assert path.read_bytes() == path2.read_bytes()


def test_corrupt_real_row_is_rejected_not_persisted(tmp_path):
    rates = load_real_m5()
    rates[100]["high"] = rates[100]["low"] - 5.0
    with pytest.raises(DatasetRejected):
        history_download.download_bars(
            fake_client(rates),
            root=tmp_path,
            canonical="GER40",
            mt5_timeframe=5,
            start_utc=WEEK_START,
            end_utc=WEEK_END,
            retrieved_at=RETRIEVED,
            broker_account_kind="DEMO",
        )
    assert not list(tmp_path.rglob("*.parquet"))


def test_ticks_pipeline(tmp_path):
    base = int(datetime(2026, 9, 25, 10, 0, tzinfo=UTC).timestamp())  # server clock 10:00
    dtype = np.dtype(
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
    assert dtype.names == TICKS_FIELDS
    ticks = np.zeros(3, dtype=dtype)
    for i in range(3):
        ticks[i] = (base, 25000.0 + i, 25001.5 + i, 0.0, 0, base * 1000 + 100 * i, 6, 0.0)
    client = fake_client(None)
    client.set_ticks_history(ticks)
    path, _prov, report = history_download.download_ticks(
        client,
        root=tmp_path,
        canonical="GER40",
        start_utc=datetime(2026, 9, 25, 8, 0, tzinfo=UTC),
        end_utc=datetime(2026, 9, 25, 8, 10, tzinfo=UTC),
        retrieved_at=RETRIEVED,
        broker_account_kind="DEMO",
    )
    assert report.status is ValidationStatus.PASSED
    back, _ = read_tick_dataset(path)
    assert len(back) == 3 and back[1].timestamp == datetime(
        2026, 9, 25, 8, 0, 0, 100000, tzinfo=UTC
    )


def test_history_modules_never_import_metatrader5():
    for module in (history, history_download):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "import MetaTrader5" not in source
        assert "get_real_client" not in source


def test_end_utc_excludes_open_bar(tmp_path):
    rates = load_real_m5()
    cut = WEEK_START + timedelta(hours=2)
    _, prov, _ = history_download.download_bars(
        fake_client(rates),
        root=tmp_path,
        canonical="GER40",
        mt5_timeframe=5,
        start_utc=WEEK_START,
        end_utc=cut,
        retrieved_at=RETRIEVED,
        broker_account_kind="DEMO",
    )
    assert datetime.fromisoformat(prov.actual_end) + timedelta(minutes=5) <= cut
