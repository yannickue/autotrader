"""C3 technical sample download: GER40 M1/M5 bars (+ short tick window) -> Parquet.

READ-ONLY, attach-only (no login, no account switching), bounded child
process, single-owner lock via `MT5Connection`. Also writes:
  - `<out>/ACTIVTRADES_MT5_CFD/GER40/symbol_info.json` (live symbol_info snapshot)
  - `<out>/raw_fixtures/*.csv` raw MT5 rows (server epochs) for offline tests.

Usage:
    <PY> scripts/mt5_download_sample.py [OUT_DIR]      (default: data/c3_sample)
"""

from __future__ import annotations

import csv
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
from adapters.activtrades_mt5.history import ServerTimePolicy, fetch_rates_range  # noqa: E402
from adapters.activtrades_mt5.history_download import download_bars, download_ticks  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_attach_only_config  # noqa: E402

# Fully closed sample week (Mon-Fri, CEST throughout, no DST transition).
WEEK_START = datetime(2026, 9, 21, tzinfo=UTC)
WEEK_END = datetime(2026, 9, 26, tzinfo=UTC)
TICK_START = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
TICK_END = datetime(2026, 9, 25, 8, 10, tzinfo=UTC)
CANONICAL = "GER40"


def _run_worker(out: Path) -> int:
    config = load_attach_only_config()
    connection = MT5Connection(get_real_client())
    result = connection.connect(config)
    if not result.success:
        print(f"CONNECT: FAIL -- {result.reason}")
        return 1
    try:
        client = connection.client
        account = client.account_info()
        kind = "DEMO" if getattr(account, "trade_mode", None) == 0 else "UNKNOWN_NOT_DEMO"
        print("account trade_mode:", getattr(account, "trade_mode", None), "->", kind)
        if kind != "DEMO":
            print("ABORT: C3 sample download only runs against a verified DEMO account")
            return 1
        retrieved = datetime.now(UTC)
        policy = ServerTimePolicy()

        info = client.symbol_info("Ger40")
        snap = out / "ACTIVTRADES_MT5_CFD" / CANONICAL / "symbol_info.json"
        snap.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "retrieved_at": retrieved.isoformat(),
            "account_kind": kind,
            "symbol_info": info._asdict(),
        }
        snap.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str))

        for tf in (1, 5):
            path, prov, report = download_bars(
                client,
                root=out,
                canonical=CANONICAL,
                mt5_timeframe=tf,
                start_utc=WEEK_START,
                end_utc=WEEK_END,
                retrieved_at=retrieved,
                broker_account_kind=kind,
                policy=policy,
            )
            print(
                f"BARS {prov.timeframe}: rows={prov.row_count} status={report.status} "
                f"expected_gaps={report.expected_gaps} suspicious={len(report.suspicious_gaps)} "
                f"summary={report.summary}"
            )
            print("  ", path, prov.actual_start, "->", prov.actual_end)
            for gap in report.suspicious_gaps[:10]:
                print("   suspicious gap:", gap.start.isoformat(), "->", gap.end.isoformat())
            raw = fetch_rates_range(
                client,
                broker_symbol="Ger40",
                mt5_timeframe=tf,
                start_utc=WEEK_START,
                end_utc=WEEK_END,
                policy=policy,
            )
            fx = out / "raw_fixtures" / f"ger40_rates_{prov.timeframe}.csv"
            fx.parent.mkdir(parents=True, exist_ok=True)
            with fx.open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(raw.dtype.names)
                for row in raw:
                    w.writerow([row[n].item() for n in raw.dtype.names])

        path, prov, report = download_ticks(
            client,
            root=out,
            canonical=CANONICAL,
            start_utc=TICK_START,
            end_utc=TICK_END,
            retrieved_at=retrieved,
            broker_account_kind=kind,
            policy=policy,
        )
        print(f"TICKS: rows={prov.row_count} status={report.status} summary={report.summary}")
        print("  ", path)
        return 0
    finally:
        connection.disconnect()


def main() -> int:
    argv = sys.argv[1:]
    if "--worker" in argv:
        rest = [a for a in argv if a != "--worker"]
        return _run_worker(Path(rest[0]) if rest else REPO_ROOT / "data" / "c3_sample")
    out = Path(argv[0]) if argv else REPO_ROOT / "data" / "c3_sample"
    try:
        config = load_attach_only_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    timeout = default_staged_timeout_seconds() * 2
    result = run_worker_bounded(Path(__file__), config, timeout=timeout, extra_args=(str(out),))
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        print(f"TIMED OUT after {timeout}s", file=sys.stderr)
        return 1
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
