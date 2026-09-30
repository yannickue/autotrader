# ruff: noqa: E501
"""Golden scenarios + streaming-causality properties for the temporal reference oracle."""

from __future__ import annotations

import math
import random
from dataclasses import replace

import numpy as np
import pytest

from alpha.events import schema as ev
from alpha.temporal.reference import MarketFrame, evaluate_reference, resolve_target
from alpha.temporal.spec import (
    Capture,
    Clause,
    StateMachineStrategySpec,
    StopRule,
    TargetRule,
    Transition,
    mirror,
)

N = 30


def name0(event: str, tf: str, variant: str = "") -> str:
    return ev.array_names(event, tf, variant)[0]


def comp(event: str, tf: str, prefix: str, variant: str = "") -> str:
    return next(n for n in ev.array_names(event, tf, variant) if n.startswith(prefix + "_"))


class FB:
    """Frame builder: flat bars at 99, ATR 1, one run, minute 600."""

    def __init__(self, n: int = N) -> None:
        self.n = n
        self.o = np.full(n, 99.0)
        self.h = np.full(n, 99.3)
        self.l = np.full(n, 98.7)
        self.c = np.full(n, 99.0)
        self.atr = np.ones(n)
        self.rs = np.zeros(n, dtype=np.int64)
        self.bm = np.full(n, 600, dtype=np.int64)
        self.arr: dict[str, np.ndarray] = {}

    def bar(self, i, o=None, h=None, l=None, c=None):  # noqa: E741
        c = self.c[i] if c is None else c
        self.o[i] = c if o is None else o
        self.c[i] = c
        self.h[i] = max(self.c[i], self.o[i]) + 0.3 if h is None else h
        self.l[i] = min(self.c[i], self.o[i]) - 0.3 if l is None else l
        return self

    def put(self, name, idx, val=1.0, dtype=np.float64, fill=0.0):
        a = self.arr.setdefault(name, np.full(self.n, fill, dtype=dtype))
        a[idx] = val
        return self

    def fill(self, name, val, dtype=np.float64):
        self.arr[name] = np.full(self.n, val, dtype=dtype)
        return self

    def frame(self) -> MarketFrame:
        return MarketFrame(self.o, self.h, self.l, self.c, self.atr, self.rs, self.bm,
                           dict(self.arr), {})


def golden_spec() -> StateMachineStrategySpec:
    return StateMachineStrategySpec(
        strategy_id="g", version=1, direction="LONG",
        anchor=(Clause("state", "TREND_UP", "H1"),
                Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster")),
        anchor_capture=(Capture("R0", "lv", "ZONE_LO"),),
        states=(
            Transition(Clause("event", "SWEEP_LOW", "M5", variant="swing_low"), within=8,
                       invalidate=(Clause("bound", "BREAK_DN", "M5", reg="R0"),),
                       capture=(Capture("R1", "evx", "SWEEP_LOW"),)),
            Transition(Clause("bound", "RECLAIM_UP", "M5", reg="R0", variant="k3"), within=3,
                       invalidate=(Clause("bound", "BREAK_DN", "M5", reg="R1"),)),
            Transition(Clause("event", "BOS_UP", "M5"), within=5,
                       capture=(Capture("R2", "evl", "BOS_UP"),)),
            Transition(Clause("bound", "RETEST_HOLD_UP", "M5", reg="R2"), within=3),
        ),
        context=(), expires_after=24, session_window=(480, 1050),
        stop=StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0),
        target=TargetRule("next_structure", levels=("h1_swing_high", "pdh", "session_high"),
                          fallback_r=2.0, min_space_r=1.5),
    )


LEVELS = {"h1_swing_high": 108.0, "pdh": 104.5, "session_high": 110.0}


