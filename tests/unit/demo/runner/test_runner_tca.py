# ruff: noqa: E501
"""TCA chain (decision -> arrival -> fill) and outcome timing analytics."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from demo import report
from demo.execution.events import Accepted, Fill, ProtectionConfirmed
from demo.labeling import PathPoint, path_analytics
from demo.testing import T0, make_pair


def _scripted_fill(intent, *, ref=None, px="100.05"):
    return [
        Accepted(intent.intent_id, Decimal("1"), Decimal("10000"), Decimal("0.01"), Decimal("100"), Decimal("2")),
        Fill(intent.intent_id, Decimal(px), Decimal("1"), Decimal("0.1"), Decimal("0.05"), Decimal("-1"), Decimal("0"),
             "ord-1", "pos-1", reference_price=None if ref is None else Decimal(ref)),
        ProtectionConfirmed(intent.intent_id, "pos-1", Decimal(str(intent.stop)), Decimal(str(intent.target))),
    ]


def test_tca_chain_is_persisted_and_missing_fields_are_listed(env):
    snap, dec, intent = make_pair(entry=100.0, risk=1.5)
    env.stack.script[intent.intent_id] = _scripted_fill(intent, ref="100.02", px="100.05")
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    t = env.store.get_tca(intent.intent_id, "ENTRY")
    assert t["decision_price"] == 100.0 and t["order_arrival_price"] == pytest.approx(100.02)
    assert t["actual_fill_price"] == pytest.approx(100.05) and t["requested_price"] is None
    assert t["decision_to_arrival_drift"] == pytest.approx(0.02)
    assert t["implementation_shortfall"] == pytest.approx(0.05)
    assert t["implementation_shortfall_r"] == pytest.approx(0.05 / 1.5)
    assert t["tca_missing_fields"] == ["requested_price"]


def test_short_trade_shortfall_is_adverse_positive(env):
    snap, dec, intent = make_pair(entry=100.0, risk=1.5, direction=-1)
    env.stack.script[intent.intent_id] = _scripted_fill(intent, ref="99.97", px="99.95")
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    t = env.store.get_tca(intent.intent_id, "ENTRY")
    assert t["decision_to_arrival_drift"] == pytest.approx(0.03) and t["implementation_shortfall"] == pytest.approx(0.05)


def test_missing_arrival_price_is_none_not_invented(env):
    snap, dec, intent = make_pair()
    env.stack.script[intent.intent_id] = _scripted_fill(intent, ref=None)
    env.engine.push("GER40", (snap, dec, intent))
    r = env.build()
    r.start()
    r.run_cycle()
    t = env.store.get_tca(intent.intent_id, "ENTRY")
    assert t["order_arrival_price"] is None and t["decision_to_arrival_drift"] is None
    assert set(t["tca_missing_fields"]) == {"order_arrival_price", "requested_price"}


def test_outcome_timing_analytics_recorded_and_reported(env):
    snap, dec, intent = make_pair()
    env.engine.push("GER40", (snap, dec, intent))
    env.stack.bar_source.overrides[("GER40", T0 + timedelta(minutes=20))] = (100.0, 103.5, 99.8, 103.0)
    r = env.build()
    r.start()
    r.run_cycle()
    env.stack.close_position(intent.intent_id, reason="TARGET", exit_price=102.6)
    env.clock.advance(minutes=35)
    r.run_cycle()
    ex = env.store.get_outcome_extra(intent.intent_id)
    assert ex["signal_age_at_fill_s"] == pytest.approx(10.0)
    assert ex["time_to_1R_s"] is not None and ex["time_to_0.25R_s"] <= ex["time_to_0.5R_s"] <= ex["time_to_1R_s"]
    assert ex["mfe_giveback_r"] >= 0 and ex["time_without_progress_s"] >= 0 and ex["resolution"] == "bar_5m"
    ea = report.build_report(env.store, "DISCOVERY")["execution_analytics"]
    assert ea["timing"]["n"] == 1 and ea["timing"]["share_reaching_1R"] == 1.0
    assert ea["tca"]["n"] == 1 and "requested_price" in ea["tca"]["fields_missing_in_stack_events"]


def test_path_analytics_pure():
    pts = [PathPoint("2026-10-01T09:05:00+00:00", 100.5, 99.9), PathPoint("2026-10-01T09:10:00+00:00", 102.0, 100.4),
           PathPoint("2026-10-01T09:15:00+00:00", 101.0, 99.0)]
    a = path_analytics(direction=1, entry_price=100.0, initial_stop=99.0, entry_ts="2026-10-01T09:00:00+00:00",
                       exit_price=99.0, exit_ts="2026-10-01T09:16:00+00:00", path=pts)
    assert a["time_to_0.25R_s"] == 300.0 and a["time_to_0.5R_s"] == 300.0 and a["time_to_1R_s"] == 600.0
    assert a["path_mfe_r"] == pytest.approx(2.0) and a["mfe_giveback_r"] == pytest.approx(3.0)
    assert a["time_without_progress_s"] == pytest.approx(360.0)  # 09:10 (last new high) -> 09:16
    s = path_analytics(direction=-1, entry_price=100.0, initial_stop=101.0, entry_ts="2026-10-01T09:00:00+00:00",
                       exit_price=101.0, exit_ts="2026-10-01T09:20:00+00:00", path=[])
    assert s["time_to_1R_s"] is None and s["mfe_giveback_r"] == 1.0 and s["time_without_progress_s"] == 1200.0
