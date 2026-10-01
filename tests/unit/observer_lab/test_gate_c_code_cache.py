# ruff: noqa: E501
"""Gate C per-market cache key includes the CONTENT hash of the code it depends on (changed enrichment / stats / script code never serves a stale cache)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = str(Path(__file__).resolve().parents[3] / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import observer_gate_c as G  # noqa: E402

PREREG = SimpleNamespace(json_sha256="p" * 64)
TASK = {"stage": "fit", "market": "GER40", "B": 100, "B_week": 50, "seed": 1, "data_fingerprint": "d" * 64, "contrasts": [{"id": "h1", "kind": "k", "feature": "f", "cell": "c"}], "cell_defs": None,
        "nc_base_alpha": 0.01, "nc_b_tol_s": 60, "bridge": False}


def test_code_hash_covers_the_enrichment_and_stats_modules_and_is_stable():
    h = G.code_hash()
    assert len(h) == 64 and h == G.code_hash()
    from research_speed.importgraph import closure

    names = {p.name for p in closure([Path(G.__file__).resolve()], [G.ROOT / "src", G.ROOT / "scripts"])}
    assert {"enrichment.py", "stats.py", "splits.py", "observer_gate_c.py"} <= names


def test_fingerprint_changes_with_the_code_hash_and_nothing_else_is_needed(monkeypatch):
    base = G.fingerprint(PREREG, TASK)
    assert G.fingerprint(PREREG, TASK) == base
    monkeypatch.setattr(G, "_CODE_HASH", "edited-enrichment-code")
    assert G.fingerprint(PREREG, TASK) != base
    monkeypatch.setattr(G, "_CODE_HASH", None)
    assert G.fingerprint(PREREG, TASK) == base  # restoring the real code restores the key


def test_jobs_cap_is_three():
    assert G.MAX_JOBS == 3