def golden_market(spec: StateMachineStrategySpec, levels=LEVELS) -> MarketFrame:
    """Section-2 sequence in the frame of ``spec`` (LONG, or its mirror with prices p -> 200-p)."""
    short = spec.direction == "SHORT"
    px = (lambda p: 200.0 - p) if short else (lambda p: p)
    fb = FB()
    bars = {6: 99.0, 7: 98.4, 8: 97.6, 9: 98.6, 10: 99.0, 11: 100.0, 12: 100.2, 13: 99.9}
    for i in range(N):
        fb.bar(i, c=px(bars.get(i, 99.0)))
    if short:  # exact mirror of the wicks
        pass
    # wick / retest shapes (LONG frame numbers)
    def setbar(i, o, h, l, c):  # noqa: E741
        if short:
            o, h, l, c = px(o), px(l), px(h), px(c)  # noqa: E741
        fb.o[i], fb.h[i], fb.l[i], fb.c[i] = o, h, l, c
    setbar(7, 98.5, 98.9, 97.2, 98.4)
    setbar(11, 99.2, 100.3, 99.1, 100.0)
    setbar(12, 100.0, 100.4, 100.0, 100.2)
    setbar(13, 100.2, 100.3, 99.3, 99.9)
    a_state, a_zone = spec.anchor
    fb.fill(name0(a_state.name, a_state.tf), 1, np.int8)
    fb.put(name0(a_zone.name, a_zone.tf, a_zone.variant), 5, 1, np.uint8, 0)
    zl = spec.anchor_capture[0]
    zc = next(c for c in spec.anchor if c.name == "ZONE_ENTER")
    # ZONE_LO/HI of a ZONE_ENTER capture come from the pulse companions (the TESTED zone): evl = lo, evx = hi
    fb.fill(comp("ZONE_ENTER", zc.tf, "evl" if zl.of == "ZONE_LO" else "evx", zc.variant), px(98.0))
    t1, t3 = spec.states[0].trigger, spec.states[2].trigger
    fb.put(name0(t1.name, t1.tf, t1.variant), 7, 1, np.uint8, 0)
    fb.put(comp(t1.name, t1.tf, "evx", t1.variant), 7, px(97.2), np.float64, math.nan)
    fb.put(name0(t3.name, t3.tf), 11, 1, np.uint8, 0)
    fb.put(comp(t3.name, t3.tf, "evl"), 11, px(99.5), np.float64, math.nan)
    for lv in spec.target.levels:
        long_name = lv if not short else ev.target_level(lv)[1]
        fb.fill(lv, px(levels[long_name]))
    return fb.frame()


def result(spec, market):
    r = evaluate_reference(spec, market)
    return r.candidates, r.trails


# ------------------------------------------------------------------------------ golden
def test_section2_golden_sequence():
    spec = golden_spec()
    cand, trails = result(spec, golden_market(spec))
    assert cand.decision_idx.tolist() == [13]
    assert cand.direction.tolist() == [1]
    assert cand.stop[0] == pytest.approx(97.1)
    assert cand.target[0] == pytest.approx(104.5)
    assert cand.target_r[0] == pytest.approx((104.5 - 99.9) / (99.9 - 97.1))
    assert trails[0].anchor_idx == 5 and trails[0].step_idx == (5, 7, 9, 11, 13)
    assert trails[0].registers[:3] == pytest.approx((98.0, 97.2, 99.5))
    assert math.isnan(trails[0].registers[3])
    assert trails[0].entry_zone_lo == 98.0 and math.isnan(trails[0].entry_zone_hi)  # no ZONE_HI array given


def test_short_via_mirror_is_exact_price_mirror():
    spec = golden_spec()
    ms = mirror(spec)
    cand, trails = result(ms, golden_market(ms))
    assert cand.decision_idx.tolist() == [13] and cand.direction.tolist() == [-1]
    assert cand.stop[0] == pytest.approx(200 - 97.1)
    assert cand.target[0] == pytest.approx(200 - 104.5)
    assert cand.target_r[0] == pytest.approx((104.5 - 99.9) / (99.9 - 97.1))
    assert trails[0].step_idx == (5, 7, 9, 11, 13)


def _feature_market(spec, x: float):
    """golden_market + ret_12 (antisymmetric, price-mirrored: x' = -x) and atr_pct (positive-only, same)."""
    m = golden_market(spec)
    sgn = -1.0 if spec.direction == "SHORT" else 1.0
    arrays = dict(m.arrays)
    arrays["ret_12"] = np.full(N, sgn * x)
    arrays["atr_pct"] = np.full(N, 1.2)
    # ONE fixed threshold table for both frames (q=0.3 deliberately looser than -thr(0.7) for the SHORT frame)
    thr = {("ret_12", 0.7): 0.5, ("ret_12", 0.3): -0.2, ("atr_pct", 0.5): 1.0}
    return replace(m, arrays=arrays, thresholds=thr)


