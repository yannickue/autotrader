# ruff: noqa: E501
"""Decision-bar features for meta-labeling (research only).

Every feature of a trade row is a function of information known at the CLOSE of the decision bar
``i`` (bars ``<= i`` only).  Two layers:

* ``bar_features(inp)``: direction-agnostic per-bar arrays derived from causal inputs (trailing
  ATR percentile, spread relative to its trailing median, D1 trend from PRIOR days' closes, ...).
* ``trade_matrix(...)``: gathers the bar arrays at the decision bars and applies the trade direction
  (a positive signed distance means "in the direction of the trade") plus per-trade plan / confluence /
  family columns.

The future-perturbation test (tests/test_v2_metalabel_features.py) randomises every value array for
bars ``> i0`` and asserts that rows with decision index ``<= i0`` are unchanged.  Inputs from the
FeatureSet / EventSet are causal by construction (they are tested upstream); this module adds no
look-ahead of its own.  No normaliser is fitted here: rolling statistics are trailing, and any
train-side standardisation happens inside the CV folds (``models.Preprocessor``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from numba import njit

ATR_RANK_WINDOW = 1440  # trailing bars (~5-6 sessions) for the ATR percentile / relative ATR
SPREAD_MED_WINDOW = 576  # trailing bars (~2 sessions) for the spread median
D1_CLOSES = 5
CLIP = 10.0

# names read from ``frame.arrays`` (FeatureSet + EventSet level/state arrays)
LEVEL_ARRAYS = ("m5_swing_high", "m5_swing_low", "m15_swing_high", "m15_swing_low", "h1_swing_high",
                "h1_swing_low", "session_high", "session_low", "pdh", "pdl")
DIST_ARRAYS = ("dist_pdh_atr", "dist_pdl_atr", "dist_pdc_atr", "dist_sess_open_atr", "dist_sess_high_atr",
               "dist_sess_low_atr", "dist_swing_high_atr", "dist_swing_low_atr", "dist_pdh_cash_atr",
               "dist_pdl_cash_atr", "dist_pdc_cash_atr", "dist_sess_open_cash_atr", "dist_sess_high_cash_atr",
               "dist_sess_low_cash_atr", "dist_ovn_high_atr", "dist_ovn_low_atr")
SIGNED_ARRAYS = ("mom_3_atr", "mom_6_atr", "mom_12_atr", "m5_ema_slope", "m15_ema_slope", "h1_ema_slope",
                 "gap_cash_atr", "dist_vwap_atr")
PLAIN_ARRAYS = ("range_ratio_12_48", "bar_range_atr", "bar_body_ratio", "compression_expansion_ratio",
                "m5_adx14", "m15_adx14", "h1_adx14", "m5_efficiency_ratio", "m5_bollinger_width",
                "m5_volatility_percentile", "minutes_since_cash_open")
OTHER_ARRAYS = ("m5_rsi14", "bar_close_loc", "brk_up_20", "brk_dn_20", "brk_up_48", "brk_dn_48",
                "from_high_24_atr", "from_low_24_atr", "previous_day_close")
STATE_ARRAYS = ("st_m15_trend_up", "st_m15_trend_dn", "st_h1_trend_up", "st_h1_trend_dn", "st_d1_trend_up",
                "st_d1_trend_dn")
CORE_ARRAYS = ("c", "spread", "m5_atr14", "berlin_day_id")
REQUIRED_ARRAYS: tuple[str, ...] = tuple(dict.fromkeys(
    CORE_ARRAYS + LEVEL_ARRAYS + DIST_ARRAYS + SIGNED_ARRAYS + PLAIN_ARRAYS + OTHER_ARRAYS + STATE_ARRAYS))
CALENDAR_KEYS = ("minute", "dow")  # local minute-of-day and weekday per bar: calendar facts, not prices

FAMILIES = ("BREAK_RETEST_MOMENTUM", "COMPRESSION_EXPANSION", "FAILED_BREAKOUT", "PATTERN_CONFIRM_RETEST",
            "RANGE_SWEEP_CHOCH_RETEST", "SWING_BOS_RETEST", "TRENDLINE_BOUNCE_BREAK", "TREND_PULLBACK_RESUME",
            "ZONE_SWEEP_RECLAIM_BOS_RETEST")


@njit(cache=True)
def trailing_rank(x: np.ndarray, w: int, min_obs: int) -> np.ndarray:
    """Fraction of the trailing window ``x[i-w+1..i]`` (NaN skipped) that is ``<= x[i]``."""
    n = len(x)
    out = np.full(n, np.nan)
    for i in range(n):
        xi = x[i]
        if np.isnan(xi):
            continue
        cnt = 0
        le = 0
        for j in range(max(0, i - w + 1), i + 1):
            v = x[j]
            if not np.isnan(v):
                cnt += 1
                if v <= xi:
                    le += 1
        if cnt >= min_obs:
            out[i] = le / cnt
    return out


@njit(cache=True)
def trailing_median(x: np.ndarray, w: int, min_obs: int) -> np.ndarray:
    """Median of the trailing window ``x[i-w+1..i]`` (NaN skipped)."""
    n = len(x)
    out = np.full(n, np.nan)
    buf = np.empty(w)
    for i in range(n):
        k = 0
        for j in range(max(0, i - w + 1), i + 1):
            v = x[j]
            if not np.isnan(v):
                buf[k] = v
                k += 1
        if k >= min_obs:
            out[i] = np.median(buf[:k])
    return out


@dataclass(frozen=True)
class FeatureInputs:
    """Causal per-bar inputs: ``a`` maps ``REQUIRED_ARRAYS`` names to float arrays, plus calendar facts."""

    a: Mapping[str, np.ndarray]
    minute: np.ndarray  # local minute of the bar open (== MarketArrays.minute)
    dow: np.ndarray  # weekday 0..6 of the bar's Berlin date
    entry_start_min: int
    entry_end_min: int

    def __post_init__(self) -> None:
        missing = [n for n in REQUIRED_ARRAYS if n not in self.a]
        if missing:
            raise KeyError(f"missing feature inputs: {missing[:8]}")

    def get(self, name: str) -> np.ndarray:
        return np.asarray(self.a[name], dtype=np.float64)

    def __len__(self) -> int:
        return len(self.a["c"])


def inputs_from_frame(frame, minute: np.ndarray, dates: np.ndarray, entry_start_min: int,
                      entry_end_min: int) -> FeatureInputs:
    """FeatureInputs from a MarketFrame (lazy arrays) + market minute + Berlin dates."""
    arrays = {n: np.asarray(frame.arrays[n], dtype=np.float64) for n in REQUIRED_ARRAYS}
    dow = ((np.asarray(dates).astype("datetime64[D]").astype(np.int64) + 3) % 7).astype(np.int64)  # 1970-01-01 = Thu
    return FeatureInputs(arrays, np.asarray(minute, dtype=np.int64), dow, entry_start_min, entry_end_min)


def _d1_arrays(c: np.ndarray, day: np.ndarray, atr: np.ndarray) -> dict[str, np.ndarray]:
    """D1 trend features from the closes of PRIOR trading days only (no same-day close is read)."""
    n = len(c)
    out_sma = np.full(n, np.nan)
    out_ret = np.full(n, np.nan)
    if n == 0:
        return {"d1_sma_dist_atr": out_sma, "d1_prev_ret_atr": out_ret}
    last = np.flatnonzero(np.r_[day[1:] != day[:-1], True])  # last bar of every day
    closes = c[last]
    first = np.r_[0, last[:-1] + 1]
    ordinal = np.repeat(np.arange(len(last)), last - first + 1)  # day ordinal per bar
    k = np.arange(len(last))
    prev1 = np.full(len(last), np.nan)
    prev1[1:] = closes[:-1]
    prev2 = np.full(len(last), np.nan)
    prev2[2:] = closes[:-2]
    mean_prev = np.full(len(last), np.nan)
    for kk in k[D1_CLOSES:]:
        mean_prev[kk] = closes[kk - D1_CLOSES:kk].mean()
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(atr > 0, atr, np.nan)
        out_sma = (prev1[ordinal] - mean_prev[ordinal]) / scale
        out_ret = (prev1[ordinal] - prev2[ordinal]) / scale
    return {"d1_sma_dist_atr": out_sma, "d1_prev_ret_atr": out_ret}


def bar_features(inp: FeatureInputs) -> dict[str, np.ndarray]:
    """Direction-agnostic per-bar arrays (causal).  Value at ``i`` uses bars ``<= i`` only."""
    atr = inp.get("m5_atr14")
    spread = inp.get("spread")
    c = inp.get("c")
    day = np.asarray(inp.a["berlin_day_id"]).astype(np.int64)
    atr_mean = pd.Series(atr).rolling(ATR_RANK_WINDOW, min_periods=288).mean().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        safe = np.where(atr > 0, atr, np.nan)
        out = {
            "atr_pctl": trailing_rank(atr, ATR_RANK_WINDOW, 288),
            "atr_rel_mean": atr / np.where(atr_mean > 0, atr_mean, np.nan),
            "atr_pct": 100.0 * atr / np.where(c != 0, c, np.nan),
        }
        med = trailing_median(spread, SPREAD_MED_WINDOW, 96)
        out["spread_rel_med"] = spread / np.where(med > 0, med, np.nan)
        out["spread_atr"] = spread / safe
    out.update(_d1_arrays(c, day, atr))
    for tf in ("m15", "h1", "d1"):
        up = np.nan_to_num(inp.get(f"st_{tf}_trend_up"), nan=0.0) > 0
        dn = np.nan_to_num(inp.get(f"st_{tf}_trend_dn"), nan=0.0) > 0
        out[f"trend_{tf}"] = up.astype(np.float64) - dn.astype(np.float64)
    return out


def _clip(x: np.ndarray) -> np.ndarray:
    return np.clip(x, -CLIP, CLIP)


@dataclass(frozen=True)
class TradeInfo:
    """Per-trade inputs known at the decision bar (plan from the candidate, confluence from the pool)."""

    decision_idx: np.ndarray
    direction: np.ndarray  # +1 / -1
    stop: np.ndarray
    target: np.ndarray  # NaN for fixed-R
    target_r: np.ndarray
    zone_lo: np.ndarray  # NaN if none
    zone_hi: np.ndarray
    family: np.ndarray  # index into FAMILIES (or -1)
    k_same: np.ndarray  # other pool strategies firing same direction at the bar
    k_opp: np.ndarray
    n_fam_same: np.ndarray


def trade_matrix(inp: FeatureInputs, raw: Mapping[str, np.ndarray], ti: TradeInfo) -> tuple[np.ndarray, list[str]]:
    """Feature matrix (rows = trades) and column names.  NaN = unknown (imputed inside CV folds)."""
    i = np.asarray(ti.decision_idx, dtype=np.int64)
    d = np.asarray(ti.direction, dtype=np.float64)
    cols: dict[str, np.ndarray] = {}
    atr = inp.get("m5_atr14")[i]
    c = inp.get("c")[i]
    with np.errstate(invalid="ignore", divide="ignore"):
        safe = np.where(atr > 0, atr, np.nan)
    # calendar
    win = float(inp.entry_end_min - inp.entry_start_min)
    tod = (inp.minute[i] - inp.entry_start_min) / win
    cols["tod"] = tod
    cols["tod_sq"] = tod * tod
    for k in range(5):
        cols[f"dow_{k}"] = (inp.dow[i] == k).astype(np.float64)
    cols["direction"] = d
    # volatility / cost
    for name in ("atr_pctl", "atr_rel_mean", "atr_pct", "spread_rel_med", "spread_atr"):
        cols[name] = raw[name][i]
    # levels ahead of the trade (positive = level lies in the trade direction), in ATR
    for name in DIST_ARRAYS:
        cols["ahead_" + name] = _clip(-d * inp.get(name)[i])
    ahead_levels, behind_levels = [], []
    for name in LEVEL_ARRAYS:
        x = d * (inp.get(name)[i] - c) / safe
        cols["lvl_" + name] = _clip(x)
        ahead_levels.append(np.where(x > 0, x, np.inf))
        behind_levels.append(np.where(x < 0, -x, np.inf))
    ahead = np.min(np.vstack(ahead_levels), axis=0)
    behind = np.min(np.vstack(behind_levels), axis=0)
    cols["space_ahead_atr"] = np.minimum(ahead, CLIP)  # no structure within CLIP ATR -> CLIP
    cols["space_behind_atr"] = np.minimum(behind, CLIP)
    # trend
    for tf in ("m15", "h1", "d1"):
        cols[f"trend_{tf}"] = d * raw[f"trend_{tf}"][i]
    cols["d1_sma_dist_atr"] = _clip(d * raw["d1_sma_dist_atr"][i])
    cols["d1_prev_ret_atr"] = _clip(d * raw["d1_prev_ret_atr"][i])
    for name in SIGNED_ARRAYS:
        cols["dir_" + name] = _clip(d * inp.get(name)[i])
    for name in PLAIN_ARRAYS:
        cols[name] = inp.get(name)[i]
    cols["dir_rsi"] = d * (inp.get("m5_rsi14")[i] - 50.0) / 50.0
    loc = inp.get("bar_close_loc")[i]
    cols["dir_close_loc"] = np.where(d > 0, loc, 1.0 - loc)
    for w in (20, 48):
        cols[f"brk_ahead_{w}"] = _clip(np.where(d > 0, inp.get(f"brk_up_{w}")[i], inp.get(f"brk_dn_{w}")[i]))
    fh, fl = inp.get("from_high_24_atr")[i], inp.get("from_low_24_atr")[i]
    cols["room_ahead_24"] = _clip(np.where(d > 0, fh, fl))
    cols["room_behind_24"] = _clip(np.where(d > 0, fl, fh))
    # plan
    stop = np.asarray(ti.stop, dtype=np.float64)
    risk_atr = np.abs(c - stop) / safe
    tgt = np.asarray(ti.target, dtype=np.float64)
    fin = np.isfinite(tgt)
    with np.errstate(invalid="ignore", divide="ignore"):
        rr = np.where(fin, np.abs(tgt - c) / np.abs(c - stop), np.asarray(ti.target_r, dtype=np.float64))
    cols["risk_atr"] = np.clip(risk_atr, 0.0, 20.0)
    cols["plan_rr"] = np.clip(rr, 0.0, 20.0)
    cols["plan_structural"] = fin.astype(np.float64)
    cols["space_ahead_r"] = np.clip(np.minimum(ahead, CLIP) / np.where(risk_atr > 0, risk_atr, np.nan), 0.0, 20.0)
    lo, hi = np.asarray(ti.zone_lo, dtype=np.float64), np.asarray(ti.zone_hi, dtype=np.float64)
    zone_ok = np.isfinite(lo) & np.isfinite(hi)
    cols["zone_width_atr"] = np.where(zone_ok, np.clip((hi - lo) / safe, 0.0, CLIP), np.nan)
    cols["zone_pos_atr"] = np.where(zone_ok, _clip(d * (c - 0.5 * (hi + lo)) / safe), np.nan)
    # confluence
    cols["k_same"] = np.log1p(np.asarray(ti.k_same, dtype=np.float64))
    cols["k_opp"] = np.log1p(np.asarray(ti.k_opp, dtype=np.float64))
    cols["n_fam_same"] = np.asarray(ti.n_fam_same, dtype=np.float64)
    fam = np.asarray(ti.family)
    for k, f in enumerate(FAMILIES):
        cols["fam_" + f] = (fam == k).astype(np.float64)
    names = list(cols)
    x = np.column_stack([np.asarray(cols[n], dtype=np.float64) for n in names]) if len(i) else np.empty((0, len(names)))
    return x, names


def feature_names() -> list[str]:
    """Column names of ``trade_matrix`` (built on a 1-row dummy)."""
    n = 3
    a = {name: np.zeros(n) for name in REQUIRED_ARRAYS}
    a["berlin_day_id"] = np.arange(n)
    inp = FeatureInputs(a, np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64), 0, 1)
    raw = bar_features(inp)
    z = np.zeros(1)
    ti = TradeInfo(np.zeros(1, dtype=np.int64), np.ones(1), z, z, z, z, z, np.zeros(1, dtype=np.int64), z, z, z)
    return trade_matrix(inp, raw, ti)[1]


def perturb_future(inp: FeatureInputs, i0: int, rng: np.random.Generator) -> FeatureInputs:
    """Copy of ``inp`` whose float value arrays are replaced by noise for bars ``> i0`` (test helper).

    Calendar/index structure (``berlin_day_id``, minute, dow) is kept: the perturbation targets VALUES."""
    new: dict[str, np.ndarray] = {}
    for k, v in inp.a.items():
        v = np.array(v, dtype=np.float64, copy=True)
        if k != "berlin_day_id" and i0 + 1 < len(v):
            scale = np.nanstd(v[: i0 + 1]) if i0 > 2 and np.isfinite(np.nanstd(v[: i0 + 1])) else 1.0
            v[i0 + 1:] = rng.normal(np.nanmean(v[: i0 + 1]) if i0 > 2 else 0.0, 3.0 * (scale + 1e-9), len(v) - i0 - 1)
        new[k] = v
    return FeatureInputs(new, inp.minute, inp.dow, inp.entry_start_min, inp.entry_end_min)


def family_index(name: str) -> int:
    try:
        return FAMILIES.index(name)
    except ValueError:
        return -1


def confluence_counts(fam: np.ndarray, decision: np.ndarray, direction: np.ndarray
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each pool candidate row: (# OTHER pool specs firing the same (bar, direction), # specs firing the
    opposite direction at that bar, # distinct families among the same-direction firings).

    Rows are the pool's raw candidate rows (one per spec/decision).  Counting over decisions at the
    same bar only: causal."""
    code = decision.astype(np.int64) * 2 + (np.asarray(direction) > 0)
    uniq, inv, cnt = np.unique(code, return_inverse=True, return_counts=True)
    same = cnt[inv] - 1
    opp_code = decision.astype(np.int64) * 2 + (np.asarray(direction) <= 0)
    pos = np.clip(np.searchsorted(uniq, opp_code), 0, max(len(uniq) - 1, 0))
    opp = np.where(uniq[pos] == opp_code, cnt[pos], 0) if len(uniq) else np.zeros(len(code), dtype=np.int64)
    pair = np.unique(np.column_stack([code, np.asarray(fam, dtype=np.int64)]), axis=0)
    codes_u, nf = np.unique(pair[:, 0], return_counts=True)
    n_fam = nf[np.searchsorted(codes_u, code)].astype(np.float64)
    return same.astype(np.float64), opp.astype(np.float64), n_fam


__all__: Sequence[str] = (
    "FAMILIES", "REQUIRED_ARRAYS", "FeatureInputs", "TradeInfo", "bar_features", "confluence_counts",
    "family_index", "feature_names", "inputs_from_frame", "perturb_future", "trade_matrix", "trailing_median",
    "trailing_rank",
)
