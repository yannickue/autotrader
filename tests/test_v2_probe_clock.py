# ruff: noqa: E501
"""Local-clock fix of the V2 probe: MarketArrays.minute / frame.berlin_minute carry the market-LOCAL minute."""

from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from alpha.common.market_costs import cost_scenarios_for, sizing_for
from alpha.common.sim import SimRules
from alpha.discovery.temporal_search import search_windows
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays, SimWindow, simulate_fast
from research.runners import v2_market_frame as mf
from research.runners import v2_probe


def _ns(s: str) -> np.ndarray:
    return np.array([pd.Timestamp(s, tz="UTC").value], dtype=np.int64)


def test_local_market_minute_known_values_and_dst():
    ny, ldn = "America/New_York", "Europe/London"
    assert v2_probe.local_market_minute(_ns("2025-03-10T13:30:00"), ny)[0] == 570  # EDT since 2025-03-09
    assert v2_probe.local_market_minute(_ns("2025-03-05T14:30:00"), ny)[0] == 570  # EST
    assert v2_probe.local_market_minute(_ns("2025-10-28T13:30:00"), ny)[0] == 570  # EDT until 2025-11-02
    assert v2_probe.local_market_minute(_ns("2025-11-03T14:30:00"), ny)[0] == 570  # EST
    assert v2_probe.local_market_minute(_ns("2025-03-28T08:00:00"), ldn)[0] == 480  # GMT
    assert v2_probe.local_market_minute(_ns("2025-04-01T07:00:00"), ldn)[0] == 480  # BST since 2025-03-30
    # Berlin minute of the mismatch-week NY open (CET vs EDT): 14:30 Berlin, NOT 09:30
    berlin = pd.DatetimeIndex(_ns("2025-03-10T13:30:00").view("datetime64[ns]"), tz="UTC").tz_convert("Europe/Berlin")
    assert berlin.hour[0] * 60 + berlin.minute[0] == 870


def test_market_clock_minute_ger40_is_the_feature_berlin_minute_object():
    from markets.spec import load_market_spec

    feats = {"berlin_minute": np.arange(10, dtype=np.int16), "ts_ns": np.zeros(10, dtype=np.int64)}
    out = v2_probe.market_clock_minute(feats, load_market_spec("GER40"))
    assert out is feats["berlin_minute"] or np.array_equal(out, feats["berlin_minute"])
    assert v2_probe.market_clock_minute(feats, None) is feats["berlin_minute"]


def test_market_arrays_uses_the_given_minute_and_keeps_berlin_day():
    n = 8
    f = {k: np.ones(n) for k in ("o", "h", "l", "c", "spread")}
    f.update(contig=np.ones(n, bool), berlin_minute=np.full(n, 1, np.int16), berlin_day_id=np.arange(n, dtype=np.int32))
    m = v2_probe.market_arrays(f, np.full(n, 7, np.int16))
    assert (m.minute == 7).all() and (m.day == np.arange(n)).all()
    assert (v2_probe.market_arrays(f).minute == 1).all()


def test_build_real_context_swaps_kernel_minute_for_non_berlin_markets():
    src = inspect.getsource(v2_probe.build_real_context)
    assert "dataclasses.replace(frame, berlin_minute=minute)" in src
    assert "market_arrays(features, minute)" in src


def test_search_windows_are_local_entry_windows():
    from markets.spec import load_market_spec

    for name in ("NAS100", "SPX500", "XAUUSD", "EURUSD"):
        spec = load_market_spec(name)
        w = SimWindow.from_spec(spec)
        wins = search_windows(w)
        assert wins and all(spec.calendar.entry_start_min <= a < b <= spec.calendar.entry_end_min for a, b in wins), name


DST_WEEKS = (("2025-03-09", "2025-03-30"), ("2025-10-26", "2025-11-02"), ("2026-03-08", "2026-03-29"))


@pytest.mark.parametrize("name", ["NAS100", "SPX500", "XAUUSD", "EURUSD"])
def test_real_dev_frame_entries_and_exits_follow_local_clock(name):
    from markets.spec import load_market_spec

    spec = load_market_spec(name)
    try:
        dev = mf.build_dev_frame(spec, "v2")
    except Exception as exc:  # data not present in this checkout
        pytest.skip(f"no real dev frame: {exc}")
    ts = pd.DatetimeIndex(dev["ts"])
    ts_ns = ts.as_unit("ns").asi8
    local = ts.tz_convert(spec.calendar.tz)
    minute = v2_probe.local_market_minute(ts_ns, spec.calendar.tz)
    berlin = ts.tz_convert("Europe/Berlin")
    day = np.unique(berlin.normalize().tz_localize(None).to_numpy().astype("datetime64[D]"), return_inverse=True)[1]
    n = len(dev)
    contig = np.r_[np.diff(ts.as_unit("s").asi8) == 300, False]
    mk = MarketArrays(dev["open"].to_numpy(float), dev["high"].to_numpy(float), dev["low"].to_numpy(float),
                      dev["close"].to_numpy(float), dev["spread_pts"].to_numpy(float) * spec.point_size,
                      minute.astype(np.int64), day.astype(np.int64), contig)
    sizing = sizing_for(spec, account_eur=10_000.0, risk_fraction=0.005)
    cost = cost_scenarios_for(spec)["COMBINED_ADVERSE"]
    win = SimWindow.from_spec(spec)
    dec = np.arange(300, n - 5, 7, dtype=np.int64)  # candidates around the clock: only the window can filter them
    direction = np.where(np.arange(len(dec)) % 2 == 0, 1, -1).astype(np.int8)
    dist = float(np.sqrt(sizing.min_risk_pts * sizing.max_risk_pts))
    stop = mk.c[dec] - direction * dist
    c = CandidateArrays(dec, direction, stop, np.full(len(dec), np.nan), np.full(len(dec), 1.0),
                        np.full(len(dec), EXIT_FIXED_R, dtype=np.int8))
    tr = simulate_fast(mk, c, cost, sizing, SimRules(max_trades_per_day=6, max_entry_spread_pts=1e9), win)
    assert len(tr) > 200
    e_min, x_min = minute[tr.entry_idx], minute[tr.exit_idx]
    assert ((e_min >= win.entry_start_min) & (e_min < win.entry_end_min)).all()
    assert (x_min <= win.flat_min).all()
    # the same entries seen through the LOCAL tz index of the timestamps (independent of ``minute``)
    lmin = np.asarray(local.hour * 60 + local.minute)
    assert ((lmin[tr.entry_idx] >= win.entry_start_min) & (lmin[tr.entry_idx] < win.entry_end_min)).all()
    assert (lmin[tr.exit_idx] <= win.flat_min).all()
    lday = np.asarray(local.normalize().tz_localize(None).to_numpy().astype("datetime64[D]"))
    assert (lday[tr.entry_idx] == lday[tr.exit_idx]).all()  # flat before the local day ends
    e_dates = lday[tr.entry_idx]
    for lo, hi in DST_WEEKS:
        sel = (e_dates >= np.datetime64(lo)) & (e_dates <= np.datetime64(hi))
        if sel.any():  # (a dev frame may not reach every week)
            assert ((e_min[sel] >= win.entry_start_min) & (e_min[sel] < win.entry_end_min)).all(), (name, lo)
    if spec.calendar.tz == "America/New_York":  # the old Berlin-minute basis would have failed this window
        bmin = np.asarray(berlin.hour * 60 + berlin.minute)[tr.entry_idx]
        assert not ((bmin >= win.entry_start_min) & (bmin < win.entry_end_min)).all()
