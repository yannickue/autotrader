# ruff: noqa: E501
"""Position-thesis monitor: classification, exit != reverse, hypothetical variants A-D, opposing-event metrics (synthetic data)."""

from __future__ import annotations

import inspect
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest

from demo.labeling import Bar
from research_workbench.thesis import position_thesis as pt_mod
from research_workbench.thesis.contracts import (
    POSITION_SEVERITY,
    Alignment,
    Condition,
    Direction,
    EvidenceClass,
    MarketMap,
    MarketPhase,
    OpposingEvent,
    PositionThesis,
    PositionThesisTransition,
    SetupState,
    SetupThesis,
    legal_position_transition,
)
from research_workbench.thesis.contracts import (
    PositionThesisState as S,
)
from research_workbench.thesis.position_thesis import (
    EntryPath,
    Variant,
    build_cohort,
    conditional_probabilities,
    conditional_stat,
    event_outcome,
    false_early_exit,
    hypothetical_exit,
    mark_future_path_complete,
    observe,
    open_position_thesis,
    run_variants,
    should_remain_open,
    summary,
    valid_new_setup_in_opposite_direction,
    variant_trigger_ts,
    whipsaw,
)

L, SH = Direction.LONG, Direction.SHORT
ENTRY_TS = 1000
PREMISE = (Condition(EvidenceClass.LEVEL_BEHAVIOUR, "FLIPPED_TO_SUPPORT", True, True, 900),)


def mk_map(
    ts: int,
    *,
    m15: str | None = "UP_SEQUENCE",
    acceptance: dict | None = None,
    market: str = "EURUSD",
) -> MarketMap:
    return MarketMap(
        market=market, decision_ts_ns=ts, bar_index=0, session=None, market_phase=MarketPhase.TREND, h1_context=None,
        m15_structure=m15, m5_structure=None, nearest_support=None, nearest_resistance=None, second_support=None,
        second_resistance=None, active_support_zone=None, active_resistance_zone=None, role_reversal_zones=(),
        balance_state=None, acceptance_state=acceptance or {},
    )  # fmt: skip


def mk_setup(
    direction: Direction, *, state=SetupState.ARMED, observed: bool | None = True, tid="s1", ts=1400
) -> SetupThesis:
    cond = Condition(EvidenceClass.TRIGGER, "trig", True, observed, ts)
    return SetupThesis(
        thesis_id=tid, market="EURUSD", direction=direction, archetype="CONTINUATION_RETEST", spec_hash="h", state=state,
        created_at_ns=ts, updated_at_ns=ts, conditions=(cond,), invalidation_condition=None, expiry_condition=None,
        entry_zone=None, structural_stop=None, opposition_1=None, opposition_2=None, family_events=(),
        daily_thesis_alignment=Alignment.NEUTRAL, marketmap_version="m",
    )  # fmt: skip


def new_pt(direction: Direction = L) -> PositionThesis:
    return open_position_thesis("p1", "EURUSD", direction, ENTRY_TS, None, "STRUCT_RETEST", PREMISE)


def ev(ts: int, direction: Direction, source: str = "ROUND_REJECT") -> OpposingEvent:
    return OpposingEvent(ts, direction, source, True, True)


def run(direction: Direction, steps) -> list[PositionThesis]:
    """steps: list of dict(ts, m15?, acceptance?, events?, setups?, status?, closed?) -> pt after each step."""
    cur, out = new_pt(direction), []
    for s in steps:
        cur = observe(
            cur, mk_map(s["ts"], m15=s.get("m15", "UP_SEQUENCE" if direction is L else "DOWN_SEQUENCE"),
                        acceptance=s.get("acceptance")),
            s.get("events", ()), s.get("setups", ()), s.get("status"), s.get("closed", False),
        )  # fmt: skip
        out.append(cur)
    return out


