"""Discover and report broker symbol metadata for the configured CFD instruments.

Once the MT5 adapter (owned by a parallel worker, not yet built) and
`src/instruments` (a parallel worker's CFD `InstrumentSpec` module) exist,
this script will output a table with one row per canonical instrument
(e.g. DAX/NASDAQ100/WTI), each carrying: canonical symbol, broker symbol,
description, trade mode, point, tick size, tick value, contract size, min
volume, volume step, bid, ask, spread, and a mapping status (e.g.
VERIFIED/UNVERIFIED) -- without ever printing the account password. Today it
only proves the config/CLI plumbing works: it loads and validates the MT5
connection env vars (see `.env.example`) and reports clearly that the actual
symbol discovery call is not yet implemented.

Usage:
    <PY> scripts/mt5_symbol_discovery.py
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


def _discover_symbols(config: MT5ConnectionConfig) -> None:
    """TODO(mt5-adapter): once a real MT5 client and `src/instruments`
    `InstrumentSpec`s exist, replace this with an actual symbol-discovery
    table: canonical/broker-symbol/description/trade-mode/point/tick-size/
    tick-value/contract-size/min-vol/vol-step/bid/ask/spread/status. The
    password (`config.password`) must never be printed or logged.
    """
    raise NotImplementedError("MT5 adapter / instrument mapping not yet implemented")


def main(argv: list[str] | None = None) -> int:
    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    print(f"Config loaded: server={config.server!r}, login={config.login}.")
    try:
        _discover_symbols(config)
    except NotImplementedError:
        print(
            "NOT YET IMPLEMENTED -- adapter pending: the MT5 adapter and CFD instrument "
            "mapping do not exist yet in this repo. Once they land, this script will print "
            "a table of canonical/broker-symbol/description/trade-mode/point/tick-size/"
            "tick-value/contract-size/min-vol/vol-step/bid/ask/spread/status here."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
