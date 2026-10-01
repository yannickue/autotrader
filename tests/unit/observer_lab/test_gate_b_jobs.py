# ruff: noqa: E501
"""Gate B report orchestration (scripts/observer_gate_b_report.py): --jobs bounds, per-market cache / missing / todo planning, result order = market order."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parents[3] / "scripts")
SRC = str(Path(__file__).resolve().parents[3] / "src")
if sys.path[0] != SRC:  # other test modules may have put scripts/ first (scripts/coverage_analysis.py would shadow the src package of the same name)
    sys.path.insert(0, SRC)
import coverage_analysis.observer_lab  # noqa: E402,F401  (import the src package BEFORE the script is imported)

_saved_path = list(sys.path)
sys.path.insert(0, SCRIPTS)
import observer_gate_b_report as GB  # noqa: E402

sys.path[:] = _saved_path  # the script puts scripts/ in front of src/; do not leak that into the rest of the session


@pytest.fixture(autouse=True)
def _scripts_on_path(monkeypatch):
    monkeypatch.syspath_prepend(SCRIPTS)  # main() imports entry_exit_quality from scripts/; undone after each test


def _result(m: str) -> dict:
    return {"market": m, "verdict": "PASS", "blocking": [], "gate_b_seconds": 1, "plausibility": {"column_summary": {}}}


def _argv(root: Path, out: Path, markets: list[str], *extra: str) -> list[str]:
    return ["--root", str(root), "--out", str(out), "--markets", *markets, *extra]


def test_jobs_above_three_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        GB.main(_argv(tmp_path, tmp_path / "o", ["GER40"], "--jobs", "4"))
    with pytest.raises(SystemExit):
        GB.main(_argv(tmp_path, tmp_path / "o", ["GER40"], "--jobs", "0"))


def test_cached_missing_and_todo_markets_keep_the_market_order(tmp_path, monkeypatch):
    root = tmp_path / "bf"
    for m in ("GER40", "NAS100", "SPX500"):
        (root / m).mkdir(parents=True)
    (root / "GER40" / "gate_b.json").write_text(json.dumps(_result("GER40")))  # cached
    (root / "NAS100" / "manifest.json").write_text("{}")  # todo (computed by the fake below)
    # SPX500 has neither -> missing
    calls: list[str] = []

    def fake(m, root_, p2, args, cached):
        calls.append(m)
        return _result(m)

    monkeypatch.setattr(GB, "gate_b_market", fake)
    monkeypatch.setattr(GB, "render", lambda results, meta: "report")
    rc = GB.main(_argv(root, tmp_path / "out", ["SPX500", "NAS100", "GER40"]))
    rep = json.loads((tmp_path / "out" / "observer_backfill_report.json").read_text())
    assert calls == ["NAS100"] and rc == 1  # only the uncached market is computed; a missing market -> not PASS
    assert [r["market"] for r in rep["markets"]] == ["NAS100", "GER40"] and rep["meta"]["missing"] == ["SPX500"]
    assert (root / "NAS100" / "gate_b.json").is_file()