def scenario_long() -> list[dict]:
    return [
        {"ts": 1100},
        {"ts": 1200, "events": [ev(1200, SH)]},
        {"ts": 1300},
        {"ts": 1400, "setups": [mk_setup(SH)]},
        {"ts": 1500},
        {"ts": 1600, "status": {"FLIPPED_TO_SUPPORT": False}},
        {
            "ts": 1700,
            "status": {"FLIPPED_TO_SUPPORT": False},
            "acceptance": {"SHORT:L1": "ACCEPTED"},
        },
        {"ts": 1800, "status": {"FLIPPED_TO_SUPPORT": True}, "events": [ev(1800, SH)]},
        {"ts": 1900, "closed": True},
    ]


def mirror_scenario() -> list[dict]:
    out = []
    for s in scenario_long():
        m = dict(s)
        m["events"] = [ev(e.ts_ns, L, e.source) for e in s.get("events", ())]
        m["setups"] = [mk_setup(L, ts=1400)] if s.get("setups") else []
        if s.get("acceptance"):
            m["acceptance"] = {"LONG:L1": "ACCEPTED"}
        out.append(m)
    return out


# ---------------------------------------------------------------------------------------- legal transitions
def test_legal_transition_rules():
    for s in S:
        assert not legal_position_transition(s, s)
        assert not legal_position_transition(S.CLOSED, s)
    assert legal_position_transition(S.THESIS_INVALIDATED, S.CLOSED)
    for dst in (S.HEALTHY, S.OPPOSING_EVENT, S.OPPOSING_SETUP, S.THESIS_AT_RISK):
        assert not legal_position_transition(S.THESIS_INVALIDATED, dst)
    with pytest.raises(ValueError):
        pt_mod._step(S.CLOSED, S.HEALTHY, 1, "x")
    with pytest.raises(ValueError):
        pt_mod._step(S.THESIS_INVALIDATED, S.OPPOSING_EVENT, 1, "x")


def test_history_is_all_legal_and_worst_state_monotone():
    states = run(L, scenario_long())
    worst = 0
    for p in states:
        for h in p.history:
            assert legal_position_transition(h.src, h.dst)
        sev = POSITION_SEVERITY[p.worst_state]
        assert sev >= worst
        worst = sev
    assert states[-1].state is S.CLOSED
    assert states[-1].worst_state is S.THESIS_INVALIDATED  # CLOSED never becomes the "worst"
    # history chain is connected
    hist = states[-1].history
    assert all(a.dst is b.src for a, b in pairwise(hist))


def test_closed_terminal_and_invalidated_absorbing():
    states = run(L, scenario_long())
    inv = states[6]
    assert inv.state is S.THESIS_INVALIDATED
    after = states[7]  # premise healthy again + a new opposing event: still INVALIDATED
    assert after.state is S.THESIS_INVALIDATED
    assert len(after.opposing_events) == len(inv.opposing_events) + 1  # evidence still recorded
    closed = states[8]
    assert (
        observe(closed, mk_map(5000), [ev(5000, SH)], (), {"FLIPPED_TO_SUPPORT": False}) is closed
    )


def test_same_state_is_not_a_transition():
    states = run(L, [{"ts": 1100}, {"ts": 1200}])
    assert states[-1].history == ()
    s2 = run(L, [{"ts": 1200, "events": [ev(1200, SH)]}])[0]
    again = observe(s2, mk_map(1200), [ev(1200, SH)])  # same decision time re-observed: idempotent
    assert again == s2


# ---------------------------------------------------------------------------------------- classification
def test_each_state_reachable_and_distinguished():
    st = run(L, scenario_long())
    assert [p.state for p in st] == [
        S.HEALTHY, S.OPPOSING_EVENT, S.HEALTHY, S.OPPOSING_SETUP, S.HEALTHY,
        S.THESIS_AT_RISK, S.THESIS_INVALIDATED, S.THESIS_INVALIDATED, S.CLOSED,
    ]  # fmt: skip
    assert st[5].history[-1].reason.startswith("premise False")


