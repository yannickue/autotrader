"""Higher-timeframe series and their causal mapping to M5 (V2 temporal engine, W1).

A ``TfSeries`` holds one timeframe's COMPLETE bars as a compact series (``n`` bars, no
incomplete bar ever enters it) together with the alignment to the M5 grid, derived from the
existing ``alpha.timeframe`` machinery (``derive_mtf`` / ``_alignment``):

* ``vis[i]`` = compact index of the newest HTF bar visible at M5 bar i (-1 = none): an HTF bar
  b is visible at i iff it is complete and ``available_at[b] <= ts[i] + 5min``.
* ``stamp[j]`` = the FIRST M5 bar i with ``vis[i] >= j``: a PULSE of HTF bar j is written to
  exactly that one M5 bar (a data hole can only move it later, never earlier); a bar that is
  never visible before the data ends emits nothing.
* states / levels are forward-filled through ``vis`` (same as ``_map_higher``).
* ``sf_*`` "so far" arrays (bar progress, partial high/low) are the only place where the
  currently forming HTF bar's data appears; they include only M5 bars <= i.

D1 = Berlin calendar day (``berlin_day_id``); day d is complete once day d+1 has bars and is
visible from the close of the first M5 bar of day d+1.
"""

from __future__ import annotations

from dataclasses import dataclass

import numba as nb
import numpy as np
import pandas as pd

from alpha.timeframe import _alignment, derive_mtf

_STEP_NS = 5 * 60 * 10**9
_TF_MINUTES = {"M5": 5, "M15": 15, "H1": 60}


@dataclass
class TfSeries:
    tf: str
    n: int  # compact bar count
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    m5_start: np.ndarray  # int64, M5 index of each bar's first source bar
    m5_end: np.ndarray  # int64, M5 index of each bar's last source bar
    vis: np.ndarray  # int64[N]
    stamp: np.ndarray  # int64[n], N = never visible
    bucket: np.ndarray  # int64[N] bucket (bar) id of each M5 bar's own, possibly incomplete, bar
    bucket_open_ns: np.ndarray  # int64[N] open time of that bucket
    n_m5: int

    # ---- mapping to the M5 grid ------------------------------------------------------------
    def pulse_to_m5(self, pulse: np.ndarray) -> np.ndarray:
        out = np.zeros(self.n_m5, dtype=np.uint8)
        idx = np.flatnonzero(pulse)
        s = self.stamp[idx]
        out[s[s < self.n_m5]] = 1
        return out

    def values_to_m5(self, values: np.ndarray, pulse: np.ndarray):
        """Pulse-bound values (evl/evx float, origin int): written on the stamp bar only
        (NaN / -1 elsewhere)."""
        if values.dtype.kind in "iu":
            out = np.full(self.n_m5, -1, dtype=values.dtype)
        else:
            out = np.full(self.n_m5, np.nan, dtype=float)
        idx = np.flatnonzero(pulse)
        s = self.stamp[idx]
        keep = s < self.n_m5
        out[s[keep]] = values[idx[keep]]
        return out

    def origin_to_m5_index(self, origin: np.ndarray) -> np.ndarray:
        """Origin bar (compact index, -1 none) -> M5 index of that bar's first source bar."""
        out = np.full(len(origin), -1, dtype=np.int64)
        ok = origin >= 0
        out[ok] = self.m5_start[origin[ok]]
        return out

    def ffill_to_m5(self, values: np.ndarray, fill=None) -> np.ndarray:
        """State / level of the newest visible bar (``_map_higher`` semantics)."""
        if values.dtype.kind in "iub":
            out = np.zeros(self.n_m5, dtype=values.dtype)
        else:
            out = np.full(self.n_m5, np.nan if fill is None else fill, dtype=values.dtype)
        known = self.vis >= 0
        out[known] = values[self.vis[known]]
        return out


def _finish(
    tf: str,
    o,
    h,
    low,
    c,
    m5_start,
    m5_end,
    vis,
    bucket,
    bucket_open_ns,
    n_m5,
) -> TfSeries:
    n = len(o)
    stamp = np.searchsorted(vis, np.arange(n), side="left").astype(np.int64)
    return TfSeries(tf, n, o, h, low, c, m5_start, m5_end, vis, stamp, bucket, bucket_open_ns, n_m5)


