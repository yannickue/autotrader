# ruff: noqa: E501
"""Shared helpers for the opportunity-engine tests (real dev bars, no MT5)."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from alpha.families.spec import EffectiveWindow
from demo.opportunity.bar_source import M5_SECONDS, Quote, ReplayBarSource
from demo.opportunity.policy import Candidate
from markets.spec import MarketSpec


def bar_times(frame: pd.DataFrame, start: str, end: str) -> list[pd.Timestamp]:
    ts = pd.DatetimeIndex(frame["ts"])
    a, b = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    return [t for t in ts if a <= t < b]


def now_of(bar_open: pd.Timestamp) -> datetime:
    return (bar_open + pd.Timedelta(seconds=M5_SECONDS)).to_pydatetime()


class LeakySource:
    """A hostile source: returns ALL bars (also the future ones, optionally garbage-mutated).

    The engine must only use bars closed at ``now``; if any future bar reached the output the
    results would differ from the clean ``ReplayBarSource``. The quote stays time-bound (a quote
    cannot exist before it is made)."""

    def __init__(self, base: ReplayBarSource, frames: dict[str, pd.DataFrame], *, mutate: bool,
                 seed: int = 7) -> None:
        self._base = base
        self._frames = frames
        self._mutate = mutate
        self._rng = np.random.default_rng(seed)

    def m5_frame(self, market: str, n: int | None = None) -> pd.DataFrame:
        fr = self._frames[market]
        now = pd.Timestamp(self._base.now)
        cut = now - pd.Timedelta(seconds=M5_SECONDS)  # last closed bar OPEN
        ts = pd.DatetimeIndex(fr["ts"])
        end = int(np.searchsorted(ts.as_unit("ns").asi8, cut.value, side="right"))
        upper = min(len(fr), end + 3000)
        lo = 0 if n is None else max(0, end - n)  # same past window as the clean source
        out = fr.iloc[lo:upper].copy()
        if self._mutate:
            fut = np.arange(lo, lo + len(out)) >= end
            k = int(fut.sum())
            if k:
                scale = self._rng.uniform(0.4, 2.5, size=k)
                for col in ("open", "high", "low", "close"):
                    out.loc[out.index[fut], col] = out.loc[out.index[fut], col].to_numpy() * scale
                out.loc[out.index[fut], "spread_pts"] = 99999.0
                out.loc[out.index[fut], "tick_volume"] = 123456.0
        return out

    def latest_quote(self, market: str) -> Quote | None:
        return self._base.latest_quote(market)

    def tick_activity(self, market: str) -> float | None:
        return self._base.tick_activity(market)


def synth_candidate(
    mspec: MarketSpec, *, direction: int = 1, close: float = 100.0, atr: float = 1.0,
    bar_spread: float = 0.1, stop: float | None = None, target: float = float("nan"),
    target_r: float = 1.5, min_space_r: float = float("nan"),
    signal_utc: datetime | None = None, window: EffectiveWindow | None = None,
    exit_kind: int = 0,
) -> Candidate:
    sig = signal_utc or datetime(2026, 6, 10, 8, 0, tzinfo=UTC)  # Wed; 10:00 Berlin, 04:00 NY, 09:00 London
    win = window or EffectiveWindow(
        mspec.calendar.entry_start_min, mspec.calendar.entry_end_min, mspec.calendar.forced_flat_min
    )
    if stop is None:
        stop = close - direction * 1.0
    return Candidate(
        market=mspec.canonical, broker_symbol=mspec.broker_symbol, family="ORB",
        strategy_id="ORB-test", spec_hash="testhash", direction=direction, signal_ts=sig,
        bar_open_ts=datetime.fromtimestamp(sig.timestamp() - M5_SECONDS, tz=UTC), close=close,
        atr=atr, bar_spread=bar_spread, stop=stop, target=target, target_r=target_r,
        exit_kind=exit_kind, min_space_r=min_space_r, window=win,
    )