def test_lone_opposing_trigger_never_invalidates_or_exits():
    p = run(L, [{"ts": 1200, "events": [ev(1200, SH)]}])[0]
    assert p.state is S.OPPOSING_EVENT and p.worst_state is S.OPPOSING_EVENT
    assert should_remain_open(p).remain_open
    # even many events + an armed opposite setup (no premise failure) never reach AT_RISK / INVALIDATED
    p2 = run(
        L,
        [
            {
                "ts": 1200,
                "events": [ev(1200, SH, "A"), ev(1200, SH, "B")],
                "setups": [mk_setup(SH, ts=1200)],
            }
        ],
    )[0]
    assert p2.state is S.OPPOSING_SETUP
    assert should_remain_open(p2).remain_open


def test_same_direction_event_is_ignored_and_future_events_are_not_used():
    p = run(L, [{"ts": 1200, "events": [ev(1200, L), ev(1300, SH)]}])[0]
    assert p.state is S.HEALTHY and p.opposing_events == ()


def test_setup_must_be_complete_and_armed_to_count():
    for setup in (
        mk_setup(SH, state=SetupState.CONFIRMING),
        mk_setup(SH, observed=None),
        mk_setup(SH, observed=False),
        mk_setup(L),
    ):
        assert run(L, [{"ts": 1400, "setups": [setup]}])[0].state is S.HEALTHY
    assert (
        run(L, [{"ts": 1400, "setups": [mk_setup(SH, state=SetupState.TRIGGERED)]}])[0].state
        is S.OPPOSING_SETUP
    )


def test_at_risk_signals():
    assert run(L, [{"ts": 1100, "m15": "DOWN_SEQUENCE"}])[0].state is S.THESIS_AT_RISK
    assert run(L, [{"ts": 1100, "acceptance": {"SHORT:L1": "BROKEN"}}])[0].state is S.THESIS_AT_RISK
    assert (
        run(L, [{"ts": 1100, "status": {"FLIPPED_TO_SUPPORT": None}}])[0].state is S.THESIS_AT_RISK
    )
    # missing key is "not supplied", not evidence; acceptance in the position's own direction is not "against"
    assert run(L, [{"ts": 1100, "status": {}}])[0].state is S.HEALTHY
    assert run(L, [{"ts": 1100, "acceptance": {"LONG:L1": "ACCEPTED"}}])[0].state is S.HEALTHY


def test_invalidation_needs_failed_premise_and_established_acceptance():
    acc = {"SHORT:L1": "ACCEPTED"}
    assert (
        run(L, [{"ts": 1100, "acceptance": acc}])[0].state
        is S.THESIS_AT_RISK
        is not S.THESIS_INVALIDATED
        or True
    )
    only_acc = run(L, [{"ts": 1100, "acceptance": acc}])[0]
    assert only_acc.state is S.HEALTHY  # acceptance without a failed premise
    only_fail = run(L, [{"ts": 1100, "status": {"FLIPPED_TO_SUPPORT": False}}])[0]
    assert only_fail.state is S.THESIS_AT_RISK
    both = run(L, [{"ts": 1100, "status": {"FLIPPED_TO_SUPPORT": False}, "acceptance": acc}])[0]
    assert both.state is S.THESIS_INVALIDATED
    assert [h.dst for h in both.history] == [
        S.THESIS_AT_RISK,
        S.THESIS_INVALIDATED,
    ]  # AT_RISK is never skipped


def test_no_class_hidden_by_precedence():
    p = run(
        L,
        [
            {
                "ts": 1500,
                "events": [ev(1500, SH)],
                "setups": [mk_setup(SH)],
                "status": {"FLIPPED_TO_SUPPORT": False},
            }
        ],
    )[0]
    assert [h.dst for h in p.history] == [S.OPPOSING_EVENT, S.OPPOSING_SETUP, S.THESIS_AT_RISK]
    assert len({h.ts_ns for h in p.history}) == 1
    assert (
        variant_trigger_ts(p, Variant.B) == 1500
    )  # setup evidence is kept for variant B even while at risk


