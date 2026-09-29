"""Validated GER40 M5 research dataset: monthly Parquet files -> one DataFrame + provenance.

Only months whose provenance says PASSED, or PASSED_WITH_WARNINGS where *every* warning is in the
reviewed allow-list below, may enter research. Content hashes are verified on read by
`data.historical.read_bar_dataset`. Bars are BID OHLC; `spread_pts` is the broker-recorded spread
in points (1 point = 0.01 index points). Timestamps are bar OPEN times in UTC.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from data.historical import read_bar_dataset

POINT = 0.01  # GER40 point size (symbol_info.point)
BAR_SECONDS = 300
BERLIN = "Europe/Berlin"

# Suspicious gaps reviewed and judged NOT to invalidate research use.
REVIEWED_GAPS: dict[tuple[str, str], str] = {
    ("2025-04-17T19:55:00+00:00", "2025-04-22T00:15:00+00:00"): "Easter closure",
    ("2025-12-23T20:55:00+00:00", "2025-12-29T00:15:00+00:00"): "Christmas closure",
    ("2026-04-02T19:55:00+00:00", "2026-04-07T00:15:00+00:00"): "Easter closure",
    ("2025-03-03T20:05:00+00:00", "2025-03-03T20:50:00+00:00"): (
        "45-minute hole at 21:05-21:50 Berlin: after the last entry (20:00) and the forced flat"
    ),
}
ACCEPTED_WARNING_KINDS = frozenset({"SPREAD_ANOMALY", "SUSPICIOUS_GAP"})


class DatasetPolicyError(RuntimeError):
    """A month does not satisfy the research-data admission policy."""


@dataclass(frozen=True)
class MonthInfo:
    month: str
    path: str
    rows: int
    validation_status: str
    warning_summary: dict[str, int]
    reviewed_gaps: tuple[str, ...]
    content_sha256: str
    actual_start: str
    actual_end: str
    broker_account_kind: str


@dataclass(frozen=True)
class ResearchDataset:
    frame: pd.DataFrame
    months: tuple[MonthInfo, ...]
    excluded: tuple[dict, ...]

    @property
    def n_bars(self) -> int:
        return len(self.frame)

    def provenance(self) -> dict:
        return {
            "source": "ActivTrades MT5 DEMO, Ger40 (canonical GER40), M5 BID OHLC + spread points",
            "broker_account_kind": "DEMO",
            "n_bars": self.n_bars,
            "first_bar_utc": self.frame["ts"].iloc[0].isoformat(),
            "last_bar_utc": self.frame["ts"].iloc[-1].isoformat(),
            "months": [m.__dict__ for m in self.months],
            "excluded_months": list(self.excluded),
            "reviewed_gaps": {f"{a}..{b}": why for (a, b), why in REVIEWED_GAPS.items()},
        }


def _admit(entry: dict) -> tuple[bool, str]:
    status = entry.get("status")
    if status == "PASSED":
        return True, "PASSED"
    if status != "PASSED_WITH_WARNINGS":
        return False, f"status={status}"
    kinds = set(entry.get("summary", {}))
    if not kinds <= ACCEPTED_WARNING_KINDS:
        return False, f"unreviewed warning kinds {sorted(kinds - ACCEPTED_WARNING_KINDS)}"
    for start, end in entry.get("suspicious_gaps", []):
        if (start, end) not in REVIEWED_GAPS:
            return False, f"unreviewed gap {start}..{end}"
    return True, "PASSED_WITH_WARNINGS (all warnings reviewed)"


def load_research_dataset(root: str | Path) -> ResearchDataset:
    """Load every admitted month listed in `<root>/download_manifest.json`."""
    root = Path(root)
    manifest = json.loads((root / "download_manifest.json").read_text(encoding="utf-8"))
    frames: list[pd.DataFrame] = []
    months: list[MonthInfo] = []
    excluded: list[dict] = []
    for entry in manifest["months"]:
        ok, why = _admit(entry)
        if not ok:
            excluded.append({"month": entry["month"], "reason": why})
            continue
        bars, prov = read_bar_dataset(entry["path"])  # verifies content_sha256
        if prov.broker_account_kind != "DEMO":
            raise DatasetPolicyError(f"{entry['month']}: unexpected account kind")
        df = pd.DataFrame(
            {
                "ts": [b.timestamp for b in bars],
                "open": [float(b.open) for b in bars],
                "high": [float(b.high) for b in bars],
                "low": [float(b.low) for b in bars],
                "close": [float(b.close) for b in bars],
                "tick_volume": [float(b.volume) for b in bars],
                "spread_pts": [b.spread_points for b in bars],
            }
        )
        frames.append(df)
        gaps = tuple(f"{a}..{b}" for a, b in entry.get("suspicious_gaps", []))
        months.append(
            MonthInfo(
                month=entry["month"],
                path=Path(entry["path"]).name,
                rows=prov.row_count,
                validation_status=prov.validation_status,
                warning_summary=dict(prov.validation_summary),
                reviewed_gaps=gaps,
                content_sha256=prov.content_sha256,
                actual_start=prov.actual_start,
                actual_end=prov.actual_end,
                broker_account_kind=prov.broker_account_kind,
            )
        )
    if not frames:
        raise DatasetPolicyError("no admitted months")
    frame = pd.concat(frames, ignore_index=True)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    if not frame["ts"].is_monotonic_increasing or frame["ts"].duplicated().any():
        raise DatasetPolicyError("timestamps not strictly increasing across months")
    if frame["spread_pts"].isna().any():
        raise DatasetPolicyError("bars without a recorded spread cannot enter cost-aware research")
    frame["spread_pts"] = frame["spread_pts"].astype(float)
    if not ((frame["high"] >= frame["low"]) & (frame["spread_pts"] >= 0)).all():
        raise DatasetPolicyError("invalid OHLC/spread rows")
    return ResearchDataset(frame=frame, months=tuple(months), excluded=tuple(excluded))
