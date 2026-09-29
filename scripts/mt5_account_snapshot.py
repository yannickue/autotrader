"""Report a point-in-time MT5/ActivTrades account snapshot.

Once the MT5 adapter (owned by a parallel worker, not yet built) exists, this
script will report: balance, equity, margin, free margin, margin level,
account currency, open-position count, open-order count, and
reconciliation status against locally persisted state -- without ever
printing the account password. Today it only proves the config/CLI plumbing
works: it loads and validates the MT5 connection env vars (see
`.env.example`) and reports clearly that the actual account snapshot call is
not yet implemented.

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
    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    print(f"Config loaded: server={config.server!r}, login={config.login}.")
    return _fetch_snapshot(config)


if __name__ == "__main__":
    sys.exit(main())
