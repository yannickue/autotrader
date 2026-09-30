# ruff: noqa: E501
"""Lane N: offline retrospective coverage analysis (synthetic bars, no MT5, no live DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from demo_factories import drive_full_trade, make_decision, make_label, make_snapshot

from alpha.families.data import atr14, build_family_data
from alpha.families.roundnum import ROUNDSpec
from alpha.families.spec import MarketCalendar
from coverage_analysis.classify import (
    EXECUTED,
    NEAR_MISS,
    NO_SETUP,
    NO_STRUCTURE,
    OUT_OF_WINDOW,
    REJECTED,
    SIGNAL_NOT_IN_R2,
    classify_moves,
    window_open_mask,
)
from coverage_analysis.control import control_table, eligible_mask, outcome_arrays, wilson
from coverage_analysis.moves import Move, MoveParams, find_moves
from coverage_analysis.r2 import load_r2
from coverage_analysis.replay import ReplayResult, Trigger, replay_market
from coverage_analysis.report import analyse_data, meta_params, render_markdown, to_json
from demo.opportunity.production_spec import FrozenSpec
from demo.store import DemoStore
from markets.spec import load_market_spec

MARKET = "XAUUSD"  # Europe/London; entry window 08:00-15:00 local (UTC+1 in June -> 07:00-14:00 UTC); major round step 10 USD
T0 = pd.Timestamp("2026-06-09 06:00", tz="UTC")  # Tuesday


@pytest.fixture(scope="module")
def ms():
    return load_market_spec(MARKET)


def _frame(o, h, low, c, start=T0, spread_pts=20):
    n = len(c)
    ts = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({
        "ts": ts, "open": o, "high": h, "low": low, "close": c,
        "tick_volume": np.full(n, 100.0), "spread_pts": np.full(n, float(spread_pts)),
    })


def _data(frame, ms):
    return build_family_data(
        frame, MarketCalendar.from_market_spec(ms), name=MARKET, point_size=ms.point_size,
        tick_size=ms.tick_size, asset_class=ms.asset_class,
    )


def _flat(n, base=2008.5):
    b = np.full(n, base)
    return b.copy(), b + 0.5, b - 0.5, b.copy()


def _round_signal_frame(gap: float, n_base: int = 60, tail: int = 3):
    """Flat bars (ATR = 1), then ONE bar whose high is ``gap`` (fraction of prox*ATR) short of the ROUND-reject
    proximity threshold (gap < 0 = beyond it = a real signal), then flat tail bars."""
    o, h, low, c = (list(x) for x in _flat(n_base))
    prox = 0.3
    hh = 2009.7
    for _ in range(8):  # fixed point: the bar's own true range feeds the ATR it is compared with
        cc, ll = hh - 0.5, 2008.3
        hs, ls, cs = np.array([*h, hh]), np.array([*low, ll]), np.array([*c, cc])
        atr = atr14(hs, ls, cs)[-1]
        hh = 2010.0 - prox * (1.0 + gap) * atr
    o.append(2008.5), h.append(hh), low.append(2008.3), c.append(hh - 0.5)
    for _ in range(tail):
        o.append(2008.5), h.append(2009.0), low.append(2008.0), c.append(2008.5)
    return _frame(o, h, low, c), n_base  # decision bar index = n_base


def _round_specs():
    return [FrozenSpec(MARKET, ROUNDSpec(), ())]


# ------------------------------------------------------------------------------- replay / near-miss
def test_near_miss_just_below_threshold(ms):
    fr, i = _round_signal_frame(gap=0.10)
    rep = replay_market(MARKET, _data(fr, ms), _round_specs())
    assert not [t for t in rep.signals if t.idx == i]
    nm = [t for t in rep.near if t.idx == i]
    assert len(nm) == 1
    t = nm[0]
    assert t.condition == "prox_atr" and t.direction == -1  # upper-wick rejection at resistance -> SHORT
    assert 0.09 < t.ratio <= 0.1125 + 1e-9
    assert t.actual == pytest.approx(0.3 * (1 + t.ratio)) and t.required == pytest.approx(0.3) and t.gap_abs == pytest.approx(0.3 * t.ratio) and t.unit == "ATR"  # the proximity it would have needed


def test_not_near_when_far_below_threshold(ms):
    fr, i = _round_signal_frame(gap=1.0)
    rep = replay_market(MARKET, _data(fr, ms), _round_specs())
    assert not [t for t in rep.near + rep.wide + rep.signals if t.idx == i]


def test_wide_trigger_is_not_near(ms):
    fr, i = _round_signal_frame(gap=0.4)
    rep = replay_market(MARKET, _data(fr, ms), _round_specs())
    assert not [t for t in rep.near if t.idx == i]
    w = [t for t in rep.wide if t.idx == i]
    assert len(w) == 1 and 0.3 < w[0].ratio <= 0.5


def test_no_near_miss_when_signal_fires(ms):
    fr, i = _round_signal_frame(gap=-0.2)
    rep = replay_market(MARKET, _data(fr, ms), _round_specs())
    assert [t for t in rep.signals if t.idx == i]
    assert not [t for t in rep.near + rep.wide if t.idx == i]


def _noise(n=2400, seed=5, centre=2008.5, sd=0.5, rev=0.05):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2026-06-01", periods=n // 120 + 2)
    ts = []
    for d in days:
        ts += list(pd.date_range(pd.Timestamp(d).tz_localize("UTC") + pd.Timedelta(hours=6), periods=120, freq="5min"))
    ts = pd.DatetimeIndex(ts[:n])
    c = np.empty(n)
    p = centre
    for k in range(n):
        p += rng.normal(0, sd) - rev * (p - centre)
        c[k] = p
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + np.abs(rng.normal(0, sd * 0.4, n))
    low = np.minimum(o, c) - np.abs(rng.normal(0, sd * 0.4, n))
    return pd.DataFrame({"ts": ts, "open": o, "high": h, "low": low, "close": c,
                         "tick_volume": np.full(n, 100.0), "spread_pts": np.full(n, 20.0)})


NOISY_SPECS = [FrozenSpec(MARKET, ROUNDSpec(prox_atr=2.5, rej_atr=0.2), ())]


def _key(t: Trigger):
    return (t.idx, t.direction, t.strategy_id, t.kind, t.condition, round(t.ratio, 6))


def test_causality_truncating_future_bars_changes_nothing(ms):
    fr = _noise()
    full = replay_market(MARKET, _data(fr, ms), NOISY_SPECS)
    assert len(full.signals) >= 10 and len(full.near) >= 3
    for cut in (800, 1333, 2001):
        part = replay_market(MARKET, _data(fr.iloc[:cut].reset_index(drop=True), ms), NOISY_SPECS)
        lim = cut - 2  # the last bar has no next bar (masked) and is never a decision
        for a, b in ((full.signals, part.signals), (full.near, part.near), (full.wide, part.wide)):
            assert {_key(t) for t in a if t.idx <= lim} == {_key(t) for t in b if t.idx <= lim}


# ------------------------------------------------------------------------------- moves
def _planted_up_move():
    n = 80
    o, h, low, c = (x.copy() for x in _flat(n))
    t = 40
    low[t], c[t], h[t] = 2006.5, 2007.5, 2008.3  # swing low
    for k in range(1, 11):  # rally ~ +1 per bar
        b = 2007.5 + k
        o[t + k], low[t + k], h[t + k], c[t + k] = b - 0.6, b - 0.9, b + 0.3, b
    for k in range(t + 11, n):
        o[k], low[k], h[k], c[k] = 2017.5, 2017.0, 2018.0, 2017.5
    return _frame(o, h, low, c), t


def test_find_moves_planted_and_flat(ms):
    fr, t = _planted_up_move()
    mv = find_moves(_data(fr, ms), MoveParams())
    ups = [m for m in mv if m.direction == 1]
    assert len(ups) == 1 and ups[0].start == t and ups[0].excursion_atr >= 3.0
    flat = _frame(*_flat(80))
    assert find_moves(_data(flat, ms), MoveParams()) == []


# ------------------------------------------------------------------------------- classification
def _classify(ms, rep, move, r2=()):
    fr = _frame(*_flat(120))
    data = _data(fr, ms)
    return classify_moves(data, [move], rep, _round_specs(), MARKET, r2, MoveParams())[0], data


def _trig(idx, d, kind, cond="prox_atr", ratio=0.05):
    return Trigger(idx, d, "ROUND-x", "ROUND", "reject", kind, cond, ratio, 0.3 * (1 + ratio), 0.3)


def test_classification_covered_near_out_unseen(ms):
    # 120 flat bars from 06:00 UTC: bars >= idx 12 (07:00 UTC, local 08:00) have an open entry window
    mv = Move(1, 50, 58, 1.0, 3.2)
    empty = ReplayResult(MARKET)
    r, _ = _classify(ms, empty, mv)
    assert r["klass"] == NO_SETUP and r["reason"] == NO_STRUCTURE

    rep = ReplayResult(MARKET, signals=[_trig(52, 1, "SIGNAL")])
    assert _classify(ms, rep, mv)[0]["klass"] == SIGNAL_NOT_IN_R2
    opp = ReplayResult(MARKET, signals=[_trig(52, -1, "SIGNAL")])  # opposite direction is NOT coverage
    assert _classify(ms, opp, mv)[0]["klass"] == NO_SETUP
    late = ReplayResult(MARKET, signals=[_trig(58, 1, "SIGNAL")])  # after the move was already completed: not coverage
    assert _classify(ms, late, mv)[0]["klass"] == NO_SETUP

    rep = ReplayResult(MARKET, near=[_trig(51, 1, "NEAR", ratio=0.09)])
    r, _ = _classify(ms, rep, mv)
    assert r["klass"] == NEAR_MISS and r["detail"]["failed_condition"] == "prox_atr" and r["detail"]["normalized_gap"] == 0.09 and r["detail"]["required"] == 0.3 and r["detail"]["actual"] == pytest.approx(0.327)

    rep = ReplayResult(MARKET, wide=[_trig(51, 1, "WIDE", "rej_atr", 0.5)])
    r, _ = _classify(ms, rep, mv)
    assert r["klass"] == NO_SETUP and r["reason"] == "ROUND/reject:rej_atr" and r["detail"]["normalized_gap"] == 0.5

    early = Move(1, 2, 8, 1.0, 3.2)  # 06:10-06:40 UTC = 07:10 local: entry window (08:00) closed
    r, _ = _classify(ms, empty, early)
    assert r["klass"] == OUT_OF_WINDOW and "SHADOW" in r["detail"]["note"]


def test_window_open_mask(ms):
    data = _data(_frame(*_flat(120)), ms)
    w = window_open_mask(data, _round_specs())
    assert not w[:11].any() and w[11] and w[90] and not w[100]  # window opens at 07:00 UTC (08:00 local) and closes at 14:00 UTC (idx 96)


def test_r2_covered_read_only_with_outcome(ms, tmp_path):
    p = tmp_path / "copy.sqlite"
    sig = datetime(2026, 6, 9, 6, 0, tzinfo=UTC) + timedelta(minutes=5 * 52)  # close of bar 51
    with DemoStore(p) as st:
        snap = make_snapshot(i=0, market=MARKET, direction=1, signal_ts=sig, signal={"family": "ROUND", "spec": {"mode": "reject"}})
        drive_full_trade(st, snap, net_r=1.25, closed=sig + timedelta(hours=1))
        snap2 = make_snapshot(i=1, market=MARKET, direction=1, signal_ts=sig + timedelta(days=3), signal={"family": "ROUND"})
        st.record_snapshot(snap2)
        st.record_decision(make_decision(snap2, False))
        st.record_counterfactual(make_label(snap2, r=-1.0))
    rows = load_r2(p)  # existing DemoStore readers, on a COPY
    assert len(rows) == 2 and rows[0].outcome_net_r == pytest.approx(1.25) and rows[1].cf_r == pytest.approx(-1.0)
    assert rows[0].executed and not rows[1].executed
    r, _ = _classify(ms, ReplayResult(MARKET), Move(1, 50, 58, 1.0, 3.2), rows)
    assert r["klass"] == EXECUTED and r["detail"][0]["outcome_net_r"] == pytest.approx(1.25)
    assert r["detail"][0]["family"] == "ROUND" and r["detail"][0]["accepted"] is True
    assert _classify(ms, ReplayResult(MARKET), Move(-1, 50, 58, 1.0, 3.2), rows)[0]["klass"] == NO_SETUP
    # a rejected R2 opportunity around the move -> REJECTED with its R2 counterfactual attached
    rej = [rows[1]]
    sig2 = rows[1].signal_ts
    mv = Move(1, 50, 58, 1.0, 3.2)
    data = _data(_frame(*_flat(120), start=sig2 - timedelta(minutes=5 * 52)), ms)
    out = classify_moves(data, [mv], ReplayResult(MARKET), _round_specs(), MARKET, rej, MoveParams())[0]
    assert out["klass"] == REJECTED and out["detail"][0]["counterfactual_r"] == pytest.approx(-1.0)


def test_r2_refuses_artifacts_path(tmp_path):
    bad = tmp_path / "artifacts" / "demo_100k"
    bad.mkdir(parents=True)
    with pytest.raises(ValueError, match="artifacts"):
        load_r2(bad / "demo.sqlite")
    assert not (bad / "demo.sqlite").exists()


# ------------------------------------------------------------------------------- false-positive control
def test_wilson_interval():
    lo, hi = wilson(50, 100)
    assert lo < 0.5 < hi and hi - lo < 0.21
    assert wilson(0, 0)[0] != wilson(0, 0)[0]  # nan


def test_control_noise_trigger_hit_rate_matches_base_rate(ms):
    """Triggers that carry NO information (random bars of a noise series) must hit at the base rate."""
    fr = _noise(n=6000, seed=11)
    data = _data(fr, ms)
    p = MoveParams(n_atr=2.0, m_bars=12, max_adverse_atr=1.0)
    follows, avail = outcome_arrays(data, p)
    elig = eligible_mask(data, NOISY_SPECS, avail)
    rng = np.random.default_rng(3)
    idx = rng.choice(np.flatnonzero(elig), size=400, replace=False)
    trig = [Trigger(int(j), int(rng.choice([1, -1])), "x", "RND", "", "SIGNAL") for j in idx]
    c = control_table(data, trig, follows, elig, seed=1)
    lo, hi = c["trigger"]["ci95"]
    assert c["n_triggers_evaluable"] == 400
    assert lo <= c["base_all"]["rate"] <= hi
    for k in ("uniform_sample", "matched_sample", "shifted"):
        assert abs(c[k]["rate"] - c["base_all"]["rate"]) < 0.06
    assert "indistinguishable" in c["verdict"]
    again = control_table(data, trig, follows, elig, seed=1)
    assert again["uniform_sample"] == c["uniform_sample"]  # seeded, reproducible


def test_control_real_family_triggers_on_noise_are_not_special(ms):
    fr = _noise(n=6000, seed=21)
    data = _data(fr, ms)
    rep = replay_market(MARKET, data, NOISY_SPECS)
    p = MoveParams(n_atr=2.0, m_bars=12)
    follows, avail = outcome_arrays(data, p)
    elig = eligible_mask(data, NOISY_SPECS, avail)
    c = control_table(data, rep.signals, follows, elig, seed=2)
    assert c["n_triggers_evaluable"] >= 30
    assert "ABOVE" not in c["verdict"], c  # ROUND reject triggers carry no edge on a mean-reverting noise series


def test_control_informed_trigger_is_detected(ms):
    """Triggers placed right before planted up-moves must beat the base rate (the control has power)."""
    fr = _noise(n=6000, seed=31)
    data0 = _data(fr, ms)
    p = MoveParams(n_atr=2.0, m_bars=12)
    _, avail0 = outcome_arrays(data0, p)
    elig0 = eligible_mask(data0, NOISY_SPECS, avail0)
    rng = np.random.default_rng(9)
    picks = np.sort(rng.choice(np.flatnonzero(elig0 & (np.arange(len(data0)) % 20 == 0)), size=60, replace=False))
    o, h, low, c = (fr[k].to_numpy(float).copy() for k in ("open", "high", "low", "close"))
    for j in picks:  # plant a rally of ~4 ATR right after each trigger bar
        for k in range(1, 7):
            if j + k < len(c):
                c[j + k] = c[j] + k * 1.2
                o[j + k], h[j + k], low[j + k] = c[j + k] - 1.0, c[j + k] + 0.3, c[j + k] - 1.1
    fr2 = fr.assign(open=o, high=h, low=low, close=c)
    data = _data(fr2, ms)
    follows, avail = outcome_arrays(data, p)
    elig = eligible_mask(data, NOISY_SPECS, avail)
    trig = [Trigger(int(j), 1, "x", "PLANT", "", "SIGNAL") for j in picks]
    res = control_table(data, trig, follows, elig, seed=4)
    assert "ABOVE" in res["verdict"] and res["trigger"]["rate"] > 0.8


def test_control_small_n_is_flagged(ms):
    fr = _noise(n=3000, seed=41)
    data = _data(fr, ms)
    p = MoveParams()
    follows, avail = outcome_arrays(data, p)
    elig = eligible_mask(data, NOISY_SPECS, avail)
    trig = [Trigger(int(j), 1, "x", "F", "", "SIGNAL") for j in np.flatnonzero(elig)[:5]]
    assert "too small" in control_table(data, trig, follows, elig)["verdict"]
    assert control_table(data, [], follows, elig)["verdict"].startswith("no triggers")


# ------------------------------------------------------------------------------- end to end
def test_analyse_data_and_render(ms):
    fr = _noise(n=3600, seed=51)
    data = _data(fr, ms)
    p = MoveParams(n_atr=2.5, m_bars=18)
    res = analyse_data(MARKET, data, NOISY_SPECS, lo=600, params=p)
    assert res["moves"] == sum(res["counts"].values()) and res["moves"] > 0
    assert res["signals"] > 0 and "ALL/SIGNAL" in res["control"] and "ROUND/SIGNAL" in res["control"]
    meta = {"params": meta_params(p), "tolerance": 0.15, "data": "synthetic", "r2": "none", "sample": "unit test"}
    md = render_markdown([res], meta)
    assert "HINDSIGHT DIAGNOSTICS" in md and "MANDATORY CONTROL" in md and "Coverage per market" in md
    assert '"label"' in to_json([res], meta)
    again = analyse_data(MARKET, data, NOISY_SPECS, lo=600, params=p)
    assert again["counts"] == res["counts"] and again["control"]["ALL/SIGNAL"]["uniform_sample"] == res["control"]["ALL/SIGNAL"]["uniform_sample"]
