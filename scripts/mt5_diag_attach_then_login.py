"""Narrowly-scoped, TEMPORARY diagnostic: separate local terminal IPC
attachment from account authentication.

Does NOT touch Risk/Execution/Research and does NOT change the adapter's
permanent connection architecture -- this exists only to answer one
question: does `MetaTrader5.initialize(path, login=, password=, server=)`
(the combined call every other script here uses) fail at the IPC-attach
step, or at the authentication step?

TEST 1 -- TERMINAL ATTACH ONLY: `initialize(terminal_path, timeout=...)`,
deliberately WITHOUT login/password/server (the terminal is expected to
already be open and manually logged in to the ActivTrades demo account).
On success: reports terminal_info/version availability and the REAL
server/account the already-running terminal is attached to (via
`account_info()`, read-only, no login() call needed since the terminal is
already authenticated) -- this is the "verify the exact broker server
string instead of assuming the configured one is canonical" check.

TEST 2 -- ACCOUNT LOGIN, only if Test 1 passed: `login(login, password=,
server=, timeout=...)`, using the exact configured credentials. Never
prints the password.

SAFETY: identical bounded-subprocess pattern to every other mt5_*.py script
here (`adapters.activtrades_mt5.bounded`) -- the main process can never
hang, only the disposable child is killed on timeout, the terminal GUI is
never touched.

Usage:
    <PY> scripts/mt5_diag_attach_then_login.py
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
from adapters.activtrades_mt5.models import account_info_to_account_state  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import (  # noqa: E402
    MT5ConfigError,
    load_mt5_connection_config,
)

# MetaTrader5.initialize()'s own internal IPC timeout, in milliseconds --
# distinct from (and smaller than) the outer subprocess bound below, so a
# genuinely stuck initialize() call still returns control to this worker
# (which can then call last_error()) rather than the whole child needing to
# be killed from outside for every case.
_MT5_INITIALIZE_TIMEOUT_MS = 15000


def _safe_last_error(client) -> tuple[int | None, str | None]:
    try:
        code, description = client.last_error()
        return int(code), str(description)
    except Exception:
        return None, None


def _run_worker() -> int:
    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"TERMINAL_ATTACH: FAIL\nreason: CONFIG_MISSING: {exc}")
        return 2

    client = get_real_client()
    attached = False
    login_ok: bool | None = None  # None = skipped

    try:
        # -- TEST 1: terminal attach only, no login/password/server --------
        try:
            attached = bool(
                client.initialize(config.terminal_path, timeout=_MT5_INITIALIZE_TIMEOUT_MS)
            )
        except Exception as exc:
            print("TERMINAL_ATTACH: FAIL")
            print(f"reason: MT5_INITIALIZE_RAISED: {exc}")
            print("ACCOUNT_LOGIN: SKIPPED")
            print("TERMINAL VERSION: unknown")
            print("SERVER OBSERVED: unknown")
            print("LAST ERROR: unknown (initialize() raised before last_error() could be read)")
            return 1

        if not attached:
            code, description = _safe_last_error(client)
            category = classify_initialize_failure(code)
            print("TERMINAL_ATTACH: FAIL")
            print("ACCOUNT_LOGIN: SKIPPED")
            print("TERMINAL VERSION: unknown")
            print("SERVER OBSERVED: unknown")
            print(
                f"LAST ERROR: code={code} description={description} "
                f"category={category.value}"
            )
            return 1

        print("TERMINAL_ATTACH: PASS")

        try:
            version_info = client.version()
        except Exception:
            version_info = None
        print(f"TERMINAL VERSION: {version_info if version_info is not None else 'unavailable'}")

        try:
            terminal_info_ok = client.terminal_info() is not None
        except Exception:
            terminal_info_ok = False
        print(f"terminal_info available: {terminal_info_ok}")

        # Read-only: the terminal is expected to already be logged in
        # manually, so this reports the REAL server/account it's actually
        # attached to -- never assume the configured MT5_SERVER string
        # matches without checking.
        server_observed = "unavailable (account_info() returned None -- terminal may not be "
        server_observed += "logged in to any account yet)"
        try:
            account_raw = client.account_info()
        except Exception:
            account_raw = None
        if account_raw is not None:
            account = account_info_to_account_state(account_raw)
            server_observed = (
                f"{account.currency} account, login={account.login}, "
                f"is_demo={account.is_demo}"
            )
            # The raw `server` field on MT5's AccountInfo is the actual
            # broker server string -- surface it directly and separately
            # from our own configured MT5_SERVER value.
            raw_server = getattr(account_raw, "server", None)
            if raw_server is not None:
                server_observed += f", server={raw_server!r}"
        print(f"SERVER OBSERVED: {server_observed}")
        if account_raw is not None:
            raw_server = getattr(account_raw, "server", None)
            if raw_server is not None and str(raw_server) != config.server:
                print(
                    f"NOTE: configured MT5_SERVER ({config.server!r}) does NOT match the "
                    f"terminal's actual connected server ({raw_server!r})"
                )

        # -- TEST 2: explicit account login, only because Test 1 passed ----
        try:
            login_ok = bool(
                client.login(
                    config.login,
                    password=config.password,
                    server=config.server,
                    timeout=_MT5_INITIALIZE_TIMEOUT_MS,
                )
            )
        except Exception as exc:
            print("ACCOUNT_LOGIN: FAIL")
            print(f"reason: MT5_LOGIN_RAISED: {type(exc).__name__}")
            login_ok = False
        else:
            if login_ok:
                print("ACCOUNT_LOGIN: PASS")
            else:
                code, description = _safe_last_error(client)
                category = classify_initialize_failure(code)
                print("ACCOUNT_LOGIN: FAIL")
                print(
                f"LAST ERROR: code={code} description={description} "
                f"category={category.value}"
            )

        return 0 if login_ok else 1
    finally:
        if attached:
            with contextlib.suppress(Exception):
                client.shutdown()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if "--worker" in argv:
        return _run_worker()

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"TERMINAL_ATTACH: FAIL\nreason: CONFIG_MISSING: {exc}", file=sys.stderr)
        return 2

    timeout = default_staged_timeout_seconds()
    result = run_worker_bounded(Path(__file__), config, timeout=timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        print("TERMINAL_ATTACH: FAIL")
        print("ACCOUNT_LOGIN: SKIPPED")
        print(f"LAST ERROR: MT5_IPC_TIMEOUT (exceeded {timeout}s, child process terminated)")
        return 1
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
