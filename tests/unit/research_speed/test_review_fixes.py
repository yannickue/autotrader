# ruff: noqa: E501
"""Regression tests for the read-only review findings of the research-speed branch (cache keys, manifests, gate C cache records)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from research_speed.artifact import ArtifactFingerprint
from research_speed.segments import SegmentStore

SCRIPTS = str(Path(__file__).resolve().parents[3] / "scripts")
if SCRIPTS not in sys.path:
    sys.path.append(SCRIPTS)  # appended: scripts/coverage_analysis.py must not shadow the src package


def _frame():
    n = 20
    ts = pd.date_range("2025-01-01", periods=n, freq="5min", tz="UTC")
    v = np.arange(n, dtype=float)
    return pd.DataFrame({"ts": ts, "open": v, "high": v + 1, "low": v - 1, "close": v + 0.5, "tick_volume": v, "spread_pts": v * 0})


def test_eval_from_is_part_of_the_data_hash_and_therefore_of_the_artifact_id():
    from coverage_analysis.observer_lab import backfill_segments as SEG

    f = _frame()
    a = SimpleNamespace(frame=f, eval_from=pd.Timestamp("2025-01-01 00:30", tz="UTC"))
    b = SimpleNamespace(frame=f, eval_from=pd.Timestamp("2025-01-01 00:45", tz="UTC"))
    assert SEG.data_identity(a) == SEG.data_identity(SimpleNamespace(frame=f.copy(), eval_from=a.eval_from))
    assert SEG.data_identity(a) != SEG.data_identity(b)
    spec = {"market": "GER40", "limit": None}
    assert SEG.events_fingerprint(spec, SEG.data_identity(a), "o").artifact_id != SEG.events_fingerprint(spec, SEG.data_identity(b), "o").artifact_id


def test_dynamic_import_scan_finds_all_dynamic_loading_forms(tmp_path):
    from research_speed.importgraph import dynamic_import_files

    (tmp_path / "ok.py").write_text("import os\nx = os.sep\n")
    (tmp_path / "a.py").write_text("import importlib\nm = importlib.import_module('x')\n")
    (tmp_path / "b.py").write_text("m = __import__('x')\n")
    (tmp_path / "c.py").write_text("import importlib.util as u\ns = u.spec_from_file_location('x', 'y')\n")
    (tmp_path / "d.py").write_text("from importlib import import_module\nm = import_module('x')\n")
    got = {p.name for p in dynamic_import_files([tmp_path / n for n in ("ok.py", "a.py", "b.py", "c.py", "d.py")])}
    assert got == {"a.py", "b.py", "c.py", "d.py"}


FP = ArtifactFingerprint("data1", "feat1", "ctl1", "cfg1", "lab1", "pre1")


def test_malformed_segment_manifests_are_a_miss_and_never_raise(tmp_path):
    (tmp_path / "M").mkdir()
    (tmp_path / "M" / "a.parquet").write_bytes(b"AAAA")
    s = SegmentStore(tmp_path)
    good = s.commit("M/events", FP, ["M/a.parquet"], wall_s=1.0)
    assert s.lookup("M/events", FP).hit
    sha = good["files"]["M/a.parquet"]["sha256"]
    bad = {
        "empty": {},
        "no files": {k: v for k, v in good.items() if k != "files"},
        "empty files": {**good, "files": {}},
        "files not dict": {**good, "files": ["M/a.parquet"]},
        "no size": {**good, "files": {"M/a.parquet": {"sha256": sha}}},
        "no sha": {**good, "files": {"M/a.parquet": {"size": 4}}},
        "meta not dict": {**good, "files": {"M/a.parquet": 4}},
        "size wrong type": {**good, "files": {"M/a.parquet": {"size": "4", "sha256": sha}}},
    }
    for name, m in bad.items():
        s.path("M/events").write_text(json.dumps(m), encoding="utf-8")
        lk = s.lookup("M/events", FP)
        assert not lk.hit and lk.reason, name


def test_gate_c_cache_record_is_atomic_and_a_corrupt_record_is_a_miss(tmp_path):
    import observer_gate_c as G

    cp = tmp_path / "GER40.json"
    assert G.read_cache_record(cp, "fp1") is None
    G.write_cache_record(cp, "fp1", {"rows": [1]})
    assert G.read_cache_record(cp, "fp1") == {"rows": [1]}
    assert G.read_cache_record(cp, "other") is None
    assert [p.name for p in tmp_path.iterdir()] == ["GER40.json"]  # no tmp leftovers
    cp.write_text(cp.read_text(encoding="utf-8")[:12], encoding="utf-8")  # truncated
    assert G.read_cache_record(cp, "fp1") is None
    G.write_cache_record(cp, "fp1", {"rows": [2]})  # recomputed and replaced
    assert G.read_cache_record(cp, "fp1") == {"rows": [2]}
    cp.write_text("[1, 2]", encoding="utf-8")  # valid JSON, wrong shape
    assert G.read_cache_record(cp, "fp1") is None
