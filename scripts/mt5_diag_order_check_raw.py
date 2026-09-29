"""Narrowly-scoped, TEMPORARY diagnostic: print the RAW order_check()
retcode/comment for the DAX (Ger40) symbol, unfiltered by this codebase's
own success/failure interpretation.

Does NOT touch Risk/Execution/Research. `order_check()` never sends a real
order -- it is inherently a dry-run/simulation, so this is safe to run
against a real account.

Why this exists: `mt5_preflight.py`'s order_check stage reported FAIL with
comment "Done" for Ger40 -- "Done" is the human-readable text MT5
associates with TRADE_RETCODE_DONE (10009), which this codebase's own
OrderCheckResult treats as SUCCESS. Seeing FAIL with that comment means the
real numeric retcode is very likely something OTHER than 10009 despite the
matching comment text, or the success-classification logic has a bug --
this script prints the actual raw integer so we stop guessing.

SAFETY: same bounded-subprocess pattern as every other mt5_*.py script here.

Usage:
    <PY> scripts/mt5_diag_order_check_raw.py [SYMBOL]
    (SYMBOL defaults to Ger40)
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
from adapters.activtrades_mt5.connection import MT5Connection  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_mt5_connection_config  # noqa: E402

_TRADE_ACTION_DEAL = 1
_ORDER_TYPE_BUY = 0


def _run_worker() -> int:
    argv = sys.argv[1:]
    worker_index = argv.index("--worker")
    has_symbol_arg = len(argv) > worker_index + 1
    symbol = argv[worker_index + 1] if has_symbol_arg else "Ger40"

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    connection = MT5Connection(get_real_client())
    result = connection.connect(config)
    if not result.success:
        print(f"CONNECT: FAIL -- {result.reason}")
        return 1
    print("CONNECT: PASS")

    try:
        tick = connection.client.symbol_info_tick(symbol)
        info = connection.client.symbol_info(symbol)
        if tick is None or info is None:
            print(f"symbol_info/tick unavailable for {symbol!r}")
            return 1

        volume_min = getattr(info, "volume_min", 0.01)
        request = {
            "action": _TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume_min,
            "type": _ORDER_TYPE_BUY,
            "price": tick.ask,
        }
        print(f"order_check request: {request}")
        raw_result = connection.client.order_check(request)
        print(f"raw retcode: {getattr(raw_result, 'retcode', 'MISSING')}")
        print(f"raw comment: {getattr(raw_result, 'comment', 'MISSING')!r}")
        print(f"full raw result: {raw_result}")
        return 0
    finally:
        with contextlib.suppress(Exception):
            connection.disconnect()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    symbol = argv[0] if argv and not argv[0].startswith("--") else "Ger40"

    if "--worker" in argv:
        return _run_worker()

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    timeout = default_staged_timeout_seconds()
    result = run_worker_bounded(Path(__file__), config, timeout=timeout, extra_args=(symbol,))
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        print(f"TIMED OUT after {timeout}s", file=sys.stderr)
        return 1
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
