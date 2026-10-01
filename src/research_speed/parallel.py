# ruff: noqa: E501
"""Bounded, deterministic process parallelism for independent units of work (markets, segments).

Determinism rule: workers share NO state (separate processes, inputs by value, outputs by file / return value) and results
are always returned in INPUT order, never in completion order. The worker count therefore cannot influence a result.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from typing import Any

MAX_WORKERS = (
    3  # 8 logical cores / 7.8 GB shared with other builders: never more than 3 research workers
)
DEFAULT_PER_WORKER_MB = (
    300  # observed peak working set per market ~240-270 MB (docs/OBSERVER_BACKFILL_*)
)
DEFAULT_RESERVE_MB = 1500  # memory kept free for the rest of the machine


def available_memory_mb() -> float | None:
    """Currently available physical memory in MB (Windows via ctypes, POSIX via sysconf); None when unknown."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMSTATUS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = MEMSTATUS()
            st.dwLength = ctypes.sizeof(MEMSTATUS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return round(st.ullAvailPhys / 1e6, 1)
            return None
        return round(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1e6, 1)
    except Exception:
        return None


def clamp_jobs(
    requested: int,
    n_tasks: int,
    *,
    per_worker_mb: float | None = None,
    reserve_mb: float | None = None,
    avail_mb: float | None = None,
) -> int:
    """Effective worker count: min(requested, MAX_WORKERS, n_tasks, what the free memory allows), at least 1.

    The reserve defaults to ``DEFAULT_RESERVE_MB`` and can be set with the env var ``RESEARCH_SPEED_RESERVE_MB``
    (the per-worker estimate with ``RESEARCH_SPEED_PER_WORKER_MB``; both are for controlled benchmarks). ``avail_mb`` is injectable for tests; when memory is unknown the memory term is skipped (never raises the count).
    """
    if requested < 1:
        raise ValueError("jobs must be >= 1")
    if per_worker_mb is None:
        per_worker_mb = float(os.environ.get("RESEARCH_SPEED_PER_WORKER_MB", DEFAULT_PER_WORKER_MB))
    if reserve_mb is None:
        reserve_mb = float(os.environ.get("RESEARCH_SPEED_RESERVE_MB", DEFAULT_RESERVE_MB))
    n = min(requested, MAX_WORKERS, max(1, n_tasks))
    mem = available_memory_mb() if avail_mb is None else avail_mb
    if mem is not None:
        n = min(n, max(1, int((mem - reserve_mb) // per_worker_mb)))
    return max(1, n)


def ordered_map[T, R](fn: Callable[[T], R], items: Sequence[T], jobs: int) -> list[R]:
    """``[fn(x) for x in items]`` with up to ``jobs`` worker processes; the result order is the input order."""
    if jobs <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with ProcessPoolExecutor(max_workers=clamp_jobs(jobs, len(items))) as ex:
        return list(ex.map(fn, items))


def describe(requested: int, n_tasks: int) -> dict[str, Any]:
    """Snapshot for logs / manifests: what was asked, what is used, why."""
    mem = available_memory_mb()
    return {
        "requested": requested,
        "effective": clamp_jobs(requested, n_tasks, avail_mb=mem),
        "max_workers": MAX_WORKERS,
        "available_mb": mem,
    }
