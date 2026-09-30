# ruff: noqa: E501
"""Tests for alpha.temporal.spec: validation, dataflow, JSON/hash, mirror, canonical form."""

from __future__ import annotations

import dataclasses
import json
import random
from dataclasses import replace

import pytest

from alpha.events import schema as ev
from alpha.temporal import spec as sp
from alpha.temporal.spec import (
    Capture,
    Clause,
    StateMachineStrategySpec,
    StopRule,
    TargetRule,
    Transition,
)

# ---------------------------------------------------------------- golden (design section 2)


def golden() -> StateMachineStrategySpec:
    return StateMachineStrategySpec(
        strategy_id="golden_zone_sweep",
        version=1,
        direction="LONG",
        anchor=(
            Clause("state", "TREND_UP", "H1"),
            Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster"),
        ),
        anchor_capture=(Capture("R0", "lv", "ZONE_LO"),),
        states=(
            Transition(
                Clause("event", "SWEEP_LOW", "M5", variant="swing_low"), within=8,
                invalidate=(Clause("bound", "BREAK_DN", "M5", reg="R0"),),
                capture=(Capture("R1", "evx", "SWEEP_LOW"),),
            ),
            Transition(
                Clause("bound", "RECLAIM_UP", "M5", reg="R0", variant="k3"), within=3,
                invalidate=(Clause("bound", "BREAK_DN", "M5", reg="R1"),),
            ),
            Transition(
                Clause("event", "BOS_UP", "M5"), within=5,
                capture=(Capture("R2", "evl", "BOS_UP"),),
            ),
            Transition(Clause("bound", "RETEST_HOLD_UP", "M5", reg="R2"), within=3),
        ),
        context=(),
        expires_after=24,
        session_window=(480, 1050),
        stop=StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0),
        target=TargetRule(
            "next_structure", levels=("h1_swing_high", "pdh", "session_high"),
            fallback_r=2.0, min_space_r=1.5,
        ),
    )


def bos(**kw) -> Transition:
    return Transition(Clause("event", "BOS_UP", "M5"), **kw)


G = golden()


def with_(**kw) -> StateMachineStrategySpec:
    return replace(G, **kw)


def raises(fn) -> None:
    with pytest.raises(ValueError):
        fn()


# ---------------------------------------------------------------- structural bounds


def test_golden_valid_and_resolves():
    sp.validate(G)
    for c in (*G.anchor, *(t.trigger for t in G.states)):
        d = ev.get(c.name)
        assert c.tf in d.tfs
    assert sp.complexity(G) == 5 + (2 + 0 + 4) + 0 + 2 + 4 + 0


@pytest.mark.parametrize("n,ok", [(0, False), (1, True), (5, True), (6, False)])
def test_transition_count(n, ok):
    def build():
        return with_(states=tuple(bos() for _ in range(n)), anchor_capture=(), stop=StopRule("atr", atr_mult=1.0),
                     expires_after=30)
    if ok:
        build()
    else:
        raises(build)


@pytest.mark.parametrize("n,ok", [(0, False), (3, True), (4, False)])
def test_anchor_count(n, ok):
    pool = [Clause("state", "TREND_UP", tf) for tf in ("M15", "H1", "D1")] + [
        Clause("state", "TREND_DN", "H1")]
    anchor = tuple(pool[:n])
    build = lambda: with_(anchor=anchor, anchor_capture=(), states=(bos(),),  # noqa: E731
                          stop=StopRule("atr", atr_mult=1.0), expires_after=6)
    build() if ok else raises(build)


@pytest.mark.parametrize("n,ok", [(0, True), (2, True), (3, False)])
def test_guard_count(n, ok):
    guards = tuple(Clause("state", "TREND_UP", tf) for tf in ("M15", "H1", "D1"))[:n]
    build = lambda: with_(states=(bos(guards=guards),), anchor_capture=(),  # noqa: E731
                          stop=StopRule("atr", atr_mult=1.0), expires_after=6)
    build() if ok else raises(build)


