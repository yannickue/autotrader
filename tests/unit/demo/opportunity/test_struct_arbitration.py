# ruff: noqa: E501
"""Lane S: all four STRUCT variants are ACTIVE_DISCOVERY_ELIGIBLE; deterministic same-cycle arbitration (no name priority);
CONCURRENT_SIGNAL / ADD_ON_CANDIDATE / REVERSAL_CANDIDATE classification; structure_event_id attribution; funnel clusters."""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from demo import funnel as fn
from demo.opportunity import production_spec as ps
from demo.opportunity.arbitration import (
    ADD_ON_CANDIDATE,
    CONCURRENT_SIGNAL,
    REVERSAL_CANDIDATE,
    classify_stack_code,
    concurrent_reasons,
    event_attribution,
    pick_winner,
    structure_event_id_of,
)
from demo.opportunity.bar_source import ReplayBarSource
from demo.opportunity.engine import OpportunityEngine
from tests.unit.demo.opportunity.test_opp_spec_v1_2 import PHASE2, _frame

VARIANTS = ("breakout", "confirmed", "retest", "fade")
TAIL = [102.6, 101.2, 100.5]  # break @12:00, confirmed+retest decide on the 12:05 bar, fade on the 12:10 bar
REPO = Path(__file__).resolve().parents[4]


def _engine_run(market: str, tail: list[float], only: str | None = None):
    from markets.spec import load_market_spec

    day = datetime(2026, 9, 24, tzinfo=UTC)
    brk = 12 * 12
    frame = _frame(day, brk, tail)
    spec = load_market_spec(market)
    src = ReplayBarSource({market: frame}, {market: spec.point_size})
    prod = ps.load_production_spec(ps.DEFAULT_PATH_V1_2)
    if only is not None:
        sel = tuple(fs for fs in prod.specs_for(market) if fs.spec.mode == only)
        prod = dataclasses.replace(prod, markets=((market, sel),))
    eng = OpportunityEngine(src, production=prod, min_history_bars=100, commit="test")
    out = []
    for k in range(len(tail)):
        now = day + timedelta(minutes=5 * (brk + k + 1))
        src.set_time(now)
        pairs = eng.on_m5_close(market, now)
        out.append((now, pairs, list(eng.last_intents)))
    return out


def _flat(runs):
    return [(s, d) for _n, pairs, _i in runs for s, d in pairs]


# ---------------------------------------------------------------- every variant may trade on its own when flat
@pytest.mark.parametrize("market", PHASE2)
@pytest.mark.parametrize("variant", VARIANTS)
def test_each_variant_alone_may_create_a_real_demo_intent_when_flat(market, variant):
    runs = _engine_run(market, TAIL, only=variant)
    mine = [(s, d) for s, d in _flat(runs) if s.signal["variant"] == variant]
    assert mine, f"{variant} must fire on the planted break"
    snap, dec = mine[0]
    assert dec.accepted is True and dec.reasons == ("ACCEPTED",)
    assert snap.signal["role"] == "ACTIVE_DISCOVERY_ELIGIBLE"
    assert snap.signal["phase"] == "PHASE2_DISCOVERY" and snap.signal["alpha_status"] == "NOT_ALPHA_VALIDATED"
    assert any(i.opportunity_id == snap.opportunity_id for _n, _p, ints in runs for i in ints)  # the intent exists


# ---------------------------------------------------------------- same-cycle arbitration
@pytest.mark.parametrize("market", PHASE2)
def test_same_cycle_contenders_one_intent_loser_persisted_as_concurrent_signal(market):
    runs = _engine_run(market, TAIL)
    cyc = next(r for r in runs if len(r[1]) >= 2)
    _now, cycle, cycle_intents = cyc
    assert {s.signal["variant"] for s, _ in cycle} >= {"confirmed", "retest"}
    accepted = [(s, d) for s, d in cycle if d.accepted]
    assert len(accepted) == 1, "exactly one variant may take the symbol in a cycle"
    losers = [(s, d) for s, d in cycle if not d.accepted]
    assert losers
    wsnap = accepted[0][0]
    for s, d in losers:
        assert d.reasons == (CONCURRENT_SIGNAL, ADD_ON_CANDIDATE)  # same direction as the winner
        arb = s.signal["arbitration"]
        assert arb["result"] == "LOSER" and arb["winner_variant"] == wsnap.signal["variant"]
        assert arb["winner_opportunity_id"] == wsnap.opportunity_id
    assert wsnap.signal["arbitration"]["result"] == "WINNER"
    assert [i.opportunity_id for i in cycle_intents] == [wsnap.opportunity_id]  # no second intent, ever


