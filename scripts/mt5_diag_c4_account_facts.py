"""Read-only: print NON-identifying account facts needed for C4 margin/leverage
assumptions (leverage, currency, margin mode, trade mode). Never prints login,
name, server or balances. Attach-only, bounded."""

from __future__ import annotations

import sys
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
from adapters.config import MT5ConfigError, load_attach_only_config  # noqa: E402

FIELDS = (
    "leverage",
    "currency",
    "margin_mode",
    "trade_mode",
    "margin_so_mode",
    "margin_so_call",
    "margin_so_so",
    "limit_orders",
    "currency_digits",
)


def _worker() -> int:
    config = load_attach_only_config()  # refuses MT5_ALLOW_ACCOUNT_LOGIN=1 before any client
    connection = MT5Connection(get_real_client())
    result = connection.connect(config)
    if not result.success:
        print("CONNECT FAIL", result.reason)
        return 1
    try:
        info = connection.client.account_info()
        for name in FIELDS:
            print(name, "=", getattr(info, name, "n/a"))
        return 0
    finally:
        connection.disconnect()


def main() -> int:
    if "--worker" in sys.argv:
        return _worker()
    try:
        config = load_attach_only_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    res = run_worker_bounded(
        Path(__file__), config, timeout=default_staged_timeout_seconds(), extra_args=()
    )
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    return res.returncode if res.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
