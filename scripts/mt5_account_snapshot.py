"""Report a point-in-time MT5/ActivTrades account snapshot.

Reports: balance, equity, margin, free margin, margin level, account
currency, open-position count, open-order count -- without ever printing the
account password.

SAFETY: the real MT5 call happens only inside a short-lived, hard-timeout-
bounded child process (see `adapters.activtrades_mt5.bounded`) -- this
script's own main process never blocks on a stuck MT5 IPC call.

Usage:
    <PY> scripts/mt5_account_snapshot.py
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
from adapters.activtrades_mt5.connection import MT5Connection  # noqa: E402
from adapters.activtrades_mt5.models import account_info_to_account_state  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import (  # noqa: E402
    MT5ConfigError,
    MT5ConnectionConfig,
    load_mt5_connection_config,
)


def _fetch_snapshot(config: MT5ConnectionConfig) -> int:
    """Connect and report a point-in-time account snapshot.

    Never prints `config.password`. Returns a process exit code.
    """
    connection = MT5Connection(get_real_client())
    result = connection.connect(config)
    if not result.success:
        print(f"MT5 CONNECTION FAILED: {result.reason}", file=sys.stderr)
        return 1

    try:
        account_raw = connection.client.account_info()
        positions = connection.client.positions_get()
        orders = connection.client.orders_get()
    finally:
        connection.disconnect()

    if account_raw is None:
        print("account_info() returned None -- account state unreadable", file=sys.stderr)
        return 1

    account = account_info_to_account_state(account_raw)
    print(f"currency={account.currency}")
    print(f"balance={account.balance} equity={account.equity}")
    print(f"margin={account.margin} free_margin={account.free_margin}")
    print(f"margin_level={account.margin_level}")
    print(f"is_demo={account.is_demo} trade_allowed={account.trade_allowed}")
    print(f"open_positions={len(positions) if positions is not None else 0}")
    print(f"open_orders={len(orders) if orders is not None else 0}")
    print(
        "reconciliation_status=NOT_CHECKED "
        "(run mt5_preflight.py for a full local-vs-broker reconciliation check)"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if "--worker" in argv:
        print(f"Config loaded: server={config.server!r}, login={config.login}.")
        return _fetch_snapshot(config)

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
