"""Netting / non-flat scenarios through BOTH real engines (HEAVY; tier ``slow``).

The lead adds ``"tests/unit/research_workbench/test_netting_e2e.py"`` to ``SLOW_FILES``.

Written-down behaviour per side:

* ``entry_on_trending_close``: FAST fills at o[6] + spread = 106; the replay must fill against the
  DECISION bar close book (105 + 1 = 106). A replay acting inside ``on_bar`` (before the exchange
  matched the bar) filled at the PREVIOUS close (105): the SYN_E2E defect.
* ``same_bar_exit_and_reentry``: trade A exits inside bar 6, trade B decides at the CLOSE of bar 6.
  FAST admits it (decision_idx == previous exit_idx); the replay must too (position already closed
  when the +1 ns decision alert fires): 2 trades per side, no ignored candidates.
* ``overlapping_candidate_dropped``: FAST never trades the overlapping candidate; the harness
  reports it as ``candidates_blocked_by_open_position``; 1 trade per side.
"""

from __future__ import annotations

import pytest

from research_workbench.differential import DiffClass, run_differential
from research_workbench.golden_netting import NETTING_SCENARIOS

pytest.importorskip("nautilus_trader")


def _run(sid: str):
    market, cands, cost, window = NETTING_SCENARIOS[sid].inputs()
    return run_differential(market, cands, cost, window=window, scenario_id=sid)


def _non_exact(r) -> dict[str, DiffClass]:
    return {
        d.field: d.diff_class
        for d in r.field_diffs
        if d.diff_class not in (DiffClass.EXACT_MATCH, DiffClass.TOLERANCE_MATCH)
    }


@pytest.mark.parametrize("sid", sorted(NETTING_SCENARIOS))
def test_scenario_passes_with_every_trade_matched_and_nothing_ignored(sid: str) -> None:
    r = _run(sid)
    exp = NETTING_SCENARIOS[sid].expected
    assert r.status == "PASS", (r.blocked_reason, r.summary.get("bug_suspected_fields"))
    assert r.fast_trade_count == r.fidelity_trade_count == exp["n_trades"]
    assert r.summary["nautilus_ignored_candidates"] == []
    assert _non_exact(r) == {}  # slip 0, contiguous bars: Nautilus reproduces FAST exactly
    assert r.summary["netting"]["overlap_violations"] == 0
    assert r.summary["netting"]["replay_ignored_active_position"] == 0


def test_trending_entry_fills_against_the_decision_bar_close() -> None:
    r = _run("entry_on_trending_close")
    got = {d.field: d.fidelity for d in r.field_diffs}
    assert got["fill_price"] == pytest.approx(106.0)  # NOT 105.0 (previous close + spread)
    assert got["entry_ref"] == pytest.approx(105.0)  # decision-bar bid close
    assert got["net_pnl"] == pytest.approx(12.0)


def test_same_bar_exit_and_reentry_is_taken_by_both_engines() -> None:
    r = _run("same_bar_exit_and_reentry")
    assert r.fast_trade_count == r.fidelity_trade_count == 2
    assert r.summary["netting"]["same_bar_reentries"] == 1
    nets = sorted(d.fidelity for d in r.field_diffs if d.field == "net_pnl")
    assert nets == [pytest.approx(-49.5), pytest.approx(75.0)]


def test_overlapping_candidate_is_reported_not_silent() -> None:
    r = _run("overlapping_candidate_dropped")
    assert r.summary["netting"]["candidates_blocked_by_open_position"] == 1
    assert r.summary["netting"]["candidates_total"] == 2