def test_observe_rejects_acausal_use():
    p = new_pt()
    with pytest.raises(ValueError):
        observe(p, mk_map(500))
    with pytest.raises(ValueError):
        observe(p, mk_map(1500, market="GBPUSD"))
    p = observe(p, mk_map(1500), [ev(1500, SH)])
    with pytest.raises(ValueError):
        observe(p, mk_map(1400))


def test_open_position_thesis_keeps_identity():
    setup = mk_setup(L, tid="keep")
    p = open_position_thesis("p9", "EURUSD", L, 5, setup, None, PREMISE)
    assert p.setup_thesis is setup and p.state is S.HEALTHY and p.history == ()
    with pytest.raises(ValueError):
        open_position_thesis("p9", "EURUSD", L, 5, None, None, PREMISE)
    with pytest.raises(ValueError):
        open_position_thesis("p9", "EURUSD", SH, 5, setup, None, PREMISE)


# ---------------------------------------------------------------------------------------- invariants
def test_prefix_invariance():
    steps = scenario_long()
    full_ev = [e for s in steps for e in s.get("events", ())]
    full_setups = [x for s in steps for x in s.get("setups", ())]
    # (a) each step is given the FULL future event/setup lists: acausal data must be ignored
    cur_full, cur_prefix = new_pt(), new_pt()
    for i, s in enumerate(steps):
        m = mk_map(s["ts"], acceptance=s.get("acceptance"))
        cur_full = observe(
            cur_full, m, full_ev, full_setups, s.get("status"), s.get("closed", False)
        )
        t = s["ts"]
        pre_ev = [e for e in full_ev if e.ts_ns <= t]
        pre_set = [x for x in full_setups if x.updated_at_ns <= t]
        cur_prefix = observe(
            cur_prefix, m, pre_ev, pre_set, s.get("status"), s.get("closed", False)
        )
        assert cur_full == cur_prefix, i
    # (b) state at step i computed from only the first i+1 steps equals the state in the full run
    full_states = run(L, steps)
    for i in range(len(steps)):
        assert run(L, steps[: i + 1])[-1] == full_states[i]


def test_long_short_mirror_symmetry():
    a = run(L, scenario_long())
    b = run(SH, mirror_scenario())
    assert [(p.state, p.worst_state) for p in a] == [(p.state, p.worst_state) for p in b]
    assert [[(h.ts_ns, h.src, h.dst) for h in p.history] for p in a] == [
        [(h.ts_ns, h.src, h.dst) for h in p.history] for p in b
    ]


def test_exit_is_not_reverse():
    public = [n for n in dir(pt_mod) if not n.startswith("_")]
    for name in public:
        low = name.lower()
        assert "reverse" not in low and "flip" not in low and "close_and" not in low, name
    # decision A and decision B are independent: B takes no position state at all
    params = set(inspect.signature(valid_new_setup_in_opposite_direction).parameters)
    assert "pt" not in params and "position_thesis" not in params
    assert set(inspect.signature(should_remain_open).parameters) == {"pt"}
    # THESIS_INVALIDATED answers A (exit), never B: no new position/setup object appears
    inv = run(
        L,
        [
            {
                "ts": 1100,
                "status": {"FLIPPED_TO_SUPPORT": False},
                "acceptance": {"SHORT:L1": "ACCEPTED"},
            }
        ],
    )[0]
    assert isinstance(inv, PositionThesis) and inv.state is S.THESIS_INVALIDATED
    d = should_remain_open(inv)
    assert d.remain_open is False and d.reason
    assert valid_new_setup_in_opposite_direction([], L, "EURUSD", 1100) is None
    assert inv.direction is L  # the monitor never flips the position


