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

from adapters.config import (  # noqa: E402
    MT5ConfigError,
    MT5ConnectionConfig,
    load_mt5_connection_config,
)


def _fetch_snapshot(config: MT5ConnectionConfig) -> None:
    """TODO(mt5-adapter): once a real MT5 client exists in `src/adapters`,
    replace this with an actual account snapshot: balance/equity/margin/
    free-margin/margin-level/currency/position-count/order-count/
    reconciliation-status. The password (`config.password`) must never be
    printed or logged.
    """
    raise NotImplementedError("MT5 adapter not yet implemented")


def main(argv: list[str] | None = None) -> int:
    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    print(f"Config loaded: server={config.server!r}, login={config.login}.")
    try:
        _fetch_snapshot(config)
    except NotImplementedError:
        print(
            "NOT YET IMPLEMENTED -- adapter pending: the MT5 adapter does not exist yet "
            "in this repo. Once it lands, this script will report balance/equity/margin/"
            "free-margin/margin-level/currency/position-count/order-count/"
            "reconciliation-status here (never the password)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