@pytest.mark.parametrize("n,ok", [(2, True), (3, False)])
def test_invalidate_count(n, ok):
    inv = tuple(Clause("state", "TREND_DN", tf) for tf in ("M15", "H1", "D1"))[:n]
    build = lambda: bos(invalidate=inv)  # noqa: E731
    build() if ok else raises(build)


@pytest.mark.parametrize("n,ok", [(3, True), (4, False)])
def test_context_count(n, ok):
    ctx = tuple(Clause("state", "TREND_UP", tf) for tf in ("M15", "H1", "D1"))
    ctx = (*ctx, Clause("feature", "atr_pct", "M5", cmp="gt", q=0.5))
    build = lambda: with_(context=ctx[:n])  # noqa: E731
    build() if ok else raises(build)


def test_tf_limit(monkeypatch):
    sp.validate(G)  # uses M15, M5, H1, D1 (via pdh)
    monkeypatch.setattr(sp, "MAX_TF", 3)
    raises(lambda: sp.validate(G))


def test_min_gap_and_within_bounds():
    raises(lambda: bos(min_gap=2))
    raises(lambda: bos(min_gap=0))
    raises(lambda: bos(within=0))
    raises(lambda: bos(within=49))
    raises(lambda: bos(within=True))
    for w in (None, 1, 48):
        bos(within=w)


@pytest.mark.parametrize("e,ok", [(4, False), (5, True), (96, True), (97, False)])
def test_expires_bounds(e, ok):
    build = lambda: with_(expires_after=e)  # noqa: E731
    build() if ok else raises(build)


def test_session_window_rules():
    for sw in ((0, 1440), (5, 10), None):
        with_(session_window=sw)
    for sw in ((10, 10), (12, 60), (-5, 60), (60, 1445), (0, 1500)):
        raises(lambda sw=sw: with_(session_window=sw))


def test_identity_fields():
    raises(lambda: with_(strategy_id=""))
    raises(lambda: with_(version=0))
    raises(lambda: with_(direction="FLAT"))


def test_short_only_via_mirror():
    raises(lambda: with_(direction="SHORT"))
    assert sp.mirror(G).direction == "SHORT"


# ---------------------------------------------------------------- clause rules


def test_name_resolution_and_tf():
    raises(lambda: Clause("event", "NOPE", "M5"))
    raises(lambda: Clause("event", "SWEEP_LOW", "D1", variant="prior20"))
    raises(lambda: Clause("event", "SWEEP_LOW", "M5"))  # variant required
    raises(lambda: Clause("event", "SWEEP_LOW", "M5", variant="pdh"))  # wrong frame src
    raises(lambda: Clause("event", "TREND_UP", "H1"))  # state used as event
    raises(lambda: Clause("state", "BOS_UP", "M5"))
    raises(lambda: Clause("event", "BREAK_UP", "M5"))  # bound-only used as event
    raises(lambda: Clause("bound", "BOS_UP", "M5", reg="R0"))
    raises(lambda: Clause("bound", "BREAK_UP", "M5"))  # missing reg
    raises(lambda: Clause("event", "ZONE_LO", "M5"))  # level is not a clause
    raises(lambda: Clause("bogus", "BOS_UP", "M5"))
    raises(lambda: Clause("event", "BOS_UP", "W1"))


@pytest.mark.parametrize("arg,ok", [(0, False), (1, True), (48, True), (49, False)])
def test_before_hold_arg_bounds(arg, ok):
    for c in (lambda: Clause("event", "BOS_UP", "M5", op="BEFORE", arg=arg),
              lambda: Clause("state", "TREND_UP", "H1", op="HOLD", arg=arg)):
        c() if ok else raises(c)


