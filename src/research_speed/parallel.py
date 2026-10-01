# ruff: noqa: E501
"""Bounded, deterministic process parallelism for independent units of work (markets, segments).

Determinism rule: workers share NO state (separate processes, inputs by value, outputs by file / return value) and results
are always returned in INPUT order, never in completion order. The worker count therefore cannot influence a result.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator, MutableMapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager, suppress
from typing import Any

MAX_WORKERS = (
    3  # 8 logical cores / 7.8 GB shared with other builders: never more than 3 research workers
)
DEFAULT_PER_WORKER_MB = (
    300  # observed peak working set per market ~240-270 MB (docs/OBSERVER_BACKFILL_*)
)
DEFAULT_RESERVE_MB = 1500  # memory kept free for the rest of the machine
MIN_PER_WORKER_MB = 50  # a smaller per-worker estimate is a configuration error, not a benchmark
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
ALLOW_LIVE_ENV = "RESEARCH_SPEED_ALLOW_WITH_LIVE"  # "1" = allow parallel research next to a running live trader (default OFF)
LIVE_MARKERS = ("demo_trader.py", "supervisor.py")
THREAD_ENV_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
EXIT_BAD_CONFIG = 2
EXIT_NO_MEMORY = 4


class InsufficientMemoryError(RuntimeError):
    """Free memory is below ``reserve + one worker``: the research run must not start (fail closed)."""


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
    """Effective worker count: min(requested, MAX_WORKERS, n_tasks, what the free memory allows).

    FAIL CLOSED: known free memory below ``reserve + per_worker`` raises ``InsufficientMemoryError`` (never rounded up to one worker); unknown free memory => 1 worker.
    ``reserve`` must be >= 0 and ``per_worker`` >= ``MIN_PER_WORKER_MB`` (ValueError otherwise).

    The reserve defaults to ``DEFAULT_RESERVE_MB`` and can be set with the env var ``RESEARCH_SPEED_RESERVE_MB``
    (the per-worker estimate with ``RESEARCH_SPEED_PER_WORKER_MB``; both are for controlled benchmarks). ``avail_mb`` is injectable for tests.
    """
    if requested < 1:
        raise ValueError("jobs must be >= 1")
    if per_worker_mb is None:
        per_worker_mb = float(os.environ.get("RESEARCH_SPEED_PER_WORKER_MB", DEFAULT_PER_WORKER_MB))
    if reserve_mb is None:
        reserve_mb = float(os.environ.get("RESEARCH_SPEED_RESERVE_MB", DEFAULT_RESERVE_MB))
    if reserve_mb < 0:
        raise ValueError(f"reserve must be >= 0 MB (got {reserve_mb})")
    if per_worker_mb < MIN_PER_WORKER_MB:
        raise ValueError(f"per-worker estimate must be >= {MIN_PER_WORKER_MB} MB (got {per_worker_mb})")
    n = min(requested, MAX_WORKERS, max(1, n_tasks))
    mem = available_memory_mb() if avail_mb is None else avail_mb
    if mem is None:
        return 1  # unknown free memory: the most conservative count
    if mem < reserve_mb + per_worker_mb:
        raise InsufficientMemoryError(
            f"only {mem:.0f} MB free, need reserve {reserve_mb:.0f} MB + one worker {per_worker_mb:.0f} MB: not starting a research run"
        )
    return max(1, min(n, int((mem - reserve_mb) // per_worker_mb)))


def kill_pool(ex: ProcessPoolExecutor) -> None:
    """Stop a pool NOW: cancel queued work and terminate the worker processes (a plain ``shutdown(wait=False)`` leaves running tasks alive)."""
    procs = list((getattr(ex, "_processes", None) or {}).values())
    with suppress(Exception):
        ex.shutdown(wait=False, cancel_futures=True)
    for p in procs:
        with suppress(Exception):
            if p.is_alive():
                p.terminate()
    for p in procs:
        with suppress(Exception):
            p.join(timeout=5)


@contextmanager
def managed_pool(
    max_workers: int, initializer: Callable[..., None] | None = None, initargs: tuple[Any, ...] = ()
) -> Iterator[ProcessPoolExecutor]:
    """``ProcessPoolExecutor`` that never leaves orphan workers: on ANY exit by exception (including KeyboardInterrupt) the queue is cancelled and the workers are terminated."""
    ex = ProcessPoolExecutor(max_workers=max_workers, initializer=initializer, initargs=initargs)
    try:
        yield ex
    except BaseException:
        kill_pool(ex)
        raise
    else:
        ex.shutdown(wait=True)


def ordered_map[T, R](fn: Callable[[T], R], items: Sequence[T], jobs: int) -> list[R]:
    """``[fn(x) for x in items]`` with up to ``jobs`` worker processes; the result order is the input order."""
    if jobs <= 1 or len(items) <= 1:
        return [fn(x) for x in items]
    with managed_pool(clamp_jobs(jobs, len(items))) as ex:
        return list(ex.map(fn, items))


# ---------------------------------------------------------------------------------------------- process hardening (never starve the live trader)
_JOB_HANDLE: Any = None  # kept alive for the whole process: closing the handle (= process exit) kills every worker in the job


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.SetPriorityClass.restype = wintypes.BOOL
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    return k


def _install_kill_on_close_job(k32: Any) -> bool:
    """Put this process (and thereby every child it spawns) into a Windows job object with KILL_ON_JOB_CLOSE: workers die with the parent, however it ends."""
    global _JOB_HANDLE
    import ctypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_ulong),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        return False
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # 9 = JobObjectExtendedLimitInformation
        return False
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        return False
    _JOB_HANDLE = job
    return True


def _python_cmdlines() -> list[str]:
    """Command lines of all running python processes (Windows: CIM; POSIX: ps). Raises on any failure (the caller is conservative)."""
    if sys.platform == "win32":
        ps = "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | ForEach-Object { $_.CommandLine }"
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=30, check=True)
        return [ln for ln in r.stdout.splitlines() if ln.strip()]
    r = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=30, check=True)
    return [ln for ln in r.stdout.splitlines() if "python" in ln]


def live_trader_running(cmdlines: Callable[[], list[str]] | None = None) -> bool:
    """True when a python process whose command line names ``demo_trader.py`` or ``supervisor.py`` runs (no file / artifacts access, only the process table).
    Raises when the process table cannot be read."""
    lines = (cmdlines or _python_cmdlines)()
    return any(m in ln for ln in lines for m in LIVE_MARKERS)


def _note(msg: str) -> None:
    print(f"[research_speed] {msg}", file=sys.stderr, flush=True)


def harden_process(
    jobs: int = 1,
    *,
    avail_mb: float | None = None,
    per_worker_mb: float | None = None,
    reserve_mb: float | None = None,
    cmdlines: Callable[[], list[str]] | None = None,
    env: MutableMapping[str, str] | None = None,
    kernel32: Any = None,
    platform: str | None = None,
) -> int:
    """Call FIRST in every research entry point. Never starve the live trader: research runs are NEVER to be started next to the live runner.

    * Windows: BELOW_NORMAL priority class (children inherit) and a KILL_ON_JOB_CLOSE job object (workers die with this process);
    * OMP/OPENBLAS/MKL/NUMEXPR threads = 1 before any pool exists (children inherit the environment);
    * FAIL CLOSED: free memory below ``reserve + one worker`` => ``SystemExit(EXIT_NO_MEMORY)``, nothing started; invalid reserve / per-worker => ``SystemExit(EXIT_BAD_CONFIG)``;
    * a running live trader (``demo_trader.py`` / ``supervisor.py`` python process) or an unreadable process table => at most 1 job, unless ``RESEARCH_SPEED_ALLOW_WITH_LIVE=1``.

    Returns the maximum number of worker processes the caller may use (<= ``jobs``).
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    for k in THREAD_ENV_VARS:
        env[k] = "1"
    if platform == "win32":
        try:
            k32 = kernel32 if kernel32 is not None else _kernel32()
            if not k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS):
                _note("could not lower the process priority")
            if not _install_kill_on_close_job(k32):
                _note("could not create the kill-on-close job object (workers may outlive this process)")
        except Exception as e:  # never let the hardening itself be the failure; the memory / live checks below still apply
            _note(f"process hardening (priority / job object) unavailable: {e!r}")
    try:
        n = clamp_jobs(max(1, jobs), MAX_WORKERS, per_worker_mb=per_worker_mb, reserve_mb=reserve_mb, avail_mb=avail_mb)
    except ValueError as e:
        _note(f"invalid memory-guard configuration: {e}")
        raise SystemExit(EXIT_BAD_CONFIG) from None
    except InsufficientMemoryError as e:
        _note(f"{e}")
        raise SystemExit(EXIT_NO_MEMORY) from None
    n = min(n, max(1, jobs))
    if env.get(ALLOW_LIVE_ENV) != "1":
        try:
            live = live_trader_running(cmdlines)
        except Exception as e:
            live = True
            _note(f"live-process check failed ({e!r}): assuming a live trader")
        if live:
            if n > 1 or jobs > 1:
                _note(f"live trader detected (or unknown): jobs forced to 1 (override only with {ALLOW_LIVE_ENV}=1). Never run research next to the live runner.")
            n = 1
    return n


def describe(requested: int, n_tasks: int) -> dict[str, Any]:
    """Snapshot for logs / manifests: what was asked, what is used, why."""
    mem = available_memory_mb()
    try:
        effective = clamp_jobs(requested, n_tasks, avail_mb=mem) if mem is not None else 1
    except InsufficientMemoryError:
        effective = 0
    return {
        "requested": requested,
        "effective": effective,
        "max_workers": MAX_WORKERS,
        "available_mb": mem,
    }
