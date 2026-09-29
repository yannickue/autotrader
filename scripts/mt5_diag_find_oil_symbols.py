"""Narrowly-scoped, TEMPORARY diagnostic: search all broker symbols for
anything plausibly matching WTI/crude oil, since automatic alias matching
did not confidently resolve it.

Read-only (symbols_get() only). Does NOT touch Risk/Execution/Research.

Usage:
    <PY> scripts/mt5_diag_find_oil_symbols.py
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
    default_staged_timeout_seconds,
    run_worker_bounded,
)
from adapters.activtrades_mt5.connection import MT5Connection  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_mt5_connection_config  # noqa: E402

_KEYWORDS = ("OIL", "WTI", "CRUDE", "CL", "BRENT", "XTI", "USO")


def _run_worker() -> int:
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
        symbols = connection.client.symbols_get()
        if not symbols:
            print("symbols_get() returned no symbols")
            return 1

        print(f"Total broker symbols: {len(symbols)}")
        print("Candidates matching oil-related keywords:")
        print(f"{'SYMBOL':<20}{'DESCRIPTION':<50}")
        found = 0
        for s in symbols:
            name = str(getattr(s, "name", ""))
            description = str(getattr(s, "description", ""))
            haystack = f"{name} {description}".upper()
            if any(keyword in haystack for keyword in _KEYWORDS):
                print(f"{name:<20}{description[:48]:<50}")
                found += 1
        if found == 0:
            print("(no matches found for any keyword)")
        return 0
    finally:
        connection.disconnect()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if "--worker" in argv:
        return _run_worker()

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    timeout = default_staged_timeout_seconds()
    result = run_worker_bounded(Path(__file__), config, timeout=timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        print(f"TIMED OUT after {timeout}s", file=sys.stderr)
        return 1
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
