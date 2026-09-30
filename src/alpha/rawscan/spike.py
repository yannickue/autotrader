# ruff: noqa: E501
"""Spread-spike vs liquidity-sweep-like events (evidence for/against a feed artefact).

Self-contained on raw M5 bars (BID OHLC + recorded spread).  A bar is a SPREAD SPIKE when its
recorded spread exceeds the p90 of the previous ``window`` bars (past-only).  A bar is SWEEP-LIKE
when it makes a new ``lookback``-bar high (low) and closes back at/below (above) the previous
extreme.  Sweeps are far more frequent at the open, where spreads also widen, so the diagnostic also
reports the time-of-day-stratified ratio (observed sweeps among spike bars / sweeps expected from the
same slots' non-spike sweep rate).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def spread_spike_diagnostic(
    frame: pd.DataFrame, tz: str, *, window: int = 2016, q: float = 0.9, lookback: int = 20,
) -> dict:
    sp = frame["spread_pts"].to_numpy(float)
    h, lo, c = (frame[k].to_numpy(float) for k in ("high", "low", "close"))
    p90 = (
        pd.Series(sp).shift(1).rolling(window, min_periods=window // 4).quantile(q).to_numpy()
    )
    hi_prev = pd.Series(h).shift(1).rolling(lookback).max().to_numpy()
    lo_prev = pd.Series(lo).shift(1).rolling(lookback).min().to_numpy()
    with np.errstate(invalid="ignore"):
        up = (h > hi_prev) & (c <= hi_prev)
        dn = (lo < lo_prev) & (c >= lo_prev)
        valid = np.isfinite(p90) & np.isfinite(hi_prev) & np.isfinite(lo_prev)
        spike = valid & (sp > p90)
    sweep = (up | dn) & valid
    ts = pd.DatetimeIndex(frame["ts"]).tz_convert(tz)
    slot = np.asarray(ts.hour * 12 + ts.minute // 5)
    n_sp, n_ns = int(spike.sum()), int((valid & ~spike).sum())
    r_sp = float(sweep[spike].mean()) if n_sp else float("nan")
    r_ns = float(sweep[valid & ~spike].mean()) if n_ns else float("nan")
    # time-of-day stratified expectation: non-spike sweep rate of the slot of every spike bar
    ns = valid & ~spike
    tot = np.bincount(slot[ns], minlength=288).astype(float)
    hit = np.bincount(slot[ns], weights=sweep[ns].astype(float), minlength=288)
    with np.errstate(invalid="ignore", divide="ignore"):
        rate = np.where(tot > 0, hit / tot, np.nan)
    expected = float(np.nansum(rate[slot[spike]]))
    observed = int(sweep[spike].sum())
    # two-proportion z for the unstratified contrast
    pp = (observed + sweep[ns].sum()) / max(n_sp + n_ns, 1)
    se = math.sqrt(pp * (1 - pp) * (1 / max(n_sp, 1) + 1 / max(n_ns, 1))) if 0 < pp < 1 else float("nan")
    z = (r_sp - r_ns) / se if se and np.isfinite(se) else float("nan")
    var_exp = float(np.nansum(rate[slot[spike]] * (1 - rate[slot[spike]])))
    z_strat = (observed - expected) / math.sqrt(var_exp) if var_exp > 0 else float("nan")
    return {
        "window_bars": window, "spike_quantile": q, "lookback_bars": lookback,
        "n_bars_valid": int(valid.sum()), "n_spike_bars": n_sp,
        "share_spike_bars": n_sp / max(int(valid.sum()), 1),
        "sweep_rate_spike": r_sp, "sweep_rate_nonspike": r_ns,
        "rate_ratio": r_sp / r_ns if r_ns else float("nan"), "z_unstratified": z,
        "sweeps_at_spike_observed": observed, "sweeps_at_spike_expected_by_slot": expected,
        "observed_over_expected_by_slot": observed / expected if expected else float("nan"),
        "z_slot_stratified": z_strat,
        "p_spike_given_sweep": float(spike[sweep].mean()) if sweep.any() else float("nan"),
        "reading": (
            "ratio ~1 (stratified) = spikes and sweeps are independent given time of day; "
            ">> 1 = sweep-like bars coincide with spread widening (feed-artefact risk)"
        ),
    }