@pytest.mark.parametrize("x, fires", [(0.6, True), (0.4, False)])
def test_feature_filters_are_an_exact_price_mirror(x, fires):
    """SHORT candidates on the price-mirrored frame == LONG candidates on the original (audit defect B:
    the old quantile mirror used thr(0.3) for the SHORT ret_12 filter, i.e. a looser threshold)."""
    from alpha.temporal.evaluate import evaluate_temporal_full

    ctx = (Clause("feature", "ret_12", "M5", cmp="gt", q=0.7), Clause("feature", "atr_pct", "M5", cmp="gt", q=0.5))
    spec = replace(golden_spec(), context=ctx)
    ms = mirror(spec)
    short_ctx = {c.name: c for c in ms.context}
    assert short_ctx["ret_12"].cmp == "lt" and short_ctx["ret_12"].q == 0.7 and short_ctx["ret_12"].neg
    assert short_ctx["atr_pct"] == ctx[1]  # positive-only feature keeps the same test
    assert mirror(ms) == spec
    lr, sr = result(spec, _feature_market(spec, x)), result(ms, _feature_market(ms, x))
    assert (len(lr[0].decision_idx) == 1) == fires
    assert sr[0].decision_idx.tolist() == lr[0].decision_idx.tolist()
    if fires:
        np.testing.assert_allclose(sr[0].stop, 200 - lr[0].stop)
        np.testing.assert_allclose(sr[0].target, 200 - lr[0].target)
        np.testing.assert_allclose(sr[0].target_r, lr[0].target_r)
    for s_, m_ in ((spec, _feature_market(spec, x)), (ms, _feature_market(ms, x))):  # numba kernel == oracle
        k = evaluate_temporal_full(s_, m_).candidates
        r = evaluate_reference(s_, m_).candidates
        assert k.decision_idx.tolist() == r.decision_idx.tolist()


def test_next_structure_min_space_rejection_and_fallback():
    spec = golden_spec()
    close_levels = {**LEVELS, "pdh": 103.0}  # space (103-99.9)/2.8 = 1.107 < 1.5
    cand, _ = result(spec, golden_market(spec, close_levels))
    assert len(cand.decision_idx) == 0
    below = {"h1_swing_high": 99.0, "pdh": 99.9, "session_high": 90.0}  # none strictly beyond close
    cand, _ = result(spec, golden_market(spec, below))
    assert cand.decision_idx.tolist() == [13]
    assert math.isnan(cand.target[0]) and cand.target_r[0] == 2.0


def test_resolve_target_never_returns_wrong_side_level():
    rule = TargetRule("next_structure", levels=("pdh",), fallback_r=2.0, min_space_r=1.0)
    t0, r0 = resolve_target(rule, "LONG", 100.0, 1.0, [99.0, 100.0, math.nan])
    assert math.isnan(t0) and r0 == 2.0  # nothing strictly beyond the close -> fallback
    t, r = resolve_target(rule, "LONG", 100.0, 1.0, [99.0, 100.0, 102.0, 105.0])
    assert (t, r) == (102.0, 2.0)
    t, r = resolve_target(rule, "SHORT", 100.0, 1.0, [101.0, 100.0, 98.0, 95.0])
    assert (t, r) == (98.0, 2.0)
    assert resolve_target(rule, "LONG", 100.0, 1.0, [100.5]) is None  # space 0.5 < min 1.0


# ------------------------------------------------------------------------------ small specs
def zone() -> Clause:
    return Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster")


def sweep(**kw) -> Clause:
    return Clause("event", "SWEEP_LOW", "M5", variant="swing_low", **kw)


BOS = Clause("event", "BOS_UP", "M5")
ATR_STOP = StopRule("atr", atr_mult=1.0, max_risk_atr=3.0)
FIX = TargetRule("fixed_r", r=2.0)


def mini(states, anchor_capture=(), expires=24, context=(), anchor=None, stop=ATR_STOP, target=FIX):
    return StateMachineStrategySpec(
        strategy_id="m", version=1, direction="LONG", anchor=anchor or (zone(),),
        anchor_capture=anchor_capture, states=tuple(states), context=context,
        expires_after=expires, session_window=None, stop=stop, target=target,
    )


def pulse(fb: FB, clause: Clause, *idx: int):
    for i in idx:
        fb.put(name0(clause.name, clause.tf, clause.variant), i, 1, np.uint8, 0)


def two_step(within1=8, within2=8, **spec_kw):
    return mini([Transition(sweep(), within=within1), Transition(BOS, within=within2)], **spec_kw)


def cands(spec, fb):
    return result(spec, fb.frame())[0].decision_idx.tolist()


