# ruff: noqa: E501
"""static-demo-policy-v1: every reason code, executable-side evaluation, simulator parity, intents."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from opp_helpers import synth_candidate

from alpha.common.sim import CostScenario, SimRules, SizingSpec
from alpha.fast.sim import CandidateArrays, MarketArrays, SimWindow, simulate_fast
from demo.contracts import OpportunitySnapshot
from demo.opportunity.bar_source import Quote
from demo.opportunity.policy import (
    ACCEPTED,
    ALL_CODES,
    CLOCK_ANOMALY,
    DUPLICATE_OPPORTUNITY,
    ENTRY_OVERSHOT,
    MARKET_CLOSED,
    NO_STRUCTURAL_STOP,
    ONE_POSITION_PER_INSTRUMENT,
    OUTSIDE_ENTRY_WINDOW,
    POLICY_ID,
    REASONS,
    SPACE_BELOW_MIN_R,
    SPREAD_TOO_WIDE,
    STALE_SIGNAL,
    TARGET_ALREADY_CROSSED,
    PolicyConfig,
    StaticDemoPolicy,
)

SIG = datetime(2026, 6, 10, 8, 0, tzinfo=UTC)  # 10:00 Berlin (CEST), inside the GER40 window


@pytest.fixture
def ger(mspecs):
    return mspecs["GER40"]


def _q(bid=100.0, ask=100.1, ts=SIG):
    return Quote(ts_utc=ts, bid=bid, ask=ask)


def _assess(ger, cand, quote="default", now=SIG, policy=None, **kw):
    pol = policy or StaticDemoPolicy()
    q = _q() if quote == "default" else quote
    return pol.assess(cand, q, now, ger, **kw)


def test_baseline_accept_and_geometry(ger):
    a = _assess(ger, synth_candidate(ger, direction=1, stop=99.0))
    assert a.reasons == (ACCEPTED,) and a.accepted
    g = a.geometry
    assert g.intended_entry == 100.1  # ask for a long
    assert g.stop == 99.0 and g.risk_distance == pytest.approx(1.1)
    assert g.target == pytest.approx(100.1 + 1.5 * 1.1)  # R target from the actual fill
    assert g.exit_kind == "fixed_r" and g.exit_r == 1.5
    assert g.space_to_opposition_r is None
    s = _assess(ger, synth_candidate(ger, direction=-1, stop=101.0))
    assert s.exec_price == 100.0  # bid for a short
    assert s.geometry.risk_distance == pytest.approx(1.0)
    assert s.geometry.target == pytest.approx(100.0 - 1.5)


def test_stale_signal(ger):
    a = _assess(ger, synth_candidate(ger), now=SIG + timedelta(seconds=121), quote=_q(ts=SIG + timedelta(seconds=120)))
    assert a.reasons == (STALE_SIGNAL,)
    old_quote = _assess(ger, synth_candidate(ger), now=SIG + timedelta(seconds=61), quote=_q(ts=SIG))
    assert old_quote.reasons == (STALE_SIGNAL,)  # quote older than 60 s


def test_outside_entry_window(ger):
    late = datetime(2026, 6, 10, 19, 0, tzinfo=UTC)  # 21:00 Berlin >= entry_end 20:00
    a = _assess(ger, synth_candidate(ger, signal_utc=late), now=late, quote=_q(ts=late))
    assert a.reasons == (OUTSIDE_ENTRY_WINDOW,)
    assert a.clock.in_entry_window is False
    early = datetime(2026, 6, 10, 6, 55, tzinfo=UTC)  # 08:55 Berlin < 09:00
    b = _assess(ger, synth_candidate(ger, signal_utc=early), now=early, quote=_q(ts=early))
    assert b.reasons == (OUTSIDE_ENTRY_WINDOW,)


def test_spread_too_wide(ger):
    wide = ger.max_entry_spread_price + 0.01
    a = _assess(ger, synth_candidate(ger, bar_spread=wide), quote=_q(ask=100.0 + 0.1))
    assert a.reasons == (SPREAD_TOO_WIDE,)
    b = _assess(ger, synth_candidate(ger), quote=_q(bid=100.0, ask=100.0 + wide))
    assert SPREAD_TOO_WIDE in b.reasons
    ok = _assess(ger, synth_candidate(ger, bar_spread=ger.max_entry_spread_price), quote=_q())
    assert SPREAD_TOO_WIDE not in ok.reasons  # cap is inclusive like the simulator (> cap skips)


def test_entry_overshot_stop_crossed_and_drift(ger):
    crossed = _assess(ger, synth_candidate(ger, stop=99.0), quote=_q(bid=98.8, ask=98.9))
    assert crossed.reasons == (ENTRY_OVERSHOT,)
    drift = _assess(ger, synth_candidate(ger, stop=95.0, atr=1.0), quote=_q(bid=100.9, ask=101.0))
    assert drift.reasons == (ENTRY_OVERSHOT,)  # 1.0 adverse > 0.5 ATR
    fine = _assess(ger, synth_candidate(ger, stop=95.0, atr=1.0), quote=_q(bid=100.3, ask=100.4))
    assert fine.reasons == (ACCEPTED,)
    short = _assess(ger, synth_candidate(ger, direction=-1, stop=101.0), quote=_q(bid=101.2, ask=101.3))
    assert short.reasons == (ENTRY_OVERSHOT,)


def test_target_already_crossed_uses_executable_side(ger):
    # long: target 100.05 is above the bid (100.0) but below the ask (100.1) -> crossed at the fill
    a = _assess(ger, synth_candidate(ger, stop=99.0, target=100.05))
    assert a.reasons == (TARGET_ALREADY_CROSSED,)
    b = _assess(ger, synth_candidate(ger, direction=-1, stop=101.0, target=100.05), quote=_q(bid=100.0, ask=100.1))
    assert b.reasons == (TARGET_ALREADY_CROSSED,)  # short fills at the bid 100.0, target above it
    c = _assess(ger, synth_candidate(ger, stop=99.0, target=100.1))
    assert c.reasons == (TARGET_ALREADY_CROSSED,)  # equal to the fill: not strictly beyond


def test_space_below_min_r_is_evaluated_at_the_ask_for_longs(ger):
    cfg = PolicyConfig(min_space_r=0.0)
    pol = StaticDemoPolicy(cfg)
    cand = synth_candidate(ger, stop=99.0, target=100.9, min_space_r=0.55)
    at_bid = _assess(ger, cand, quote=_q(bid=100.0, ask=100.0), policy=pol)  # zero spread
    assert at_bid.reasons == (ACCEPTED,)  # 0.9 / 1.0 = 0.9 >= 0.55
    at_ask = _assess(ger, cand, quote=_q(bid=100.0, ask=100.4), policy=pol)
    assert at_ask.reasons == (SPACE_BELOW_MIN_R,)  # 0.5 / 1.4 = 0.357 < 0.55
    assert at_ask.implied_r == pytest.approx(0.5 / 1.4)


def test_default_min_space_applies_only_to_finite_targets(ger):
    r_target = _assess(ger, synth_candidate(ger, stop=99.0, target_r=0.1))
    assert r_target.reasons == (ACCEPTED,)  # R targets carry no space check (sim: NaN target path)
    structural = _assess(ger, synth_candidate(ger, stop=99.0, target=100.2))  # 0.1/1.1 < 0.25
    assert structural.reasons == (SPACE_BELOW_MIN_R,)


def test_no_structural_stop(ger):
    assert _assess(ger, synth_candidate(ger, stop=float("nan"))).reasons == (NO_STRUCTURAL_STOP,)
    wrong_side = _assess(ger, synth_candidate(ger, direction=1, stop=101.0))
    assert wrong_side.reasons == (NO_STRUCTURAL_STOP,)
    trail = _assess(ger, synth_candidate(ger, stop=99.0, exit_kind=1))
    assert trail.reasons == (NO_STRUCTURAL_STOP,)


def test_duplicate_market_closed_clock_anomaly_position(ger):
    assert _assess(ger, synth_candidate(ger), is_duplicate=True).reasons == (DUPLICATE_OPPORTUNITY,)
    assert _assess(ger, synth_candidate(ger), quote=None).reasons == (MARKET_CLOSED,)
    assert _assess(ger, synth_candidate(ger), quote=_q(bid=100.2, ask=100.1)).reasons == (MARKET_CLOSED,)
    sat = datetime(2026, 6, 13, 8, 0, tzinfo=UTC)
    weekend = _assess(ger, synth_candidate(ger, signal_utc=sat), now=sat, quote=_q(ts=sat))
    assert MARKET_CLOSED in weekend.reasons
    future = _assess(ger, synth_candidate(ger), now=SIG - timedelta(seconds=60))
    assert future.reasons == (CLOCK_ANOMALY,)
    assert _assess(ger, synth_candidate(ger), position_open=True).reasons == (ONE_POSITION_PER_INSTRUMENT,)


def test_multiple_reasons_are_ordered_and_complete(ger):
    a = _assess(
        ger, synth_candidate(ger, stop=99.0), quote=None, now=SIG + timedelta(seconds=500),
        is_duplicate=True, position_open=True,
    )
    assert a.reasons == (MARKET_CLOSED, STALE_SIGNAL, DUPLICATE_OPPORTUNITY, ONE_POSITION_PER_INSTRUMENT)
    order = [REASONS.index(r) for r in a.reasons]
    assert order == sorted(order)


def test_reason_code_set_is_exact():
    assert set(ALL_CODES) == {
        "STALE_SIGNAL", "OUTSIDE_ENTRY_WINDOW", "SPREAD_TOO_WIDE", "ENTRY_OVERSHOT",
        "TARGET_ALREADY_CROSSED", "SPACE_BELOW_MIN_R", "NO_STRUCTURAL_STOP", "DUPLICATE_OPPORTUNITY",
        "MARKET_CLOSED", "CLOCK_ANOMALY", "ONE_POSITION_PER_INSTRUMENT", "ACCEPTED",
    }
    assert POLICY_ID == "static-demo-policy-v1"


def test_decision_and_intent(ger):
    from demo.contracts import MarketState, opportunity_id_for

    pol = StaticDemoPolicy()
    cand = synth_candidate(ger, stop=99.0)
    a = _assess(ger, cand, policy=pol)
    oid = opportunity_id_for("GER40", "ORB-test", "testhash", SIG.isoformat(), 1)
    dec = pol.decision(oid, "DISCOVERY", SIG, a)
    assert dec.accepted and dec.reasons == (ACCEPTED,) and dec.policy_id == POLICY_ID
    snap = OpportunitySnapshot(
        opportunity_id=oid, phase="DISCOVERY", market="GER40", broker_symbol="Ger40", direction=1,
        signal_ts_utc=SIG.isoformat(), created_utc=SIG.isoformat(), versions={}, context={},
        structure={}, geometry=a.geometry,
        market_state=MarketState(bid=100.0, ask=100.1, spread=0.1, atr=1.0, realized_vol=None,
                                 tick_activity=None, clock=a.clock),
        signal={},
    )
    intent = pol.intent_for(snap, dec, ger, cand.window)
    assert intent is not None
    assert intent.risk_fraction == 0.01 and intent.entry_ref == 100.1 and intent.stop == 99.0
    assert intent.valid_until_utc == (SIG + timedelta(minutes=5)).isoformat()
    # GER40 forced flat 21:30 Berlin (CEST, UTC+2) = 19:30 UTC
    assert intent.forced_flat_utc == datetime(2026, 6, 10, 19, 30, tzinfo=UTC).isoformat()
    assert intent.intent_id == pol.intent_for(snap, dec, ger, cand.window).intent_id  # deterministic
    rej = pol.decision(oid, "DISCOVERY", SIG, _assess(ger, cand, quote=None, policy=pol))
    assert pol.intent_for(snap, rej, ger, cand.window) is None
    with pytest.raises(FrozenInstanceError):
        a.reasons = ()  # type: ignore[misc]


# ------------------------------------------------------------------ parity with the fast simulator
def _sim_skips(direction, o_j, stop, target, min_space, spread=0.2):
    n = 6
    o = np.full(n, 100.0)
    o[2:] = o_j
    px = o.copy()
    market = MarketArrays(
        o=o, h=px, l=px, c=px.copy(), spread=np.full(n, spread), minute=np.arange(n) * 5 + 600,
        day=np.zeros(n, dtype=np.int64), contig_next=np.array([True] * (n - 1) + [False]),
    )
    cand = CandidateArrays(
        decision_idx=np.array([1]), direction=np.array([direction], dtype=np.int8),
        stop=np.array([stop]), target=np.array([target]), target_r=np.array([1.5]),
        exit_kind=np.array([0], dtype=np.int8), min_space_r=np.array([min_space]),
    )
    ta = simulate_fast(
        market, cand, CostScenario("T", 1.0, 0.0, 0.0),
        SizingSpec(equity_eur=1e6, risk_fraction=0.005, lot_step=0.01, min_lot=0.01,
                   max_leverage=1e6, min_risk_pts=0.0, max_risk_pts=1e9, contract_size=1.0),
        SimRules(max_trades_per_day=6, max_entry_spread_pts=1e9), SimWindow(0, 1440, 1440),
    )
    return ta.skips, len(ta), bool(ta.entry_gap.any()) if len(ta) else False


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("offset", [-0.5, 0.0, 1e-10, 0.3, 2.0])
@pytest.mark.parametrize("min_space", [float("nan"), 0.5, 1.5])
def test_parity_target_crossed_and_min_space_with_fast_sim(ger, direction, offset, min_space):
    spread, o_j = 0.2, 100.0
    fill = o_j + spread if direction > 0 else o_j
    stop = o_j - 1.0 if direction > 0 else o_j + 1.0
    target = fill + direction * offset
    skips, _, _ = _sim_skips(direction, o_j, stop, target, min_space, spread)
    cand = synth_candidate(ger, direction=direction, close=100.0, atr=1.0, bar_spread=spread,
                           stop=stop, target=target, min_space_r=min_space)
    pol = StaticDemoPolicy(PolicyConfig(min_space_r=0.0))
    a = pol.assess(cand, Quote(ts_utc=SIG, bid=o_j, ask=o_j + spread), SIG, ger)
    assert (TARGET_ALREADY_CROSSED in a.reasons) == (skips["target_crossed_at_fill"] == 1)
    assert (SPACE_BELOW_MIN_R in a.reasons) == (skips["space_below_min_at_fill"] == 1)


@pytest.mark.parametrize("direction", [1, -1])
def test_parity_stop_gap_matches_sim_entry_gap(ger, direction):
    spread, o_j = 0.2, 98.0 if direction > 0 else 102.0  # gap through a stop 1.0 away from close
    stop = 99.0 if direction > 0 else 101.0
    skips, n_trades, gap = _sim_skips(direction, o_j, stop, float("nan"), float("nan"), spread)
    assert gap and skips["entry_gap_stop"] == 1
    cand = synth_candidate(ger, direction=direction, close=100.0, bar_spread=spread, stop=stop)
    a = StaticDemoPolicy().assess(cand, Quote(ts_utc=SIG, bid=o_j, ask=o_j + spread), SIG, ger)
    assert ENTRY_OVERSHOT in a.reasons
    del n_trades


def test_parity_clean_trade_is_accepted_and_booked(ger):
    skips, n_trades, gap = _sim_skips(1, 100.0, 99.0, 102.0, 0.5)
    assert n_trades == 1 and not gap and sum(skips.values()) == 0
    cand = synth_candidate(ger, stop=99.0, target=102.0, min_space_r=0.5, bar_spread=0.2)
    a = StaticDemoPolicy().assess(cand, Quote(ts_utc=SIG, bid=100.0, ask=100.2), SIG, ger)
    assert a.reasons == (ACCEPTED,)
