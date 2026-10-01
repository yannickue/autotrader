# ruff: noqa: E501
from __future__ import annotations

import numpy as np
import pytest

from market_observer.schema import ObserverBars, SessionSpec

T0_NS = 1_751_000_000_000_000_000  # arbitrary UTC anchor (2025-06-27)


def build_bars(
    o, h, low, c, *, spread=0.0, atr=1.0, segment=None, local_day=None, local_minute=None,
    market="TST", bar_seconds=300, cash_close_min=None, t0_ns=T0_NS,
) -> ObserverBars:
    n = len(h)
    f = lambda x: np.full(n, x, float) if np.isscalar(x) else np.asarray(x, float)  # noqa: E731
    step = bar_seconds * 1_000_000_000
    return ObserverBars(
        market=market, ts_ns=t0_ns + step * np.arange(n, dtype=np.int64),
        o=f(o), h=f(h), l=f(low), c=f(c), tick_volume=np.full(n, 100.0), spread=f(spread), atr=f(atr),
        segment_id=np.zeros(n, np.int64) if segment is None else np.asarray(segment, np.int64),
        local_minute=(np.arange(n, dtype=np.int64) * (bar_seconds // 60) if local_minute is None else np.asarray(local_minute, np.int64)),
        local_day=np.zeros(n, np.int64) if local_day is None else np.asarray(local_day, np.int64),
        tick_size=0.01, session=SessionSpec("Europe/Berlin", 540, 1050 if cash_close_min is None else cash_close_min),
        bar_seconds=bar_seconds,
    )


@pytest.fixture(scope="session")
def make_bars():
    return build_bars


@pytest.fixture(scope="session", autouse=True)
def _no_real_process_hardening():
    """The CLI entry points call research_speed.parallel.harden_process() (priority class, job object, fail-closed memory, live-process check): never for real inside the test
    process. Session scope because module-scoped fixtures run the CLIs; tests/unit/research_speed/test_hardening.py keeps its own reference to the real function (taken at import time)."""
    mp = pytest.MonkeyPatch()
    mp.setattr("research_speed.parallel.harden_process", lambda jobs=1, **_kw: jobs)
    yield
    mp.undo()