def test_timeout_within_boundary():
    for sweep_at, expect in ((2 + 8, [11]), (2 + 9, [])):
        fb = FB()
        pulse(fb, zone(), 2)
        pulse(fb, sweep(), sweep_at)
        pulse(fb, BOS, sweep_at + 1)
        assert cands(two_step(), fb) == expect


def test_expires_after_counts_from_anchor():
    spec = mini([Transition(sweep()), Transition(BOS)], expires=5)
    for bos_at, expect in ((7, [7]), (8, [])):
        fb = FB()
        pulse(fb, zone(), 2)
        pulse(fb, sweep(), 4)
        pulse(fb, BOS, bos_at)
        assert cands(spec, fb) == expect


def test_invalidation_beats_trigger_on_same_bar():
    r0 = (Capture("R0", "lv", "ZONE_LO"),)
    inv = (Clause("bound", "BREAK_DN", "M5", reg="R0"),)
    spec = mini([Transition(sweep(), within=8, invalidate=inv), Transition(BOS, within=8)], anchor_capture=r0)
    for c5, expect in ((98.5, [7]), (97.5, [])):  # 97.5: close breaks below R0=98 on the sweep bar
        fb = FB()
        pulse(fb, zone(), 2)
        fb.fill(comp("ZONE_ENTER", "M15", "evl", "swing_cluster"), 98.0)
        pulse(fb, sweep(), 5)
        pulse(fb, BOS, 7)
        fb.bar(5, c=c5)
        assert cands(spec, fb) == expect


def test_same_bar_ordering_advances_exactly_one_state():
    r0 = (Capture("R0", "lv", "ZONE_LO"),)
    spec = mini([
        Transition(sweep(), within=8),
        Transition(Clause("bound", "RECLAIM_UP", "M5", reg="R0", variant="k3"), within=8),
        Transition(BOS, within=8),
    ], anchor_capture=r0)

    def build(bos_at, reclaim_again):
        fb = FB()
        pulse(fb, zone(), 4)
        fb.fill(comp("ZONE_ENTER", "M15", "evl", "swing_cluster"), 98.0)
        fb.bar(5, c=97.0)
        fb.bar(6, c=98.8)
        pulse(fb, sweep(), 6)  # bar 6 satisfies SWEEP, RECLAIM and BOS
        pulse(fb, BOS, *bos_at)
        if not reclaim_again:
            fb.bar(7, c=97.5)  # no reclaim at 7
        return fb
    cand, trails = result(spec, build([6, 8], True).frame())
    assert cand.decision_idx.tolist() == [8] and trails[0].step_idx == (4, 6, 7, 8)
    assert cands(spec, build([6], True)) == []  # BOS only on bar 6: it was consumed by nothing
    assert cands(spec, build([6, 8], False)) == []  # reclaim only at 8 -> T3 needs a BOS after 8


def test_guard_failing_on_trigger_bar_blocks_advance():
    g = (Clause("state", "TREND_UP", "H1"),)
    spec = mini([Transition(sweep(), within=8, guards=g), Transition(BOS, within=8)])
    for trend, expect in ((1, [8]), (0, [])):
        fb = FB()
        pulse(fb, zone(), 2)
        pulse(fb, sweep(), 5)
        pulse(fb, BOS, 8)
        fb.fill(name0("TREND_UP", "H1"), 0, np.int8)
        fb.arr[name0("TREND_UP", "H1")][6:] = trend  # guard true only AFTER the trigger bar
        fb.arr[name0("TREND_UP", "H1")][5] = trend if trend else 0
        assert cands(spec, fb) == expect
    fb = FB()  # guard false on the trigger bar, true later: the single pulse is lost
    pulse(fb, zone(), 2)
    pulse(fb, sweep(), 5)
    pulse(fb, BOS, 8)
    fb.fill(name0("TREND_UP", "H1"), 1, np.int8)
    fb.arr[name0("TREND_UP", "H1")][5] = 0
    assert cands(spec, fb) == []


def test_run_boundary_kills_instance():
    spec = two_step()
    for boundary, expect in ((False, [8]), (True, [])):
        fb = FB()
        pulse(fb, zone(), 3)
        pulse(fb, sweep(), 5)
        pulse(fb, BOS, 8)
        if boundary:
            fb.rs[6:] = 6  # new run starts at bar 6, between sweep (5) and BOS (8)
        assert cands(spec, fb) == expect


