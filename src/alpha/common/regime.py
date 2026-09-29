"""Causal market-regime labels (state known at the decision bar's close).

TREND state : Kaufman efficiency ratio of the last 48 closed M5 bars (4h). >= 0.30 -> TRENDING,
              otherwise RANGING. (Fixed a priori; not tuned.)
VOL state   : previous-5-day mean daily range / previous-60-day median daily range (Berlin dates,
              STRICTLY prior days only). > 1.25 HIGH_VOL, < 0.80 LOW_VOL, else NORMAL_VOL.
Labels never use the current day's range or any later bar.
"""

from __future__ import annotations

import numpy as np

from alpha.common.frame import Frame

ER_LEN = 48
ER_TREND = 0.30
VOL_SHORT, VOL_LONG, VOL_MIN_HISTORY = 5, 60, 20
VOL_HIGH, VOL_LOW = 1.25, 0.80


def daily_vol_ratio(fr: Frame) -> np.ndarray:
    """Per-bar ratio (broadcast from the Berlin day); NaN until enough prior days exist."""
    n_days = int(fr.day.max()) + 1
    hi = np.full(n_days, -np.inf)
    lo = np.full(n_days, np.inf)
    np.maximum.at(hi, fr.day, fr.h)
    np.minimum.at(lo, fr.day, fr.l)
    rng = hi - lo
    ratio = np.full(n_days, np.nan)
    for d in range(VOL_MIN_HISTORY, n_days):
        prior = rng[max(0, d - VOL_LONG) : d]
        short = rng[max(0, d - VOL_SHORT) : d]
        med = np.median(prior)
        if med > 0:
            ratio[d] = short.mean() / med
    return ratio[fr.day]


def regime_labels(fr: Frame) -> dict[str, np.ndarray]:
    er = fr.efficiency_ratio(ER_LEN)
    trend = np.where(np.isnan(er), "NA", np.where(er >= ER_TREND, "TRENDING", "RANGING"))
    ratio = daily_vol_ratio(fr)
    vol = np.where(
        np.isnan(ratio),
        "NA",
        np.where(ratio > VOL_HIGH, "HIGH_VOL", np.where(ratio < VOL_LOW, "LOW_VOL", "NORMAL_VOL")),
    )
    return {"trend": trend.astype(object), "vol": vol.astype(object)}