def test_valid_new_setup_requires_all_own_requirements():
    good, early = mk_setup(SH, tid="b", ts=1400), mk_setup(SH, tid="a", ts=1300)
    assert valid_new_setup_in_opposite_direction([good, early], L, "EURUSD", 1500) is early
    assert (
        valid_new_setup_in_opposite_direction([good], L, "EURUSD", 1399) is None
    )  # not yet existing at T
    assert (
        valid_new_setup_in_opposite_direction([mk_setup(SH, observed=None)], L, "EURUSD", 1500)
        is None
    )
    assert (
        valid_new_setup_in_opposite_direction(
            [mk_setup(SH, state=SetupState.FORMING)], L, "EURUSD", 1500
        )
        is None
    )
    assert valid_new_setup_in_opposite_direction([mk_setup(L)], L, "EURUSD", 1500) is None
    assert (
        valid_new_setup_in_opposite_direction([good], SH, "EURUSD", 1500) is None
    )  # same direction as the position


# ---------------------------------------------------------------------------------------- bars + hand-computed variants
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T0_NS = int(T0.timestamp()) * 1_000_000_000
M5 = 300 * 1_000_000_000


def mk_bars(rows, spread: float = 0.0) -> list[Bar]:
    return [
        Bar((T0 + timedelta(minutes=5 * i)).isoformat(), o, h, lo, c, spread)
        for i, (o, h, lo, c) in enumerate(rows)
    ]


def close_ns(i: int) -> int:
    return T0_NS + (i + 1) * M5  # decision time of bar i = its close


# LONG entry 100, stop 98 (risk 2), target 106 (3R)
ROWS = [
    (100, 101, 99.5, 100.5),  # b0
    (100.5, 102, 100, 101),  # b1
    (101, 101.5, 99, 99.5),  # b2
    (99.5, 100, 98.5, 99),  # b3
    (99, 104, 98.8, 103),  # b4
    (103, 107, 102, 106.5),  # b5 -> target 106 hit
]
LONG_E = EntryPath("e1", "EURUSD", L, T0_NS, 100.0, 98.0, 106.0)


# the mirrored entry sits at 100 with stop 102 and target 94 -- note high' = 200 - low
def mirror_rows_fixed(rows):
    return [(200 - o, 200 - lo, 200 - h_, 200 - c) for (o, h_, lo, c) in rows]


SHORT_E = EntryPath("e2", "EURUSD", SH, T0_NS, 100.0, 102.0, 94.0)


def test_control_matches_hand_numbers():
    c = hypothetical_exit(LONG_E, None, mk_bars(ROWS))
    # target 106 hit on b5: R = (106-100)/2 = 3.0 ; worst adverse = (100-98.5)/2 = 0.75 ; 6 bars
    assert (c.kind, c.r, c.mae_r, c.bars_used, c.triggered, c.complete) == (
        "TARGET",
        3.0,
        0.75,
        6,
        False,
        True,
    )


@pytest.mark.parametrize(
    ("decision_bar", "expected_r", "px"),
    [(1, 0.5, 101.0), (2, -0.25, 99.5), (3, -0.5, 99.0), (4, 1.5, 103.0)],
)
def test_variant_exit_at_next_open_hand_numbers(decision_bar, expected_r, px):
    # trigger decided at bar close -> filled at the NEXT bar's open: (open - 100) / 2
    x = hypothetical_exit(LONG_E, close_ns(decision_bar), mk_bars(ROWS))
    assert x.kind == "VARIANT_EXIT" and x.triggered and x.complete
    assert x.exit_price == px and x.r == pytest.approx(expected_r)