def test_dedup_keeps_largest_anchor_and_overlapping_anchors_are_independent():
    fb = FB()
    pulse(fb, zone(), 3, 4)
    pulse(fb, sweep(), 6)
    pulse(fb, BOS, 7)
    cand, trails = result(two_step(), fb.frame())
    assert cand.decision_idx.tolist() == [7]
    assert trails[0].anchor_idx == 4 and trails[0].step_idx == (4, 6, 7)
    # independent parts: anchor A=2 completes at 5, anchor B=6 completes at 9
    spec = two_step(within1=3, within2=3)
    fb = FB()
    pulse(fb, zone(), 2, 6)
    pulse(fb, sweep(), 4, 8)
    pulse(fb, BOS, 5, 9)
    cand, trails = result(spec, fb.frame())
    assert cand.decision_idx.tolist() == [5, 9]
    assert [t.anchor_idx for t in trails] == [2, 6]


def test_stop_side_risk_and_context_session():
    fb = FB()
    pulse(fb, zone(), 2)
    pulse(fb, sweep(), 4)
    pulse(fb, BOS, 6)
    assert cands(two_step(), fb) == [6]
    wide = replace(two_step(), stop=StopRule("atr", atr_mult=4.0, max_risk_atr=3.0))
    assert cands(wide, fb) == []  # 4 ATR risk > 3 ATR cap
    fb.atr[6] = math.nan
    assert cands(two_step(), fb) == []
    fb.atr[6] = 1.0
    ctx = Clause("state", "TREND_UP", "H1")
    assert cands(replace(two_step(), context=(ctx,)), fb.fill(name0("TREND_UP", "H1"), 0, np.int8)) == []
    fb.fill(name0("TREND_UP", "H1"), 1, np.int8)
    assert cands(replace(two_step(), context=(ctx,)), fb) == [6]
    for bm, expect in ((479, []), (480, [6]), (1049, [6]), (1050, [])):
        fb.bm[6] = bm
        assert cands(replace(two_step(), session_window=(480, 1050)), fb) == expect


# ------------------------------------------------------------------------------ bound clauses (section 8)
def bound_spec(name, tol=0.0, variant="", ns="TOUCH") -> StateMachineStrategySpec:
    caps = (Capture("R0", "close"),)
    return mini([
        Transition(BOS, within=8, capture=(Capture("R1", "evl", "BOS_UP"),)),
        Transition(Clause("bound", name, "M5", reg="R1", tol_atr=tol, variant=variant), within=8),
    ], anchor_capture=caps)


def run_bound(name, closes, lows=None, highs=None, tol=0.0, variant="", level=100.0, short=False):
    spec = bound_spec(name, tol, variant)
    if short:
        spec = mirror(spec)
    fb = FB()
    pulse(fb, zone(), 0)
    bos = spec.states[0].trigger
    fb.put(name0(bos.name, "M5"), 1, 1, np.uint8, 0)
    fb.put(comp(bos.name, "M5", "evl"), 1, level, np.float64, math.nan)
    for i, c in enumerate(closes):
        j = 2 + i
        fb.bar(j, c=c)
        if lows and i in lows:
            fb.l[j] = lows[i]
        if highs and i in highs:
            fb.h[j] = highs[i]
    return cands(spec, fb)


def test_break_reg_first_close_through():
    assert run_bound("BREAK_UP", [99.0, 100.5]) == [3]
    assert run_bound("BREAK_UP", [101.0, 101.5]) == [2]  # bar 1's close is the (own-run) previous bar, <= R
    assert run_bound("BREAK_UP", [100.05], tol=0.1) == []  # inside tol
    assert run_bound("BREAK_UP", [100.2], tol=0.1) == [2]


def test_touch_reg_low_reaches_level_and_closes_above():
    assert run_bound("TOUCH", [101.0], lows={0: 99.9}) == [2]
    assert run_bound("TOUCH", [101.0], lows={0: 100.05}) == []
    assert run_bound("TOUCH", [101.0], lows={0: 100.05}, tol=0.1) == [2]
    assert run_bound("TOUCH", [99.5], lows={0: 99.0}) == []  # closed below R
    # SHORT: TOUCH is self-mirrored, the price mirror comes from spec.direction
    assert run_bound("TOUCH", [99.0], highs={0: 100.1}, short=True) == [2]


def test_reclaim_reg_needs_prior_close_below_within_k():
    assert run_bound("RECLAIM_UP", [99.0, 100.5], variant="k3") == [3]
    assert run_bound("RECLAIM_UP", [99.0, 99.5], variant="k3") == []  # never above R
    assert run_bound("RECLAIM_UP", [99.0, 100.5, 100.6, 100.7], variant="k3") == [3]  # earliest advance


