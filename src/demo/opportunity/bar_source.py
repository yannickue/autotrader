# ruff: noqa: E501
"""Bar/quote source protocol of the opportunity engine + a deterministic replay source.

The engine never talks to MT5. It reads from a ``BarSource``:

* ``m5_frame(market, n)``  the last ``n`` CLOSED M5 bars of ``market`` as a DataFrame in the V1 frame
  schema: ``ts`` (tz-aware UTC bar OPEN), ``open, high, low, close`` (BID), ``tick_volume`` and
  ``spread_pts`` (recorded spread in broker POINTS, price = points * point_size). Strictly increasing,
  on the 5-minute grid, no forming bar. The engine still drops any bar whose close lies after ``now``.
* ``latest_quote(market)``  the most recent executable bid/ask (``Quote``) or ``None`` if unknown.
* ``tick_activity(market)``  OPTIONAL (may be absent / return ``None``): ticks or tick volume per minute.

The live MT5 implementation is Lane C's job (it must satisfy exactly this surface). Everything the
engine derives (D1/H4/H1/M15 context, leader alignment) is built from the M5 frames of the markets it
asks for, so a source only ever has to serve M5 + one quote per market.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

M5_SECONDS = 300
FRAME_COLUMNS = ("ts", "open", "high", "low", "close", "tick_volume", "spread_pts")


@dataclass(frozen=True, slots=True, kw_only=True)
class Quote:
    """Latest executable quote (BID / ASK in price units) with its UTC timestamp."""

    ts_utc: datetime
    bid: float
    ask: float

    @property
    def valid(self) -> bool:
        return (
            self.bid > 0.0 and self.ask > 0.0 and self.ask >= self.bid
            and np.isfinite(self.bid) and np.isfinite(self.ask)
        )

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@runtime_checkable
class BarSource(Protocol):
    def m5_frame(self, market: str, n: int | None = None) -> pd.DataFrame: ...

    def latest_quote(self, market: str) -> Quote | None: ...


def tick_activity_of(source: object, market: str) -> float | None:
    """Optional capability: ``source.tick_activity(market)`` if the source provides it."""
    fn = getattr(source, "tick_activity", None)
    if fn is None:
        return None
    val = fn(market)
    return None if val is None else float(val)


def validate_frame(frame: pd.DataFrame, market: str = "?") -> None:
    missing = [c for c in FRAME_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"{market}: frame misses columns {missing}")
    if len(frame) == 0:
        return
    ts = pd.DatetimeIndex(frame["ts"])
    if ts.tz is None or str(ts.tz) != "UTC":
        raise ValueError(f"{market}: ts must be tz-aware UTC")
    if not ts.is_monotonic_increasing or ts.has_duplicates:
        raise ValueError(f"{market}: timestamps not strictly increasing")
    if (ts.as_unit("s").asi8 % M5_SECONDS != 0).any():
        raise ValueError(f"{market}: bars off the M5 grid")
    if frame[["open", "high", "low", "close", "spread_pts", "tick_volume"]].isna().any().any():
        raise ValueError(f"{market}: NaN in bar columns")


def closed_bars_only(frame: pd.DataFrame, now_utc: datetime) -> pd.DataFrame:
    """Bars whose CLOSE (open + 5 min) is <= ``now_utc`` (a forming bar is never usable)."""
    if len(frame) == 0:
        return frame
    now = pd.Timestamp(now_utc)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    ts = pd.DatetimeIndex(frame["ts"])
    keep = (ts + pd.Timedelta(seconds=M5_SECONDS)) <= now
    return frame.loc[keep]


class ReplayBarSource:
    """Deterministic in-memory source for tests / backfill / replay statistics.

    ``set_time(now)`` moves the cursor: only bars whose close is ``<= now`` are visible, so the data
    "beyond" the cursor cannot influence anything (used by the truncation / perturbation tests).
    The quote is synthesised from the last visible bar: ``bid = close``, ``ask = close + spread``,
    timestamped at the bar close (no intra-bar information exists in bar data).
    """

    def __init__(self, frames: Mapping[str, pd.DataFrame], point_sizes: Mapping[str, float]) -> None:
        self._frames: dict[str, pd.DataFrame] = {}
        self._ts: dict[str, np.ndarray] = {}
        self._point = dict(point_sizes)
        for market, frame in frames.items():
            validate_frame(frame, market)
            self._frames[market] = frame.reset_index(drop=True)
            self._ts[market] = pd.DatetimeIndex(frame["ts"]).as_unit("s").asi8
        self._now_s: int | None = None

    def set_time(self, now_utc: datetime) -> None:
        now = pd.Timestamp(now_utc)
        now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
        self._now_s = int(now.as_unit("s").value // 10**9)

    @property
    def now(self) -> datetime:
        if self._now_s is None:
            raise RuntimeError("set_time() first")
        return datetime.fromtimestamp(self._now_s, tz=UTC)

    def markets(self) -> tuple[str, ...]:
        return tuple(self._frames)

    def _end(self, market: str) -> int:
        if self._now_s is None:
            raise RuntimeError("set_time() first")
        # visible: open + 300 <= now  <=>  open <= now - 300
        return int(np.searchsorted(self._ts[market], self._now_s - M5_SECONDS, side="right"))

    def m5_frame(self, market: str, n: int | None = None) -> pd.DataFrame:
        end = self._end(market)
        start = 0 if n is None else max(0, end - n)
        return self._frames[market].iloc[start:end].copy()

    def latest_quote(self, market: str) -> Quote | None:
        end = self._end(market)
        if end == 0:
            return None
        row = self._frames[market].iloc[end - 1]
        close = float(row["close"])
        ask = close + float(row["spread_pts"]) * self._point[market]
        ts = pd.Timestamp(row["ts"]) + pd.Timedelta(seconds=M5_SECONDS)
        return Quote(ts_utc=ts.to_pydatetime(), bid=close, ask=ask)

    def tick_activity(self, market: str) -> float | None:
        end = self._end(market)
        if end == 0:
            return None
        return float(self._frames[market].iloc[end - 1]["tick_volume"]) / 5.0
