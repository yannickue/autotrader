# ruff: noqa: E501
"""Bars adapter of the Market Structure Observer (OBSERVATION ONLY / NOT ALPHA VALIDATED): plain arrays / an M5 frame -> ``ObserverBars``.

ONE code path for the live shadow hook AND the offline backfill: both go through :func:`build_observer_bars` (batch) or :class:`BarBuffer`
(incremental, append-only, used by the live hook). ``BarBuffer.bars()`` after any sequence of ``sync`` calls equals ``build_observer_bars`` of the
same bars (tested bit-exactly), so a feature computed live equals the batch feature at the same decision bar.

``market_observer`` must not import ``alpha``: every market fact (tick size, point size, calendar tz / cash minutes, round steps) is passed in as a
plain value by the caller (``demo.opportunity.observer_hook`` derives them from ``MarketSpec`` / ``alpha.families.data.round_steps``).

Derived arrays (all causal, all independent of how much history was loaded before a bar):

* ``atr``        simple 14-bar mean of the true range (same DEFINITION as ``alpha.families.data.atr14`` / the FeatureStore ``m5_atr14``; the true range
                 of bar j uses the previous bar's close whatever the gap). Evaluated as ``sum(tr[j-13..j]) / 14`` over exactly that window for EVERY j (batch
                 and incremental use the same helper), so the value is a pure function of its own 14 true ranges: no running-sum drift and bit-identical
                 between batch and incremental (pandas ``rolling().mean()`` is path dependent at the last ulp). NaN until 14 bars exist.
* ``segment_id`` +1 at every contiguity break (``ts[j] - ts[j-1] != bar_seconds``): the same fact as ``FamilyData.contig_next`` (``run_start`` additionally
                 splits at a local-day change, which is NOT a data break; callers may pass FamilyData-derived ids through ``segment_id=``).
* ``local_minute`` / ``local_day`` market-local minute-of-day of the bar OPEN and the market-local calendar date as an ABSOLUTE day ordinal (days since
                 1970-01-01), so it does not depend on the loaded window (``alpha.session.local_clock`` numbers the days of the loaded window instead).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_observer.schema import ObserverBars, SessionSpec

NS = 1_000_000_000
ATR_WINDOW = 14
DEFAULT_MAX_BUFFER_BARS = 24_000  # BarBuffer rebuilds from the current frame beyond this many bars (bounded memory, one replay per ~60 days of M5)


# ---------------------------------------------------------------------------------------------- derived arrays
def true_range(h: np.ndarray, low: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Per-bar true range; bar 0 uses its own range (``nanmax`` over the NaN previous close), same as ``alpha.families.data.true_range``."""
    n = len(h)
    prev = np.empty(n)
    if n:
        prev[0] = np.nan
        prev[1:] = c[:-1]
    return np.fmax(np.fmax(h - low, np.abs(h - prev)), np.abs(low - prev))


def atr_at(tr: np.ndarray, j: int) -> float:
    """ATR(14) of bar ``j``: mean of ``tr[j-13..j]`` (NaN if fewer than 14 bars or any true range is NaN)."""
    if j < ATR_WINDOW - 1:
        return float("nan")
    return float(np.sum(tr[j - ATR_WINDOW + 1: j + 1]) / ATR_WINDOW)


def causal_atr14(h: np.ndarray, low: np.ndarray, c: np.ndarray) -> np.ndarray:
    tr = true_range(np.asarray(h, float), np.asarray(low, float), np.asarray(c, float))
    return np.array([atr_at(tr, j) for j in range(len(tr))], dtype=float)


def segment_ids(ts_ns: np.ndarray, bar_seconds: int = 300, first: int = 0) -> np.ndarray:
    ts = np.asarray(ts_ns, dtype=np.int64)
    out = np.full(len(ts), first, dtype=np.int64)
    if len(ts) > 1:
        out[1:] = first + np.cumsum(np.diff(ts) != bar_seconds * NS)
    return out


def local_clock_arrays(ts_ns: np.ndarray, tz: str) -> tuple[np.ndarray, np.ndarray]:
    """(local minute-of-day, local date ordinal) of the bar OPEN timestamps in the IANA zone ``tz`` (DST handled by the tz database)."""
    ts = np.asarray(ts_ns, dtype=np.int64)
    if len(ts) == 0:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    local = pd.DatetimeIndex(ts.view("datetime64[ns]"), tz="UTC").tz_convert(tz)
    minute = (local.hour * 60 + local.minute).to_numpy(np.int64)
    day = local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]").astype(np.int64)
    return minute, day


def session_spec(tz: str, cash_open_min: int | None, cash_close_min: int | None, *, defensible_open: bool = True) -> SessionSpec:
    """``SessionSpec`` from a calendar. ``defensible_open=False`` (24 h crypto, observed-only calendars) drops the cash minutes: no session level is
    ever invented from a provisional bootstrap schedule."""
    if not defensible_open:
        return SessionSpec(tz, None, None)
    return SessionSpec(tz, cash_open_min, cash_close_min)


