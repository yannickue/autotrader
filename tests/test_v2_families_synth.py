# ruff: noqa: E501
"""Synthetic M5 frames for the family tests (GER40-like calendar: cash 09:00-17:30 Berlin, bars 06:00-22:00).

``synth_frame`` builds a random-walk market with an optional PLANTED edge:

* ``plant="gap_fade"``: the first 12 cash bars pull back ``phi`` of the cash-open gap (open vs previous cash close);
* ``plant="orb_break"``: after the first 30 minutes the price continues in the direction of the opening drift.

Prices live around 20000 with ~4-point bars so the V1 GER40 sizing (risk band 5..400) and costs apply unchanged.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from alpha.families.data import FamilyData, build_family_data
from alpha.families.spec import MarketCalendar

B = 192  # bars per day: 06:00 .. 21:55
START_MIN = 6 * 60
CASH_OPEN_BAR = (9 * 60 - START_MIN) // 5  # 36
CASH_CLOSE_BAR = (17 * 60 + 30 - START_MIN) // 5  # 138 (first non-cash bar)
CAL = MarketCalendar()


def synth_frame(n_days: int = 200, seed: int = 0, sigma: float = 4.0, jump_sigma: float = 25.0, plant: str | None = None,
                phi: float = 0.7, psi: float = 0.6, price0: float = 20000.0, start: str = "2025-03-03",
                tz: str = "Europe/Berlin", cash_open_min: int = 9 * 60, cash_close_min: int = 17 * 60 + 30) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    cash_open_bar = (cash_open_min - START_MIN) // 5
    cash_close_bar = (cash_close_min - START_MIN) // 5
    days = pd.bdate_range(start, periods=n_days)
    rows = []
    prev_close = price0
    prev_cash_close = price0
    for d in days:
        r = rng.normal(0.0, sigma, B)
        jump = rng.normal(0.0, jump_sigma)
        open0 = prev_close + jump

        def path(rr: np.ndarray, open0: float = open0) -> np.ndarray:
            return open0 + np.cumsum(rr)

        c = path(r)
        if plant == "gap_fade":
            gap = (c[cash_open_bar - 1]) - prev_cash_close  # open of the first cash bar == close of the previous bar
            r = r.copy()
            r[cash_open_bar:cash_open_bar + 12] += -phi * gap / 12.0
            c = path(r)
        elif plant == "orb_break":
            drift = np.sign(c[cash_open_bar + 5] - c[cash_open_bar - 1])
            r = r.copy()
            r[cash_open_bar + 6:cash_open_bar + 6 + 30] += psi * drift * sigma
            c = path(r)
        o = np.r_[open0, c[:-1]]
        hi = np.maximum(o, c) + np.abs(rng.normal(0.0, 0.5 * sigma, B))
        lo = np.minimum(o, c) - np.abs(rng.normal(0.0, 0.5 * sigma, B))
        local = d + pd.to_timedelta(START_MIN + 5 * np.arange(B), unit="min")
        ts = pd.DatetimeIndex(local).tz_localize(tz).tz_convert("UTC")
        rows.append(pd.DataFrame({"ts": ts, "open": o, "high": hi, "low": lo, "close": c,
                                  "tick_volume": rng.integers(50, 500, B).astype(float), "spread_pts": np.full(B, 120.0)}))
        prev_close = c[-1]
        prev_cash_close = c[cash_close_bar - 1]
    return pd.concat(rows, ignore_index=True)


def synth_data(**kw) -> FamilyData:
    return build_family_data(synth_frame(**kw), CAL, name="SYN", point_size=0.01, tick_size=0.01, asset_class="index_cfd")


def perturb_future(frame: pd.DataFrame, t: int, seed: int = 123) -> pd.DataFrame:
    """Frame with bars ``> t`` replaced by an unrelated random walk (same timestamps)."""
    rng = np.random.default_rng(seed)
    out = frame.copy()
    n = len(frame)
    k = n - (t + 1)
    if k <= 0:
        return out
    c = out["close"].to_numpy(float).copy()
    base = c[t]
    steps = rng.normal(0.0, 9.0, k)
    nc = base + np.cumsum(steps)
    no = np.r_[base, nc[:-1]] + rng.normal(0.0, 3.0, k)
    nh = np.maximum(no, nc) + np.abs(rng.normal(0.0, 3.0, k))
    nl = np.minimum(no, nc) - np.abs(rng.normal(0.0, 3.0, k))
    out.loc[t + 1:, "open"] = no
    out.loc[t + 1:, "close"] = nc
    out.loc[t + 1:, "high"] = nh
    out.loc[t + 1:, "low"] = nl
    out.loc[t + 1:, "tick_volume"] = rng.integers(1, 900, k).astype(float)
    out.loc[t + 1:, "spread_pts"] = rng.integers(50, 400, k).astype(float)
    return out


def mirror_frame(frame: pd.DataFrame, k: float) -> pd.DataFrame:
    """Price mirror ``p -> k - p`` (high <-> low)."""
    out = frame.copy()
    out["open"], out["close"] = k - frame["open"], k - frame["close"]
    out["high"], out["low"] = k - frame["low"], k - frame["high"]
    return out