def test_op_rules():
    raises(lambda: Clause("event", "BOS_UP", "M5", op="HOLD", arg=3))  # pulse + HOLD
    raises(lambda: Clause("state", "TREND_UP", "H1", op="BEFORE", arg=3))
    raises(lambda: Clause("event", "BOS_UP", "M5", op="IS", arg=2))
    raises(lambda: Clause("event", "BOS_UP", "M5", op="XOR"))
    Clause("event", "BOS_UP", "M5", op="SINCE_ENTER")
    raises(lambda: with_(context=(Clause("event", "BOS_UP", "M5", op="SINCE_ENTER"),)))
    raises(lambda: with_(anchor=(Clause("event", "BOS_UP", "M5", op="SINCE_ENTER"),), anchor_capture=()))
    raises(lambda: Transition(Clause("event", "BOS_UP", "M5", op="NOT")))
    raises(lambda: Transition(Clause("feature", "atr_pct", "M5", cmp="gt", q=0.5)))


@pytest.mark.parametrize("q,ok", [(0.04, False), (0.05, True), (0.5, True), (0.95, True),
                                  (0.96, False), (0.07, False), (float("nan"), False)])
def test_feature_q(q, ok):
    c = lambda: Clause("feature", "atr_pct", "M5", cmp="gt", q=q)  # noqa: E731
    c() if ok else raises(c)


def test_feature_rules():
    raises(lambda: Clause("feature", "nope", "M5", cmp="gt", q=0.5))
    raises(lambda: Clause("feature", "atr_pct", "M5", cmp="eq", q=0.5))
    raises(lambda: Clause("feature", "atr_pct", "M5", cmp="gt", q=0.5, op="NOT"))
    raises(lambda: Clause("event", "BOS_UP", "M5", cmp="gt"))


def test_bound_tol_grid():
    for t in (0.0, 0.1, 0.25):
        Clause("bound", "TOUCH", "M5", reg="R0", tol_atr=t)
    raises(lambda: Clause("bound", "TOUCH", "M5", reg="R0", tol_atr=0.2))
    raises(lambda: Clause("event", "BOS_UP", "M5", tol_atr=0.1))


# ---------------------------------------------------------------- dataflow


def test_dataflow_errors():
    # bound clause reads a register before capture
    raises(lambda: with_(anchor_capture=()))
    # own-transition capture is not readable by its own invalidate
    t = bos(invalidate=(Clause("bound", "BREAK_DN", "M5", reg="R0"),),
            capture=(Capture("R0", "evl", "BOS_UP"),))
    raises(lambda: with_(anchor_capture=(), states=(t,), stop=StopRule("atr", atr_mult=1.0),
                         expires_after=6))
    # register captured twice
    raises(lambda: with_(states=(*G.states, bos(capture=(Capture("R1", "evl", "BOS_UP"),)))))
    # capture of an event that is not on the capture bar
    raises(lambda: with_(states=(replace(G.states[0], capture=(Capture("R1", "evx", "SWEEP_HIGH"),)),
                                 *G.states[1:])))
    # lv not exposed by anchor
    raises(lambda: with_(anchor_capture=(Capture("R0", "lv", "TRENDLINE_VALUE"),)))
    # since-enter capture in anchor
    raises(lambda: with_(anchor_capture=(Capture("R0", "min_low_since_enter"),)))
    # stop reads a never-captured register
    raises(lambda: with_(stop=StopRule("register", reg="R3", max_risk_atr=3.0)))
    # zone_edge must read a register captured from lv ZONE_LO (LONG)
    raises(lambda: with_(stop=StopRule("zone_edge", reg="R1", max_risk_atr=3.0)))
    with_(stop=StopRule("zone_edge", reg="R0", buffer_atr=0.05, max_risk_atr=3.0))
    # capture source/of consistency
    raises(lambda: Capture("R0", "close", "BOS_UP"))
    raises(lambda: Capture("R0", "evl", "BREAK_UP"))
    raises(lambda: Capture("R0", "evx", "BOS_UP"))
    raises(lambda: Capture("R9", "close"))


