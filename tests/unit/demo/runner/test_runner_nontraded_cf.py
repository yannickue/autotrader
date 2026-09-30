# ruff: noqa: E501
"""Counterfactual labels for EVERY non-traded opportunity, tagged with the gate that blocked it."""

from __future__ import annotations

import pytest

from demo.contracts import Decision
from demo.execution.events import Rejected
from demo.store import DemoStoreError
from demo.testing import M5, T0, make_pair


def _label(r, env, minutes=70):
    env.clock.advance(minutes=minutes)
    return r.label_now(env.clock())


def _meta(store, oid):
    rows = {x["opportunity_id"]: x for x in store.counterfactual_rows()}
    return rows[oid]


def test_stack_rejected_accepted_opportunity_is_labelled_with_the_stack_gate(env):
    snap, dec, intent = make_pair(horizon_s=3600)
    env.stack.script[intent.intent_id] = [
        Rejected(intent.intent_id, "size_below_min", {"decision": "SKIP", "reject_code": "size_below_min", "gate_reject_class": "LEGACY_ARBITRARY"})
    ]
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    assert env.store.get_state(intent.intent_id) == "RISK_REJECTED"
    assert env.store.get_counterfactual(snap.opportunity_id) is None  # horizon not elapsed
    assert _label(r, env) == 1
    row = _meta(env.store, snap.opportunity_id)
    assert row["source"] == "STACK_REJECTED" and row["gate_code"] == "size_below_min"
    assert row["gate_class"] == "LEGACY_ARBITRARY" and row["gate_codes"] == ["size_below_min"]


def test_halted_cancel_and_expired_cancel_are_labelled(env):
    snap, dec, intent = make_pair(horizon_s=1800, valid_s=1)  # expired at submit (valid 1 s after the signal)
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    assert env.store.get_state(intent.intent_id) == "CANCELLED"
    assert _label(r, env, 40) == 1
    row = _meta(env.store, snap.opportunity_id)
    assert row["source"] == "INTENT_CANCELLED" and row["gate_code"] == "CANCELLED:expired" and row["gate_class"] == "OPERATIONAL"


def test_shadow_dry_run_is_labelled_but_stays_distinct(env):
    env.stack.shadow = True
    snap, dec, intent = make_pair(horizon_s=1800)
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build(mode="shadow")
    r.start()
    r.run_cycle()
    assert _label(r, env, 40) == 1
    row = _meta(env.store, snap.opportunity_id)
    assert row["source"] == "SHADOW_DRY_RUN" and row["gate_code"] == "SHADOW_DRY_RUN"


def test_catchup_missed_is_labelled_with_catchup_code(env):
    r = env.build()
    r.start()
    r.run_cycle()
    env.clock.advance(minutes=10)
    s, _, _ = make_pair(signal_ts=T0 + M5, tag="cu", horizon_s=1800)
    miss = Decision(opportunity_id=s.opportunity_id, phase=s.phase, decided_utc=env.clock().isoformat(), accepted=False,
                    reasons=("CATCHUP_MISSED", "EXPIRED_ENTRY", "ALREADY_MOVED"), policy_id="static-demo-policy-v1")
    env.engine.push_catchup("GER40", (s, miss, None))
    r.run_cycle()
    assert _label(r, env, 40) == 1
    row = _meta(env.store, s.opportunity_id)
    assert row["source"] == "CATCHUP_MISSED" and row["gate_code"] == "CATCHUP_MISSED"
    assert row["gate_codes"] == ["CATCHUP_MISSED", "EXPIRED_ENTRY", "ALREADY_MOVED"] and row["gate_class"] == "CATCHUP"


def test_engine_rejected_keeps_its_gate_codes_and_traded_is_never_labelled(env):
    rej, rdec, _ = make_pair(accepted=False, horizon_s=1800, tag="rej")
    rdec = Decision(opportunity_id=rdec.opportunity_id, phase=rdec.phase, decided_utc=rdec.decided_utc, accepted=False,
                    reasons=("SPREAD_TOO_WIDE", "ENTRY_OVERSHOT"), policy_id=rdec.policy_id)
    ok, dec, intent = make_pair(horizon_s=1800, tag="ok")
    env.engine.push("GER40", (rej, rdec, None), (ok, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    assert env.store.get_state(intent.intent_id) == "PROTECTED"
    assert _label(r, env, 40) == 1  # only the rejected one
    assert env.store.get_counterfactual(ok.opportunity_id) is None
    row = _meta(env.store, rej.opportunity_id)
    assert row["source"] == "ENGINE_REJECTED" and row["gate_code"] == "SPREAD_TOO_WIDE" and row["gate_class"] == "SAFETY"
    assert row["gate_codes"] == ["SPREAD_TOO_WIDE", "ENTRY_OVERSHOT"]
    # the store itself refuses to label a traded / in-flight intent
    from demo.contracts import CounterfactualLabel

    lab = CounterfactualLabel(opportunity_id=ok.opportunity_id, phase="DISCOVERY", horizon_end_utc="x", hypothetical_mfe_r=0.0,
                              hypothetical_mae_r=0.0, hypothetical_r=0.0, target_before_stop=None, labelled_utc="x")
    with pytest.raises(DemoStoreError):
        env.store.record_counterfactual(lab, source="STACK_REJECTED")