def test_retest_hold_reg():
    # prior close > R, then low touches R and closes above
    assert run_bound("RETEST_HOLD_UP", [101.0, 100.5], lows={1: 99.9}) == [3]
    assert run_bound("RETEST_HOLD_UP", [99.0, 100.5], lows={1: 99.9}) == []  # no prior break above
    assert run_bound("RETEST_HOLD_UP", [101.0, 100.5], lows={1: 100.2}) == []  # low did not reach R


def test_hold_above_guard_needs_k_bars_inside_run():
    hold = Clause("bound", "HOLD_ABOVE", "M5", reg="R1", variant="k3")
    spec = mini([
        Transition(BOS, within=8, capture=(Capture("R1", "evl", "BOS_UP"),)),
        Transition(sweep(), within=8, guards=(hold,)),
    ])

    def run(closes, sw, rs=None):
        fb = FB()
        pulse(fb, zone(), 0)
        fb.put(name0("BOS_UP", "M5"), 1, 1, np.uint8, 0)
        fb.put(comp("BOS_UP", "M5", "evl"), 1, 100.0, np.float64, math.nan)
        pulse(fb, sweep(), sw)
        for i, c in enumerate(closes):
            fb.bar(2 + i, c=c)
        if rs is not None:
            fb.rs[rs:] = rs
        return cands(spec, fb)
    assert run([101, 101, 101, 101], 5) == [5]
    assert run([101, 99, 101, 101], 5) == []  # bar 3 closes below
    assert run([101, 101, 101, 101], 5, rs=4) == []  # window would start before the run start


def test_before_op_is_strictly_before_and_run_local():
    before = Clause("event", "SWEEP_LOW", "M5", op="BEFORE", arg=3, variant="swing_low")
    spec = mini([Transition(BOS, within=8, guards=(before,))])
    for sw in (1, 2, 3, 4, 5):  # BEFORE(3) at u=5 looks at bars 3,4 only
        fb = FB()
        pulse(fb, zone(), 1)
        pulse(fb, BOS, 5)
        pulse(fb, sweep(), sw)
        assert cands(spec, fb) == ([5] if sw in (3, 4) else [])


# ------------------------------------------------------------------------------ streaming causality
def _rand_spec(rng: random.Random) -> StateMachineStrategySpec:
    n_tr = rng.randint(1, 3)
    nreg = 1
    states = []
    kinds = ["SWEEP", "BOS", "RECLAIM", "RETEST", "TOUCH", "BREAK", "MOM"]
    for _ in range(n_tr):
        kind = rng.choice(kinds)
        tol = rng.choice(ev.TOL_GRID)
        caps: tuple[Capture, ...] = ()
        if kind == "SWEEP":
            trig, cap = sweep(), ("evx", "SWEEP_LOW")
        elif kind == "BOS":
            trig, cap = BOS, ("evl", "BOS_UP")
        elif kind == "MOM":
            trig, cap = Clause("event", "MOMENTUM_RESUME_UP", "M5"), ("evl", "MOMENTUM_RESUME_UP")
        else:
            nm = {"RECLAIM": "RECLAIM_UP", "RETEST": "RETEST_HOLD_UP", "TOUCH": "TOUCH", "BREAK": "BREAK_UP"}[kind]
            trig = Clause("bound", nm, "M5", reg="R0", tol_atr=tol if kind != "RECLAIM" else 0.0,
                          variant=rng.choice(("k3", "k6")) if kind == "RECLAIM" else "")
            cap = (rng.choice(("bar_low", "min_low_since_enter", "close")), "")
        if nreg < 4 and rng.random() < 0.6:
            caps = (Capture(f"R{nreg}", cap[0], cap[1]),)
            nreg += 1
        guards = []
        if rng.random() < 0.3:
            guards.append(rng.choice((
                Clause("state", "TREND_UP", "H1"),
                Clause("bound", "HOLD_ABOVE", "M5", reg="R0", variant="k3"),
                Clause("feature", "atr_pct", "M5", cmp="gt", q=0.5),
                sweep(op="SINCE_ENTER"),
            )))
        inv = (Clause("bound", "BREAK_DN", "M5", reg="R0", tol_atr=rng.choice(ev.TOL_GRID)),) if rng.random() < 0.4 else ()
        states.append(Transition(trig, within=rng.choice((2, 3, 5, 8, 12, None)), guards=tuple(guards),
                                 invalidate=inv, capture=caps))
    ctx = ()
    if rng.random() < 0.3:
        ctx = (rng.choice((Clause("state", "TREND_UP", "M15"), sweep(op="BEFORE", arg=5))),)
    stop = rng.choice((StopRule("register", reg="R0", buffer_atr=0.1, max_risk_atr=3.0), ATR_STOP))
    tgt = rng.choice((FIX, TargetRule("next_structure", levels=("h1_swing_high", "pdh"), fallback_r=1.5, min_space_r=1.0)))
    return StateMachineStrategySpec(
        strategy_id="r", version=1, direction="LONG", anchor=(zone(),),
        anchor_capture=(Capture("R0", "lv", "ZONE_LO"),), states=tuple(states), context=ctx,
        expires_after=rng.randint(len(states) + 1, 40),
        session_window=rng.choice((None, (300, 1200))), stop=stop, target=tgt,
    )


