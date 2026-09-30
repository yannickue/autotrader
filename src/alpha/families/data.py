# ruff: noqa: E501
"""FamilyData: the causal arrays every family generator reads (NO labels, NO later-partition bars).

Everything here is built from the M5 development frame (``ts`` UTC bar-OPEN, OHLC BID, ``spread_pts``,
``tick_volume``) by trailing/causal operations only: bar ``i`` is known at its close, all arrays at ``i`` use
bars ``<= i``.  ``prefix(n)`` is therefore exactly equal to building from the first ``n`` frame rows (asserted
in ``tests/test_v2_families_causality.py``).

Clock.  ``minute`` / ``day`` are the LOCAL calendar clock of the market (``MarketCalendar.tz``), i.e. the same
basis ``SimWindow`` and ``MarketArrays.minute`` use (see ``alpha.fast.sim.SimWindow``).  NOTE: the FeatureStore
arrays ``berlin_minute`` / ``berlin_day_id`` stay Europe/Berlin for every market; they must not be paired with
a local ``SimWindow`` for a non-Berlin market, which is why this module derives its own local clock.

Cross-market leaders (``LeaderFeatures``) come from ``alpha.common.market_data.align_markets``: for each follower
bar ``i`` the leader's latest bar COMPLETED at the follower's bar close is used (prefix stable).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from alpha.common.market_data import align_markets, assert_no_forward_holdout
from alpha.families.spec import MarketCalendar
from alpha.fast.sim import MarketArrays
from alpha.session import cash_session_arrays, local_clock

ATR_WINDOW = 14
LEADER_NS: tuple[int, ...] = (1, 3, 6, 12)
# round-number scale in TICKS of the market (minor, major): index 50/100 points, gold 5/10 USD, EURUSD 0.005/0.01
ROUND_TICKS: dict[str, tuple[int, int]] = {
    "index_cfd": (5000, 10000),  # tick 0.01 -> 50 / 100
    "metal_cfd": (500, 1000),  # tick 0.01 -> 5 / 10
    "fx_cfd": (500, 1000),  # tick 1e-5 -> 0.005 / 0.01
    # Phase-2 asset classes (Lane M2). PROVISIONAL, NOT researched: the scale only lets ``_assemble`` build the
    # (context-only) round-number features; the ROUND family is deliberately NOT in any Phase-2 frozen spec.
    "energy_cfd": (50, 100),  # tick 0.01 -> 0.5 / 1.0 USD per bbl
    "crypto_cfd": (50000, 100000),  # tick 0.01 -> 500 / 1000 USD per BTC
}


def round_steps(asset_class: str, tick_size: float) -> tuple[float, float]:
    """(minor, major) round-number spacing in PRICE units derived from the MarketSpec tick scale."""
    try:
        lo, hi = ROUND_TICKS[asset_class]
    except KeyError:
        raise ValueError(f"no round-number scale for asset class {asset_class!r}") from None
    return lo * float(tick_size), hi * float(tick_size)


def true_range(h: np.ndarray, low: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev = np.r_[np.nan, c[:-1]]
    return np.nanmax(np.vstack((h - low, np.abs(h - prev), np.abs(low - prev))), axis=0)


def atr14(h: np.ndarray, low: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Same definition as the FeatureStore ``m5_atr14`` (simple 14-bar mean of the true range)."""
    return pd.Series(true_range(h, low, c)).rolling(ATR_WINDOW, min_periods=ATR_WINDOW).mean().to_numpy()


def run_starts(day: np.ndarray, contig_next: np.ndarray) -> np.ndarray:
    """Index of the first bar of the contiguous same-day run each bar belongs to."""
    n = len(day)
    new = np.ones(n, dtype=bool)
    if n > 1:
        new[1:] = (day[1:] != day[:-1]) | ~contig_next[:-1]
    return np.maximum.accumulate(np.where(new, np.arange(n), 0)).astype(np.int64)


@dataclass(frozen=True)
class LeaderFeatures:
    """A leader market's features aligned to the follower's bar grid (causal, see ``align_markets``)."""

    name: str
    ret: Mapping[int, np.ndarray]  # n -> leader n-bar return in LEADER ATR units at the aligned bar (NaN = invalid)
    active: np.ndarray  # bool: the aligned leader bar lies inside the leader's CASH session

    def prefix(self, n: int) -> LeaderFeatures:
        return LeaderFeatures(self.name, {k: v[:n] for k, v in self.ret.items()}, self.active[:n])


def build_leader_features(
    follower_frame: pd.DataFrame, leader_frame: pd.DataFrame, leader_cal: MarketCalendar, leader_name: str,
    ns: tuple[int, ...] = LEADER_NS, max_lag_bars: int = 1,
) -> LeaderFeatures:
    assert_no_forward_holdout(follower_frame)
    assert_no_forward_holdout(leader_frame)
    al = align_markets({"F": follower_frame, "L": leader_frame}, ref="F", max_lag_bars=max_lag_bars)["L"]
    ts = pd.DatetimeIndex(leader_frame["ts"]).as_unit("ns").asi8
    h, low, c = (leader_frame[k].to_numpy(float) for k in ("high", "low", "close"))
    atr = atr14(h, low, c)
    step_ns = 300 * 10**9
    scale = np.where(atr > 0, atr, np.nan)
    ret: dict[int, np.ndarray] = {}
    for n in ns:
        r = np.full(len(c), np.nan)
        if len(c) > n:
            ok = (ts[n:] - ts[:-n]) == n * step_ns  # contiguous in the leader's own frame
            r[n:] = np.where(ok, (c[n:] - c[:-n]) / scale[n:], np.nan)
        ret[n] = al.take(r)
    minute, _ = local_clock(ts, leader_cal.session_calendar())
    in_cash = ((minute >= leader_cal.cash_open_min) & (minute < leader_cal.cash_close_min)).astype(float)
    active = al.take(in_cash) > 0.5
    return LeaderFeatures(leader_name, ret, active)


