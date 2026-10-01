# ruff: noqa: E501
"""Lane X hook in the runner: a closed real trade records entry-vs-exit fields next to the timing analytics."""

from __future__ import annotations

from datetime import timedelta

import pytest

from demo import report
from demo.testing import T0, make_pair


def test_closed_trade_records_entry_exit_fields_and_the_report_section(env):
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
    ee = ex["entry_exit"]
    assert ee is not None and ee["eeq_version"].startswith("eeq-")
    assert ee["mfe_r"] == pytest.approx(ex["path_mfe_r"])
    for key in ("time_to_0.75R_s", "time_to_1.5R_s", "time_to_2R_s", "mfe_before_mae", "capture_ratio", "entry_exit_label", "capture_label"):
        assert key in ee
    assert ee["entry_label"] == "POTENTIAL_USEFUL_ENTRY"
    rep = report.build_report(env.store, "DISCOVERY")
    real = rep["entry_exit_quality"]["real_trades"]
    assert real["overall"]["n"] == 1 and real["n_with_path_fields"] == 1
    assert "## Entry quality vs exit quality" in report.render_markdown(rep)
