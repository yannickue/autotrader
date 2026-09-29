"""Shared C5 fixtures. No MetaTrader5 import; the broker is the in-memory FakeMT5Broker
built from the REAL captured GER40 symbol_info (tests/fixtures/ger40/symbol_info.json)."""

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.activtrades_mt5.fake_broker import FakeBrokerConfig, FakeMT5Broker
from nautilus_mt5.instruments import InstrumentAssumptions

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "ger40" / "symbol_info.json"


def real_symbol_info() -> SimpleNamespace:
    return SimpleNamespace(**json.loads(FIXTURE.read_text())["symbol_info"])


@pytest.fixture
def broker() -> FakeMT5Broker:
    return FakeMT5Broker(real_symbol_info(), FakeBrokerConfig())


@pytest.fixture
def assumptions() -> InstrumentAssumptions:
    return InstrumentAssumptions(margin_init=Decimal("0.05"), margin_maint=Decimal("0.05"))
