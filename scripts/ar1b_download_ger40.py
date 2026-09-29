"""AR1B: GER40 M5 multi-month history download -> one validated Parquet per calendar month.

READ-ONLY, attach-only (no login / account switching), bounded child process, DEMO only.
Every month is validated and persisted independently with its own provenance, so a
bad month cannot contaminate the others. Months that return no rows are reported and
skipped (no synthetic data). Writes a manifest of what was fetched.

Usage:
    <PY> scripts/ar1b_download_ger40.py [FIRST_YYYY-MM] [LAST_YYYY-MM] [OUT_DIR]
Default: 2024-01 .. 2026-08 into data/ar1_ger40.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
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
from adapters.activtrades_mt5.history_download import download_bars  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_attach_only_config  # noqa: E402
from data.historical import DatasetRejected  # noqa: E402

CANONICAL = "GER40"


def _months(first: str, last: str) -> list[tuple[datetime, datetime]]:
    y, m = (int(p) for p in first.split("-"))
    ly, lm = (int(p) for p in last.split("-"))
    out = []
    while (y, m) <= (ly, lm):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((datetime(y, m, 1, tzinfo=UTC), datetime(ny, nm, 1, tzinfo=UTC)))
        y, m = ny, nm
    return out


def _run_worker(first: str, last: str, out: Path) -> int:
    config = load_attach_only_config()
    connection = MT5Connection(get_real_client())
    result = connection.connect(config)
    if not result.success:
        print(f"CONNECT: FAIL -- {result.reason}")
        return 1
    try:
        client = connection.client
        account = client.account_info()
        if getattr(account, "trade_mode", None) != 0:
            print("ABORT: history download only runs against a verified DEMO account")
            return 1
        retrieved = datetime.now(UTC)
        policy = ServerTimePolicy()
        manifest = []
        for start, end in _months(first, last):
            label = start.strftime("%Y-%m")
            try:
                path, prov, report = download_bars(
                    client,
                    root=out,
                    canonical=CANONICAL,
                    mt5_timeframe=5,
                    start_utc=start,
                    end_utc=end,
                    retrieved_at=retrieved,
                    broker_account_kind="DEMO",
                    policy=policy,
                )
                entry = {
                    "month": label,
                    "path": str(path),
                    "rows": prov.row_count,
                    "status": str(report.status),
                    "expected_gaps": report.expected_gaps,
                    "suspicious_gaps": [
                        [g.start.isoformat(), g.end.isoformat()] for g in report.suspicious_gaps
                    ],
                    "summary": dict(report.summary),
                }
            except DatasetRejected as exc:
                entry = {"month": label, "status": "REJECTED", "reason": str(exc)[:300]}
            except Exception as exc:
                entry = {
                    "month": label,
                    "status": "ERROR",
                    "reason": f"{type(exc).__name__}: {exc}"[:300],
                }
            manifest.append(entry)
            print(json.dumps(entry, default=str))
        (out / "download_manifest.json").write_text(
            json.dumps(
                {"retrieved_at": retrieved.isoformat(), "months": manifest}, indent=1, default=str
            )
        )
        return 0
    finally:
        connection.disconnect()


def main() -> int:
    argv = [a for a in sys.argv[1:] if a != "--worker"]
    first = argv[0] if len(argv) > 0 else "2024-01"
    last = argv[1] if len(argv) > 1 else "2026-08"
    out = Path(argv[2]) if len(argv) > 2 else REPO_ROOT / "data" / "ar1_ger40"
    out.mkdir(parents=True, exist_ok=True)
    if "--worker" in sys.argv[1:]:
        return _run_worker(first, last, out)
    try:
        config = load_attach_only_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    timeout = default_staged_timeout_seconds() * 6
    result = run_worker_bounded(
        Path(__file__), config, timeout=timeout, extra_args=(first, last, str(out))
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        print(f"TIMED OUT after {timeout}s", file=sys.stderr)
        return 1
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
