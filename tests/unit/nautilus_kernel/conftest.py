"""Shared C4 fixtures: REAL captured GER40 M5 week -> C3 pipeline -> Parquet dataset."""

import csv
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from adapters.activtrades_mt5 import history_download
from adapters.activtrades_mt5.testing import FakeMT5Client

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ger40"
SYMBOL_INFO = FIXTURES / "symbol_info.json"
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


@pytest.fixture(scope="session")
def c3_dataset(tmp_path_factory) -> Path:
    client = FakeMT5Client()
    client.set_symbol_info("Ger40", SimpleNamespace(name="Ger40", path="Cash Indices\\Ger40"))
    client.set_rates(load_real_m5())
    path, _prov, _report = history_download.download_bars(
        client,
        root=tmp_path_factory.mktemp("c3"),
        canonical="GER40",
        mt5_timeframe=5,
        start_utc=datetime(2026, 9, 21, tzinfo=UTC),
        end_utc=datetime(2026, 9, 26, tzinfo=UTC),
        retrieved_at=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
        broker_account_kind="DEMO",
    )
    return path


@pytest.fixture(scope="session")
def symbol_info_path() -> Path:
    return SYMBOL_INFO
