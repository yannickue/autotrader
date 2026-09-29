"""Narrowly-scoped, TEMPORARY diagnostic: inspect the REAL structure
`copy_rates_from`/`copy_ticks_from` return from the connected terminal.

Read-only. Does NOT touch Risk/Execution/Research. Downloads only a tiny
sample (a handful of bars/ticks) purely to inspect field names/dtypes --
this is not the real historical downloader.

Usage:
    <PY> scripts/mt5_diag_inspect_rates_ticks.py [SYMBOL]
    (SYMBOL defaults to Ger40)
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
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

_TIMEFRAME_M1 = 1
_COPY_TICKS_ALL = -1


def _run_worker() -> int:
    argv = sys.argv[1:]
    worker_index = argv.index("--worker")
    symbol = argv[worker_index + 1] if len(argv) > worker_index + 1 else "Ger40"

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

    try:
        now = datetime.now(UTC)

        print(f"=== copy_rates_from({symbol!r}, TIMEFRAME_M1, now, 5) ===")
        rates = connection.client.copy_rates_from(symbol, _TIMEFRAME_M1, now, 5)
        if rates is None:
            print("copy_rates_from returned None")
        else:
            print(f"type: {type(rates)}")
            print(f"len: {len(rates)}")
            if hasattr(rates, "dtype"):
                print(f"dtype.names: {rates.dtype.names}")
                print(f"dtype: {rates.dtype}")
            if len(rates) > 0:
                row = rates[0]
                print(f"first row: {row}")
                print(f"first row type: {type(row)}")
                if hasattr(row, "dtype") and row.dtype.names:
                    for field in row.dtype.names:
                        print(f"  {field} = {row[field]!r} (python type: {type(row[field])})")

        print()
        print(f"=== copy_ticks_from({symbol!r}, now, 5, COPY_TICKS_ALL) ===")
        ticks = connection.client.copy_ticks_from(symbol, now, 5, _COPY_TICKS_ALL)
        if ticks is None:
            print("copy_ticks_from returned None")
        else:
            print(f"type: {type(ticks)}")
            print(f"len: {len(ticks)}")
            if hasattr(ticks, "dtype"):
                print(f"dtype.names: {ticks.dtype.names}")
                print(f"dtype: {ticks.dtype}")
            if len(ticks) > 0:
                row = ticks[0]
                print(f"first row: {row}")
                if hasattr(row, "dtype") and row.dtype.names:
                    for field in row.dtype.names:
                        print(f"  {field} = {row[field]!r} (python type: {type(row[field])})")

        return 0
    finally:
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
