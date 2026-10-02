# ruff: noqa: E501
"""Deterministic synthetic ``ObserverBars`` builders + mirror/prefix helpers for the MarketMap / main-thesis tests (no tests here)."""

from __future__ import annotations

import numpy as np

from market_observer.schema import ObserverBars, SessionSpec

NS = 1_000_000_000
BAR = 300
DAY0 = 20_000  # arbitrary UTC day ordinal; day * 86400 is aligned to an hour boundary (H1 buckets line up)


def curve(
    kind: str, n: int, *, amp: float = 1.5, period: int = 60, slope: float = 0.02
) -> np.ndarray:
    k = np.arange(n, dtype=float)
    wave = amp * np.sin(2 * np.pi * k / period)
    if kind == "up":
        return 100.0 + slope * k + wave
    if kind == "down":
        return 100.0 - slope * k - wave
    if kind == "flat":
        return 100.0 + wave
    raise ValueError(kind)


def bars_from_closes(
    closes: np.ndarray,
    *,
    seed: int = 1,
    bars_per_day: int | None = None,
    session: SessionSpec | None = None,
    market: str = "SYN",
    tick: float = 0.01,
    wick: float = 0.08,
    tv_base: float = 50.0,
) -> ObserverBars:
    """Closes -> OHLC (open = previous close). ``bars_per_day=None``: one contiguous 24h timeline (single segment). Otherwise each
    local day holds that many bars, the next day starts at 08:00 UTC and the data break bumps ``segment_id``."""
    n = len(closes)
    rng = np.random.default_rng(seed)
    c = np.asarray(closes, dtype=float)
    o = np.concatenate([[c[0]], c[:-1]])
    h = np.maximum(o, c) + np.abs(rng.normal(0.0, wick, n)) + tick
    lo = np.minimum(o, c) - np.abs(rng.normal(0.0, wick, n)) - tick
    ts = np.zeros(n, dtype=np.int64)
    seg = np.zeros(n, dtype=np.int64)
    minute = np.zeros(n, dtype=np.int64)
    day = np.zeros(n, dtype=np.int64)
    if bars_per_day is None:
        t0 = DAY0 * 86400
        for i in range(n):
            t = t0 + i * BAR
            ts[i] = t * NS
            day[i] = t // 86400
            minute[i] = (t % 86400) // 60
    else:
        d, in_day, s = DAY0, 0, 0
        t = d * 86400 + 8 * 3600
        for i in range(n):
            if in_day == bars_per_day:
                d += 1
                in_day = 0
                s += 1
                t = d * 86400 + 8 * 3600
            ts[i], seg[i], day[i], minute[i] = t * NS, s, d, (t - d * 86400) // 60
            t += BAR
            in_day += 1
    tr = np.empty(n)
    tr[0] = h[0] - lo[0]
    tr[1:] = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])])
    atr = np.full(n, np.nan)
    for i in range(13, n):
        atr[i] = float(tr[i - 13 : i + 1].mean())
    tv = tv_base + rng.integers(0, 20, n).astype(float)
    return ObserverBars(
        market,
        ts,
        o,
        h,
        lo,
        c,
        tv,
        np.full(n, 0.02),
        atr,
        seg,
        minute,
        day,
        tick,
        session or SessionSpec("UTC", 8 * 60, 16 * 60),
        BAR,
    )


def synth(kind: str, n: int = 800, *, seed: int = 1, **kw) -> ObserverBars:
    return bars_from_closes(curve(kind, n), seed=seed, **kw)


def mirror_bars(b: ObserverBars, pivot: float = 0.0) -> ObserverBars:
    """Price mirror p -> pivot - p (highs and lows swap); time, ATR, spread, ticks, segments unchanged."""
    return ObserverBars(
        b.market,
        b.ts_ns,
        pivot - b.o,
        pivot - b.l,
        pivot - b.h,
        pivot - b.c,
        b.tick_volume,
        b.spread,
        b.atr,
        b.segment_id,
        b.local_minute,
        b.local_day,
        b.tick_size,
        b.session,
        b.bar_seconds,
    )