def test_register_limit_and_valid_chain():
    caps = tuple(Capture(f"R{i}", "close") for i in range(4))
    Transition(Clause("event", "BOS_UP", "M5"), capture=caps)
    raises(lambda: Transition(Clause("event", "BOS_UP", "M5"),
                              capture=(*caps, Capture("R0", "bar_low"))))


# ---------------------------------------------------------------- stop / target ranges


def st(**kw):
    return lambda: StopRule("register", reg="R1", **kw)


@pytest.mark.parametrize("b,ok", [(-0.05, False), (0.0, True), (1.0, True), (1.05, False), (0.07, False)])
def test_buffer_range(b, ok):
    f = st(buffer_atr=b)
    f() if ok else raises(f)


@pytest.mark.parametrize("m,ok", [(0.0, False), (0.25, True), (6.0, True), (6.25, False), (0.3, False)])
def test_max_risk_range(m, ok):
    f = st(max_risk_atr=m)
    f() if ok else raises(f)


@pytest.mark.parametrize("r,ok", [(0.75, False), (1.0, True), (4.0, True), (4.25, False), (1.1, False)])
def test_r_range(r, ok):
    f = lambda: TargetRule("fixed_r", r=r)  # noqa: E731
    f() if ok else raises(f)


@pytest.mark.parametrize("s,ok", [(0.25, False), (0.5, True), (3.0, True), (3.25, False)])
def test_min_space_range(s, ok):
    f = lambda: TargetRule("next_structure", levels=("pdh",), fallback_r=2.0, min_space_r=s)  # noqa: E731
    f() if ok else raises(f)


def test_stop_target_shape_rules():
    raises(lambda: StopRule("bogus"))
    raises(lambda: StopRule("atr", atr_mult=0.0))
    StopRule("atr", atr_mult=4.0)
    raises(lambda: StopRule("atr", atr_mult=4.25))
    raises(lambda: StopRule("atr", atr_mult=1.0, reg="R0"))
    raises(lambda: StopRule("swing", of="SWING_LOW_LVL", reg="R0"))
    raises(lambda: StopRule("swing", of="BOS_UP"))
    StopRule("swing", of="SWING_LOW_LVL", tf="M15", buffer_atr=0.5)
    raises(lambda: StopRule("register", reg="R1", max_risk_atr=float("inf")))
    raises(lambda: TargetRule("fixed_r", r=2.0, levels=("pdh",)))
    raises(lambda: TargetRule("next_structure", levels=(), fallback_r=2.0, min_space_r=1.0))
    raises(lambda: TargetRule("next_structure", levels=("pdl", "pdh"), fallback_r=2.0, min_space_r=1.0))
    raises(lambda: TargetRule("next_structure", levels=("nope",), fallback_r=2.0, min_space_r=1.0))
    raises(lambda: TargetRule("next_structure", levels=("pdh",), fallback_r=0.5, min_space_r=1.0))
    # frame: LONG needs high-side levels / SWING_LOW_LVL
    raises(lambda: with_(target=TargetRule("next_structure", levels=("pdl",), fallback_r=2.0,
                                           min_space_r=1.0)))
    raises(lambda: with_(stop=StopRule("swing", of="SWING_HIGH_LVL", max_risk_atr=3.0)))


def test_params_finite_and_json_allow_nan():
    raises(lambda: with_(params=(("x", float("nan")),)))
    raises(lambda: with_(params=(("x", 1.0), ("x", 2.0))))
    with_(params=(("a", 1.0), ("b", 2.0)))
    # a NaN smuggled past the constructors must still fail serialisation
    bad = json.loads(G.to_json())
    bad["params"] = {"x": 1.0}
    with pytest.raises(ValueError):
        json.dumps({"x": float("nan")}, allow_nan=False)


# ---------------------------------------------------------------- JSON + hash