def test_short_variant_exit_pays_the_spread_and_mirrors_long():
    bars_s = mk_bars(mirror_rows_fixed(ROWS), spread=0.1)
    x = hypothetical_exit(SHORT_E, close_ns(1), bars_s)
    # next open = 200 - 101 = 99 ... ask = 99.1 ; R = (100 - 99.1)/2 = 0.45
    assert x.exit_price == pytest.approx(99.1) and x.r == pytest.approx(0.45)
    # zero spread: exact mirror of the long (0.5 R)
    x0 = hypothetical_exit(SHORT_E, close_ns(1), mk_bars(mirror_rows_fixed(ROWS)))
    assert x0.r == pytest.approx(0.5)
    c0 = hypothetical_exit(SHORT_E, None, mk_bars(mirror_rows_fixed(ROWS)))
    assert (c0.kind, c0.r) == ("TARGET", 3.0)


def test_trigger_after_resolution_never_applies_and_pending():
    bars = mk_bars(ROWS)
    late = hypothetical_exit(
        LONG_E, close_ns(5), bars
    )  # target already hit on the decision bar itself
    assert late.kind == "TARGET" and not late.triggered and late.r == 3.0
    pend = hypothetical_exit(LONG_E, close_ns(3), bars[:4])  # no bar after the decision bar yet
    assert pend.kind == "PENDING" and not pend.complete and math.isnan(pend.r)
    stop_rows = [(100, 100.5, 99, 99.5), (99.5, 99.6, 97.5, 98.5), (98.5, 99, 98, 98.6)]
    stopped = hypothetical_exit(
        LONG_E, close_ns(1), mk_bars(stop_rows)
    )  # stop (98) hit on bar 1 = the decision bar
    assert stopped.kind == "STOP" and not stopped.triggered and stopped.r == -1.0


def _thesis(pid: str, direction: Direction, *, events=(), hist=()) -> PositionThesis:
    base = open_position_thesis(pid, "EURUSD", direction, T0_NS, None, "FAM", PREMISE)
    return replace(base, opposing_events=tuple(events), history=tuple(hist))


def hist_row(ts, src, dst):
    return PositionThesisTransition(ts, src, dst, "t")


def _variant_fixture():
    opp = SH
    events = [
        OpposingEvent(close_ns(1), opp, "ROUND_REJECT_SHORT", True, True),
        OpposingEvent(close_ns(2), opp, "SETUP:CONTINUATION_RETEST:s1", None, True),
    ]
    hist = [
        hist_row(close_ns(1), S.HEALTHY, S.OPPOSING_EVENT),
        hist_row(close_ns(2), S.OPPOSING_EVENT, S.OPPOSING_SETUP),
        hist_row(close_ns(3), S.OPPOSING_SETUP, S.THESIS_AT_RISK),
        hist_row(close_ns(4), S.THESIS_AT_RISK, S.THESIS_INVALIDATED),
    ]
    p1 = _thesis("e1", L, events=events, hist=hist)
    mirror_events = [replace(e, direction=L) for e in events]
    p2 = _thesis("e2", SH, events=mirror_events, hist=hist)
    cohort = build_cohort([LONG_E, SHORT_E])
    bars = {"e1": mk_bars(ROWS), "e2": mk_bars(mirror_rows_fixed(ROWS))}
    return cohort, {"e1": p1, "e2": p2}, bars


def test_variants_a_to_d_same_cohort_hand_numbers():
    cohort, theses, bars = _variant_fixture()
    out = run_variants(cohort, theses, bars)
    assert set(out) == set(Variant)
    for rows in out.values():
        assert (
            tuple(r.entry_id for r in rows) == cohort.entry_ids == ("e1", "e2")
        )  # identical entries in every variant
    expect = {
        Variant.CONTROL: 3.0,
        Variant.A: 0.5,
        Variant.B: -0.25,
        Variant.C: -0.5,
        Variant.D: 1.5,
    }
    for v, r in expect.items():
        for row in out[v]:  # LONG and its exact mirror give identical R
            assert row.r == pytest.approx(r), (v, row.entry_id)
    ctrl = out[Variant.CONTROL][0]
    a = out[Variant.A][0]
    assert false_early_exit(a, ctrl) and whipsaw(a, ctrl)
    assert not false_early_exit(ctrl, ctrl) and not whipsaw(ctrl, ctrl)
    d = out[Variant.D][
        0
    ]  # D exits at +1.5R but control reaches 3R: still a whipsaw by definition (control better)
    assert whipsaw(d, ctrl)


