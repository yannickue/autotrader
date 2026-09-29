"""The smallest possible reproducible MT5 IPC test.

Does ONLY: load config -> initialize() -> report package version ->
terminal_info() -> account_info() -> shutdown() -> exit. No symbol
discovery, no history, no order_check, no trading.

SAFETY: this script never lets a hung MT5 call block indefinitely. Invoked
normally (no `--worker` flag), it re-invokes itself as a child process with
`--worker` and a hard timeout (`adapters.activtrades_mt5.bounded`); if the
child exceeds the timeout, the PARENT kills only that disposable child
process (never the user's MT5 terminal GUI) and reports MT5_IPC_TIMEOUT
cleanly rather than hanging. The actual MT5 calls only ever happen inside
the bounded `--worker` child.

Usage:
    <PY> scripts/mt5_ipc_probe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"

for path in (str(REPO_ROOT), str(SRC_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from adapters.activtrades_mt5.bounded import (  # noqa: E402
    default_probe_timeout_seconds,
    run_worker_bounded,
)
from adapters.activtrades_mt5.probe import run_isolated_probe  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_mt5_connection_config  # noqa: E402


def _run_worker() -> int:
    """The actual, potentially-blocking MT5 work. Only ever runs inside the
    bounded child process spawned by the non-worker branch of `main()`."""
    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    print(f"Python: {sys.version.split()[0]}")
    report = run_isolated_probe(config, get_real_client())
    print(f"MetaTrader5 package: {report.package_version or 'unknown'}")
    print(f"Terminal: {'FOUND' if report.terminal_info_ok else 'unknown/not confirmed'}")

    if report.success:
        print("IPC initialize: PASS")
        print("terminal_info: PASS" if report.terminal_info_ok else "terminal_info: WARN")
        print("account_info: PASS")
        print("shutdown: PASS")
        return 0

    print("IPC initialize: FAIL")
    print(f"reason: {report.category.value if report.category else 'UNKNOWN'}")
    if report.mt5_error_code is not None:
        print(
            f"last_error: code={report.mt5_error_code} description={report.mt5_error_description}"
        )
    print(f"detail: {report.message}")
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if "--worker" in argv:
        return _run_worker()

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    timeout = default_probe_timeout_seconds()
    result = run_worker_bounded(Path(__file__), config, timeout=timeout)
    if result.timed_out:
        print(
            f"IPC initialize: FAIL\nreason: MT5_IPC_TIMEOUT\n"
            f"detail: probe exceeded {timeout}s, child process terminated "
            "(the MT5 terminal itself was left untouched)",
            file=sys.stderr,
        )
        return 1

    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
