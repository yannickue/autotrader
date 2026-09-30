from __future__ import annotations

import pytest

from alpha.common.market_data import load_dev_market_frame
from demo.opportunity.production_spec import load_production_spec
from markets.spec import CANONICALS, load_market_spec


@pytest.fixture(scope="session")
def mspecs():
    return {m: load_market_spec(m) for m in CANONICALS}


@pytest.fixture(scope="session")
def prod():
    return load_production_spec()


@pytest.fixture(scope="session")
def frames(mspecs):
    """Real DEV frames (<= 2026-08-31) of all five markets."""
    return {m: load_dev_market_frame(mspecs[m]) for m in CANONICALS}
