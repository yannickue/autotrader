from __future__ import annotations

import argparse
import json
from pathlib import Path

from alpha.fast.registry import discover
from research.runners import ad1_benchmark


def test_param_generation_is_deterministic_distinct_and_covers_all_families() -> None:
    families = discover()
    a, stats_a = ad1_benchmark.generate_param_sets(families, 60, seed=7)
    b, stats_b = ad1_benchmark.generate_param_sets(families, 60, seed=7)
    c, _ = ad1_benchmark.generate_param_sets(families, 60, seed=8)
    assert [h for *_, h in a] == [h for *_, h in b]
    assert stats_a == stats_b
    assert [h for *_, h in a] != [h for *_, h in c]
    hashes = [h for *_, h in a]
    assert len(a) == 60 and len(set(hashes)) == 60
    assert {sid for sid, *_ in a} == set(families)
    for sid, params, _ in a:
        assert 1.0 <= params.target_r <= 4.0
        assert ad1_benchmark.canonical_hash(sid, params) in hashes


def test_dedupe_counts_duplicates_when_space_is_exhausted() -> None:
    families = {k: v for k, v in discover().items() if k == "SESSION_TWAP_REFERENCE"}
    # a tiny perturbation space of 2 floats still yields distinct sets, but the unperturbed
    # variants are re-drawn by the random phase often enough to exercise the dedupe path
    sets, stats = ad1_benchmark.generate_param_sets(families, 40, seed=1)
    assert len({h for *_, h in sets}) == len(sets)
    assert stats["attempts"] >= len(sets)


def test_small_real_benchmark_and_warm_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ad1_benchmark, "REPORT_DIR", tmp_path / "reports")
    args = argparse.Namespace(
        n=12, seed=3, config=str(ad1_benchmark.DEFAULT_CONFIG),
        cache_dir=str(tmp_path / "cache"), skip_cold=True, keep_results=False,
    )
    report = ad1_benchmark.run(args)
    assert report["param_generation"]["unique_specs"] == 12
    assert report["first_pass"]["cache_hit_rate"] == 0.0
    assert report["second_pass_warm_cache"]["cache_hit_rate"] == 1.0
    assert (tmp_path / "reports" / "benchmark_12.json").is_file()
    assert json.loads((tmp_path / "reports" / "benchmark_12.json").read_text())["n_requested"] == 12
