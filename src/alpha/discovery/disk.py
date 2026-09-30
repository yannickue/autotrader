"""Small, research-only disk lifecycle guards for discovery campaigns."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

GIB = 1024**3


class CacheSpaceError(RuntimeError):
    """Raised before a research run would exceed its disk-space budget."""


def assert_free_space(path: str | Path = "C:/", min_gb: float = 2.0) -> int:
    """Return free bytes, or fail closed when fewer than ``min_gb`` remain."""
    free = shutil.disk_usage(Path(path).anchor or path).free
    if free < min_gb * GIB:
        raise CacheSpaceError(
            f"need at least {min_gb:.2f} GiB free on {Path(path).anchor or path}; "
            f"only {free / GIB:.2f} GiB available"
        )
    return free


def limit_workers_by_space(
    requested: int, path: str | Path = "C:/", *, min_free_gb: float = 2.0,
    per_worker_gb: float = 1.0,
) -> int:
    """Cap parallel workers to the remaining scratch budget; refuse when none fit."""
    free = shutil.disk_usage(Path(path).anchor or path).free / GIB
    affordable = int(max(0.0, free - min_free_gb) // per_worker_gb)
    if affordable < 1:
        raise CacheSpaceError(
            f"no worker fits: {free:.2f} GiB free, {min_free_gb:.2f} GiB reserve, "
            f"{per_worker_gb:.2f} GiB/worker"
        )
    return min(max(1, requested), affordable)


def prune_oldest_shards(root: str | Path, max_bytes: int) -> None:
    """Evict oldest evaluator shards until their aggregate size fits the bound."""
    root = Path(root)
    shards = sorted(root.glob("shard_*.jsonl"), key=lambda p: (p.stat().st_mtime_ns, p.name))
    total = sum(p.stat().st_size for p in shards)
    for path in shards:
        if total <= max_bytes:
            break
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size


@dataclass
class RunScratch:
    """An explicitly owned temporary cache directory with deterministic cleanup."""

    path: Path

    @classmethod
    def create(cls, parent: str | Path | None = None, prefix: str = "ad1-") -> RunScratch:
        return cls(Path(tempfile.mkdtemp(prefix=prefix, dir=parent)))

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> Path:
        return self.path

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()
