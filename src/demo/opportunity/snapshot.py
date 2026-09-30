# ruff: noqa: E501
"""Causal multi-timeframe context, cheap structure and the OpportunitySnapshot builder.

Everything here is a function of M5 bars that are CLOSED at the decision instant (the last real bar's
close). Higher timeframes are resampled from those M5 bars only; a bucket counts as ``closed`` only if
its end is <= the decision instant, the still-forming bucket is reported separately under ``forming``
(built from closed M5 bars only, so also causal). Bucket alignment is an approximation of the broker's
own H4/D1 bars: H1/M15/H4 use UTC epoch buckets, D1 uses the market's LOCAL calendar day; the choice is
stated in ``alignment``. M1 is not available from an M5 source and is an explicit null.

Structure only holds events that are cheap and unambiguous from the bars (prior-day / overnight /
opening-range levels, distances in ATR, round numbers, VWAP proxy, breakout flags). Anything that would
need real modelling (BOS/CHOCH/sweep/retest/zones/patterns) is an explicit ``None`` and listed in
``not_computed``: nothing is faked.
"""

from __future__ import annotations

import math
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from alpha.families.data import FamilyData
from demo.contracts import (
    MarketState,
    OpportunitySnapshot,
    Phase,
    opportunity_id_for,
)
from demo.opportunity.clock import to_utc
from demo.opportunity.policy import Assessment, Candidate
from markets.spec import MarketSpec

FEATURE_VERSION = "opp-features-1"
ENGINE_VERSION = "opportunity-engine-1"
REPO_ROOT = Path(__file__).resolve().parents[3]
TF_MINUTES = {"M15": 15, "H1": 60, "H4": 240}
NOT_COMPUTED = ("bos", "choch", "liquidity_sweep", "retest", "zones", "pattern")


