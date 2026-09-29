"""Read-only: infer MT5 server-time DST behaviour from session boundaries.

For sample weeks in winter/summer/around DST transitions, print per-day first
and last M5 bar time-of-day in SERVER-epoch terms (time field taken as naive
wall clock). If the boundaries stay constant across DST changes, the server
clock follows a local (DST-observing) zone; if they shift by 1h, it is fixed.
Requests are made with server-epoch-shifted datetimes so ranges are not clipped.
"""

from __future__ import annotations

import sys
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
from adapters.activtrades_mt5.history import ServerTimePolicy  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_attach_only_config  # noqa: E402

WEEKS = [
    "2026-01-12",
    "2026-03-16",
    "2026-03-23",
    "2026-03-30",
    "2026-04-06",
    "2026-09-21",
    "2026-10-12",
    "2025-10-20",
    "2025-10-27",
    "2025-11-03",
]


def _worker() -> int:
    try:
        config = load_attach_only_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    conn = MT5Connection(get_real_client())
    r = conn.connect(config)
    if not r.success:
        print("CONNECT FAIL", r.reason)
        return 1
    try:
        c = conn.client
        for w in WEEKS:
            start = datetime.fromisoformat(w).replace(tzinfo=UTC)
            pol = ServerTimePolicy()
            rates = c.copy_rates_range(
                "Ger40",
                5,
                pol.utc_to_request_datetime(start),
                pol.utc_to_request_datetime(start + timedelta(days=7)),
            )
            if rates is None or len(rates) == 0:
                print(w, "NO DATA", c.last_error())
                continue
            days: dict = {}
            for row in rates:
                d = datetime.fromtimestamp(int(row["time"]), tz=UTC)
                k = d.date()
                lo, hi = days.get(k, (d, d))
                days[k] = (min(lo, d), max(hi, d))
            print(w, "bars", len(rates))
            for k in sorted(days):
                lo, hi = days[k]
                print(
                    "   ",
                    k,
                    k.strftime("%a"),
                    "first",
                    lo.strftime("%H:%M"),
                    "last",
                    hi.strftime("%H:%M"),
                )
        return 0
    finally:
        conn.disconnect()


def main() -> int:
    if "--worker" in sys.argv:
        return _worker()
    try:
        config = load_attach_only_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    t = default_staged_timeout_seconds()
    res = run_worker_bounded(Path(__file__), config, timeout=t, extra_args=())
    sys.stdout.write(res.stdout)
    sys.stderr.write(res.stderr)
    return res.returncode if res.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
