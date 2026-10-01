# ruff: noqa: E501
"""Exclusive run lock for an output directory: two research runs must never write the same ``out`` at the same time.

``O_CREAT | O_EXCL`` creates ``<out>/_run.lock`` containing the owner pid. A lock whose owner process is dead is stale and is taken over;
a lock with a live owner (or an unreadable owner: conservative) refuses the second run. Known limit: a recycled pid of a long-dead owner looks alive (the run is refused, never silently doubled).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

EXIT_LOCKED = 5


class RunLockError(RuntimeError):
    """Another live process holds the lock."""


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied => exists; invalid parameter => gone
        try:
            code = wintypes.DWORD()
            k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunLock:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._held = False

    def _read_owner(self) -> int | None:
        try:
            return int(self.path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def acquire(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = self._read_owner()
                if owner is None or pid_alive(owner):
                    raise RunLockError(f"{self.path} is held by {'an unknown process' if owner is None else f'pid {owner}'}: another run uses this output directory") from None
                self.path.unlink(missing_ok=True)  # stale: the owner is dead
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            self._held = True
            return self
        raise RunLockError(f"could not acquire {self.path}")

    def release(self) -> None:
        if self._held:
            self._held = False
            if self._read_owner() == os.getpid():
                self.path.unlink(missing_ok=True)

    def __enter__(self) -> RunLock:
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()