def _num(v: Any) -> float | None:
    """JSON-safe float: NaN/inf -> None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def git_commit(root: Path = REPO_ROOT) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
            timeout=5, check=False,
        )
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and len(sha) >= 7 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# ------------------------------------------------------------------------------- resampling
def _atr(h: np.ndarray, low: np.ndarray, c: np.ndarray, n: int = 14) -> float | None:
    if len(c) < n + 1:
        return None
    prev = c[:-1]
    tr = np.maximum.reduce([h[1:] - low[1:], np.abs(h[1:] - prev), np.abs(low[1:] - prev)])
    return _num(tr[-n:].mean())


def _ema(x: np.ndarray, span: int) -> float | None:
    if len(x) < span:
        return None
    a = 2.0 / (span + 1)
    e = float(x[0])
    for v in x[1:]:
        e = a * float(v) + (1 - a) * e
    return e


def _agg(df: pd.DataFrame, key: pd.Series) -> pd.DataFrame:
    g = df.groupby(key, sort=True)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "n": g["open"].size(),
        "first_ts": g["ts"].first(),
    })
    return out


def _tf_state(buckets: pd.DataFrame, bucket_starts: list[pd.Timestamp], closed_mask: list[bool],
              first_complete: list[bool]) -> dict[str, Any]:
    closed = [i for i, (m, fc) in enumerate(zip(closed_mask, first_complete, strict=True)) if m and fc]
    forming = [i for i, m in enumerate(closed_mask) if not m]
    state: dict[str, Any] = {"n_closed": len(closed), "last_closed": None, "atr14": None,
                             "trend": None, "ret_1_atr": None, "forming": None}
    if closed:
        b = buckets.iloc[closed]
        o, h, low, c = (b[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        last_i = closed[-1]
        atr = _atr(h, low, c)
        state["last_closed"] = {
            "start_utc": bucket_starts[last_i].isoformat(), "open": _num(o[-1]), "high": _num(h[-1]),
            "low": _num(low[-1]), "close": _num(c[-1]),
        }
        state["atr14"] = atr
        fast, slow = _ema(c, 8), _ema(c, 21)
        if fast is not None and slow is not None:
            state["trend"] = "up" if fast > slow else "down" if fast < slow else "flat"
        if atr and len(c) >= 2:
            state["ret_1_atr"] = _num((c[-1] - c[-2]) / atr)
    if forming:
        f = buckets.iloc[forming[-1]]
        state["forming"] = {
            "start_utc": bucket_starts[forming[-1]].isoformat(), "open": _num(f["open"]),
            "high": _num(f["high"]), "low": _num(f["low"]), "close": _num(f["close"]),
            "n_m5": int(f["n"]),
        }
    return state


def build_context(frame: pd.DataFrame, decision_utc: datetime, tz: str) -> dict[str, Any]:
    """Per-timeframe causal state from CLOSED M5 bars only (``frame`` ends at the deciding bar)."""
    cutoff = pd.Timestamp(to_utc(decision_utc))
    ts = pd.DatetimeIndex(frame["ts"])
    ctx: dict[str, Any] = {"alignment": {"M15": "utc_epoch", "H1": "utc_epoch", "H4": "utc_epoch",
                                         "D1": f"local_day:{tz}"},
                           "M1": None}
    # M5 itself
    o, h, low, c = (frame[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    m5 = {
        "n_closed": len(frame),
        "last_closed": {"start_utc": ts[-1].isoformat(), "open": _num(o[-1]), "high": _num(h[-1]),
                        "low": _num(low[-1]), "close": _num(c[-1])},
        "atr14": _atr(h, low, c), "trend": None, "ret_1_atr": None, "forming": None,
    }
    f8, s21 = _ema(c[-200:], 8), _ema(c[-200:], 21)
    if f8 is not None and s21 is not None:
        m5["trend"] = "up" if f8 > s21 else "down" if f8 < s21 else "flat"
    if m5["atr14"] and len(c) >= 2:
        m5["ret_1_atr"] = _num((c[-1] - c[-2]) / m5["atr14"])
    ctx["M5"] = m5
    for name, minutes in TF_MINUTES.items():
        key = ts.floor(f"{minutes}min")
        b = _agg(frame, key)
        starts = [pd.Timestamp(k) for k in b.index]
        closed_mask = [(s + pd.Timedelta(minutes=minutes)) <= cutoff for s in starts]
        first_ok = [True] * len(starts)
        if starts and ts[0] > starts[0]:
            first_ok[0] = False  # window began inside this bucket: incomplete
        ctx[name] = _tf_state(b, starts, closed_mask, first_ok)
    # D1 on the market's local calendar day
    local = ts.tz_convert(tz)
    day_key = pd.Series(local.normalize().tz_localize(None), index=frame.index)
    b = _agg(frame, day_key)
    starts = [pd.Timestamp(k).tz_localize(tz).tz_convert("UTC") for k in b.index]
    cutoff_day = pd.Timestamp(cutoff).tz_convert(tz).normalize().tz_localize(None)
    closed_mask = [pd.Timestamp(k) < cutoff_day for k in b.index]
    first_ok = [True] * len(starts)
    if starts:
        first_ok[0] = False  # the rolling window usually begins inside a local day: never trust it
    ctx["D1"] = _tf_state(b, starts, closed_mask, first_ok)
    return ctx


# ------------------------------------------------------------------------------- structure
def build_structure(data: FamilyData, i: int, mspec: MarketSpec, direction: int) -> dict[str, Any]:
    """Cheap causal structure at bar ``i`` of ``data`` (values of bars <= i only)."""
    atr = float(data.atr[i]) if np.isfinite(data.atr[i]) and data.atr[i] > 0 else float("nan")
    c = float(data.c[i])
    cash = data.cash

    def lvl(name: str) -> dict[str, float | None]:
        v = float(cash[name][i])
        return {"price": _num(v), "dist_atr": _num((c - v) / atr) if math.isfinite(atr) else None}

    step_minor, step_major = data.round_steps
    rn: dict[str, Any] = {}
    for nm, st in (("minor", step_minor), ("major", step_major)):
        up = (math.floor(c / st) + 1) * st
        dn = math.floor(c / st) * st
        rn[nm] = {
            "up": _num(up), "down": _num(dn),
            "up_dist_atr": _num((up - c) / atr) if math.isfinite(atr) else None,
            "down_dist_atr": _num((c - dn) / atr) if math.isfinite(atr) else None,
        }
    vwap = float(data.vwap[i])
    pdh, pdl = float(cash["pdh_cash"][i]), float(cash["pdl_cash"][i])
    sess_hi, sess_lo = float(cash["sess_high_cash"][i]), float(cash["sess_low_cash"][i])
    ovn_h, ovn_l = float(cash["overnight_high"][i]), float(cash["overnight_low"][i])
    rng = sess_hi - sess_lo if math.isfinite(sess_hi) and math.isfinite(sess_lo) else float("nan")
    events = {
        "close_above_pdh": bool(math.isfinite(pdh) and c > pdh),
        "close_below_pdl": bool(math.isfinite(pdl) and c < pdl),
        "close_above_overnight_high": bool(math.isfinite(ovn_h) and c > ovn_h),
        "close_below_overnight_low": bool(math.isfinite(ovn_l) and c < ovn_l),
        "at_session_high": bool(math.isfinite(sess_hi) and float(data.h[i]) >= sess_hi),
        "at_session_low": bool(math.isfinite(sess_lo) and float(data.l[i]) <= sess_lo),
    }
    return {
        "direction": int(direction),
        "prev_day_high": lvl("pdh_cash"), "prev_day_low": lvl("pdl_cash"),
        "prev_day_close": lvl("pdc_cash"), "session_open": lvl("sess_open_cash"),
        "session_high": lvl("sess_high_cash"), "session_low": lvl("sess_low_cash"),
        "overnight_high": lvl("overnight_high"), "overnight_low": lvl("overnight_low"),
        "session_range_atr": _num(rng / atr) if math.isfinite(rng) and math.isfinite(atr) else None,
        "gap_cash_atr": _num(cash["gap_cash_atr"][i]),
        "minutes_since_cash_open": _num(cash["minutes_since_cash_open"][i]),
        "vwap_proxy": {"price": _num(vwap),
                       "dist_atr": _num((c - vwap) / atr) if math.isfinite(vwap) and math.isfinite(atr)
                       else None},
        "round_numbers": rn,
        "events": events,
        "not_computed": {k: None for k in NOT_COMPUTED},
    }


def realized_vol(c: np.ndarray, n: int = 48) -> float | None:
    if len(c) < n + 1:
        return None
    r = np.diff(np.log(c[-(n + 1):]))
    return _num(r.std(ddof=1))


# ------------------------------------------------------------------------------- versions
def build_versions(
    *, commit: str, config_hash: str, market_spec_hash: str, strategy_hash: str, policy_id: str,
) -> dict[str, str]:
    return {
        "git_commit": commit, "config_hash": config_hash, "market_spec": market_spec_hash,
        "feature": FEATURE_VERSION, "strategy_hash": strategy_hash, "model": "none",
        "policy": policy_id, "engine": ENGINE_VERSION,
    }


# ------------------------------------------------------------------------------- snapshot
def build_snapshot(
    *,
    cand: Candidate,
    assessment: Assessment,
    phase: Phase,
    created_utc: datetime,
    mspec: MarketSpec,
    versions: dict[str, str],
    context: dict[str, Any],
    structure: dict[str, Any],
    signal_meta: dict[str, Any],
    tick_activity: float | None,
    rvol: float | None,
    forced_flat_iso: str,
) -> OpportunitySnapshot:
    sig_iso = to_utc(cand.signal_ts).isoformat()
    oid = opportunity_id_for(cand.market, cand.strategy_id, cand.spec_hash, sig_iso, cand.direction)
    state = MarketState(
        bid=assessment.bid, ask=assessment.ask, spread=float(assessment.ask - assessment.bid),
        atr=float(cand.atr) if math.isfinite(cand.atr) else 0.0, realized_vol=rvol,
        tick_activity=tick_activity, clock=assessment.clock,
    )
    signal = dict(signal_meta)
    signal["forced_flat_utc"] = forced_flat_iso
    return OpportunitySnapshot(
        opportunity_id=oid,
        phase=phase,
        market=cand.market,
        broker_symbol=cand.broker_symbol,
        direction=cand.direction,
        signal_ts_utc=sig_iso,
        created_utc=to_utc(created_utc).isoformat(),
        versions=dict(versions),
        context=context,
        structure=structure,
        geometry=assessment.geometry,
        market_state=state,
        signal=signal,
    )