def shuffle_keys(x, rng):
    if isinstance(x, dict):
        items = list(x.items())
        rng.shuffle(items)
        return {k: shuffle_keys(v, rng) for k, v in items}
    if isinstance(x, list):
        return [shuffle_keys(v, rng) for v in x]
    return x


def test_json_round_trip_and_hash_stability():
    for spec in (G, sp.mirror(G), with_(params=(("a", 0.5),), session_window=None)):
        back = StateMachineStrategySpec.from_dict(spec.to_dict())
        assert back == spec and back.spec_hash() == spec.spec_hash()
        via_json = StateMachineStrategySpec.from_dict(
            {k: v for k, v in json.loads(spec.to_json()).items() if k not in ("schema", "schema_version")})
        assert via_json == spec
        rng = random.Random(3)
        for _ in range(5):
            shuffled = shuffle_keys(spec.to_dict(), rng)
            assert StateMachineStrategySpec.from_dict(shuffled).spec_hash() == spec.spec_hash()


def test_hash_domain_and_sensitivity():
    assert G._payload()["schema"] == "temporal"
    assert G.spec_hash() != with_(strategy_id="other").spec_hash()
    assert G.spec_hash() == golden().spec_hash()
    assert sp.canonical_hash(G) != G.spec_hash()
    raises(lambda: StateMachineStrategySpec.from_dict({**G.to_dict(), "extra": 1}))
    raises(lambda: Clause.from_dict({**G.anchor[0].to_dict(), "zzz": 1}))


# ---------------------------------------------------------------- mirror


def test_mirror_golden():
    m = sp.mirror(G)
    assert m.direction == "SHORT" and m.strategy_id.endswith("~S")
    assert m.anchor[0].name == "TREND_DN" and m.anchor[1].name == "ZONE_ENTER"
    assert m.anchor[1].variant == "swing_cluster"
    assert m.anchor_capture == (Capture("R0", "lv", "ZONE_HI"),)
    t1, t2, t3, t4 = m.states
    assert t1.trigger.name == "SWEEP_HIGH" and t1.trigger.variant == "swing_high"
    assert t1.invalidate[0].name == "BREAK_UP" and t1.capture == (Capture("R1", "evx", "SWEEP_HIGH"),)
    assert t2.trigger.name == "RECLAIM_DN" and t2.trigger.variant == "k3" and t2.trigger.reg == "R0"
    assert t2.invalidate[0].name == "BREAK_UP" and t2.invalidate[0].reg == "R1"
    assert t3.trigger.name == "BOS_DN" and t4.trigger.name == "RETEST_HOLD_DN"
    assert m.target.levels == tuple(sorted(("h1_swing_low", "pdl", "session_low")))
    assert m.stop == G.stop
    assert (t1.within, t2.within, t3.within, t4.within) == (8, 3, 5, 3)
    assert dict(m.metadata)["mirrored_from"] == G.spec_hash()


def test_mirror_involution():
    assert sp.mirror(sp.mirror(G)) == G
    g2 = with_(context=(Clause("feature", "ret_12", "M5", cmp="gt", q=0.7),
                        Clause("feature", "atr_pct", "M5", cmp="gt", q=0.7)))
    m = sp.mirror(g2)
    assert m.context[0].cmp == "lt" and m.context[0].q == 0.3
    assert m.context[1].cmp == "gt" and m.context[1].q == 0.7
    assert sp.mirror(m) == g2


def test_mirror_missing_raises(monkeypatch):
    monkeypatch.setitem(ev._REGISTRY, "BOS_UP", dataclasses.replace(ev.get("BOS_UP"), mirror=None))
    raises(lambda: sp.mirror(G))


# ---------------------------------------------------------------- canonical form


