# ruff: noqa: E501
"""Synthetic world for the backfill tests (no tests in this module): a small random-walk M5 frame, a fake ``MarketInputs`` whose candidate generator is
monkeypatched, and a cached build of the whole backfill (one build is shared by every test file)."""

from __future__ import annotations

import contextlib
import tempfile
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from coverage_analysis.observer_lab import backfill as BF
from market_observer import bars_adapter as BA

N_BARS = 1700
T0 = pd.Timestamp("2026-07-06 00:00", tz="UTC")  # a Monday, development period
MARKET = "TST"
CODE = {"git_sha": "test", "git_dirty": False, "backfill_source_hash": "test"}
EVENT_BARS = (900, 1000, 1100, 1200, 1300, 1400, 1500)


def make_frame(n: int = N_BARS, t0: pd.Timestamp = T0, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 20000.0 + np.cumsum(rng.normal(0, 3.0, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.uniform(0.2, 2.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.2, 2.5, n)
    return pd.DataFrame({
        "ts": pd.date_range(t0, periods=n, freq="5min"), "open": open_, "high": high, "low": low, "close": close,
        "tick_volume": rng.integers(20, 200, n).astype(float), "spread_pts": np.full(n, 5.0),
    })


def make_mspec() -> SimpleNamespace:
    return SimpleNamespace(
        asset_class="index_cfd", tick_size=0.01, point_size=0.01, canonical=MARKET,
        calendar=SimpleNamespace(tz="Europe/Berlin", cash_open_min=540, cash_close_min=1050),
    )


class FakeData:
    def __init__(self, frame: pd.DataFrame) -> None:
        a = BA.frame_arrays(frame, 0.01)
        self.ts_ns, self.c, self.h, self.l = a["ts_ns"], a["c"], a["h"], a["low"]
        self.atr = BA.causal_atr14(a["h"], a["low"], a["c"])

    def __len__(self) -> int:
        return len(self.c)


def fake_candidates(data: FakeData, event_bars=EVENT_BARS):
    """Deterministic candidates incl. the cases the generator must filter: a duplicate, an early (warm-up) candidate, a NaN stop, a wrong-side stop."""
    idx, dirs, stops = [], [], []
    for k, i in enumerate(event_bars):
        d = 1 if k % 2 == 0 else -1
        idx.append(i), dirs.append(d), stops.append(float(data.c[i]) - d * 2.0 * float(data.atr[i]))
    idx += [event_bars[0], 100, 1600, 1610 % 1700]
    dirs += [1, 1, 1, -1]
    stops += [float(data.c[event_bars[0]]) - 2.0 * float(data.atr[event_bars[0]]), float(data.c[100]) - 5.0, float("nan"), float(data.c[1610]) - 3.0]  # last: short with stop below entry
    n = len(idx)
    return SimpleNamespace(
        decision_idx=np.array(idx, dtype=np.int64), direction=np.array(dirs, dtype=np.int8), stop=np.array(stops), target=np.full(n, np.nan),
        target_r=np.full(n, np.nan), exit_kind=np.zeros(n, dtype=np.int8),
    )


def make_inputs(frame: pd.DataFrame | None = None) -> SimpleNamespace:
    frame = make_frame() if frame is None else frame
    fs = SimpleNamespace(family="STRUCT", strategy_id="STRUCT-fake", role="PRIMARY", thr=None, spec=SimpleNamespace(FAMILY="STRUCT", mode="breakout"))
    return SimpleNamespace(
        market=MARKET, data=FakeData(frame), frame=frame, specs=[fs], eval_from=pd.Timestamp(frame["ts"].iloc[min(300, len(frame) - 1)]), tz="Europe/Berlin", tick_size=0.01,
    )


@contextlib.contextmanager
def patched_generators():
    """Replace the family generator entry points used by ``backfill`` for the duration of the block, then restore the real ones."""
    old = BF.generate_candidates, BF.describe_candidate
    BF.generate_candidates = lambda d, spec, thr: fake_candidates(d)  # type: ignore[assignment]
    BF.describe_candidate = lambda d, spec, i, direction: {"break_bar_offset": 2}  # type: ignore[assignment]
    try:
        yield
    finally:
        BF.generate_candidates, BF.describe_candidate = old


@lru_cache(maxsize=1)
def built() -> dict:
    """Run the backfill once into a temp dir; returns {out, manifest, frame, mspec, mi, seed}."""
    frame = make_frame()
    mi = make_inputs(frame)
    mspec = make_mspec()
    out = Path(tempfile.mkdtemp(prefix="ol_backfill_"))
    with patched_generators():
        manifest = BF.run_market_backfill(mi, mspec, out, seed=7, code=CODE)
    return {"out": out, "mdir": out / MARKET, "manifest": manifest, "frame": frame, "mspec": mspec, "mi": mi, "seed": 7}
