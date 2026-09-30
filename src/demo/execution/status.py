"""Atomic heartbeat/status snapshots for operators and supervisors."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def write_status_atomic(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "mode": "DEMO MODE",
        "updated_utc": datetime.now(UTC).isoformat(),
        "process_alive": True,
        "equity": None,
        "balance": None,
        "pnl": None,
        "reconciliation": "NOT_RECONCILED",
        "open_positions": 0,
        "open_orders": 0,
        "protection_state": "UNKNOWN",
        "last_tick": None,
        "last_bar": None,
        "last_signal": None,
        "last_fill": None,
        "last_error": None,
        "mt5_connected": False,
        "feed_freshness_seconds": None,
        "disk_free_bytes": shutil.disk_usage(path.parent).free,
    }
    payload.update(values)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
    os.replace(temporary, path)
