# ruff: noqa: E501
"""Confluence data contract: SignalRecord/SignalTable construction and the visibility invariant."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from alpha.events import schema as ev
from alpha.temporal.confluence import (
    SignalTable,
    make_signal_id,
    records_from_reference,
    visible_at,
)
from alpha.temporal.reference import MarketFrame, evaluate_reference
from alpha.temporal.spec import (
    Capture,
    Clause,
    StateMachineStrategySpec,
    StopRule,
    TargetRule,
    Transition,
)

ZONE = Clause("event", "ZONE_ENTER", "M15", variant="swing_cluster")
SWEEP = Clause("event", "SWEEP_LOW", "M5", variant="swing_low")
SPEC = StateMachineStrategySpec(
    strategy_id="conf", version=1, direction="LONG", anchor=(ZONE,),
    anchor_capture=(Capture("R0", "lv", "ZONE_LO"),),
    states=(Transition(SWEEP, within=8, capture=(Capture("R1", "evx", "SWEEP_LOW"),)),),
    context=(), expires_after=10, session_window=None,
    stop=StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0),
    target=TargetRule("fixed_r", r=2.0),
)


def market(n=30) -> MarketFrame:
    c = np.full(n, 99.0)
    zone = np.zeros(n, dtype=np.uint8)
    zone[[2, 12]] = 1
    sw = np.zeros(n, dtype=np.uint8)
    sw[[4, 14]] = 1
    evx = np.full(n, np.nan)
    evx[[4, 14]] = 98.0
    names = ev.array_names("SWEEP_LOW", "M5", "swing_low")
    arrays = {
        ev.array_names("ZONE_ENTER", "M15", "swing_cluster")[0]: zone,
        ev.array_names("ZONE_ENTER", "M15", "swing_cluster")[1]: np.full(n, 98.5),
        ev.array_names("ZONE_ENTER", "M15", "swing_cluster")[2]: np.full(n, 99.5),
        names[0]: sw,
        next(x for x in names if x.startswith("evx_")): evx,
    }
    return MarketFrame(c, c + 0.3, c - 0.3, c, np.ones(n), np.zeros(n, dtype=np.int64),
                       np.full(n, 600, dtype=np.int64), arrays, {},
                       ts_close_ns=np.arange(n, dtype=np.int64) * 300_000_000_000)


def build():
    m = market()
    r = evaluate_reference(SPEC, m)
    return m, r, records_from_reference(r.candidates, r.trails, SPEC, m, family="zone", lineage_signature="L", feature_set_signature="F")


def test_records_contract_fields_and_id():
    m, _r, recs = build()
    assert [x.decision_idx for x in recs] == [4, 14]
    x = recs[0]
    assert x.signal_id == hashlib.sha256(f"{SPEC.spec_hash()}:4".encode()).hexdigest()[:16] == make_signal_id(SPEC.spec_hash(), 4)
    assert x.signal_id != recs[1].signal_id and len(x.signal_id) == 16
    assert (x.strategy_id, x.family, x.lineage_signature, x.feature_set_signature) == ("conf", "zone", "L", "F")
    assert x.direction == 1 and x.ts_close_ns == int(m.ts_close_ns[4])
    assert (x.entry_zone_lo, x.entry_zone_hi) == (98.5, 99.5)
    assert x.anchor_idx == 2 and x.step_idx == (2, 4)
    assert x.stop == pytest.approx(97.9) and np.isnan(x.target) and x.target_r == 2.0
    assert x.registers[:2] == (98.5, 98.0) and x.event_ids == ()


def test_table_columns_and_visibility_invariant():
    _, _, recs = build()
    t = SignalTable.from_records(recs)
    assert len(t) == 2
    assert t.columns["step_idx"].shape == (2, 6) and t.columns["registers"].shape == (2, 4)
    assert t.columns["decision_idx"].dtype == np.int64
    for u, want in ((3, []), (4, [4]), (13, [4]), (14, [4, 14]), (99, [4, 14])):
        assert visible_at(t, u).columns["decision_idx"].tolist() == want
    assert len(visible_at(t, 0)) == 0  # empty table stays well-formed
    assert set(visible_at(t, 0).columns) == set(t.columns)


def test_empty_records_and_mismatch_rejected():
    assert len(SignalTable.from_records(())) == 0
    m, r, _ = build()
    with pytest.raises(ValueError):
        records_from_reference(r.candidates, r.trails[:1], SPEC, m)