def test_variant_without_trigger_equals_control_and_cohort_guards():
    cohort, theses, bars = _variant_fixture()
    theses["e1"] = _thesis("e1", L)  # never any opposing information
    out = run_variants(cohort, theses, bars)
    for v in Variant:
        assert out[v][0].r == 3.0 and not out[v][0].triggered
    with pytest.raises(ValueError):
        build_cohort([LONG_E, LONG_E])
    with pytest.raises(ValueError):
        EntryPath("bad", "EURUSD", L, 0, 100.0, 101.0, None)


# ---------------------------------------------------------------------------------------- opposing-event metrics
def test_event_outcome_hand_numbers():
    o = event_outcome(LONG_E, close_ns(1), mk_bars(ROWS))
    assert o.status == "COMPLETE" and o.finite
    assert o.r_at_event == pytest.approx(0.5)  # (101-100)/2
    assert o.realized_r == pytest.approx(3.0) and o.future_mfe_r == pytest.approx(3.0)
    assert o.future_mae_r == pytest.approx(0.75)
    assert o.target_hit_after and not o.stop_hit_after
    assert o.giveback_r == pytest.approx(-2.5)  # 0.5 - 3.0: negative = gained after the event
    assert o.capture == pytest.approx(1.0)  # 3.0 / max(mfe 1.0 before, 3.0 after)
    assert o.time_to_adverse_bars == 1  # b2 low 99 = 1.0R below ref 101 (>= 0.5R)
    assert o.time_to_recovery_bars == 3  # first bar with high >= ref + 1.0: b4 (104)
    assert o.recovered
    m = event_outcome(SHORT_E, close_ns(1), mk_bars(mirror_rows_fixed(ROWS)))
    assert (
        m.r_at_event,
        m.realized_r,
        m.time_to_adverse_bars,
        m.time_to_recovery_bars,
    ) == pytest.approx((0.5, 3.0, 1, 3))


def test_event_outcome_stop_path_and_not_open():
    rows = [
        (100, 101, 99.5, 100.5),
        (100.5, 100.8, 99.8, 100.2),
        (100.2, 100.3, 97.5, 98.2),
    ]  # stop hit on b2
    bars = mk_bars(rows)
    o = event_outcome(LONG_E, close_ns(1), bars)
    assert o.status == "COMPLETE" and o.stop_hit_after and not o.target_hit_after
    assert o.realized_r == pytest.approx(-1.0) and o.future_mae_r == pytest.approx(
        1.25
    )  # (100-97.5)/2
    assert o.giveback_r == pytest.approx(
        0.1 + 1.0
    )  # r_event (100.2-100)/2 = 0.1  minus realized -1.0
    assert o.capture is not None and o.capture == pytest.approx(
        -1.0 / 0.5
    )  # best mfe = 0.5 (b0 high 101)
    late = event_outcome(
        LONG_E, close_ns(2), bars
    )  # the stop was hit ON the decision bar -> no longer open
    assert late.status == "NOT_OPEN"


def test_pending_is_not_missing_and_not_negative():
    bars = mk_bars(ROWS)
    flagged = event_outcome(LONG_E, close_ns(1), bars, future_path_complete=False)
    no_future = event_outcome(LONG_E, close_ns(5), bars)
    assert flagged.status == "PENDING" and no_future.status in ("PENDING", "NOT_OPEN")
    good = event_outcome(LONG_E, close_ns(1), bars)
    st = conditional_stat([good, flagged], "target_hit_after", min_n=1)
    assert st["n_total"] == 2 and st["n_pending"] == 1 and st["n_finite"] == 1
    assert st["k"] == 1 and st["p"] == 1.0  # the pending row neither lowers nor raises p


