from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest

from alpha.discovery.disk import (
    CacheSpaceError,
    RunScratch,
    assert_free_space,
    limit_workers_by_space,
    prune_oldest_shards,
)


def test_prune_oldest_shards_stays_under_limit_and_preserves_other_files(tmp_path: Path) -> None:
    shards = []
    for i in range(3):
        path = tmp_path / f"shard_{i}.jsonl"
        path.write_bytes(bytes([i]) * 10)
        path.touch()
        shards.append(path)
    artifact = tmp_path / "provenance.json"
    artifact.write_text("keep", encoding="utf-8")

    prune_oldest_shards(tmp_path, max_bytes=20)

    assert not shards[0].exists()
    assert shards[1].exists() and shards[2].exists()
    assert artifact.read_text(encoding="utf-8") == "keep"


def test_run_scratch_cleanup_removes_only_owned_directory(tmp_path: Path) -> None:
    parent_file = tmp_path / "keep.txt"
    parent_file.write_text("keep", encoding="utf-8")
    scratch = RunScratch.create(tmp_path, prefix="campaign-")
    (scratch.path / "cache.bin").write_bytes(b"x")

    scratch.cleanup()

    assert not scratch.path.exists()
    assert parent_file.exists()


def test_free_space_guard_and_worker_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Usage = namedtuple("Usage", "total used free")
    gib = 1024**3
    monkeypatch.setattr(
        "alpha.discovery.disk.shutil.disk_usage", lambda _p: Usage(10 * gib, 9 * gib, gib)
    )

    with pytest.raises(CacheSpaceError, match=r"2\.00 GiB"):
        assert_free_space(tmp_path, min_gb=2)
    assert limit_workers_by_space(4, tmp_path, min_free_gb=0.5, per_worker_gb=0.25) == 2
    with pytest.raises(CacheSpaceError):
        limit_workers_by_space(1, tmp_path, min_free_gb=2, per_worker_gb=1)


def test_free_space_guard_accepts_relative_and_not_yet_created_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert assert_free_space("data/feature_store/never_created_yet", min_gb=0.0) > 0
    workers = limit_workers_by_space(2, "nested/not/created", min_free_gb=0.0, per_worker_gb=0.0001)
    assert workers == 2
