"""Causal Berlin-clock multi-timeframe views derived from validated AR1 M5 bars.

AR1 timestamps are timezone-aware UTC *bar-open* times.  A row at ``10:35`` is therefore
available at its ``10:40`` close.  M15/H1 boundaries are calculated in Europe/Berlin wall time
(``:00/:15/:30/:45`` and ``:00``), while bucket identities remain UTC instants.  This preserves
both distinct copies of the repeated autumn hour and naturally omits the nonexistent spring hour.
No timezone-naive timestamp is accepted.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from alpha.common.dataset import BAR_SECONDS, BERLIN

_REQUIRED = ("ts", "open", "high", "low", "close", "spread_pts")


@dataclass(frozen=True)
class MtfData:
    """Derived higher-timeframe tables, including explicitly incomplete buckets."""

    m15: pd.DataFrame
    h1: pd.DataFrame


@dataclass(frozen=True)
class SessionLevels:
    """Reference levels known at the close of one M5 bar."""

    previous_day_high: float | None
    previous_day_low: float | None
    previous_day_close: float | None
    session_open: float
    session_high: float
    session_low: float
    phase: str


@dataclass(frozen=True)
class MtfState:
    """Information available at one completed M5 close."""

    m5: pd.Series
    m15: pd.Series | None
    h1: pd.Series | None
    levels: SessionLevels


def _validated_m5(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(_REQUIRED).difference(frame.columns)
    if missing:
        raise ValueError(f"missing M5 columns: {sorted(missing)}")
    out = frame.loc[:, _REQUIRED].copy()
    index = pd.DatetimeIndex(out.pop("ts"))
    if index.tz is None:
        raise ValueError("M5 timestamps must be timezone-aware bar-open times")
    index = index.tz_convert("UTC")
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("M5 timestamps must be strictly increasing and unique")
    seconds = index.as_unit("s").asi8
    if np.any(seconds % BAR_SECONDS):
        raise ValueError("M5 timestamps must lie on exact five-minute boundaries")
    out.index = index
    out.index.name = "ts"
    return out


def _bucket_opens(index: pd.DatetimeIndex, minutes: int) -> pd.DatetimeIndex:
    local = index.tz_convert(BERLIN)
    within = np.asarray(local.minute % minutes, dtype=np.int64)
    # Subtraction is performed on absolute instants.  The wall-clock remainder selects the
    # boundary, and UTC identity keeps the two autumn 02:00 buckets separate.
    return index - pd.to_timedelta(within, unit="min")


def _aggregate(m5: pd.DataFrame, minutes: int) -> pd.DataFrame:
    expected_count = minutes // 5
    buckets = _bucket_opens(m5.index, minutes)
    records: list[dict[str, object]] = []
    starts: list[pd.Timestamp] = []
    for bucket_open, positions in pd.Series(
        np.arange(len(m5)), index=buckets
    ).groupby(level=0, sort=False):
        source_positions = positions.to_numpy(dtype=np.int64)
        source = m5.iloc[source_positions]
        expected = pd.date_range(bucket_open, periods=expected_count, freq="5min")
        complete = len(source) == expected_count and source.index.equals(expected)
        starts.append(bucket_open)
        records.append(
            {
                "open": float(source["open"].iloc[0]) if complete else np.nan,
                "high": float(source["high"].max()) if complete else np.nan,
                "low": float(source["low"].min()) if complete else np.nan,
                "close": float(source["close"].iloc[-1]) if complete else np.nan,
                "spread_pts": float(source["spread_pts"].iloc[-1]) if complete else np.nan,
                "complete": bool(complete),
                "source_count": len(source),
                "source_start": int(source_positions[0]),
                "source_end": int(source_positions[-1]),
                "available_at": bucket_open + pd.Timedelta(minutes=minutes),
            }
        )
    result = pd.DataFrame(records, index=pd.DatetimeIndex(starts, name="ts"))
    return result


def derive_mtf(frame: pd.DataFrame) -> MtfData:
    """Derive deterministic M15 and H1 bars without filling missing M5 observations."""

    m5 = _validated_m5(frame)
    return MtfData(m15=_aggregate(m5, 15), h1=_aggregate(m5, 60))


def _alignment(
    m5_index: pd.DatetimeIndex, higher: pd.DataFrame
) -> np.ndarray:
    result = np.full(len(m5_index), -1, dtype=np.int64)
    completed = np.flatnonzero(higher["complete"].to_numpy(bool))
    if not len(completed):
        return result
    available_ns = pd.DatetimeIndex(higher.iloc[completed]["available_at"]).asi8
    m5_close_ns = (m5_index + pd.Timedelta(minutes=5)).asi8
    locations = np.searchsorted(available_ns, m5_close_ns, side="right") - 1
    known = locations >= 0
    result[known] = completed[locations[known]]
    return result


def _phase(minute: int) -> str:
    if 9 * 60 <= minute < 10 * 60:
        return "EUROPEAN_OPEN"
    if 10 * 60 <= minute < 12 * 60:
        return "MORNING"
    if 12 * 60 <= minute < 15 * 60 + 30:
        return "MIDDAY"
    if 15 * 60 + 30 <= minute < 17 * 60 + 30:
        return "US_CASH_OPEN_OVERLAP"
    return "LATE"


def _session_levels(m5: pd.DataFrame) -> tuple[SessionLevels, ...]:
    local = m5.index.tz_convert(BERLIN)
    dates = local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
    unique_dates, day = np.unique(dates, return_inverse=True)
    del unique_dates
    levels: list[SessionLevels] = []
    completed_days: dict[int, tuple[float, float, float]] = {}
    for day_id in range(int(day.max()) + 1):
        positions = np.flatnonzero(day == day_id)
        source = m5.iloc[positions]
        previous = completed_days[day_id - 1] if day_id else (None, None, None)
        running_high = source["high"].cummax().to_numpy(float)
        running_low = source["low"].cummin().to_numpy(float)
        session_open = float(source["open"].iloc[0])
        for offset, position in enumerate(positions):
            stamp = local[position]
            levels.append(
                SessionLevels(
                    previous_day_high=previous[0],
                    previous_day_low=previous[1],
                    previous_day_close=previous[2],
                    session_open=session_open,
                    session_high=float(running_high[offset]),
                    session_low=float(running_low[offset]),
                    phase=_phase(stamp.hour * 60 + stamp.minute),
                )
            )
        completed_days[day_id] = (
            float(source["high"].max()),
            float(source["low"].min()),
            float(source["close"].iloc[-1]),
        )
    return tuple(levels)


class MtfView:
    """Precomputed causal alignment from every completed M5 bar to M15/H1 state."""

    def __init__(self, frame: pd.DataFrame):
        self.m5 = _validated_m5(frame)
        derived = derive_mtf(frame)
        self.m15 = derived.m15
        self.h1 = derived.h1
        self.m15_alignment = _alignment(self.m5.index, self.m15)
        self.h1_alignment = _alignment(self.m5.index, self.h1)
        self._levels = _session_levels(self.m5)

    def _position(self, index_or_ts: int | pd.Timestamp) -> int:
        if isinstance(index_or_ts, (int, np.integer)):
            position = int(index_or_ts)
            if position < 0 or position >= len(self.m5):
                raise IndexError(position)
            return position
        stamp = pd.Timestamp(index_or_ts)
        if stamp.tzinfo is None:
            raise ValueError("lookup timestamp must be timezone-aware")
        location = self.m5.index.get_indexer([stamp.tz_convert("UTC")])[0]
        if location < 0:
            raise KeyError(stamp)
        return int(location)

    @staticmethod
    def _known(table: pd.DataFrame, position: int) -> pd.Series | None:
        return None if position < 0 else table.iloc[position].copy()

    def at(self, index_or_ts: int | pd.Timestamp) -> MtfState:
        """Return state known at the close of the selected M5 bar-open timestamp."""

        position = self._position(index_or_ts)
        return MtfState(
            m5=self.m5.iloc[position].copy(),
            m15=self._known(self.m15, int(self.m15_alignment[position])),
            h1=self._known(self.h1, int(self.h1_alignment[position])),
            levels=self._levels[position],
        )


def truncation_invariant(frame: pd.DataFrame, cutoff: int) -> bool:
    """Check that all states through ``cutoff`` equal those built from that prefix alone."""

    if cutoff < 0 or cutoff >= len(frame):
        raise IndexError(cutoff)
    full = MtfView(frame)
    prefix = MtfView(frame.iloc[: cutoff + 1].copy())
    if not np.array_equal(full.m15_alignment[: cutoff + 1], prefix.m15_alignment):
        return False
    if not np.array_equal(full.h1_alignment[: cutoff + 1], prefix.h1_alignment):
        return False
    for position in range(cutoff + 1):
        left = full.at(position)
        right = prefix.at(position)
        if left.levels != right.levels:
            return False
        for name in ("m15", "h1"):
            left_bar = getattr(left, name)
            right_bar = getattr(right, name)
            if (left_bar is None) != (right_bar is None):
                return False
            if left_bar is not None and not left_bar.equals(right_bar):
                return False
    return True


__all__ = [
    "MtfData",
    "MtfState",
    "MtfView",
    "SessionLevels",
    "derive_mtf",
    "truncation_invariant",
]