def _ro(a: np.ndarray) -> np.ndarray:
    a.setflags(write=False)
    return a


# ---------------------------------------------------------------------------------------------- batch
def build_observer_bars(
    market: str, ts_ns: np.ndarray, o: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, tick_volume: np.ndarray, spread: np.ndarray,
    *, tick_size: float, session: SessionSpec, bar_seconds: int = 300, atr: np.ndarray | None = None, segment_id: np.ndarray | None = None,
    local_minute: np.ndarray | None = None, local_day: np.ndarray | None = None,
) -> ObserverBars:
    """Closed-bar arrays (bar OPEN timestamps, ascending; ``spread`` already in PRICE units) -> ``ObserverBars``. Optional ``atr`` / ``segment_id`` /
    ``local_*`` override the derived arrays (e.g. FamilyData-derived ids); they must be causal and the same length."""
    ts = np.array(ts_ns, dtype=np.int64)
    f = lambda a: np.array(a, dtype=float)  # noqa: E731
    o_, h_, l_, c_, v_, s_ = f(o), f(h), f(low), f(c), f(tick_volume), f(spread)
    lm, ld = local_clock_arrays(ts, session.tz)
    bars = ObserverBars(
        market=market, ts_ns=_ro(ts), o=_ro(o_), h=_ro(h_), l=_ro(l_), c=_ro(c_), tick_volume=_ro(v_), spread=_ro(s_),
        atr=_ro(causal_atr14(h_, l_, c_) if atr is None else np.array(atr, dtype=float)),
        segment_id=_ro(segment_ids(ts, bar_seconds) if segment_id is None else np.array(segment_id, dtype=np.int64)),
        local_minute=_ro(lm if local_minute is None else np.array(local_minute, dtype=np.int64)),
        local_day=_ro(ld if local_day is None else np.array(local_day, dtype=np.int64)),
        tick_size=float(tick_size), session=session, bar_seconds=int(bar_seconds),
    )
    bars.validate()
    return bars


def frame_arrays(frame: pd.DataFrame, point_size: float) -> dict[str, np.ndarray]:
    """The demo V1 frame (``ts`` tz-aware UTC bar open, ``open/high/low/close``, ``tick_volume``, ``spread_pts``) as plain arrays; copies, the frame is never mutated."""
    ts = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8.astype(np.int64)
    return {
        "ts_ns": ts, "o": frame["open"].to_numpy(float).copy(), "h": frame["high"].to_numpy(float).copy(),
        "low": frame["low"].to_numpy(float).copy(), "c": frame["close"].to_numpy(float).copy(),
        "tick_volume": frame["tick_volume"].to_numpy(float).copy(),
        "spread": frame["spread_pts"].to_numpy(float) * float(point_size),
    }


def bars_from_frame(
    market: str, frame: pd.DataFrame, *, point_size: float, tick_size: float, session: SessionSpec, bar_seconds: int = 300, **overrides: np.ndarray,
) -> ObserverBars:
    """THE function both the live hook and the offline backfill use to turn a frame of CLOSED M5 bars into ``ObserverBars``."""
    return build_observer_bars(market, **frame_arrays(frame, point_size), tick_size=tick_size, session=session, bar_seconds=bar_seconds, **overrides)


