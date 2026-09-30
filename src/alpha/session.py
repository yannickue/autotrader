"""Per-market session calendar and CASH-session level arrays (research only, causal).

V1 ``previous_day_*`` / ``session_*`` levels span the whole Berlin calendar day including the
overnight futures session.  The arrays below are defined on the CASH session of a
``SessionCalendar`` (default GER40: Europe/Berlin 09:00-17:30) and are all causal: bar ``i``
(a bar-OPEN timestamp, known at its close) only uses bars ``<= i``.

Names (float64, NaN = unknown):

``pdh_cash / pdl_cash / pdc_cash``  high / low / last close of the previous CASH session, i.e.
    the last calendar day (< today) that has cash bars (holidays / half days are skipped).
``sess_open_cash``  open of the first cash bar of today (NaN before the cash open).
``sess_high_cash / sess_low_cash``  extremes of today's cash bars so far (NaN before the cash
    open; frozen after the cash close).
``overnight_high / overnight_low``  extremes of the non-cash bars between the previous cash
    session's last bar and today's first cash bar.  Before the cash open: running (so far);
    from the cash open on (also after the cash close, same day): the frozen complete range.
``gap_cash_atr``  (sess_open_cash - pdc_cash) / m5_atr14  (the CASH open gap).
``minutes_since_cash_open``  local minutes since the cash open; negative before it (never NaN).
``dist_{pdh,pdl,pdc,sess_open,sess_high,sess_low}_cash_atr`` and
``dist_ovn_{high,low}_atr``  signed (close - level) / m5_atr14.

Session buckets (``SessionCalendar.buckets``) partition the local minute of day; the last
bucket (OVERNIGHT) is the complement of the others.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

import numpy as np
import pandas as pd

BUCKET_OVERNIGHT = "OVERNIGHT"


@dataclass(frozen=True)
class SessionCalendar:
    """Local-time session definition of one market (all times in local minutes of day)."""

    name: str = "GER40"
    tz: str = "Europe/Berlin"
    cash_open_min: int = 9 * 60
    cash_close_min: int = 17 * 60 + 30
    entry_start_min: int = 9 * 60
    entry_end_min: int = 20 * 60
    flat_min: int = 21 * 60 + 30
    # (name, start_min inclusive, end_min exclusive); everything else is OVERNIGHT
    buckets: tuple[tuple[str, int, int], ...] = (
        ("EUROPE_OPEN", 9 * 60, 11 * 60),
        ("MIDDAY", 11 * 60, 15 * 60 + 30),
        ("US_OVERLAP", 15 * 60 + 30, 20 * 60),
    )

    def __post_init__(self) -> None:
        if not 0 <= self.cash_open_min < self.cash_close_min <= 24 * 60:
            raise ValueError("cash session must satisfy 0 <= open < close <= 1440")
        spans = sorted((s, e) for _, s, e in self.buckets)
        if any(e <= s for s, e in spans) or any(a[1] > b[0] for a, b in pairwise(spans)):
            raise ValueError("session buckets must be non-empty and non-overlapping")

    @property
    def bucket_names(self) -> tuple[str, ...]:
        return (*(name for name, _, _ in self.buckets), BUCKET_OVERNIGHT)

    def bucket_codes(self, minutes: np.ndarray) -> np.ndarray:
        """int8 bucket index per local minute (OVERNIGHT = len(buckets))."""
        minutes = np.asarray(minutes)
        codes = np.full(len(minutes), len(self.buckets), dtype=np.int8)
        for code, (_, start, end) in enumerate(self.buckets):
            codes[(minutes >= start) & (minutes < end)] = code
        return codes


DEFAULT_CALENDAR = SessionCalendar()

CASH_LEVEL_NAMES: tuple[str, ...] = (
    "pdh_cash",
    "pdl_cash",
    "pdc_cash",
    "sess_open_cash",
    "sess_high_cash",
    "sess_low_cash",
    "overnight_high",
    "overnight_low",
    "gap_cash_atr",
    "minutes_since_cash_open",
    "dist_pdh_cash_atr",
    "dist_pdl_cash_atr",
    "dist_pdc_cash_atr",
    "dist_sess_open_cash_atr",
    "dist_sess_high_cash_atr",
    "dist_sess_low_cash_atr",
    "dist_ovn_high_atr",
    "dist_ovn_low_atr",
)

# session-quantile-rank features (opt-in, see alpha.discovery.session_norm)
SQ_BASE_FEATURES: tuple[str, ...] = (
    "bar_range_atr",
    "m5_volatility_percentile",
    "mom_3_atr",
    "mom_6_atr",
    "mom_12_atr",
    "brk_up_20",
    "brk_dn_20",
    "brk_up_48",
    "brk_dn_48",
)
SQ_FEATURE_NAMES: tuple[str, ...] = tuple(f"{name}_sq" for name in SQ_BASE_FEATURES)


def local_clock(ts_ns: np.ndarray, calendar: SessionCalendar) -> tuple[np.ndarray, np.ndarray]:
    """(local minute of day int16, local day id int32) from UTC nanosecond timestamps."""
    local = pd.DatetimeIndex(np.asarray(ts_ns, dtype="int64").view("datetime64[ns]"), tz="UTC")
    local = local.tz_convert(calendar.tz)
    minute = (local.hour * 60 + local.minute).to_numpy(np.int16)
    dates = local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]")
    _, day_id = np.unique(dates, return_inverse=True)
    return minute, day_id.astype(np.int32)


def _by_group(values: np.ndarray, group: np.ndarray, how: str) -> np.ndarray:
    """Per-group max/min/first/last over NaN-masked values; groups are 0..G-1 (NaN if empty)."""
    frame = pd.Series(values).groupby(group)
    if how == "max":
        return frame.max().reindex(range(int(group.max()) + 1) if len(group) else []).to_numpy()
    if how == "min":
        return frame.min().reindex(range(int(group.max()) + 1) if len(group) else []).to_numpy()
    raise ValueError(how)


def _running(values: np.ndarray, group: np.ndarray, how: str) -> np.ndarray:
    """Within-group running max/min of ``values`` (+-inf = skipped bar); non-finite -> NaN."""
    series = pd.Series(values).groupby(group)
    out = (series.cummax() if how == "max" else series.cummin()).to_numpy(float)
    return np.where(np.isfinite(out), out, np.nan)


def cash_session_arrays(
    minute: np.ndarray,
    day_id: np.ndarray,
    o: np.ndarray,
    h: np.ndarray,
    low: np.ndarray,
    c: np.ndarray,
    atr: np.ndarray,
    calendar: SessionCalendar = DEFAULT_CALENDAR,
) -> dict[str, np.ndarray]:
    """Causal cash-session levels (see module docstring); all arrays are M5-aligned."""
    n = len(c)
    minute = np.asarray(minute)
    day_id = np.asarray(day_id)
    if n == 0:
        return {name: np.zeros(0) for name in CASH_LEVEL_NAMES}
    nan = np.nan
    cash = (minute >= calendar.cash_open_min) & (minute < calendar.cash_close_min)
    idx = np.arange(n)
    n_days = int(day_id.max()) + 1

    # ---- per-day cash statistics (indexed by day id) -------------------------------------
    d_high = _by_group(np.where(cash, h, nan), day_id, "max")
    d_low = _by_group(np.where(cash, low, nan), day_id, "min")
    has_cash = np.zeros(n_days, dtype=bool)
    has_cash[day_id[cash]] = True
    last_pos = np.full(n_days, -1, dtype=np.int64)
    first_pos = np.full(n_days, -1, dtype=np.int64)
    cash_pos = idx[cash]
    last_pos[day_id[cash_pos]] = cash_pos  # positions ascend -> last write wins
    first_pos[day_id[cash_pos[::-1]]] = cash_pos[::-1]  # reversed -> earliest position wins
    d_close = np.where(has_cash, c[np.maximum(last_pos, 0)], nan)
    d_open = np.where(has_cash, o[np.maximum(first_pos, 0)], nan)

    # previous CASH session: last day < today that has cash bars (forward-filled, then shifted)
    last_cash_day = np.maximum.accumulate(np.where(has_cash, np.arange(n_days), -1))
    prev_day = np.r_[-1, last_cash_day[:-1]]
    known_prev = prev_day >= 0
    safe_prev = np.maximum(prev_day, 0)

    def prev_of(values: np.ndarray) -> np.ndarray:
        return np.where(known_prev, values[safe_prev], nan)[day_id]

    pdh, pdl, pdc = prev_of(d_high), prev_of(d_low), prev_of(d_close)

    # ---- today's cash open / cash extremes so far ---------------------------------------
    seen_today = pd.Series(cash.astype(np.int8)).groupby(day_id).cummax().to_numpy().astype(bool)
    sess_open = np.where(seen_today, d_open[day_id], nan)
    sess_high = _running(np.where(cash, h, -np.inf), day_id, "max")
    sess_low = _running(np.where(cash, low, np.inf), day_id, "min")
    sess_high = np.where(seen_today, sess_high, nan)
    sess_low = np.where(seen_today, sess_low, nan)

    # ---- overnight range: non-cash bars between two cash sessions ------------------------
    is_end = np.zeros(n, dtype=bool)
    is_end[last_pos[has_cash]] = True  # last cash bar of each cash day
    seg = np.cumsum(is_end) - is_end  # cash-session ends strictly before bar i
    seg_count = int(seg.max()) + 1
    nc_high = np.where(cash, nan, h)
    nc_low = np.where(cash, nan, low)
    run_high = _running(np.where(cash, -np.inf, h), seg, "max")
    run_low = _running(np.where(cash, np.inf, low), seg, "min")
    tot_high = _by_group(nc_high, seg, "max")
    tot_low = _by_group(nc_low, seg, "min")
    assert len(tot_high) == seg_count
    post_close = (~cash) & (minute >= calendar.cash_close_min) & seen_today
    frozen_seg = np.where(post_close, np.maximum(seg - 1, 0), seg)
    # cash bars and same-day post-close bars: complete range of the overnight before today's open
    use_total = cash | post_close
    ovn_high = np.where(use_total, tot_high[frozen_seg], run_high)
    ovn_low = np.where(use_total, tot_low[frozen_seg], run_low)

    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(atr > 0, atr, nan)
        out = {
            "pdh_cash": pdh,
            "pdl_cash": pdl,
            "pdc_cash": pdc,
            "sess_open_cash": sess_open,
            "sess_high_cash": sess_high,
            "sess_low_cash": sess_low,
            "overnight_high": ovn_high,
            "overnight_low": ovn_low,
            "gap_cash_atr": (sess_open - pdc) / scale,
            "minutes_since_cash_open": (minute - calendar.cash_open_min).astype(float),
            "dist_pdh_cash_atr": (c - pdh) / scale,
            "dist_pdl_cash_atr": (c - pdl) / scale,
            "dist_pdc_cash_atr": (c - pdc) / scale,
            "dist_sess_open_cash_atr": (c - sess_open) / scale,
            "dist_sess_high_cash_atr": (c - sess_high) / scale,
            "dist_sess_low_cash_atr": (c - sess_low) / scale,
            "dist_ovn_high_atr": (c - ovn_high) / scale,
            "dist_ovn_low_atr": (c - ovn_low) / scale,
        }
    return {name: np.asarray(out[name], dtype=np.float64) for name in CASH_LEVEL_NAMES}


__all__ = (
    "BUCKET_OVERNIGHT",
    "CASH_LEVEL_NAMES",
    "DEFAULT_CALENDAR",
    "SQ_BASE_FEATURES",
    "SQ_FEATURE_NAMES",
    "SessionCalendar",
    "cash_session_arrays",
    "local_clock",
)
