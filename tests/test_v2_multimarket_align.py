"""Causal cross-market alignment: no look-ahead, prefix stability, different sessions, DST weeks."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha.common.market_data import align_markets, load_market_frame
from markets.spec import load_market_spec
from tests._v2mm_helpers import make_frame

STEP = 300


def _t(df):
    return pd.DatetimeIndex(df["ts"]).as_unit("s").asi8


def _frames(start="2025-03-24", end="2025-04-04"):
    return {
        # GER40-like: 07:00-16:00 UTC ; NAS-like: 13:30-20:00 UTC ; FX: 24h
        "GER40": make_frame(start, end, seed=1, daily_windows=((7 * 60, 16 * 60),)),
        "NAS100": make_frame(start, end, seed=2, daily_windows=((13 * 60 + 30, 20 * 60),)),
        "EURUSD": make_frame(start, end, seed=3),
    }


def test_alignment_never_uses_a_bar_completing_after_the_reference_close():
    fr = _frames()
    al = align_markets(fr, "GER40")
    ref_t = _t(fr["GER40"])
    for name, a in al.items():
        t = _t(fr[name])
        v = a.valid
        # matched bar is completed at the reference close: open + step <= ref open + step
        assert (t[a.idx[v]] + STEP <= ref_t[v] + STEP).all()
        # and it is the LATEST such bar: the next bar (if any) completes strictly later
        nxt = a.idx[v] + 1
        ok = nxt < len(t)
        assert (t[nxt[ok]] > ref_t[v][ok]).all()
        assert (a.lag_seconds[v] >= 0).all() and (a.lag_seconds[~v] == -1).all()
        assert (a.idx[~v] == -1).all()


def test_reference_aligns_to_itself_and_take_gives_nan_when_invalid():
    fr = _frames()
    al = align_markets(fr, "GER40")
    assert np.array_equal(al["GER40"].idx, np.arange(len(fr["GER40"])))
    assert al["GER40"].valid.all()
    vals = fr["NAS100"]["close"].to_numpy()
    out = al["NAS100"].take(vals)
    assert np.isnan(out[~al["NAS100"].valid]).all()
    assert np.array_equal(out[al["NAS100"].valid], vals[al["NAS100"].idx[al["NAS100"].valid]])


def test_different_sessions_give_invalid_masks_outside_overlap():
    fr = _frames()
    al = align_markets(fr, "GER40")
    ref = pd.DatetimeIndex(fr["GER40"]["ts"])
    minute = np.asarray(ref.hour * 60 + ref.minute)
    nas = al["NAS100"]
    assert not nas.valid[minute < 13 * 60 + 30].any()  # NAS not open yet -> no fresh bar
    assert nas.valid[(minute >= 13 * 60 + 30) & (minute < 16 * 60)].all()
    assert al["EURUSD"].valid.all()  # FX is always fresh


def test_stale_bar_is_invalid_beyond_max_lag():
    fr = _frames()
    # a market that stopped for the day: yesterday's last bar is still "most recent" but stale
    al0 = align_markets(fr, "GER40", max_lag_bars=10**9)
    al1 = align_markets(fr, "GER40", max_lag_bars=1)
    assert al0["NAS100"].valid.sum() > al1["NAS100"].valid.sum()
    assert (al1["NAS100"].lag_seconds[al1["NAS100"].valid] <= STEP).all()


@pytest.mark.parametrize("k_frac", [0.3, 0.6, 0.9])
def test_prefix_equality_ref_prefix(k_frac):
    fr = _frames()
    full = align_markets(fr, "GER40")
    k = int(len(fr["GER40"]) * k_frac)
    part = dict(fr)
    part["GER40"] = fr["GER40"].iloc[:k]
    pre = align_markets(part, "GER40")
    for name in fr:
        for f in ("idx", "valid", "lag_seconds"):
            assert np.array_equal(getattr(pre[name], f), getattr(full[name], f)[:k]), (name, f)


@pytest.mark.parametrize("k_frac", [0.4, 0.8])
def test_no_future_leak_truncating_all_data_at_time_T(k_frac):
    """Alignment of reference bars <= T is unchanged when every market is cut at T."""
    fr = _frames()
    full = align_markets(fr, "GER40")
    ref = fr["GER40"]
    T = ref["ts"].iloc[int(len(ref) * k_frac)]
    cut = {n: f[f["ts"] <= T].reset_index(drop=True) for n, f in fr.items()}
    part = align_markets(cut, "GER40")
    k = len(cut["GER40"])
    for name in fr:
        assert np.array_equal(part[name].idx, full[name].idx[:k]), name
        assert np.array_equal(part[name].valid, full[name].valid[:k]), name


def test_future_perturbation_does_not_change_earlier_alignment():
    fr = _frames()
    base = align_markets(fr, "GER40")
    pert = {n: f.copy() for n, f in fr.items()}
    T = pert["GER40"]["ts"].iloc[len(pert["GER40"]) // 2]
    for f in pert.values():
        f.loc[f["ts"] > T, ["open", "high", "low", "close"]] *= 1.5
    after = align_markets(pert, "GER40")
    k = int((pert["GER40"]["ts"] <= T).sum())
    for name in fr:
        assert np.array_equal(after[name].idx[:k], base[name].idx[:k])
        # values seen through the alignment up to T are unchanged
        a = base[name].take(fr[name]["close"].to_numpy())[:k]
        b = after[name].take(pert[name]["close"].to_numpy())[:k]
        assert np.array_equal(a, b, equal_nan=True)


def test_dst_weeks_utc_alignment_is_dst_independent():
    """Sessions defined in LOCAL time shift by 1h in UTC across DST changes (EU 2025-03-30,
    US 2025-03-09); the alignment is on UTC instants, so validity follows the actual bars."""
    start, end = "2025-03-03", "2025-04-11"
    # GER40 cash 09:00-17:30 Berlin, NAS cash 09:30-16:00 New York, both converted to UTC per day
    def local_session(tz, a, b, seed):
        rows = []
        for d in pd.bdate_range(start, end):
            lo = pd.Timestamp(d.date()).tz_localize(tz) + pd.Timedelta(minutes=a)
            hi = pd.Timestamp(d.date()).tz_localize(tz) + pd.Timedelta(minutes=b)
            rows.append(pd.date_range(lo.tz_convert("UTC"), hi.tz_convert("UTC"), freq="5min",
                                      inclusive="left"))
        idx = rows[0].append(rows[1:])
        n = len(idx)
        rng = np.random.default_rng(seed)
        c = 100 + np.cumsum(rng.normal(0, 0.05, n))
        o = np.r_[100.0, c[:-1]]
        return pd.DataFrame({"ts": idx, "open": o, "high": np.maximum(o, c) + 0.01,
                             "low": np.minimum(o, c) - 0.01, "close": c,
                             "tick_volume": 1.0, "spread_pts": 10.0})

    fr = {"GER40": local_session("Europe/Berlin", 9 * 60, 17 * 60 + 30, 1),
          "NAS100": local_session("America/New_York", 9 * 60 + 30, 16 * 60, 2)}
    al = align_markets(fr, "GER40")["NAS100"]
    ref_t, nas_t = _t(fr["GER40"]), _t(fr["NAS100"])
    v = al.valid
    assert (nas_t[al.idx[v]] <= ref_t[v]).all()
    assert (ref_t[v] - nas_t[al.idx[v]] <= STEP).all()
    ref = pd.DatetimeIndex(fr["GER40"]["ts"])
    berlin = ref.tz_convert("Europe/Berlin")
    ny = ref.tz_convert("America/New_York")
    ny_min = np.asarray(ny.hour * 60 + ny.minute)
    berlin_min = np.asarray(berlin.hour * 60 + berlin.minute)
    nas_open = (ny_min >= 9 * 60 + 30) & (ny_min < 16 * 60) & (berlin_min < 17 * 60 + 30)
    assert np.array_equal(v, nas_open)  # valid exactly where both markets are in session
    # the US-DST-only week (2025-03-10..14): overlap is 14:30-16:30 Berlin -> starts an hour earlier
    week = (berlin >= "2025-03-10") & (berlin < "2025-03-15")
    first_valid_min = berlin_min[week & v].min()
    assert first_valid_min == 14 * 60 + 30
    normal = (berlin >= "2025-04-07") & (berlin < "2025-04-12")
    assert berlin_min[normal & v].min() == 15 * 60 + 30


DATA = Path(__file__).resolve().parents[1] / "data" / "markets"


@pytest.mark.skipif(not (DATA / "manifest_NAS100.json").is_file(), reason="data/markets missing")
def test_real_data_ger40_nas100_alignment_causal():
    def load(c):
        return load_market_frame(load_market_spec(c), "M5", "2025-10-20", "2025-11-07", source="v2")

    ger, nas = load("GER40"), load("NAS100")
    al = align_markets({"GER40": ger, "NAS100": nas}, "GER40")["NAS100"]
    r, n = _t(ger), _t(nas)
    v = al.valid
    assert v.mean() > 0.9  # both are near-24h CFDs: almost always a fresh bar
    assert (n[al.idx[v]] <= r[v]).all() and (r[v] - n[al.idx[v]] <= STEP).all()
    k = len(ger) // 2
    pre = align_markets({"GER40": ger.iloc[:k], "NAS100": nas}, "GER40")["NAS100"]
    assert np.array_equal(pre.idx, al.idx[:k])
