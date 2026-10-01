# ruff: noqa: E501
"""Checkpoint / resume / cache for long runs, segmented by market x stage.

A SEGMENT is the smallest unit that is computed, committed and skipped as a whole (e.g. ``GER40/events``,
``GER40/controls3_a``). Its manifest ``<root>/_segments/<market>__<stage>.json`` is the commit marker: it is written LAST,
atomically (tmp file + ``os.replace``), and carries the ARTIFACT_ID plus size and sha256 of every output file.

``SegmentStore.lookup`` is the only place that decides CACHE HIT vs MISS; a miss always names its reason:

    NO_MANIFEST | NOT_COMPLETE | FINGERPRINT_CHANGED:<components> | FILE_MISSING:<f> | FILE_CHANGED:<f>

so nothing is reused silently after a change of data, code, config or version, and an aborted run (no manifest, or a
manifest that no longer matches its files) is simply recomputed. A half-written output never carries a manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_speed.artifact import ArtifactFingerprint

SEGMENT_SCHEMA = "research-speed-segment-1"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so that a reader sees either the old or the complete new file, never a partial one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=1, default=str, sort_keys=True))


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class Lookup:
    hit: bool
    reason: str  # "HIT" or the miss reason (see module docstring)
    artifact_id: str | None = None
    manifest: dict[str, Any] | None = field(default=None, compare=False)


class SegmentStore:
    """Segment manifests below ``<root>/_segments``; ``root`` is the backfill / run output directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.dir = self.root / "_segments"

    def path(self, segment_id: str) -> Path:
        return self.dir / f"{segment_id.replace('/', '__')}.json"

    def read(self, segment_id: str) -> dict[str, Any] | None:
        p = self.path(segment_id)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def lookup(
        self, segment_id: str, fp: ArtifactFingerprint, *, verify_hash: bool = True
    ) -> Lookup:
        m = self.read(segment_id)
        if m is None:
            return Lookup(False, "NO_MANIFEST")
        if m.get("status") != "COMPLETE" or m.get("schema") != SEGMENT_SCHEMA:
            return Lookup(False, "NOT_COMPLETE")
        changed = fp.diff(m.get("fingerprint") or {})
        if changed or m.get("artifact_id") != fp.artifact_id:
            return Lookup(False, "FINGERPRINT_CHANGED:" + (",".join(changed) or "artifact_id"))
        files = m.get("files")
        if not isinstance(files, dict) or not files or not isinstance(m.get("artifact_id"), str):
            return Lookup(False, "NOT_COMPLETE")  # a COMPLETE manifest names a non-empty expected file set
        for meta in files.values():
            if (
                not isinstance(meta, dict)
                or not isinstance(meta.get("size"), int)
                or isinstance(meta.get("size"), bool)
                or not isinstance(meta.get("sha256"), str)
            ):
                return Lookup(False, "NOT_COMPLETE")
        for rel, meta in files.items():
            f = self.root / rel
            if not f.is_file():
                return Lookup(False, f"FILE_MISSING:{rel}")
            if f.stat().st_size != meta["size"] or (
                verify_hash and file_sha256(f) != meta["sha256"]
            ):
                return Lookup(False, f"FILE_CHANGED:{rel}")
        return Lookup(True, "HIT", m["artifact_id"], m)

    def commit(
        self,
        segment_id: str,
        fp: ArtifactFingerprint,
        files: list[str],
        *,
        wall_s: float,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write the manifest (the commit marker). ``files`` are paths relative to ``root``; all must exist."""
        meta = {}
        for rel in sorted(files):
            f = self.root / rel
            meta[rel] = {"size": f.stat().st_size, "sha256": file_sha256(f)}
        m = {
            "schema": SEGMENT_SCHEMA,
            "status": "COMPLETE",
            "segment": segment_id,
            "artifact_id": fp.artifact_id,
            "fingerprint": fp.components(),
            "files": meta,
            "wall_s": round(wall_s, 2),
            "completed_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            **(extra or {}),
        }
        atomic_write_json(self.path(segment_id), m)
        return m

    def invalidate(self, segment_id: str) -> None:
        self.path(segment_id).unlink(missing_ok=True)


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.monotonic()

    def __call__(self) -> float:
        return time.monotonic() - self.t0
