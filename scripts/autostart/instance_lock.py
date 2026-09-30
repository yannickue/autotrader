# ruff: noqa: E501
"""Per-artifacts-directory single-instance lock (PID + process creation time).

The lock is a small JSON file (``<artifacts>/runner.lock`` for the trader, ``supervisor.lock`` for the
supervisor).  A lock is *held* only while the recorded process is alive AND its creation time still
matches the recorded one (defends against PID reuse).  Anything else is a stale lock left by a crash
and is taken over.  Release only removes the file if it still names this process, so a crash-safe
take-over by a newer process is never undone.

Pure stdlib; Windows creation times come from ``GetProcessTimes`` via ctypes (no psutil dependency).
The liveness/creation-time probes are injectable so tests can use fake PIDs.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXIT_ALREADY_RUNNING = 9  # a second runner/supervisor on the same artifacts dir is refused
CTIME_TOLERANCE_S = 2.0

CreateTimeFn = Callable[[int], "float | None"]


def process_create_time(pid: int) -> float | None:
    """Process creation time as Unix seconds, or None if the process does not exist / is unreadable."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _win_create_time(pid)
    return _proc_create_time(pid)


def _win_create_time(pid: int) -> float | None:  # pragma: no cover - exercised on Windows only
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    process_query_limited_information = 0x1000
    handle = k32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return None
        if exited.dwLowDateTime or exited.dwHighDateTime:  # process already exited (handle still open)
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime  # 100 ns since 1601-01-01
        return ticks / 1e7 - 11644473600.0
    finally:
        k32.CloseHandle(handle)


def _proc_create_time(pid: int) -> float | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime"))
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        return boot + start_ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration, IndexError):
        return None


class LockHeld(Exception):
    """Another live process holds the lock."""

    def __init__(self, path: Path, holder: dict[str, Any]) -> None:
        self.path = path
        self.holder = holder
        super().__init__(
            f"{path.name} is held by live pid {holder.get('pid')} ({holder.get('role')}, "
            f"started {holder.get('started_utc')}) - refusing a second instance on {path.parent}"
        )


def read_lock(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None


def holder_alive(holder: dict[str, Any] | None, create_time: CreateTimeFn = process_create_time) -> bool:
    """True iff the recorded process exists and is the same process (creation time matches)."""
    if not holder:
        return False
    now_ct = create_time(int(holder["pid"]))
    if now_ct is None:
        return False
    recorded = holder.get("create_time")
    if not isinstance(recorded, (int, float)):
        return True  # unknown recorded start: a live pid is treated as held (fail closed)
    return abs(now_ct - float(recorded)) <= CTIME_TOLERANCE_S


def lock_status(path: Path, create_time: CreateTimeFn = process_create_time) -> dict[str, Any]:
    holder = read_lock(path)
    return {"exists": Path(path).exists(), "holder": holder, "alive": holder_alive(holder, create_time)}


class InstanceLock:
    def __init__(self, path: Path, role: str, *, pid: int | None = None,
                 create_time: CreateTimeFn = process_create_time) -> None:
        self.path = Path(path)
        self.role = role
        self.pid = os.getpid() if pid is None else pid
        self._create_time = create_time
        self.acquired = False

    def _payload(self) -> str:
        return json.dumps({
            "pid": self.pid,
            "create_time": self._create_time(self.pid),
            "role": self.role,
            "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        })

    def _fresh_file(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime < 1.0
        except OSError:
            return False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = read_lock(self.path)
                if holder is None and self._fresh_file():
                    time.sleep(0.2)  # a peer is between O_EXCL create and write: give it a moment
                    continue
                if holder_alive(holder, self._create_time):
                    raise LockHeld(self.path, holder or {}) from None
                # stale (dead pid, reused pid, or unreadable/partial file): take over, then retry O_EXCL
                with contextlib.suppress(OSError):
                    if read_lock(self.path) == holder:  # do not delete a lock a peer just re-created
                        self.path.unlink()
                time.sleep(0.05)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self._payload())
            self.acquired = True
            return
        raise LockHeld(self.path, read_lock(self.path) or {})

    def release(self) -> None:
        if not self.acquired:
            return
        self.acquired = False
        holder = read_lock(self.path)
        if holder is not None and holder.get("pid") == self.pid:
            with contextlib.suppress(OSError):
                self.path.unlink()

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
