# ruff: noqa: E501
"""Shared synthetic-bar builders for the levels tests (no tests in this module)."""

from __future__ import annotations

import numpy as np

from market_observer import schema as S

NS = 1_000_000_000
BAR = 300
DAY0 = 20_000  # arbitrary local-day ordinal origin


def make_bars(
    rows: list[tuple[float, float, float, float]],
    *,
    minutes: list[int] | None = None,
    days: list[int] | None = None,
    segments: list[int] | None = None,
    atr: float | list[float] = 1.0,
    spread: float = 0.0,
    tick: float = 0.01,
    session: S.SessionSpec | None = None,
    market: str = "T",
) -> S.ObserverBars:
    """rows = (o, h, l, c). Default: one day, one segment, consecutive 5-minute bars from minute 0. ts is derived from
    day*86400 + minute*60 (UTC == local), so a gap in ``minutes``/``days`` is a gap in time."""
    n = len(rows)
    minutes = minutes if minutes is not None else [5 * k for k in range(n)]
    days = days if days is not None else [DAY0] * n
    segments = segments if segments is not None else [0] * n
    a = np.asarray(rows, dtype=float)
    ts = np.array([(d * 86400 + m * 60) * NS for d, m in zip(days, minutes, strict=True)], dtype=np.int64)
    atr_arr = np.full(n, atr, dtype=float) if isinstance(atr, (int, float)) else np.asarray(atr, dtype=float)
    return S.ObserverBars(
        market, ts, a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.full(n, 50.0), np.full(n, spread), atr_arr,
        np.asarray(segments, dtype=np.int64), np.asarray(minutes, dtype=np.int64), np.asarray(days, dtype=np.int64), tick,
        session or S.SessionSpec("UTC", None, None),
    )


def flat(price: float, k: int = 1, half: float = 0.05) -> list[tuple[float, float, float, float]]:
    return [(price, price + half, price - half, price)] * k


def closes_to_rows(closes: list[float], wick: float = 0.1) -> list[tuple[float, float, float, float]]:
    rows = []
    prev = closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + wick, min(prev, c) - wick, c))
        prev = c
    return rows


def random_walk(n: int, seed: int, *, segment_breaks: tuple[int, ...] = (), start: float = 100.0, bars_per_day: int = 40,
                session: S.SessionSpec | None = None, tick: float = 0.01, atr_warm: int = 14) -> S.ObserverBars:
    """Random-walk bars with optional segment breaks (time gap + segment id bump) and a daily structure (``bars_per_day`` bars/day,
    days advancing by 1, every 5th day by 3 to emulate a weekend)."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.4, n)
    c = start + np.cumsum(steps)
    o = np.concatenate([[start], c[:-1]])
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.15, n))
    lo = np.minimum(o, c) - np.abs(rng.normal(0, 0.15, n))
    ts = np.zeros(n, dtype=np.int64)
    seg = np.zeros(n, dtype=np.int64)
    minute = np.zeros(n, dtype=np.int64)
    day = np.zeros(n, dtype=np.int64)
    d = DAY0
    cur_seg = 0
    k_in_day = 0
    t = d * 86400
    for i in range(n):
        if i > 0 and i in segment_breaks:
            cur_seg += 1
            t += 3600  # pause inside the same day (segment break)
        if k_in_day == bars_per_day:
            step = 3 if (d - DAY0) % 5 == 4 else 1
            d += step
            k_in_day = 0
            t = d * 86400 + 8 * 3600  # new day starts at 08:00
            cur_seg += 1  # every overnight / weekend gap is a contiguity break (segment boundary)
        if i == 0:
            t = d * 86400 + 8 * 3600
        ts[i] = t * NS
        minute[i] = ((t - d * 86400) // 60)
        day[i] = d
        seg[i] = cur_seg
        t += BAR
        k_in_day += 1
    atr = np.full(n, np.nan)
    tr = h - lo
    for i in range(atr_warm, n):
        atr[i] = float(np.mean(tr[i - atr_warm + 1:i + 1]))
    return S.ObserverBars(
        "RW", ts, o, h, lo, c, np.full(n, 50.0), np.full(n, 0.02), atr, seg, minute, day, tick, session or S.SessionSpec("UTC", 8 * 60 + 30, 8 * 60 + 150),
    )


def mirror(b: S.ObserverBars) -> S.ObserverBars:
    """Price mirror p -> -p (highs and lows swap); time, ATR, spread, segments unchanged."""
    return S.ObserverBars(b.market, b.ts_ns, -b.o, -b.l, -b.h, -b.c, b.tick_volume, b.spread, b.atr, b.segment_id, b.local_minute,
                          b.local_day, b.tick_size, b.session, b.bar_seconds)


def window(b: S.ObserverBars, start: int) -> S.ObserverBars:
    """Bars ``start..end`` only: what a live adapter holds after loading less history (index shift = ``start``)."""
    sl = slice(start, None)
    return S.ObserverBars(b.market, b.ts_ns[sl], b.o[sl], b.h[sl], b.l[sl], b.c[sl], b.tick_volume[sl], b.spread[sl], b.atr[sl],
                          b.segment_id[sl], b.local_minute[sl], b.local_day[sl], b.tick_size, b.session, b.bar_seconds)