def _spec_array_names(spec: StateMachineStrategySpec) -> dict[str, str]:
    """array name -> prefix kind for every non-bound clause/capture the spec reads."""
    out: dict[str, str] = {}
    clauses = list(spec.anchor) + list(spec.context)
    for t in spec.states:
        clauses += [t.trigger, *t.guards, *t.invalidate]
    for c in clauses:
        if c.kind in ("event", "state"):
            for a in ev.array_names(c.name, c.tf, c.variant):
                out[a] = a.split("_")[0]
    out[comp("ZONE_ENTER", "M15", "evl", "swing_cluster")] = "evl"
    out[comp("ZONE_ENTER", "M15", "evx", "swing_cluster")] = "evx"
    return out


def _rand_frame(rng: random.Random, spec: StateMachineStrategySpec, n: int, seed_arrays=True) -> MarketFrame:
    g = np.random.default_rng(rng.randrange(2**32))
    c = 100 + np.cumsum(g.normal(0, 0.4, n))
    o = np.r_[c[0], c[:-1]] + g.normal(0, 0.1, n)
    h = np.maximum(o, c) + np.abs(g.normal(0.2, 0.2, n))
    l = np.minimum(o, c) - np.abs(g.normal(0.2, 0.2, n))  # noqa: E741
    atr = np.abs(g.normal(1.0, 0.2, n)) + 0.2
    days = np.sort(g.integers(0, 4, n))
    rs = np.zeros(n, dtype=np.int64)
    for i in range(1, n):
        rs[i] = rs[i - 1] if days[i] == days[i - 1] else i
    arrays: dict[str, np.ndarray] = {}
    for name, pre in _spec_array_names(spec).items():
        if pre == "ev":
            arrays[name] = (g.random(n) < 0.12).astype(np.uint8)
        elif pre == "st":
            arrays[name] = (g.random(n) < 0.6).astype(np.int8)
        elif pre in ("evl", "evx", "lv"):
            arrays[name] = np.round(c + g.normal(0, 1.0, n), 2)
        elif pre == "zid":
            arrays[name] = g.integers(0, 5, n).astype(np.int32)
        else:
            arrays[name] = np.zeros(n, dtype=np.int32)
    arrays["atr_pct"] = g.random(n)
    arrays["h1_swing_high"] = np.round(c + np.abs(g.normal(2, 1.5, n)), 2)
    arrays["pdh"] = np.round(c + np.abs(g.normal(3, 2, n)), 2)
    bm = g.integers(0, 288, n) * 5
    return MarketFrame(o, h, l, c, atr, rs, bm.astype(np.int64), arrays, {("atr_pct", 0.5): 0.5})


def _perturb(m: MarketFrame, t: int, seed: int) -> MarketFrame:
    """Randomise EVERYTHING after bar t (prices, run structure, all event arrays)."""
    g = np.random.default_rng(seed)

    def noisy(a: np.ndarray) -> np.ndarray:
        a = a.copy()
        k = len(a) - (t + 1)
        if a.dtype.kind == "f":
            a[t + 1:] = g.normal(100, 30, k)
        else:
            a[t + 1:] = g.integers(0, 2, k).astype(a.dtype)
        return a
    rs = m.run_start.copy()
    rs[t + 1:] = g.integers(0, len(rs), len(rs) - t - 1)
    atr = noisy(m.atr)
    atr[t + 1:] = np.abs(atr[t + 1:]) + 0.1
    return MarketFrame(
        noisy(m.o), noisy(m.h), noisy(m.l), noisy(m.c), atr, rs,
        np.r_[m.berlin_minute[: t + 1], g.integers(0, 1440, len(rs) - t - 1)].astype(np.int64), {k: noisy(v) for k, v in m.arrays.items()},
        dict(m.thresholds),
    )


