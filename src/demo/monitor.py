# ruff: noqa: E501
"""Operator-facing monitoring for the DEMO runner: heartbeat/status file, staleness verdict, milestone
reports and the offline ``--analyze`` pass.

Kept import-light on purpose: ``--status`` must work without pandas/pyarrow/MT5/learning libraries
(``report``/``export`` are imported lazily inside the functions that need them).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HEARTBEAT_MAX_AGE_S = 90.0
MIN_DISK_FREE_BYTES = 2 * 1024**3
RUNNING = "RUNNING"
NOT_RUNNING = "NOT RUNNING"


def git_commit(root: Path | None = None) -> str:
    """HEAD sha of the repo (or ``unknown``); never raises."""
    base = root or Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and len(sha) >= 7 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def disk_free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def write_heartbeat(path: Path, values: dict[str, Any]) -> None:
    """Atomic (tmp + os.replace) JSON write; reuses the Lane C status writer."""
    from demo.execution.status import write_status_atomic

    write_status_atomic(path, values)


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _parse(ts: Any) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def heartbeat_verdict(
    path: Path, now: datetime | None = None, max_age_s: float = HEARTBEAT_MAX_AGE_S
) -> dict[str, Any]:
    """``{"verdict": RUNNING|NOT RUNNING, "age_s", "reason", "status"}``.

    NOT RUNNING when the file is missing/unreadable, ``process_alive`` is false (orderly stop) or the
    heartbeat is older than ``max_age_s`` (default 90 s)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    status = read_heartbeat(path)
    if status is None:
        return {"verdict": NOT_RUNNING, "age_s": None, "reason": "no readable heartbeat", "status": None}
    updated = _parse(status.get("updated_utc"))
    if updated is None:
        return {"verdict": NOT_RUNNING, "age_s": None, "reason": "heartbeat has no updated_utc", "status": status}
    age = (now - updated).total_seconds()
    if not status.get("process_alive", False):
        return {"verdict": NOT_RUNNING, "age_s": age, "reason": "runner stopped (process_alive=false)", "status": status}
    if age > max_age_s:
        return {"verdict": NOT_RUNNING, "age_s": age, "reason": f"heartbeat stale ({age:.0f}s > {max_age_s:.0f}s)", "status": status}
    return {"verdict": RUNNING, "age_s": age, "reason": "heartbeat fresh", "status": status}


def check_milestone(store: Any, phase: str | None, reports_dir: Path) -> tuple[int, Path, Path] | None:
    """If a 10/25/50/100/250/500-trade milestone is due (exactly once per milestone), write the
    markdown+json report and return ``(milestone, md_path, json_path)``."""
    from demo.report import should_emit_milestone, write_report

    n = store.count_trades(phase)
    milestone = should_emit_milestone(store, n, phase)
    if milestone is None:
        return None
    md, js = write_report(store, reports_dir, phase, tag=f"n{milestone}")
    return milestone, md, js


def analyze(
    store: Any, phase: str | None, reports_dir: Path, export_dir: Path
) -> dict[str, Any]:
    """Report over ALL data of ``phase`` (None = every phase) + parquet export. Returns a summary."""
    from demo.export import export_all
    from demo.report import build_report, write_report

    rep = build_report(store, phase)
    md, js = write_report(store, reports_dir, phase, tag="analyze")
    exported = export_all(store, export_dir, phase)
    from demo.funnel import funnel as build_funnel
    from demo.funnel import render as render_funnel

    fun = build_funnel(store, None, phase)
    Path(reports_dir).mkdir(parents=True, exist_ok=True)
    (Path(reports_dir) / f"funnel-{phase or 'ALL'}.json").write_text(
        json.dumps(fun, indent=1, sort_keys=True, default=str), encoding="utf-8"
    )
    print(render_funnel(fun), file=sys.stderr)
    return {
        "rejection_funnel": fun["summary"],
        "phase": phase or "ALL",
        "n_trades": store.count_trades(phase),
        "n_decisions": len(store.list_decisions(phase)),
        "n_counterfactuals": len(store.list_counterfactuals(phase)),
        "report_md": str(md),
        "report_json": str(js),
        "export": {k: [str(p) for p in v] for k, v in exported.items()},
        "report_keys": sorted(rep)[:12],
    }
