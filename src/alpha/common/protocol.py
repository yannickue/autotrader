"""Chronological partitions, the one-shot OOS gate and the reproducibility record."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from itertools import pairwise
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class Partition:
    name: str
    start: str  # inclusive Berlin date YYYY-MM-DD
    end: str  # inclusive


@dataclass(frozen=True)
class SplitPlan:
    """Disjoint, ordered, never shuffled. TRAIN builds, VALIDATION selects, OOS is final."""

    train: Partition
    validation: Partition
    oos: Partition

    def __post_init__(self) -> None:
        parts = (self.train, self.validation, self.oos)
        for a, b in pairwise(parts):
            if not a.end < b.start:
                raise ValueError(f"partitions overlap or are unordered: {a.name} -> {b.name}")

    def mask(self, dates: np.ndarray, part: Partition) -> np.ndarray:
        d = dates.astype("datetime64[D]")
        return (d >= np.datetime64(part.start)) & (d <= np.datetime64(part.end))

    def to_dict(self) -> dict:
        return {p.name: [p.start, p.end] for p in (self.train, self.validation, self.oos)}


class OosAccessError(RuntimeError):
    """Raised when the untouched OOS partition would be evaluated a second time."""


class OosGate:
    """Records every OOS evaluation; a second, different candidate set is refused.

    Each evaluation must present the frozen configuration hash. The log file is part of the
    reviewable evidence (research/reports). Re-running the identical frozen set is allowed
    (pure reproducibility) and logged; changing the set after seeing OOS is not.
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = Path(log_path)

    def _read(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return json.loads(self.log_path.read_text(encoding="utf-8"))

    def evaluate(self, frozen_hash: str, note: str) -> None:
        log = self._read()
        first = next((e for e in log if e["kind"] == "evaluation"), None)
        if first is not None and first["frozen_hash"] != frozen_hash:
            raise OosAccessError(
                "OOS already evaluated for a different frozen candidate set "
                f"({first['frozen_hash'][:12]} != {frozen_hash[:12]}); refusing to peek again"
            )
        log.append({"kind": "evaluation", "frozen_hash": frozen_hash, "note": note})
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(json.dumps(log, indent=1), encoding="utf-8")


def stable_hash(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def git_commit(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return out.stdout.strip() + ("+DIRTY" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def run_record(*, repo: Path, config: dict, dataset_provenance: dict, seed: int) -> dict:
    def version(pkg: str) -> str:
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return "NOT_INSTALLED"

    return {
        "git_commit": git_commit(repo),
        "config_hash": stable_hash(config),
        "config": config,
        "dataset_hashes": {m["month"]: m["content_sha256"] for m in dataset_provenance["months"]},
        "dataset_bars": dataset_provenance["n_bars"],
        "seed": seed,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "versions": {
            "nautilus_trader": version("nautilus_trader"),
            "numpy": version("numpy"),
            "pandas": version("pandas"),
            "pyarrow": version("pyarrow"),
        },
        "note": "backtest engine is the AR1 bar simulator (alpha.common.sim); Nautilus is NOT used",
    }
