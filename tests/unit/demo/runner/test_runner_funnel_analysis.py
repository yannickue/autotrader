# ruff: noqa: E501
"""Per-gate funnel: stages, special buckets, per-market/hour tables, counterfactual stats per gate,
near-duplicate structure indicator; compact form in the heartbeat."""

from __future__ import annotations

import dataclasses

from demo.contracts import Decision
from demo.execution.events import Rejected
from demo.funnel import STAGES, funnel, render
from demo.testing import M5, T0, make_pair


def _dec(pair, reasons):
    s, d, _ = pair
    return (s, Decision(opportunity_id=s.opportunity_id, phase=s.phase, decided_utc=d.decided_utc, accepted=False,
                        reasons=tuple(reasons), policy_id=d.policy_id), None)


def test_funnel_stages_gates_special_buckets_and_counterfactuals(env):
    p_spread = _dec(make_pair(accepted=False, tag="spr", horizon_s=1800), ("SPREAD_TOO_WIDE",))
    p_window = _dec(make_pair(accepted=False, tag="win", horizon_s=1800), ("OUTSIDE_ENTRY_WINDOW",))
    p_addon = make_pair(tag="add", horizon_s=1800)
    p_size = make_pair(tag="siz", horizon_s=1800)
    p_ok = make_pair(tag="ok", horizon_s=1800)
    env.stack.script[p_addon[2].intent_id] = [Rejected(p_addon[2].intent_id, "ADDON_EXPOSURE_NOT_SUPPORTED_V1",
                                                      {"decision": "SKIP", "gate_reject_class": "TEMPORARY", "otherwise_valid": True})]
    env.stack.script[p_size[2].intent_id] = [Rejected(p_size[2].intent_id, "size_below_min",
                                                     {"decision": "SKIP", "gate_reject_class": "SAFETY", "violated_cap": "max_cluster_stop_risk_fraction"})]
    env.engine.push("GER40", p_spread, p_window, p_addon, p_size, p_ok)
    r = env.build()
    r.start()
    r.run_cycle()
    # one catch-up miss
    env.clock.advance(minutes=10)
    s_m = make_pair(signal_ts=T0 + M5, tag="miss", horizon_s=1800)
    miss = (s_m[0], Decision(opportunity_id=s_m[0].opportunity_id, phase="DISCOVERY", decided_utc=env.clock().isoformat(), accepted=False,
                             reasons=("CATCHUP_MISSED", "EXPIRED_ENTRY", "ALREADY_MOVED"), policy_id="p"), None)
    env.engine.push_catchup("GER40", miss)
    r.run_cycle()
    env.clock.advance(minutes=60)
    assert r.label_now(env.clock()) == 5  # every non-traded opportunity
    fun = funnel(env.store, None, "DISCOVERY")
    a = fun["analysis"]
    st = {x["stage"]: x for x in a["stages"]}
    assert [x["stage"] for x in a["stages"]] == list(STAGES)
    assert st["RAW"]["passed"] == 6 and st["EXECUTED"]["passed"] == 1
    assert st["COST"]["blocked_here"] == 1 and st["STRATEGY_WINDOW"]["blocked_here"] == 1
    assert st["PORTFOLIO"]["blocked_here"] == 2  # ADDON + size_below_min on a portfolio cap
    assert st["TRADABLE"]["blocked_here"] == 1  # the catch-up miss
    assert a["special_buckets"] == {"MISSED": 1, "EXPIRED": 1, "ALREADY_MOVED": 1, "ADDON_NOT_SUPPORTED": 1, "OPPOSITE_NOT_SUPPORTED": 0}
    g = a["gates"]
    assert g["SPREAD_TOO_WIDE"]["layer"] == "engine" and g["SPREAD_TOO_WIDE"]["class"] == "SAFETY"
    assert g["ADDON_EXPOSURE_NOT_SUPPORTED_V1"]["class"] == "TEMPORARY"
    assert g["ADDON_EXPOSURE_NOT_SUPPORTED_V1"]["counterfactual"]["n_labelled"] == 1
    assert g["CATCHUP_MISSED"]["counterfactual"]["n_labelled"] == 1 and g["CATCHUP_MISSED"]["class"] == "CATCHUP"
    cf = g["SPREAD_TOO_WIDE"]["counterfactual"]
    assert cf["mean_r"] is not None and cf["mean_mfe_r"] is not None and cf["mean_mae_r"] is not None
    assert g["SPREAD_TOO_WIDE"]["by_market"] == {"GER40": 1} and sum(g["SPREAD_TOO_WIDE"]["by_hour"].values()) == 1
    assert a["counterfactual_by_source"]["STACK_REJECTED"]["n_labelled"] == 2
    # all six share market/family/direction/stop -> ONE structure: pure near-duplicates
    sd = a["structure_duplicates"]
    assert sd["opportunities"] == 6 and sd["unique_structures"] == 1 and sd["near_duplicate_ratio"] > 0.8
    assert a["shares"]["raw"]["market"]["GER40"]["share"] == 1.0
    assert a["shares"]["raw"]["cluster"]["INDEX"]["n"] == 6
    tbl = a["opportunities_per_market_per_hour"]["GER40"]
    assert sum(tbl["opportunities_by_local_hour"].values()) == 6 and tbl["active_days"] == 1
    text = render(fun)
    assert "STAGES:" in text and "SPREAD_TOO_WIDE" in text and "OUTSIDE_ENTRY_WINDOW is not observable" in text


def test_compact_funnel_is_in_the_heartbeat(env):
    env.engine.push("GER40", make_pair(tag="a"))
    r = env.build()
    r.start()
    r.run_cycle()
    hb = r.status(env.clock())["rejection_funnel"]
    assert hb["compact"]["stages"]["RAW"] == 1 and hb["compact"]["stages"]["EXECUTED"] == 1
    assert set(hb["compact"]["special_buckets"]) == {"MISSED", "EXPIRED", "ALREADY_MOVED", "ADDON_NOT_SUPPORTED", "OPPOSITE_NOT_SUPPORTED"}


def test_different_stops_are_different_structures(env):
    a = make_pair(tag="a", risk=1.5)
    b = make_pair(tag="b", risk=5.0)
    b_snap = dataclasses.replace(b[0])
    env.engine.push("GER40", a, (b_snap, b[1], b[2]))
    r = env.build()
    r.start()
    r.run_cycle()
    sd = funnel(env.store, None, "DISCOVERY")["analysis"]["structure_duplicates"]
    assert sd["opportunities"] == 2 and sd["unique_structures"] == 2