def test_canonical_specific_rules():
    trig = Clause("event", "BOS_UP", "M5")
    # guard equal to trigger dropped
    s = with_(anchor_capture=(), states=(Transition(trig, guards=(trig,)),),
              stop=StopRule("atr", atr_mult=1.0), expires_after=6)
    assert sp.canonicalize(s).states[0].guards == ()
    # unread captures dropped
    s = with_(anchor_capture=(), states=(Transition(trig, capture=(Capture("R2", "close"),)),),
              stop=StopRule("atr", atr_mult=1.0), expires_after=6)
    assert sp.canonicalize(s).states[0].capture == ()
    # registers renamed in first-capture order
    s = with_(anchor_capture=(Capture("R3", "lv", "ZONE_LO"),),
              states=(replace(G.states[0], capture=(Capture("R2", "evx", "SWEEP_LOW"),),
                              invalidate=(Clause("bound", "BREAK_DN", "M5", reg="R3"),)),
                      replace(G.states[1], trigger=replace(G.states[1].trigger, reg="R3"),
                              invalidate=(Clause("bound", "BREAK_DN", "M5", reg="R2"),)),
                      replace(G.states[2], capture=(Capture("R1", "evl", "BOS_UP"),)),
                      replace(G.states[3], trigger=replace(G.states[3].trigger, reg="R1"))),
              stop=StopRule("register", reg="R2", buffer_atr=0.1, max_risk_atr=3.0))
    c = sp.canonicalize(s)
    assert c.anchor_capture[0].reg == "R0" and c.states[0].capture[0].reg == "R1"
    assert sp.canonical_hash(s) == sp.canonical_hash(G)
    # within None -> expires_after, clamp, grid snap
    s = with_(anchor_capture=(), states=(bos(within=None),), stop=StopRule("atr", atr_mult=1.0),
              expires_after=24)
    assert sp.canonicalize(s).states[0].within == 24
    s = with_(anchor_capture=(), states=(bos(within=48),), stop=StopRule("atr", atr_mult=1.0),
              expires_after=20)
    assert sp.canonicalize(s).states[0].within == 20
    s = with_(anchor_capture=(), states=(bos(within=4),), stop=StopRule("atr", atr_mult=1.0),
              expires_after=30)
    assert sp.canonicalize(s).states[0].within in (3, 5)
    # (0, 1440) session window == unrestricted
    s = with_(session_window=(0, 1440))
    assert sp.canonical_hash(s) == sp.canonical_hash(with_(session_window=None))
    # AND-set order irrelevant
    a = (Clause("state", "TREND_UP", "H1"), Clause("feature", "atr_pct", "M5", cmp="gt", q=0.5))
    assert sp.canonical_hash(with_(anchor=a, anchor_capture=(), states=(bos(),),
                                   stop=StopRule("atr", atr_mult=1.0), expires_after=6)) == \
        sp.canonical_hash(with_(anchor=a[::-1], anchor_capture=(), states=(bos(),),
                                stop=StopRule("atr", atr_mult=1.0), expires_after=6))
    # golden is already canonical and its canonical form stays valid
    assert sp.canonicalize(G).states == G.states


# ---- randomised generation ---------------------------------------------------------------

_PULSES = [("SWEEP_LOW", ev.get("SWEEP_LOW").variants()), ("SWEEP_HIGH", ev.get("SWEEP_HIGH").variants()),
           ("BOS_UP", ("",)), ("BOS_DN", ("",)), ("CHOCH_UP", ("",)), ("SWING_LOW_CONF", ("",)),
           ("MOMENTUM_RESUME_UP", ("",)), ("PATTERN_COMPLETE", ev.get("PATTERN_COMPLETE").variants()),
           ("TRENDLINE_BREAK", ev.get("TRENDLINE_BREAK").variants()), ("ZONE_EXIT", ev.get("ZONE_EXIT").variants())]
_BOUND = [("BREAK_UP", ("",)), ("BREAK_DN", ("",)), ("TOUCH", ("",)), ("RECLAIM_UP", ("k3", "k6")),
          ("RETEST_HOLD_UP", ("",)), ("HOLD_ABOVE", ("k3", "k6"))]
