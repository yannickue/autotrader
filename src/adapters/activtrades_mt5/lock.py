"""Single-owner lock for real MT5 IPC access.

MT5 has exactly one current logged-in account per running terminal instance.
Two Python processes independently calling `initialize()`/`login()` against
that same terminal can race or step on each other's session -- this is the
account-protection invariant `MT5Connection.connect()` (`connection.py`)
enforces by acquiring this lock before ever touching the real client.

Deliberately simple: plain atomic file creation (`O_CREAT | O_EXCL`), not a
distributed-systems-grade lock -- this only needs to protect one developer's
machine against two of this repo's own processes overlapping, never against
untrusted or networked callers. A lock older than `_STALE_AFTER_SECONDS` is
treated as abandoned (e.g. a process that crashed without releasing it) and
cleared rather than wedging every future run forever.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path

DEFAULT_LOCK_PATH = Path(tempfile.gettempdir()) / "autotrader_mt5_connection.lock"

# A killed worker (`bounded.run_worker_bounded`'s `subprocess.run(...,
# timeout=...)` forcibly terminates a hung child via TerminateProcess on
# Windows) never runs its own `finally: lock.release()` -- so a genuine
# timeout leaks this lock file. Must be longer than the longest bounded
# timeout this codebase uses (45s, `bounded.default_staged_timeout_seconds`)
# with real margin, but short enough that a legitimate retry shortly after a
# timeout doesn't spuriously see CONNECTION_BUSY for minutes.
_STALE_AFTER_SECONDS = 90.0


class MT5Lock:
    """A held lock. Call `release()` exactly once when done (idempotent if
    called again, e.g. from both an error path and a later `finally`)."""

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd: int | None = fd

    @property
    def path(self) -> Path:
        return self._path

    def touch(self) -> bool:
        """Heartbeat for a LONG-RUNNING owner: refresh the lock mtime so it never looks stale
        (`_STALE_AFTER_SECONDS`). Returns False if the lock file is gone or now belongs to another
        process (ownership lost -> the caller must fail closed)."""
        if self._fd is None:
            return False
        try:
            if self._path.read_text(encoding="ascii").strip() != str(os.getpid()):
                return False
            os.utime(self._path)
        except (OSError, ValueError):
            return False
        return True

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
        with contextlib.suppress(OSError):
            self._path.unlink()


def acquire_mt5_lock(path: Path | None = None) -> MT5Lock | None:
    """Try to acquire the single-owner MT5 lock.

    Returns `None` (never raises) if another live holder already has it --
    callers MUST treat `None` as `ConnectionDiagnosticCategory.CONNECTION_BUSY`
    and must not attempt any real `initialize()`/`login()` call in that case.
    """
    lock_path = path if path is not None else DEFAULT_LOCK_PATH

    _clear_if_stale(lock_path)

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with contextlib.suppress(OSError):
        os.write(fd, str(os.getpid()).encode("ascii"))
    return MT5Lock(lock_path, fd)


def _clear_if_stale(lock_path: Path) -> None:
    try:
        age_seconds = time.time() - lock_path.stat().st_mtime
    except OSError:
        return  # does not exist (or unreadable) -- nothing to clear
    if age_seconds > _STALE_AFTER_SECONDS:
        with contextlib.suppress(OSError):
            lock_path.unlink()