def build_series(features, tf: str) -> TfSeries:
    """Compact complete-bar series + M5 alignment for ``tf`` from a FeatureSet's M5 arrays."""
    ts_ns = np.asarray(features["ts_ns"], dtype=np.int64)
    o = np.asarray(features["o"], dtype=float)
    h = np.asarray(features["h"], dtype=float)
    low = np.asarray(features["l"], dtype=float)
    c = np.asarray(features["c"], dtype=float)
    n_m5 = len(ts_ns)
    idx = np.arange(n_m5, dtype=np.int64)
    if tf == "M5":
        return _finish("M5", o, h, low, c, idx, idx, idx.copy(), idx, ts_ns.copy(), n_m5)
    if tf == "D1":
        return _build_d1(features, ts_ns, o, h, low, c)
    if tf not in _TF_MINUTES:
        raise ValueError(f"unknown timeframe {tf!r}")
    frame = pd.DataFrame(
        {
            "ts": pd.to_datetime(ts_ns, utc=True),
            "open": o,
            "high": h,
            "low": low,
            "close": c,
            "spread_pts": np.zeros(n_m5),
        }
    )
    derived = derive_mtf(frame)
    table = derived.m15 if tf == "M15" else derived.h1
    m5_index = pd.DatetimeIndex(frame["ts"])
    alignment = _alignment(m5_index, table)
    complete = table["complete"].to_numpy(bool)
    completed = np.flatnonzero(complete)
    comp_of_full = np.full(len(table), -1, dtype=np.int64)
    comp_of_full[completed] = np.arange(len(completed))
    vis = np.full(n_m5, -1, dtype=np.int64)
    known = alignment >= 0
    vis[known] = comp_of_full[alignment[known]]
    src_start = table["source_start"].to_numpy(np.int64)
    src_end = table["source_end"].to_numpy(np.int64)
    bucket = np.searchsorted(src_start, idx, side="right") - 1
    open_ns = table.index.as_unit("ns").asi8
    return _finish(
        tf,
        table["open"].to_numpy(float)[completed],
        table["high"].to_numpy(float)[completed],
        table["low"].to_numpy(float)[completed],
        table["close"].to_numpy(float)[completed],
        src_start[completed],
        src_end[completed],
        vis,
        bucket.astype(np.int64),
        open_ns[bucket].astype(np.int64),
        n_m5,
    )


def _build_d1(features, ts_ns, o, h, low, c) -> TfSeries:
    n_m5 = len(ts_ns)
    day = np.asarray(features["berlin_day_id"], dtype=np.int64)
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]]).astype(np.int64)
    ends = np.r_[starts[1:], n_m5].astype(np.int64) - 1
    n_days = len(starts)
    n = max(n_days - 1, 0)  # a day is complete only once the next day has bars
    d_o = o[starts][:n]
    d_h = np.maximum.reduceat(h, starts)[:n] if n_days else h[:0]
    d_l = np.minimum.reduceat(low, starts)[:n] if n_days else low[:0]
    d_c = c[ends][:n]
    # M5 bar in day k sees day k-1 (available from the close of the first bar of day k)
    pos = np.searchsorted(starts, np.arange(n_m5), side="right") - 1  # day index per M5 bar
    vis = (pos - 1).astype(np.int64)
    vis[vis < 0] = -1
    bucket = pos.astype(np.int64)
    return _finish(
        "D1", d_o, d_h, d_l, d_c, starts[:n], ends[:n], vis, bucket, ts_ns[starts][pos], n_m5
    )


@nb.njit(cache=True)
def _partial_extremes(h, low, bucket):
    n = len(h)
    ph = np.empty(n)
    pl = np.empty(n)
    for i in range(n):
        if i > 0 and bucket[i] == bucket[i - 1]:
            ph[i] = max(ph[i - 1], h[i])
            pl[i] = min(pl[i - 1], low[i])
        else:
            ph[i] = h[i]
            pl[i] = low[i]
    return ph, pl


def so_far_arrays(features, series: TfSeries) -> dict[str, np.ndarray]:
    """``sf_{tf}_bar_progress`` / ``partial_h`` / ``partial_l``: the forming bar's data through
    M5 bar i inclusive (the ONLY place partial HTF data appears)."""
    h = np.asarray(features["h"], dtype=float)
    low = np.asarray(features["l"], dtype=float)
    ts_ns = np.asarray(features["ts_ns"], dtype=np.int64)
    tf = series.tf
    if tf == "M5":
        progress = np.ones(len(h))
    elif tf == "D1":
        progress = (np.asarray(features["berlin_minute"], dtype=float) + 5.0) / 1440.0
    else:
        span = _TF_MINUTES[tf] * 60.0 * 1e9
        progress = (ts_ns - series.bucket_open_ns + _STEP_NS) / span
    ph, pl = _partial_extremes(h, low, series.bucket)
    return {"progress": np.clip(progress, 0.0, 1.0), "partial_h": ph, "partial_l": pl}
