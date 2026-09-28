"""Report MT5/ActivTrades terminal connection health.

Once the MT5 adapter (owned by a parallel worker, not yet built) exists, this
script will connect to the configured terminal/account and report: terminal
path, account number, server, account currency, equity, margin, open-symbol
count, and overall connection health -- without ever printing the account
password. Today it only proves the config/CLI plumbing works: it loads and
validates `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER`/`MT5_TERMINAL_PATH` from the
environment (see `.env.example`) and reports clearly that the actual MT5
connection call is not yet implemented.

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

from adapters.config import (  # noqa: E402
    MT5ConfigError,
    MT5ConnectionConfig,
    load_mt5_connection_config,
)


def _connect_and_report(config: MT5ConnectionConfig) -> None:
    """TODO(mt5-adapter): once a real MT5 client exists in `src/adapters`,
    replace this with an actual terminal connection and report of
    terminal-path/account/server/currency/equity/margin/symbol-count/health.
    The password (`config.password`) must never be printed or logged.
    """
    raise NotImplementedError("MT5 adapter not yet implemented")


def main(argv: list[str] | None = None) -> int:
    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    print(
        f"Config loaded: server={config.server!r}, login={config.login}, "
        f"terminal_path={config.terminal_path or '<default>'!r}."
    )
    try:
        _connect_and_report(config)
    except NotImplementedError:
        print(
            "NOT YET IMPLEMENTED -- adapter pending: the MT5 adapter does not exist yet "
            "in this repo. Once it lands, this script will report terminal/account/server/"
            "currency/equity/margin/symbol-count/health here (never the password)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
