"""Read-only C3 observation: symbol resolution, full symbol_info, server-time
offset vs true UTC, and a small M1/M5 range sample. Attach-only, bounded,
single-owner locked (same worker pattern as mt5_diag_inspect_rates_ticks.py).

Usage: <PY> scripts/mt5_diag_c3_observe.py [SYMBOL]
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from adapters.activtrades_mt5.bounded import (  # noqa: E402
    default_staged_timeout_seconds,
    run_worker_bounded,
)
from adapters.activtrades_mt5.connection import MT5Connection  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_mt5_connection_config  # noqa: E402


def _run_worker() -> int:
    argv = sys.argv[1:]
    idx = argv.index("--worker")
    symbol = argv[idx + 1] if len(argv) > idx + 1 else "Ger40"
    config = load_mt5_connection_config()
    connection = MT5Connection(get_real_client())
    result = connection.connect(config)
    if not result.success:
        print(f"CONNECT: FAIL -- {result.reason}")
        return 1
    try:
        c = connection.client
        print("=== symbols matching *GER*/*DAX*/*DE4* ===")
        for grp in ("*GER*", "*Ger*", "*DAX*", "*DE40*", "*DE4*"):
            found = c.symbols_get(grp) or ()
            print(grp, [s.name for s in found])
        info = c.symbol_info(symbol)
        print("=== symbol_info ===")
        print(json.dumps(info._asdict(), default=str, indent=1) if info else None)
        tick = c.symbol_info_tick(symbol)
        sys_now = time.time()
        print("=== server time vs system UTC ===")
        print("system_utc_epoch", sys_now)
        print("tick", tick)
        if tick:
            print("tick.time_msc/1000 - system_utc (s):", tick.time_msc / 1000 - sys_now)
        print("terminal_info.time? ", getattr(c.terminal_info(), "_asdict", lambda: {})())
        now = datetime.now(UTC)
        for tf, name in ((1, "M1"), (5, "M5")):
            rates = c.copy_rates_range(symbol, tf, now - timedelta(days=3), now)
            print(
                f"=== copy_rates_range {name} last 3d ===",
                None if rates is None else len(rates),
                c.last_error(),
            )
            if rates is not None and len(rates):
                print("first", rates[0], "last", rates[-1])
        ticks = c.copy_ticks_range(symbol, now - timedelta(hours=1), now, -1)
        print("=== copy_ticks_range 1h ===", None if ticks is None else len(ticks), c.last_error())
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
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