def test_arbitration_is_deterministic_across_runs():
    a = [(s.opportunity_id, d.accepted, d.reasons) for s, d in _flat(_engine_run("BTCUSD", TAIL))]
    b = [(s.opportunity_id, d.accepted, d.reasons) for s, d in _flat(_engine_run("BTCUSD", TAIL))]
    assert a == b


def test_tie_break_is_earliest_timestamp_then_hash_never_a_fixed_variant_order():
    ts = "2026-09-24T12:05:00+00:00"
    # 1) the earliest signal timestamp wins regardless of names / hash
    items = [("2026-09-24T12:10:00+00:00", "e1", "breakout"), ("2026-09-24T12:05:00+00:00", "e1", "fade")]
    assert pick_winner(items) == 1
    # 2) equal timestamps: independent of input order, and both names win for some structure events (no name priority)
    wins = {"confirmed": 0, "retest": 0}
    for n in range(400):
        eid = structure_event_id_of("BTCUSD", 100.0 + n, 99.0, ts)
        pair = [(ts, eid, "confirmed"), (ts, eid, "retest")]
        w1 = pair[pick_winner(pair)][2]
        assert w1 == pair[::-1][pick_winner(pair[::-1])][2]
        wins[w1] += 1
        four = [(ts, eid, v) for v in VARIANTS]
        assert four[pick_winner(four)][2] == four[::-1][pick_winner(four[::-1])][2]
    assert wins["confirmed"] > 100 and wins["retest"] > 100  # neither is systematically preferred
    # 3) varying the name inputs flips the winner for the same event
    got = {pick_winner([(ts, "ev", a), (ts, "ev", b)]) for a, b in (("zzz", "aaa"), ("aaa", "zzz"))}
    assert got == {0, 1}


def test_concurrent_classification_codes():
    assert concurrent_reasons(1, 1) == (CONCURRENT_SIGNAL, ADD_ON_CANDIDATE)
    assert concurrent_reasons(-1, -1) == (CONCURRENT_SIGNAL, ADD_ON_CANDIDATE)
    assert concurrent_reasons(-1, 1) == (REVERSAL_CANDIDATE,) and concurrent_reasons(1, -1) == (REVERSAL_CANDIDATE,)
    assert classify_stack_code("ADDON_EXPOSURE_NOT_SUPPORTED_V1") == (CONCURRENT_SIGNAL, ADD_ON_CANDIDATE)
    assert classify_stack_code("ADDON_SHARED_STOP_POSSIBLE_NOT_YET_IMPLEMENTED") == (CONCURRENT_SIGNAL, ADD_ON_CANDIDATE)
    assert classify_stack_code("OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1") == (REVERSAL_CANDIDATE,)
    assert classify_stack_code("position_exists") == () and classify_stack_code(None) == ()


# ---------------------------------------------------------------- structure event attribution
def test_structure_event_id_is_deterministic_and_shared_by_variants_of_one_break():
    pairs = _flat(_engine_run("BTCUSD", TAIL))
    by_variant = {}
    for s, _d in pairs:
        by_variant.setdefault(s.signal["variant"], s.signal)
    assert set(by_variant) == set(VARIANTS)
    ids = {sig["structure_event_id"] for sig in by_variant.values()}
    assert len(ids) == 1, "all variants react to the ONE break"
    assert {s.signal["structure_event_id"] for s, _ in _flat(_engine_run("BTCUSD", TAIL))} == ids
    sig = by_variant["fade"]
    assert sig["direction"] in (1, -1) and sig["signal_timestamp"] and isinstance(sig["price"], float)
    pr = sig["parent_range"]
    assert pr["range_high"] > pr["range_low"] and pr["bars"] == 24 and pr["width_atr"] > 0 and pr["break_bar_ts"]
    assert sig["signal_timestamp"] != pr["break_bar_ts"]
    eid = structure_event_id_of("BTCUSD", pr["range_high"], pr["range_low"], pr["break_bar_ts"])
    assert eid == next(iter(ids))
    assert structure_event_id_of("BRENT", pr["range_high"], pr["range_low"], pr["break_bar_ts"]) != eid
    assert structure_event_id_of("BTCUSD", pr["range_high"] + 1, pr["range_low"], pr["break_bar_ts"]) != eid
    assert event_attribution("BTCUSD", "fade", "t", 1, 1.0, {}) == {}


