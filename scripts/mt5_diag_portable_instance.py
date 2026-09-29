"""Narrowly-scoped, TEMPORARY diagnostic: does a clean, isolated, portable
MT5 terminal instance avoid the -10005 (MT5_IPC_TIMEOUT) seen against the
existing ActivTrades installation?

Does NOT touch Risk/Execution/Research and does NOT change the permanent
adapter architecture. Uses a SEPARATE, already-copied installation at
C:\\MT5\\ActivTradesBot (copied from the real ActivTrades install, which
this script never touches) with `portable=True`, so MT5 stores its
config/data alongside the exe instead of in AppData -- fully isolated from
the existing installation's profile, data, and any running instance of it.

TEST: `initialize(portable_terminal_path, portable=True, timeout=...)`,
deliberately WITHOUT login/password/server first (mirrors
`mt5_diag_attach_then_login.py`'s Test 1), to isolate whether a clean
instance can even attach over IPC at all.

SAFETY: same bounded-subprocess pattern as every other mt5_*.py script here
(`adapters.activtrades_mt5.bounded`) -- cannot hang the main process, only
the disposable child is killed on timeout.

Usage:
    <PY> scripts/mt5_diag_portable_instance.py
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"

for path in (str(REPO_ROOT), str(SRC_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from adapters.activtrades_mt5.bounded import (  # noqa: E402
    default_staged_timeout_seconds,
    run_worker_bounded,
)
from adapters.activtrades_mt5.diagnostics import classify_initialize_failure  # noqa: E402
from adapters.activtrades_mt5.lock import acquire_mt5_lock  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_mt5_connection_config  # noqa: E402

PORTABLE_TERMINAL_PATH = r"C:\MT5\ActivTradesBot\terminal64.exe"
_MT5_INITIALIZE_TIMEOUT_MS = 15000


def _safe_last_error(client) -> tuple[int | None, str | None]:
    try:
        code, description = client.last_error()
        return int(code), str(description)
    except Exception:
        return None, None


def _run_worker() -> int:
    if not Path(PORTABLE_TERMINAL_PATH).is_file():
        print("PORTABLE_ATTACH: FAIL")
        print(f"reason: TERMINAL_NOT_FOUND: {PORTABLE_TERMINAL_PATH!r} does not exist")
        return 1

    # ACCOUNT PROTECTION INVARIANT: every real initialize() call in this
    # codebase is single-owner-locked (see `adapters/activtrades_mt5/
    # lock.py`), including this diagnostic's attach-only call against a
    # separate portable instance -- a second real MT5-touching process must
    # never overlap this one.
    lock = acquire_mt5_lock()
    if lock is None:
        print("PORTABLE_ATTACH: FAIL")
        print("reason: MT5_CONNECTION_BUSY: another process already holds the real MT5 lock")
        return 1

    client = get_real_client()
    attached = False
    try:
        try:
            attached = bool(
                client.initialize(
                    PORTABLE_TERMINAL_PATH,
                    portable=True,
                    timeout=_MT5_INITIALIZE_TIMEOUT_MS,
                )
            )
        except Exception as exc:
            print("PORTABLE_ATTACH: FAIL")
            print(f"reason: MT5_INITIALIZE_RAISED: {exc}")
            return 1

        if not attached:
            code, description = _safe_last_error(client)
            category = classify_initialize_failure(code)
            print("PORTABLE_ATTACH: FAIL")
            print(f"LAST ERROR: code={code} description={description} category={category.value}")
            return 1

        print("PORTABLE_ATTACH: PASS")
        try:
            version_info = client.version()
        except Exception:
            version_info = None
        print(f"TERMINAL VERSION: {version_info if version_info is not None else 'unavailable'}")
        try:
            terminal_ok = client.terminal_info() is not None
        except Exception:
            terminal_ok = False
        print(f"terminal_info available: {terminal_ok}")
        return 0
    finally:
        if attached:
            with contextlib.suppress(Exception):
                client.shutdown()
        lock.release()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if "--worker" in argv:
        return _run_worker()

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError:
        # This diagnostic doesn't need real MT5_* config (it deliberately
        # attaches without login/password/server), but bounded.run_worker_bounded
        # needs a config object to build the child's environment from -- a
        # harmless placeholder is fine since Test 1 never uses these fields.
        from adapters.config import MT5ConnectionConfig

        config = MT5ConnectionConfig(login=0, password="", server="", terminal_path=None)

    timeout = default_staged_timeout_seconds()
    result = run_worker_bounded(Path(__file__), config, timeout=timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        print("PORTABLE_ATTACH: FAIL")
        print(f"LAST ERROR: MT5_IPC_TIMEOUT (exceeded {timeout}s, child process terminated)")
        return 1
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
