"""Non-vacuity of the end-to-end differential: inputs changed in the REPLAY ONLY must fail.

HEAVY (starts Nautilus engines). Must be tiered ``slow`` like ``test_differential_e2e.py``: the lead
adds ``"tests/unit/research_workbench/test_differential_e2e_nonvacuity.py"`` to ``SLOW_FILES`` in
``tests/conftest.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from research_workbench.differential import DiffClass, run_differential
from research_workbench.golden import GOLDEN_SCENARIOS

pytest.importorskip("nautilus_trader")

SID = "long_normal_target"


def _run(mutate=None):
    market, cands, cost, window = GOLDEN_SCENARIOS[SID].inputs()
    return run_differential(
        market, cands, cost, window=window, scenario_id="mut", _replay_mutator=mutate
    )


def _bug_fields(r) -> set[str]:
    return {d.field for d in r.field_diffs if d.diff_class is DiffClass.BUG_SUSPECTED}


def test_untampered_baseline_passes() -> None:
    assert _run().status == "PASS"


def test_tampered_replay_stop_is_detected() -> None:
    r = _run(lambda cs: [dataclasses.replace(c, stop=c.stop + 1.0) for c in cs])
    assert r.status == "FAIL" and "stop" in _bug_fields(r)


def test_tampered_replay_target_is_detected_in_target_and_exit() -> None:
    r = _run(lambda cs: [dataclasses.replace(c, target=c.target - 5.0) for c in cs])
    assert r.status == "FAIL"
    assert {"target", "exit_price"} <= _bug_fields(r)  # Nautilus really exited at 108


def test_tampered_replay_quantity_is_detected_in_qty_and_pnl() -> None:
    r = _run(lambda cs: [dataclasses.replace(c, qty=c.qty * 2) for c in cs])
    assert r.status == "FAIL"
    assert {"qty", "net_pnl"} <= _bug_fields(r)


def test_by_construction_marking_and_scope_on_real_run() -> None:
    r = _run()
    expected = ["signal_ts_ns", "direction", "stop", "target", "qty"]
    assert r.summary["by_construction_fields"] == expected
    assert "Does NOT validate candidate generation or position sizing" in r.summary["scope"]
    marked = {d.field for d in r.field_diffs if d.by_construction}
    assert marked == set(expected)
    assert r.summary["matched_fields"] == len(r.field_diffs) - len(marked)