# ---------------------------------------------------------------------------------------------- incremental
class BarBuffer:
    """Append-only per-market bar store for the live hook. The engine's frame is a SLIDING window (index 0 moves every bar) but the level registry
    is sequential in absolute indices, so the observer keeps its own growing arrays and appends only the bars newer than its last one.

    ``sync`` outcomes: ``init`` (first bars), ``append``, ``noop`` (nothing newer / an older frame), ``reset`` (frame does not overlap or contradicts
    the buffer, or the buffer is longer than ``max_bars``: rebuilt from the frame; the caller must restart anything that follows the buffer
    index, e.g. the level registry). Bars already buffered are never revised."""

    def __init__(self, market: str, *, tick_size: float, session: SessionSpec, bar_seconds: int = 300, max_bars: int = DEFAULT_MAX_BUFFER_BARS) -> None:
        self.market, self.tick_size, self.session, self.bar_seconds, self.max_bars = market, float(tick_size), session, int(bar_seconds), int(max_bars)
        self.generation = 0  # +1 at every reset
        self._clear(16)

    def _clear(self, cap: int) -> None:
        self._n = 0
        self._cap = cap
        self._ts = np.zeros(cap, np.int64)
        self._f = {k: np.zeros(cap) for k in ("o", "h", "l", "c", "v", "s", "atr", "tr")}
        self._seg = np.zeros(cap, np.int64)
        self._lm = np.zeros(cap, np.int64)
        self._ld = np.zeros(cap, np.int64)

    def __len__(self) -> int:
        return self._n

    @property
    def last_ts_ns(self) -> int | None:
        return int(self._ts[self._n - 1]) if self._n else None

    def _grow(self, need: int) -> None:
        if need <= self._cap:
            return
        cap = max(need, self._cap * 2)
        for name in ("_ts", "_seg", "_lm", "_ld"):
            new = np.zeros(cap, np.int64)
            new[: self._n] = getattr(self, name)[: self._n]
            setattr(self, name, new)
        for k, a in self._f.items():
            new = np.zeros(cap)
            new[: self._n] = a[: self._n]
            self._f[k] = new
        self._cap = cap

    def _append(self, ts: np.ndarray, o: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, v: np.ndarray, s: np.ndarray) -> None:
        k = len(ts)
        if k == 0:
            return
        n0 = self._n
        self._grow(n0 + k)
        sl = slice(n0, n0 + k)
        self._ts[sl] = ts
        for key, arr in (("o", o), ("h", h), ("l", low), ("c", c), ("v", v), ("s", s)):
            self._f[key][sl] = arr
        prev_c = np.empty(k)
        prev_c[0] = self._f["c"][n0 - 1] if n0 else np.nan
        prev_c[1:] = c[:-1]
        self._f["tr"][sl] = np.fmax(np.fmax(h - low, np.abs(h - prev_c)), np.abs(low - prev_c))
        tr = self._f["tr"]
        self._f["atr"][sl] = [atr_at(tr, j) for j in range(n0, n0 + k)]
        first = int(self._seg[n0 - 1]) if n0 else 0
        seg = np.empty(k, np.int64)
        brk = np.diff(ts) != self.bar_seconds * NS
        seg[0] = first + (1 if (n0 and int(ts[0]) - int(self._ts[n0 - 1]) != self.bar_seconds * NS) else 0)
        seg[1:] = seg[0] + np.cumsum(brk)
        self._seg[sl] = seg
        lm, ld = local_clock_arrays(ts, self.session.tz)
        self._lm[sl], self._ld[sl] = lm, ld
        self._n = n0 + k

    def sync(
        self, ts_ns: np.ndarray, o: np.ndarray, h: np.ndarray, low: np.ndarray, c: np.ndarray, tick_volume: np.ndarray, spread: np.ndarray,
        max_append: int | None = None,
    ) -> str:
        """Append the frame bars newer than the buffer's last. ``max_append`` bounds the work of ONE call (the ATR of an appended bar is the expensive
        part: ~6000 bars at a cold start / reset): at most that many bars are appended, the caller calls again with the SAME frame until
        ``last_ts_ns`` equals the frame's last bar (the buffer then holds exactly what an unbounded call would have produced, tested bit-identical)."""
        ts = np.asarray(ts_ns, dtype=np.int64)
        n_f = len(ts)
        if n_f == 0:
            return "noop"
        arrs = (o, h, low, c, tick_volume, spread)
        step = self.bar_seconds * NS

        def take(a: int) -> tuple[np.ndarray, ...]:
            b = n_f if max_append is None else min(n_f, a + max(1, int(max_append)))
            return (ts[a:b], *(np.asarray(x, dtype=float)[a:b] for x in arrs))

        if self._n == 0:
            self._append(*take(0))
            return "init"
        last = int(self._ts[self._n - 1])
        if int(ts[-1]) < last:  # an OLDER frame (e.g. a past-bar evaluation): nothing to append; inconsistent only if its bars are unknown to the buffer
            if self.index_of(int(ts[-1])) is not None:
                return "noop"
            self._clear(max(16, n_f))
            self.generation += 1
            self._append(*take(0))
            return "reset"
        k = int(np.searchsorted(ts, last, side="right"))  # first frame bar newer than the buffer's last
        reset = False
        if k > 0 and int(ts[k - 1]) != last:
            reset = True  # the buffered last bar is absent from the frame: inconsistent source
        elif k == 0 and int(ts[0]) - last != step:
            reset = True  # no overlap and not directly contiguous: bars were missed
        elif self._n + (n_f - k) > self.max_bars:
            reset = True
        if reset:
            self._clear(max(16, n_f))
            self.generation += 1
            self._append(*take(0))
            return "reset"
        if k >= n_f:
            return "noop"
        self._append(*take(k))
        return "append"

    def bars(self, n: int | None = None) -> ObserverBars:
        """Read-only ``ObserverBars`` view over the first ``n`` (default all) buffered bars (no copy)."""
        n = self._n if n is None else n
        f = self._f

        def v(a: np.ndarray) -> np.ndarray:
            x = a[:n].view()
            x.setflags(write=False)
            return x

        return ObserverBars(
            self.market, v(self._ts), v(f["o"]), v(f["h"]), v(f["l"]), v(f["c"]), v(f["v"]), v(f["s"]), v(f["atr"]), v(self._seg), v(self._lm), v(self._ld),
            self.tick_size, self.session, self.bar_seconds,
        )

    def index_of(self, ts_ns: int) -> int | None:
        i = int(np.searchsorted(self._ts[: self._n], ts_ns, side="left"))
        return i if i < self._n and int(self._ts[i]) == ts_ns else None