# ---------------------------------------------------------------- funnel: classifications + clustered counts
def _row(i, *, eid, variant, accepted, reasons=("ACCEPTED",), state=None, code=None, market="BTCUSD"):
    return {
        "opportunity_id": f"o{i}", "market": market, "family": "STRUCT", "accepted": accepted, "reasons": list(reasons),
        "intent_id": None, "state": state, "stack_reject_code": code, "stack_gate_class": None, "otherwise_valid": None,
        "approved": None, "shadow_dry_run": False, "has_outcome": False, "signal_ts": "2026-09-24T12:05:00+00:00",
        "direction": 1, "stop": 99.0, "atr": 1.0, "local_minute": 0, "session": None, "origin": "LIVE", "violated_cap": None,
        "cancel_reason": None, "trade_type": "STRATEGY", "structure_event_id": eid, "variant": variant,
    }


def test_funnel_counts_classifications_and_event_level_clusters():
    rows = [
        _row(1, eid="E1", variant="breakout", accepted=True, state="CLOSED"),
        _row(2, eid="E1", variant="confirmed", accepted=True, state="RISK_REJECTED", code="ADDON_EXPOSURE_NOT_SUPPORTED_V1"),
        _row(3, eid="E1", variant="retest", accepted=False, reasons=(CONCURRENT_SIGNAL, ADD_ON_CANDIDATE)),
        _row(4, eid="E1", variant="fade", accepted=True, state="RISK_REJECTED", code="OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1"),
        _row(5, eid="E2", variant="confirmed", accepted=False, reasons=(REVERSAL_CANDIDATE,)),
        _row(6, eid=None, variant=None, accepted=True, state="CLOSED"),
    ]
    a = fn.analysis(rows, [])
    assert a["arbitration"] == {CONCURRENT_SIGNAL: 2, ADD_ON_CANDIDATE: 2, REVERSAL_CANDIDATE: 2}
    se = a["structure_events"]
    assert se["raw_signals"] == 5 and se["structure_events"] == 2 and se["events_with_multiple_variants"] == 1
    assert se["events_with_a_trade"] == 1 and se["unattributed_signals"] == 1
    assert se["raw_by_variant"]["confirmed"] == 2 and se["events_by_variant"]["confirmed"] == 2
    assert "highly related" in se["note"]
    assert a["special_buckets"]["ADDON_NOT_SUPPORTED"] == 1 and a["special_buckets"]["OPPOSITE_NOT_SUPPORTED"] == 1  # untouched
    assert fn.engine_class(CONCURRENT_SIGNAL) == "TEMPORARY"
    assert "arbitration" in fn.compact({"analysis": a})


# ---------------------------------------------------------------- wording: no variant is described as winning / primary
def test_no_confirmed_is_winner_wording_anywhere():
    files = [REPO / "docs/V2_MARKETS.md", REPO / "docs/OPEN_QUESTIONS.md", REPO / "src/demo/opportunity/production_spec.py",
             REPO / "src/demo/opportunity/production_spec_v1_2.json", REPO / "src/demo/opportunity/arbitration.py",
             REPO / "src/alpha/families/structbrk.py"]
    bad = re.compile(
        r"confirmed`?\s*(=|is|as)\s*(the\s+)?(PRIMARY|winner|best|primary alpha)|primary variant\s*`?confirmed|"
        r"`?confirmed`?\s+(wins|won|is validated|is the best)|best variant", re.I)
    for f in files:
        text = f.read_text(encoding="utf-8")
        for m in bad.finditer(text):
            ctx = text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")
            pytest.fail(f"{f.name}: forbidden wording {m.group(0)!r} in ...{ctx}...")


def test_spec_amendment_is_recorded_and_all_variants_share_one_role():
    payload = json.loads(ps.DEFAULT_PATH_V1_2.read_text(encoding="utf-8"))
    assert payload["amendments"] and "Lane S" in payload["amendments"][0]
    for m in PHASE2:
        assert {e["role"] for e in payload["markets"][m]} == {"ACTIVE_DISCOVERY_ELIGIBLE"}
        assert all(e["tags"]["alpha_status"] == "NOT_ALPHA_VALIDATED" for e in payload["markets"][m])
    assert {"ACTIVE_DISCOVERY_ELIGIBLE", "PRIMARY", "SHADOW"} <= set(ps.ROLES)  # old mechanism stays available