def _same(a, b) -> bool:
    return all(
        (getattr(a, f) is None and getattr(b, f) is None)
        or np.array_equal(getattr(a, f), getattr(b, f), equal_nan=True)
        for f in a.__dataclass_fields__
    )


def _restrict(res, t: int):
    from alpha.fast.sim import CandidateArrays
    k = int(np.searchsorted(res.candidates.decision_idx, t, side="right"))
    c = res.candidates
    return CandidateArrays(c.decision_idx[:k], c.direction[:k], c.stop[:k], c.target[:k],
                           c.target_r[:k], c.exit_kind[:k]), res.trails[:k]


def _trails_eq(a, b) -> bool:
    return len(a) == len(b) and all(
        x.anchor_idx == y.anchor_idx and x.step_idx == y.step_idx and x.event_ids == y.event_ids
        and np.array_equal(x.registers, y.registers, equal_nan=True) for x, y in zip(a, b, strict=True))


def test_streaming_causality_truncation_and_future_perturbation():
    rng = random.Random(20260930)
    total = 0
    n = 90
    for trial in range(100):
        spec = _rand_spec(rng)
        m = _rand_frame(rng, spec, n)
        full = evaluate_reference(spec, m)
        total += len(full.candidates.decision_idx)
        t = rng.randrange(8, n - 2)
        want_c, want_t = _restrict(full, t)
        pre = evaluate_reference(spec, m.prefix(t + 1))
        assert _same(pre.candidates, want_c), f"trial {trial} truncation"
        assert _trails_eq(pre.trails, want_t)
        per = evaluate_reference(spec, _perturb(m, t, trial))
        got_c, got_t = _restrict(per, t)
        assert _same(got_c, want_c), f"trial {trial} perturbation"
        assert _trails_eq(got_t, want_t)
    assert total >= 20, "generator too sparse: property would be vacuous"


def test_reference_reads_are_causal_structurally():
    from alpha.temporal.reference import _View
    m = golden_market(golden_spec())
    v = _View(m, 5)
    assert v.col("c", 5) == m.c[5]
    with pytest.raises(IndexError):
        v.col("c", 6)
    with pytest.raises(IndexError):
        v.col("c", -1)


@pytest.mark.parametrize("short", [False, True])
def test_zone_capture_reads_pulse_companions_oracle_equals_kernel(short):
    """Section-2 example: the zone register / entry_zone / event_ids come from the ZONE_ENTER pulse companions
    (tested zone), NOT from the lv_ zone arrays (planted with different values as a trap); kernel == oracle."""
    from alpha.temporal.evaluate import evaluate_temporal_full

    spec = golden_spec()
    spec = mirror(spec) if short else spec
    m = golden_market(spec)
    zc = next(c for c in spec.anchor if c.name == "ZONE_ENTER")
    z_n = comp("ZONE_ENTER", zc.tf, "evz", zc.variant)
    other = comp("ZONE_ENTER", zc.tf, "evx" if not short else "evl", zc.variant)  # the array the golden fill left empty
    arrays = dict(m.arrays)
    arrays[other] = np.full(N, 99.0 if not short else 101.0)
    arrays[z_n] = np.full(N, 7, dtype=np.int32)
    for nm in ("ZONE_LO", "ZONE_HI"):  # trap: lv arrays hold a different (close-of-bar) zone
        arrays[name0(nm, zc.tf, zc.variant)] = np.full(N, 50.0)
    m = replace(m, arrays=arrays)
    ref = evaluate_reference(spec, m)
    ker = evaluate_temporal_full(spec, m)
    assert ref.candidates.decision_idx.tolist() == ker.candidates.decision_idx.tolist() == [13]
    rt, kt = ref.trails[0], ker.trails.to_instance_trails()[0]
    assert rt.event_ids == (7,) and kt.event_ids == (7,)
    assert 50.0 not in (rt.entry_zone_lo, rt.entry_zone_hi)
    assert (kt.entry_zone_lo, kt.entry_zone_hi) == pytest.approx((rt.entry_zone_lo, rt.entry_zone_hi))
    assert kt.registers[0] == rt.registers[0]
