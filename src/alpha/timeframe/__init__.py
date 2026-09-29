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
    # Vectorised over buckets. M5 timestamps are strictly increasing and bucket opens are a
    # non-decreasing function of them, so every bucket is one contiguous run of positions.
    expected_count = minutes // 5
    buckets = _bucket_opens(m5.index, minutes)
    if not len(m5):
        return pd.DataFrame(
            {
                "open": [], "high": [], "low": [], "close": [], "spread_pts": [],
                "complete": [], "source_count": [], "source_start": [], "source_end": [],
                "available_at": [],
            },
            index=pd.DatetimeIndex([], name="ts"),
        )  # fmt: skip
    bucket_ns = buckets.as_unit("ns").asi8
    starts = np.flatnonzero(np.r_[True, bucket_ns[1:] != bucket_ns[:-1]])
    ends = np.r_[starts[1:], len(m5)] - 1
    counts = ends - starts + 1
    step = pd.Timedelta(minutes=5).value
    stamps = m5.index.as_unit("ns").asi8
    bucket_open_ns = bucket_ns[starts]
    # Strictly increasing timestamps on the 5-minute grid: exactly ``expected_count`` bars that
    # start at the bucket open and end (count - 1) steps later are the complete expected run.
    complete = (
        (counts == expected_count)
        & (stamps[starts] == bucket_open_ns)
        & (stamps[ends] == bucket_open_ns + (expected_count - 1) * step)
    )

    def column(name: str) -> np.ndarray:
        return m5[name].to_numpy(dtype=float)

    high = np.fmax.reduceat(column("high"), starts)  # NaN-skipping, like Series.max
    low = np.fmin.reduceat(column("low"), starts)  # NaN-skipping, like Series.min
    open_index = buckets[starts]

    def masked(values: np.ndarray) -> np.ndarray:
        return np.where(complete, values, np.nan)

    result = pd.DataFrame(
        {
            "open": masked(column("open")[starts]),
            "high": masked(high),
            "low": masked(low),
            "close": masked(column("close")[ends]),
            "spread_pts": masked(column("spread_pts")[ends]),
            "complete": complete.astype(bool),
            "source_count": counts.astype(np.int64),
            "source_start": starts.astype(np.int64),
            "source_end": ends.astype(np.int64),
            "available_at": open_index + pd.Timedelta(minutes=minutes),
        },
        index=pd.DatetimeIndex(open_index, name="ts"),
    )
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
        # Runtime-only acceleration of ``at``. Row access through ``iloc`` on these frames costs
        # far more than the strategy logic that consumes it, and every state is requested once
        # per strategy variant. M5 rows are rebuilt from one homogeneous numpy block (same dtype
        # and values ``iloc`` produces); M15/H1 rows (mixed dtypes) are materialised through
        # ``iloc`` once per higher bar and handed out as copies, exactly as before.
        self._m5_values = self.m5.to_numpy()
        self._m5_columns = self.m5.columns
        self._m5_dtype = self._m5_values.dtype
        self._row_cache: dict[tuple[str, int], pd.Series] = {}

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

    def _known(self, name: str, position: int) -> pd.Series | None:
        if position < 0:
            return None
        key = (name, position)
        row = self._row_cache.get(key)
        if row is None:
            row = getattr(self, name).iloc[position].copy()
            self._row_cache[key] = row
        return row.copy()

    def at(self, index_or_ts: int | pd.Timestamp) -> MtfState:
        """Return state known at the close of the selected M5 bar-open timestamp."""

        position = self._position(index_or_ts)
        m5 = pd.Series(
            self._m5_values[position].copy(),
            index=self._m5_columns,
            name=self.m5.index[position],
            dtype=self._m5_dtype,
        )
        return MtfState(
            m5=m5,
            m15=self._known("m15", int(self.m15_alignment[position])),
            h1=self._known("h1", int(self.h1_alignment[position])),
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
