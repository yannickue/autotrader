"""Report MT5/ActivTrades terminal connection health.

Connects to the configured terminal/account and reports: terminal path,
account number, server, account currency, equity, margin, open-symbol count,
and overall connection health -- without ever printing the account password.

SAFETY: the real MT5 call happens only inside a short-lived, hard-timeout-
bounded child process (see `adapters.activtrades_mt5.bounded`) -- this
script's own main process never blocks on a stuck MT5 IPC call. On timeout,
only the disposable child is killed; the user's MT5 terminal GUI is never
touched.

Usage:
    <PY> scripts/mt5_connection_check.py
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
from adapters.activtrades_mt5.connection import ConnectionState, MT5Connection  # noqa: E402
from adapters.activtrades_mt5.models import (  # noqa: E402
    account_info_to_account_state,
    terminal_info_to_terminal_state,
)
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import (  # noqa: E402
    MT5ConfigError,
    MT5ConnectionConfig,
    load_mt5_connection_config,
)


def _connect_and_report(config: MT5ConnectionConfig) -> int:
    """Connect to the configured MT5 terminal and report health.

    Never prints `config.password`. Returns a process exit code.
    """
    connection = MT5Connection(get_real_client())
    result = connection.connect(config)
    if not result.success:
        print(f"MT5 CONNECTION FAILED: {result.reason}", file=sys.stderr)
        if result.error_code is not None:
            print(
                f"  last_error: code={result.error_code} description={result.error_description}",
                file=sys.stderr,
            )
        return 1

    print("MT5 CONNECTED")
    try:
        terminal_raw = connection.client.terminal_info()
        account_raw = connection.client.account_info()
        symbols = connection.client.symbols_get()
    finally:
        connection.disconnect()

    if terminal_raw is not None:
        terminal = terminal_info_to_terminal_state(terminal_raw)
        print(f"  terminal: company={terminal.company!r} name={terminal.name!r}")
        print(f"  terminal connected={terminal.connected} trade_allowed={terminal.trade_allowed}")
    else:
        print("  terminal_info() returned None", file=sys.stderr)

    if account_raw is not None:
        account = account_info_to_account_state(account_raw)
        print(f"  server={config.server!r} login={config.login} is_demo={account.is_demo}")
        print(f"  currency={account.currency} equity={account.equity} margin={account.margin}")
    else:
        print("  account_info() returned None -- account state unreadable", file=sys.stderr)
        return 1

    symbol_count = len(symbols) if symbols is not None else 0
    print(f"  symbol_count={symbol_count}")
    print(f"  health: {ConnectionState.CONNECTED.value} (run mt5_preflight for full READY check)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if "--worker" in argv:
        print(
            f"Config loaded: server={config.server!r}, login={config.login}, "
            f"terminal_path={config.terminal_path or '<default>'!r}."
        )
        return _connect_and_report(config)

    # The bounded child (below) prints its own "Config loaded" line as part
    # of its relayed stdout -- no need to print it again here.
    timeout = default_probe_timeout_seconds()
    result = run_worker_bounded(Path(__file__), config, timeout=timeout)
    if result.timed_out:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        print(
            f"MT5 CONNECTION FAILED: MT5_IPC_TIMEOUT (exceeded {timeout}s, child terminated; "
            "the MT5 terminal itself was left untouched)",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