@dataclass(frozen=True)
class FamilyData:
    name: str
    cal: MarketCalendar
    ts_ns: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    spread: np.ndarray  # PRICE units (recorded points * point size)
    vol: np.ndarray  # tick volume
    atr: np.ndarray
    minute: np.ndarray  # LOCAL minute of day of the bar OPEN
    day: np.ndarray  # LOCAL day id 0..D-1
    contig_next: np.ndarray  # bar i+1 exists and starts 5 minutes after bar i
    run_start: np.ndarray
    cash: Mapping[str, np.ndarray]  # alpha.session.cash_session_arrays
    vwap: np.ndarray  # running per-day tick-volume weighted typical price over CASH bars (NaN before)
    round_steps: tuple[float, float]  # (minor, major)
    cross: Mapping[str, LeaderFeatures] = field(default_factory=dict)
    _memo: dict = field(default_factory=dict, repr=False, compare=False)

    def __len__(self) -> int:
        return len(self.c)

    def prefix(self, n: int) -> FamilyData:
        """Bars ``[0, n)`` only (truncation): later bars are unreachable from the result."""
        cn = self.contig_next[:n].copy()
        if n:
            cn[-1] = False
        return FamilyData(
            self.name, self.cal, self.ts_ns[:n], self.o[:n], self.h[:n], self.l[:n], self.c[:n], self.spread[:n],
            self.vol[:n], self.atr[:n], self.minute[:n], self.day[:n], cn, self.run_start[:n],
            {k: v[:n] for k, v in self.cash.items()}, self.vwap[:n], self.round_steps,
            {k: v.prefix(n) for k, v in self.cross.items()},
        )

    def with_ohlc(self, o, h, low, c, spread=None, vol=None) -> FamilyData:
        """Copy with replaced price arrays and every derived array RECOMPUTED (perturbation / mirror tests)."""
        return _assemble(
            self.name, self.cal, self.ts_ns, np.asarray(o, float), np.asarray(h, float), np.asarray(low, float),
            np.asarray(c, float), self.spread if spread is None else np.asarray(spread, float),
            self.vol if vol is None else np.asarray(vol, float), self.round_steps, self.cross,
        )

    def market_arrays(self) -> MarketArrays:
        return MarketArrays(self.o, self.h, self.l, self.c, self.spread, self.minute, self.day, self.contig_next)

    def memo(self, key, builder):
        try:
            return self._memo[key]
        except KeyError:
            out = self._memo[key] = builder()
            return out


def _vwap_proxy(h, low, c, vol, minute, day, cal: MarketCalendar) -> np.ndarray:
    cash = (minute >= cal.cash_open_min) & (minute < cal.cash_close_min)
    tp = (h + low + c) / 3.0
    w = np.where(cash, np.maximum(vol, 0.0), 0.0)
    num = pd.Series(tp * w).groupby(day).cumsum().to_numpy()
    den = pd.Series(w).groupby(day).cumsum().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / den, np.nan)


def _assemble(name, cal, ts_ns, o, h, low, c, spread, vol, rsteps, cross) -> FamilyData:
    n = len(c)
    atr = atr14(h, low, c)
    minute, day = local_clock(ts_ns, cal.session_calendar())
    cn = np.zeros(n, dtype=bool)
    if n > 1:
        cn[:-1] = np.diff(ts_ns) == 300 * 10**9
    rs = run_starts(day, cn)
    cash = cash_session_arrays(minute, day, o, h, low, c, atr, cal.session_calendar())
    vwap = _vwap_proxy(h, low, c, vol, minute, day, cal)
    return FamilyData(name, cal, ts_ns, o, h, low, c, spread, vol, atr, minute.astype(np.int64), day.astype(np.int64),
                      cn, rs, cash, vwap, rsteps, dict(cross))


def build_family_data(
    frame: pd.DataFrame, cal: MarketCalendar, *, name: str, point_size: float, tick_size: float,
    asset_class: str = "index_cfd", cross: Mapping[str, LeaderFeatures] | None = None,
) -> FamilyData:
    """FamilyData from a development frame; REFUSES any bar on a Berlin date after 2026-08-31."""
    assert_no_forward_holdout(frame)
    ts = pd.DatetimeIndex(frame["ts"])
    ts_ns = ts.as_unit("ns").asi8.astype(np.int64)
    if len(ts_ns) > 1 and not (np.diff(ts_ns) > 0).all():
        raise ValueError("frame timestamps must be strictly increasing")
    o, h, low, c = (frame[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    spread = frame["spread_pts"].to_numpy(float) * float(point_size)
    vol = frame["tick_volume"].to_numpy(float)
    data = _assemble(name, cal, ts_ns, o, h, low, c, spread, vol, round_steps(asset_class, tick_size), cross or {})
    for lf in data.cross.values():
        if len(lf.active) != len(c):
            raise ValueError("leader features must be aligned to the follower grid")
    return data


__all__ = (
    "ATR_WINDOW", "LEADER_NS", "ROUND_TICKS", "FamilyData", "LeaderFeatures", "atr14", "build_family_data",
    "build_leader_features", "round_steps", "run_starts", "true_range",
)
