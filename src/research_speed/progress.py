# ruff: noqa: E501
"""Run status file with heartbeat: no long run is ever silent.

``<root>/_status.json`` (atomic rewrite) holds, per segment, state / start / finish / wall seconds, plus run-level progress,
the ``heartbeat_utc`` (refreshed every ``interval`` seconds by a daemon thread while the run is alive) and the list of
segments that have been running longer than ``warn_after_s`` (default 30 min). A reader can therefore tell a live run
(fresh heartbeat) from a dead one (stale heartbeat) and see which segment is slow.
"""

from __future__ import annotations

import contextlib
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_speed.segments import atomic_write_json

PENDING, RUNNING, DONE, CACHED, FAILED, SKIPPED = (
    "pending",
    "running",
    "done",
    "cached",
    "failed",
    "skipped_dep_failed",
)
WARN_AFTER_S = 30 * 60


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StatusFile:
    def __init__(
        self,
        path: Path | str,
        run_id: str,
        segment_ids: list[str],
        *,
        meta: dict[str, Any] | None = None,
        interval_s: float = 30.0,
        warn_after_s: float = WARN_AFTER_S,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.interval_s = interval_s
        self.warn_after_s = warn_after_s
        self.meta = meta or {}
        self.started_utc = _now()
        self._t0 = time.monotonic()
        self._lock = threading.Lock()
        self._seg: dict[str, dict[str, Any]] = {s: {"state": PENDING} for s in segment_ids}
        self._start_mono: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- state changes
    def start(self, seg: str) -> None:
        with self._lock:
            self._seg[seg] = {"state": RUNNING, "started_utc": _now()}
            self._start_mono[seg] = time.monotonic()
        self.flush()

    def finish(self, seg: str, state: str = DONE, **info: Any) -> None:
        with self._lock:
            started = self._start_mono.get(seg)
            wall = (
                round(time.monotonic() - started, 2)
                if started is not None
                else info.pop("wall_s", None)
            )
            prev = self._seg.get(seg, {})
            self._seg[seg] = {
                **prev,
                "state": state,
                "finished_utc": _now(),
                "wall_s": wall,
                **info,
            }
        self.flush()

    # ---- output
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            segs = {k: dict(v) for k, v in self._seg.items()}
            long_running = [
                k
                for k, v in segs.items()
                if v["state"] == RUNNING and now - self._start_mono.get(k, now) > self.warn_after_s
            ]
            for k, v in segs.items():
                if v["state"] == RUNNING:
                    v["elapsed_s"] = round(now - self._start_mono.get(k, now), 1)
            finished = sum(1 for v in segs.values() if v["state"] in (DONE, CACHED))
            return {
                "run_id": self.run_id,
                "started_utc": self.started_utc,
                "heartbeat_utc": _now(),
                "elapsed_s": round(now - self._t0, 1),
                "segments_total": len(segs),
                "segments_finished": finished,
                "segments_failed": sum(1 for v in segs.values() if v["state"] in (FAILED, SKIPPED)),
                "long_running_over_s": self.warn_after_s,
                "long_running": long_running,
                "meta": self.meta,
                "segments": segs,
            }

    def flush(self) -> None:
        with contextlib.suppress(OSError):  # status is advisory; never fail a run because of it
            atomic_write_json(self.path, self.snapshot())

    def __enter__(self) -> StatusFile:
        self.flush()

        def beat() -> None:
            while not self._stop.wait(self.interval_s):
                self.flush()

        self._thread = threading.Thread(target=beat, name="status-heartbeat", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.flush()