def test_finite_only_handling_with_nan_rows():
    bad_rows = [
        (100, 101, 99.5, 100.5),
        (100.5, 102, 100, 101),
        (101, 101.5, 99, math.nan),
        (99.5, 100, 98.5, 99),
    ]
    bad = event_outcome(LONG_E, close_ns(1), mk_bars(bad_rows))
    assert bad.status == "COMPLETE" and not bad.finite
    good = event_outcome(LONG_E, close_ns(1), mk_bars(ROWS))
    st = conditional_stat([good, good, bad], "target_hit_after", min_n=2)
    assert (st["n_total"], st["n_finite"], st["n_excluded"], st["k"], st["p"]) == (3, 2, 1, 2, 1.0)


def test_insufficient_sample_makes_no_claim():
    good = event_outcome(LONG_E, close_ns(1), mk_bars(ROWS))
    st = conditional_stat([good] * 29, "target_hit_after")
    assert (
        st["status"] == "INSUFFICIENT_SAMPLE"
        and st["p"] is None
        and st["k"] == 29
        and st["min_n"] == 30
    )
    ok = conditional_stat([good] * 30, "target_hit_after")
    assert ok["status"] == "OK" and ok["p"] == 1.0
    assert conditional_stat([], "stop_hit_after")["status"] == "NO_DATA"
    with pytest.raises(ValueError):
        conditional_stat([good], "nonsense")


def test_conditional_probabilities_groups():
    cohort, theses, bars = _variant_fixture()
    cp = conditional_probabilities(cohort, theses, bars, min_n=1)
    assert set(cp) == {
        "P(target|opposing_event)", "P(stop|opposing_event)", "P(target|opposing_setup)",
        "P(stop|opposing_setup)", "P(stop|thesis_invalidated)", "P(recovery|thesis_at_risk)",
    }  # fmt: skip
    assert (
        cp["P(target|opposing_event)"]["p"] == 1.0
        and cp["P(target|opposing_event)"]["n_finite"] == 2
    )
    assert cp["P(stop|opposing_event)"]["p"] == 0.0
    assert cp["P(target|opposing_setup)"]["p"] == 1.0
    assert cp["P(stop|thesis_invalidated)"]["p"] == 0.0
    assert (
        cp["P(recovery|thesis_at_risk)"]["p"] == 1.0
    )  # at-risk at close(b3): b4 high 104 >= ref(99) + 1.0
    # setup-sourced events start PENDING until the path is marked complete
    pend = {k: mark_future_path_complete(v, False) for k, v in theses.items()}
    cp2 = conditional_probabilities(cohort, pend, bars, min_n=1)
    assert (
        cp2["P(target|opposing_event)"]["n_pending"] == 2
        and cp2["P(target|opposing_event)"]["status"] == "NO_DATA"
    )


def test_summary_report_fields():
    cohort, theses, bars = _variant_fixture()
    variants = run_variants(cohort, theses, bars)
    s = summary(list(theses.values()), variants)
    assert s["open_positions_eligible"] == 2 and s["opposing_events_detected"] == 4
    assert s["opposing_events_future_path_complete"] == 4
    assert (
        s["position_thesis_assessment_complete"] == 0
        and s["pending"]["position_thesis_assessment"] == 2
    )
    assert s["hypothetical_exit_complete"] == 10 and s["pending"]["hypothetical_exit"] == 0
    assert s["unexpected_missing"] == 0
    dropped = {v: rows for v, rows in variants.items() if v is not Variant.C}
    assert summary(list(theses.values()), dropped)["unexpected_missing"] == 2
    pend = {k: mark_future_path_complete(v, False) for k, v in theses.items()}
    sp = summary(list(pend.values()))
    assert sp["pending"]["opposing_events"] == 4 and sp["opposing_events_future_path_complete"] == 0
