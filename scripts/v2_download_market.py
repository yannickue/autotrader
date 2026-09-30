"""V2: generic resumable market history download + per-market data-quality report.

Generalises `scripts/ar1b_download_ger40.py`. READ-ONLY, attach-only (no login / account
switching), bounded child process, DEMO only. The only terminal-side effect beyond reads is
`symbol_select(symbol, True)` (adds a symbol to Market Watch so history is served); no orders,
no position/order calls of any kind.

Timestamp policy (identical for every timeframe and market): MT5 bar `time` is the SERVER wall
clock encoded as a UTC epoch; server = Europe/Berlin-style clock (UTC+2 observed in CEST) ->
`ServerTimePolicy` converts to true UTC. Stored `ts` = bar-OPEN time in UTC. The rule is
INFERRED from a live offset measurement, not broker-confirmed; `weekly_open_utc_hhmm_top` in the
quality report is the cross-check (see docs/V2_MARKETS.md).

Usage:
  <PY> scripts/v2_download_market.py CANONICAL[,CANONICAL...]|ALL FIRST LAST TFSPEC [OUT_DIR]
     FIRST/LAST : YYYY-MM (inclusive month range; H4/D1 use a single request over the range)
     TFSPEC     : comma list, each TF[@FIRST_MONTH], TF in M1 M5 H1 H4 D1
                  e.g.  M5,M1@2026-01,H4,D1
     OUT_DIR    : default data/markets
  Add --analyze-only to skip the terminal and rebuild the quality reports from existing files.
Refuses to run if free disk < 2 GB or the estimated output (incl. existing) > 150 MB.
Symbols come ONLY from research/reports/v2_markets/symbol_snapshot.json (resolved by the
discovery rules, never guessed).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v2_common import REPO_ROOT, load_config

from adapters.activtrades_mt5.bounded import (
    default_staged_timeout_seconds,
    run_worker_bounded,
)
from adapters.activtrades_mt5.connection import MT5Connection
from adapters.activtrades_mt5.history import ServerTimePolicy
from adapters.activtrades_mt5.history_download import download_bars
from adapters.activtrades_mt5.real_client import get_real_client
from adapters.config import MT5ConfigError
from data.historical import DatasetRejected
from markets.quality import analyze_coarse, analyze_intraday, load_frame

MARKETS = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD")
PHASE2_MARKETS = ("BRENT", "BTCUSD")  # Lane F: symbol/path come from configs/markets_phase2/*.toml
MT5_TF = {"M1": 1, "M5": 5, "H1": 16385, "H4": 16388, "D1": 16408}
TF_SECONDS = {"M1": 60, "M5": 300, "H1": 3600, "H4": 14400, "D1": 86400}
MONTHLY = {"M1", "M5", "H1"}
SNAPSHOT = REPO_ROOT / "research" / "reports" / "v2_markets" / "symbol_snapshot.json"
REPORT_DIR = REPO_ROOT / "research" / "reports" / "v2_markets"
MIN_FREE_BYTES = 2 * 1024**3
MAX_TOTAL_BYTES = 150 * 1024**2
BYTES_PER_BAR_EST = 40  # measured ~17 B/bar (GER40 M5 parquet); 40 is a conservative bound


def _months(first: str, last: str) -> list[tuple[datetime, datetime]]:
    y, m = (int(p) for p in first.split("-"))
    ly, lm = (int(p) for p in last.split("-"))
    out = []
    while (y, m) <= (ly, lm):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((datetime(y, m, 1, tzinfo=UTC), datetime(ny, nm, 1, tzinfo=UTC)))
        y, m = ny, nm
    return out


def parse_tfspec(spec: str, first: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in spec.split(","):
        tf, _, start = part.strip().partition("@")
        if tf not in MT5_TF:
            raise SystemExit(f"unknown timeframe {tf!r}; allowed {sorted(MT5_TF)}")
        out[tf] = start or first
    return out


def estimate_bytes(tfs: dict[str, str], last: str, n_markets: int) -> int:
    total = 0
    for tf, start in tfs.items():
        n_months = len(_months(start, last))
        bars_per_month = 22 * (86400 // TF_SECONDS[tf]) if TF_SECONDS[tf] < 86400 else 22
        total += n_months * bars_per_month * BYTES_PER_BAR_EST
    return total * n_markets


def guard(out: Path, tfs: dict[str, str], last: str, n_markets: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(out).free
    if free < MIN_FREE_BYTES:
        raise SystemExit(f"REFUSED: free disk {free / 1e9:.2f} GB < 2 GB")
    existing = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    est = estimate_bytes(tfs, last, n_markets) + existing
    if est > MAX_TOTAL_BYTES:
        raise SystemExit(f"REFUSED: estimated output {est / 1e6:.0f} MB > 150 MB")
    print(f"guard OK: free {free / 1e9:.1f} GB, estimated total {est / 1e6:.1f} MB", flush=True)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_symbol(canonical: str) -> tuple[str, str]:
    if canonical in PHASE2_MARKETS:
        import tomllib

        cfg = REPO_ROOT / "configs" / "markets_phase2" / f"{canonical}.toml"
        m = tomllib.loads(cfg.read_text(encoding="utf-8"))["market"]
        return m["broker_symbol"], str(m["broker_path"]).rsplit("\\", 1)[0] + "\\"
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    entry = snap["markets"].get(canonical)
    if not entry or not entry.get("resolved_broker_symbol"):
        raise SystemExit(f"{canonical}: no resolved broker symbol in snapshot (not guessing)")
    info = entry["resolved_info"]
    return entry["resolved_broker_symbol"], str(info["path"]).rsplit("\\", 1)[0] + "\\"


def _run_worker(canons: list[str], first: str, last: str, tfs: dict[str, str], out: Path) -> int:
    connection = MT5Connection(get_real_client())
    result = connection.connect(load_config())
    if not result.success:
        print(f"CONNECT: FAIL -- {result.reason}")
        return 1
    try:
        client = connection.client
        if getattr(client.account_info(), "trade_mode", None) != 0:
            print("ABORT: history download only runs against a verified DEMO account")
            return 1
        retrieved = datetime.now(UTC)
        policy = ServerTimePolicy()
        for canonical in canons:
            broker, prefix = _snapshot_symbol(canonical)
            info = client.symbol_info(broker)
            if info is not None and not getattr(info, "visible", True):
                client.symbol_select(broker, True)  # Market Watch only; no trading effect
            entries = []
            for tf, start_month in tfs.items():
                if tf in MONTHLY:
                    chunks = _months(start_month, last)
                else:
                    chunks = [(_months(start_month, last)[0][0], _months(start_month, last)[-1][1])]
                for start, end in chunks:
                    label = start.strftime("%Y-%m") if tf in MONTHLY else f"{tf}_full"
                    entry = {"tf": tf, "month": label}
                    try:
                        path, prov, report = download_bars(
                            client,
                            root=out,
                            canonical=canonical,
                            mt5_timeframe=MT5_TF[tf],
                            start_utc=start,
                            end_utc=end,
                            retrieved_at=retrieved,
                            broker_account_kind="DEMO",
                            policy=policy,
                            broker_symbol=broker,
                            path_prefix=prefix,
                        )
                        entry.update(
                            path=str(path.relative_to(out)),
                            rows=prov.row_count,
                            status=str(report.status),
                            first=prov.actual_start,
                            last=prov.actual_end,
                            content_sha256=prov.content_sha256,
                            file_sha256=_sha256(path),
                            bytes=path.stat().st_size,
                            summary=dict(report.summary),
                            n_suspicious_gaps=len(report.suspicious_gaps),
                            suspicious_gaps=[
                                [g.start.isoformat(), g.end.isoformat()]
                                for g in report.suspicious_gaps[:40]
                            ],
                        )
                    except DatasetRejected as exc:
                        msg = str(exc)[:300]
                        # empty month after range filtering -> "validation FAILED: {}"
                        empty = msg.strip() == "validation FAILED: {}"
                        entry.update(status="NO_DATA" if empty else "REJECTED", reason=msg)
                    except Exception as exc:
                        msg = f"{type(exc).__name__}: {exc}"[:300]
                        entry.update(status="NO_DATA" if "no rows" in msg else "ERROR", reason=msg)
                    entries.append(entry)
                    print(
                        f"{canonical} {tf} {label}: {entry['status']} rows={entry.get('rows', 0)}",
                        flush=True,
                    )
            (out / f"manifest_{canonical}.json").write_text(
                json.dumps(
                    {
                        "canonical": canonical,
                        "broker_symbol": broker,
                        "retrieved_at": retrieved.isoformat(),
                        "timezone_policy": policy.to_dict(),
                        "entries": entries,
                    },
                    indent=1,
                    default=str,
                ),
                encoding="utf-8",
            )
        return 0
    finally:
        connection.disconnect()


def analyze(canonical: str, out: Path) -> dict:
    """Rebuild the compact per-market quality report from the manifest + parquet files."""
    manifest = json.loads((out / f"manifest_{canonical}.json").read_text(encoding="utf-8"))
    report: dict = {
        "canonical": canonical,
        "broker_symbol": manifest["broker_symbol"],
        "retrieved_at": manifest["retrieved_at"],
        "timezone_policy": manifest["timezone_policy"],
        "timeframes": {},
    }
    by_tf: dict[str, list[dict]] = {}
    for e in manifest["entries"]:
        by_tf.setdefault(e["tf"], []).append(e)
    for tf, entries in by_tf.items():
        ok = [e for e in entries if e.get("status") in ("PASSED", "PASSED_WITH_WARNINGS")]
        bad = [
            {k: e.get(k) for k in ("month", "status", "reason")}
            for e in entries
            if e not in ok and e.get("status") != "NO_DATA"
        ]
        nodata = [e["month"] for e in entries if e.get("status") == "NO_DATA"]
        sec: dict = {
            "files": len(ok),
            "bytes": sum(e.get("bytes", 0) for e in ok),
            "rejected_or_error": bad,
            "no_data_months": nodata,
            "validator_status": {
                s: sum(1 for e in ok if e["status"] == s) for s in {e["status"] for e in ok}
            },
            "validator_warning_kinds": {},
            "file_sha256": {e["path"]: e["file_sha256"] for e in ok},
        }
        for e in ok:
            for k, v in (e.get("summary") or {}).items():
                sec["validator_warning_kinds"][k] = sec["validator_warning_kinds"].get(k, 0) + v
        if ok:
            df = load_frame([out / e["path"] for e in ok])
            if TF_SECONDS[tf] <= 3600:
                sec["analysis"] = analyze_intraday(df, TF_SECONDS[tf])
            else:
                sec["analysis"] = analyze_coarse(df, TF_SECONDS[tf])
        report["timeframes"][tf] = sec
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{canonical}_quality.json").write_text(
        json.dumps(report, indent=1, default=str), encoding="utf-8"
    )
    return report


def main() -> int:
    raw = [a for a in sys.argv[1:] if a != "--worker"]
    analyze_only = "--analyze-only" in raw
    argv = [a for a in raw if not a.startswith("--")]
    if len(argv) < 4:
        print(__doc__)
        return 2
    canons = list(MARKETS) if argv[0].upper() == "ALL" else argv[0].upper().split(",")
    for c in canons:
        if c not in MARKETS + PHASE2_MARKETS:
            raise SystemExit(f"unknown market {c!r}; allowed {MARKETS + PHASE2_MARKETS}")
    first, last = argv[1], argv[2]
    tfs = parse_tfspec(argv[3], first)
    out = Path(argv[4]) if len(argv) > 4 else REPO_ROOT / "data" / "markets"

    if "--worker" in sys.argv[1:]:
        return _run_worker(canons, first, last, tfs, out)

    guard(out, tfs, last, len(canons))
    if not analyze_only:
        try:
            config = load_config()
        except MT5ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return 2
        n_calls = sum(len(_months(s, last)) if tf in MONTHLY else 1 for tf, s in tfs.items()) * len(
            canons
        )
        timeout = max(default_staged_timeout_seconds() * 2, 4.0 * n_calls + 60)
        result = run_worker_bounded(
            Path(__file__),
            config,
            timeout=timeout,
            extra_args=(",".join(canons), first, last, argv[3], str(out)),
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if result.timed_out:
            print(f"TIMED OUT after {timeout}s (terminal not responding); partial manifest only")
            return 1
        if result.returncode != 0:
            return result.returncode or 1
    for canonical in canons:
        if (out / f"manifest_{canonical}.json").exists():
            rep = analyze(canonical, out)
            for tf, sec in rep["timeframes"].items():
                a = sec.get("analysis", {})
                print(
                    f"{canonical} {tf}: files={sec['files']} rows={a.get('rows')} "
                    f"bad={len(sec['rejected_or_error'])} nodata={len(sec['no_data_months'])}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