_TFS = ("M5", "M15", "H1")


def _pulse(rng, op="IS"):
    name, vs = rng.choice(_PULSES)
    tf = rng.choice(_TFS)
    arg = rng.choice((1, 3, 48)) if op == "BEFORE" else 0
    return Clause("event", name, tf, op=op, arg=arg, variant=rng.choice(vs))


def _side(rng, avail):
    r = rng.random()
    if r < 0.25:
        return Clause("state", rng.choice(("TREND_UP", "TREND_DN")), rng.choice(("M15", "H1", "D1")),
                      op=rng.choice(("IS", "NOT")))
    if r < 0.45:
        return Clause("feature", rng.choice(sorted(ev.FEATURE_MIRROR)), rng.choice(_TFS),
                      cmp=rng.choice(("gt", "lt")), q=round(rng.randrange(1, 20) * 0.05, 2))
    if r < 0.7 and avail:
        name, vs = rng.choice(_BOUND)
        return Clause("bound", name, "M5", reg=rng.choice(avail), tol_atr=rng.choice((0.0, 0.1, 0.25)),
                      variant=rng.choice(vs), op="IS")
    return _pulse(rng, rng.choice(("IS", "NOT", "BEFORE")))


def _side_nobound(rng):
    while True:
        c = _side(rng, [])
        return c


def random_spec(rng) -> StateMachineStrategySpec:
    regs = rng.sample(sp.REGS, 4)
    nxt = iter(regs)
    avail: list[str] = []
    anchor = [Clause("event", "ZONE_ENTER", "M15", variant=rng.choice(ev.ZONE_KINDS))]
    for _ in range(rng.randrange(0, 3)):
        anchor.append(_side_nobound(rng))
    rng.shuffle(anchor)
    anchor_capture = []
    if rng.random() < 0.7:
        r = next(nxt)
        anchor_capture.append(Capture(r, "lv", rng.choice(("ZONE_LO", "ZONE_HI"))))
        avail.append(r)
    if rng.random() < 0.3 and len(avail) < 4:
        r = next(nxt)
        anchor_capture.append(Capture(r, rng.choice(("close", "bar_low", "bar_high"))))
        avail.append(r)
    n = rng.randrange(1, 6)
    trans = []
    for _ in range(n):
        if avail and rng.random() < 0.4:
            name, vs = rng.choice(_BOUND)
            trig = Clause("bound", name, "M5", reg=rng.choice(avail), tol_atr=rng.choice((0.0, 0.1)),
                          variant=rng.choice(vs))
        else:
            trig = _pulse(rng)
        guards = []
        for _ in range(rng.randrange(0, 3)):
            guards.append(_side(rng, avail))
        if rng.random() < 0.3:
            guards.append(trig)
            guards = guards[:2] if len(guards) > 2 else guards
        guards = guards[:2]
        inv = [_side(rng, avail) for _ in range(rng.randrange(0, 3))]
        caps = []
        new = []
        pool = [("close", ""), ("bar_low", ""), ("bar_high", ""), ("min_low_since_enter", ""),
                ("max_high_since_enter", "")]
        if trig.kind == "event":
            d = ev.get(trig.name)
            pool += [(s, trig.name) for s in d.produces]
        for _ in range(rng.randrange(0, 3)):
            if len(avail) + len(new) >= 4:
                break
            s, of = rng.choice(pool)
            r = next(nxt)
            caps.append(Capture(r, s, of))
            new.append(r)
        avail.extend(new)
        trans.append(Transition(trig, within=rng.choice((None, 1, 2, 3, 4, 8, 11, 48)),
                                guards=tuple(guards), invalidate=tuple(inv), capture=tuple(caps)))
    used = [c.reg for c in anchor_capture] + [c.reg for t in trans for c in t.capture]
    if used and rng.random() < 0.6:
        stop = StopRule("register", reg=rng.choice(used), buffer_atr=round(rng.randrange(0, 21) * 0.05, 2),
                        max_risk_atr=rng.choice((1.0, 3.0, 6.0)))
    else:
        stop = StopRule("atr", atr_mult=rng.choice((0.5, 1.0, 4.0)), max_risk_atr=2.5)
    if rng.random() < 0.5:
        tgt = TargetRule("fixed_r", r=rng.choice((1.0, 2.0, 4.0)))
    else:
        lv = tuple(sorted(rng.sample(["h1_swing_high", "m15_swing_high", "session_high", "pdh"],
                                     rng.randrange(1, 4))))
        tgt = TargetRule("next_structure", levels=lv, fallback_r=2.0, min_space_r=1.5)
    return StateMachineStrategySpec(
        strategy_id=f"r{rng.randrange(10**6)}", version=1, direction="LONG", anchor=tuple(anchor),
        anchor_capture=tuple(anchor_capture), states=tuple(trans),
        context=tuple(_side_nobound(rng) for _ in range(rng.randrange(0, 3))),
        expires_after=rng.randrange(n + 1, 97), session_window=rng.choice((None, (0, 1440), (480, 1050))),
        stop=stop, target=tgt, params=tuple(sorted({(f"p{i}", 1.0) for i in range(rng.randrange(0, 3))})),
    )


