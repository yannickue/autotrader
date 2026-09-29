"""The minimal, isolated MT5 IPC probe: initialize -> terminal_info ->
account_info -> shutdown, nothing else.

`run_isolated_probe` is pure/testable (takes an injected `MT5ClientProtocol`
-- a `testing.FakeMT5Client` in tests, `real_client.get_real_client()` in
`scripts/mt5_ipc_probe.py`). It never raises -- every failure path is
captured into a `ProbeReport` with a specific `ConnectionDiagnosticCategory`
(see `diagnostics.py`), and `shutdown()` is always attempted in a `finally`
block regardless of how far initialize/terminal_info/account_info got.

This function itself does not enforce a timeout -- callers that need one
(anything running in the parent/main process) must run it inside a
subprocess bounded by `bounded.run_worker_bounded`, since a blocking C
extension call cannot be interrupted from within the same process.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from adapters.activtrades_mt5.client import MT5ClientProtocol
from adapters.activtrades_mt5.diagnostics import (
    ConnectionDiagnosticCategory,
    classify_account_info_failure,
    classify_initialize_failure,
)
from adapters.activtrades_mt5.lock import DEFAULT_LOCK_PATH, acquire_mt5_lock
from adapters.config import MT5ConnectionConfig


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeReport:
    """Outcome of `run_isolated_probe`. Never carries a password."""

    success: bool
    category: ConnectionDiagnosticCategory | None
    message: str
    mt5_error_code: int | None = None
    mt5_error_description: str | None = None
    package_version: str | None = None
    terminal_info_ok: bool = False
    account_info_ok: bool = False


def check_terminal_path(terminal_path: str | None) -> ConnectionDiagnosticCategory | None:
    """Pre-flight check MT5 itself cannot report a distinct error code for:
    an explicitly configured terminal path that doesn't exist on disk at
    all. Returns `None` (no problem detected) when `terminal_path` is unset
    -- MT5 locates a running/default terminal itself in that case."""
    if terminal_path is None:
        return None
    if not Path(terminal_path).is_file():
        return ConnectionDiagnosticCategory.TERMINAL_NOT_FOUND
    return None


def run_isolated_probe(
    config: MT5ConnectionConfig,
    client: MT5ClientProtocol,
    *,
    lock_path: Path | None = None,
) -> ProbeReport:
    """ACCOUNT PROTECTION INVARIANT (mirrors `MT5Connection.connect`, do not
    weaken without an explicit, separate directive): by default
    (`config.allow_account_login is False`), this only ATTACHES to whatever
    account is already logged into the terminal (`initialize(terminal_path)`,
    no credentials) and verifies its identity against `config.login`, failing
    closed on any mismatch -- it never authenticates and never can switch or
    log out the terminal's current account. Real authentication only happens
    when `config.allow_account_login` is explicitly `True` (`MT5_ALLOW_
    ACCOUNT_LOGIN=1`). Also acquires the single-owner MT5 lock
    (`lock.py`) before any real call -- a second concurrent caller gets
    `CONNECTION_BUSY` immediately rather than racing this one.
    """
    path_problem = check_terminal_path(config.terminal_path)
    if path_problem is not None:
        return ProbeReport(
            success=False,
            category=path_problem,
            message=f"configured MT5_TERMINAL_PATH does not exist: {config.terminal_path!r}",
        )

    lock = acquire_mt5_lock(lock_path if lock_path is not None else DEFAULT_LOCK_PATH)
    if lock is None:
        return ProbeReport(
            success=False,
            category=ConnectionDiagnosticCategory.CONNECTION_BUSY,
            message=(
                "MT5_CONNECTION_BUSY: another process already holds the real MT5 "
                "connection lock"
            ),
        )

    try:
        package_version: str | None = None
        try:
            version_info = client.version()
            if version_info is not None:
                package_version = str(version_info)
        except Exception:
            package_version = None

        initialized = False
        try:
            try:
                if config.allow_account_login:
                    initialized = bool(
                        client.initialize(
                            config.terminal_path,
                            login=config.login,
                            password=config.password,
                            server=config.server,
                        )
                    )
                else:
                    initialized = bool(client.initialize(config.terminal_path))
            except Exception as exc:
                return ProbeReport(
                    success=False,
                    category=ConnectionDiagnosticCategory.MT5_INITIALIZE_FAILED,
                    message=f"initialize() raised: {exc}",
                    package_version=package_version,
                )

            if not initialized:
                error_code, error_description = _safe_last_error(client)
                return ProbeReport(
                    success=False,
                    category=classify_initialize_failure(error_code),
                    message="initialize() returned False",
                    mt5_error_code=error_code,
                    mt5_error_description=error_description,
                    package_version=package_version,
                )

            terminal_info_ok = False
            try:
                terminal_info_ok = client.terminal_info() is not None
            except Exception:
                terminal_info_ok = False

            account_info_ok = False
            account_error_code: int | None = None
            account_error_description: str | None = None
            account_raw = None
            try:
                account_raw = client.account_info()
                account_info_ok = account_raw is not None
            except Exception as exc:
                account_info_ok = False
                account_error_description = str(exc)
            if not account_info_ok and account_error_description is None:
                account_error_code, account_error_description = _safe_last_error(client)

            if not account_info_ok:
                return ProbeReport(
                    success=False,
                    category=classify_account_info_failure(account_error_code),
                    message=(
                        "account_info() returned None or raised after a successful initialize()"
                    ),
                    mt5_error_code=account_error_code,
                    mt5_error_description=account_error_description,
                    package_version=package_version,
                    terminal_info_ok=terminal_info_ok,
                )

            if not config.allow_account_login:
                observed_login = getattr(account_raw, "login", None)
                if observed_login != config.login:
                    return ProbeReport(
                        success=False,
                        category=ConnectionDiagnosticCategory.ACCOUNT_MISMATCH,
                        message=(
                            f"MT5_ACCOUNT_MISMATCH: expected login {config.login}, the "
                            f"terminal is currently attached to login {observed_login!r} "
                            "instead -- never logging in automatically to correct this"
                        ),
                        package_version=package_version,
                        terminal_info_ok=terminal_info_ok,
                        account_info_ok=True,
                    )

            return ProbeReport(
                success=True,
                category=None,
                message="initialize/terminal_info/account_info all succeeded",
                package_version=package_version,
                terminal_info_ok=terminal_info_ok,
                account_info_ok=True,
            )
        finally:
            if initialized:
                with contextlib.suppress(Exception):
                    client.shutdown()
    finally:
        lock.release()


def _safe_last_error(client: MT5ClientProtocol) -> tuple[int | None, str | None]:
    try:
        code, description = client.last_error()
        return int(code), str(description)
    except Exception:
        return None, None


def env_terminal_running_hint() -> bool:
    """Best-effort, read-only hint (never authoritative) for whether an MT5
    terminal process is running, used only for diagnostic reporting -- never
    to gate a decision. Returns `False` (unknown/not detected) if the check
    itself is unsupported on this platform rather than raising."""
    if os.name != "nt":
        return False
    try:
        import subprocess

        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "terminal64.exe" in result.stdout
    except Exception:
        return False