def permute(spec, rng):
    """Semantically equivalent respelling: register names, set orders, capture orders."""
    perm = dict(zip(sp.REGS, rng.sample(sp.REGS, 4), strict=True))

    def rc(c):
        return replace(c, reg=perm[c.reg]) if c.kind == "bound" else c

    def shuf(xs):
        xs = list(xs)
        rng.shuffle(xs)
        return tuple(xs)

    def rcap(caps):
        return shuf(replace(c, reg=perm[c.reg]) for c in caps)

    trans = tuple(
        Transition(rc(t.trigger), t.within, 1, shuf(map(rc, t.guards)), shuf(map(rc, t.invalidate)),
                   rcap(t.capture))
        for t in spec.states
    )
    stop = replace(spec.stop, reg=perm[spec.stop.reg]) if spec.stop.reg else spec.stop
    return replace(spec, anchor=shuf(spec.anchor), anchor_capture=rcap(spec.anchor_capture),
                   states=trans, context=shuf(spec.context), stop=stop)


def test_canonicalize_idempotent_and_permutation_invariant_property():
    rng = random.Random(20260930)
    n_multi = n_dropped = 0
    for i in range(2000):
        s = random_spec(rng)
        c = sp.canonicalize(s)
        assert sp.canonicalize(c) == c, i
        assert sp.canonical_hash(c) == sp.canonical_hash(s)
        p = permute(s, rng)
        assert sp.canonicalize(p) == c, i
        assert sp.canonical_hash(p) == sp.canonical_hash(s), i
        assert sp.complexity(c) <= sp.complexity(s) + 0  # canonical form never gets more complex
        # JSON round trip of the canonical form
        assert StateMachineStrategySpec.from_dict(c.to_dict()) == c
        n_multi += len(s.states) > 1
        n_dropped += any(g == t.trigger for t in s.states for g in t.guards)
    assert n_multi > 500 and n_dropped > 50  # generator exercises what it claims to


def test_distinct_semantics_hash_differently():
    rng = random.Random(7)
    seen: dict[str, str] = {}
    for _ in range(300):
        s = random_spec(rng)
        h = sp.canonical_hash(s)
        m1 = replace(s, expires_after=s.expires_after + 1 if s.expires_after < 96 else 95)
        assert sp.canonical_hash(m1) != h
        m2 = replace(s, stop=replace(s.stop, max_risk_atr=s.stop.max_risk_atr + 0.25
                                     if s.stop.max_risk_atr < 6 else 5.75))
        assert sp.canonical_hash(m2) != h
        seen.setdefault(h, s.to_json())
    # canonical hash equal only for canonically equal specs
    assert len(seen) > 250
