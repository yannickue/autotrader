"""Parquet export of flat analysis tables (pyarrow; no new dependencies).

Layout (partition-safe: every phase lives in its own directory, so DISCOVERY and FROZEN data can
never be mixed by a reader that globs one partition):

    <out_dir>/opportunities/phase=DISCOVERY/data.parquet
    <out_dir>/trades/phase=FROZEN/data.parquet
    <out_dir>/counterfactuals/phase=DISCOVERY/data.parquet

Each file is written to a temporary name in the same directory and moved with `os.replace`
(atomic on the same filesystem), so a reader never sees a half-written file. Exports are full
snapshots of the store (idempotent; re-running replaces the file, adding any new rows).
Nested structures are flattened with '_' separators; lists become JSON strings; columns whose values
cannot share one Arrow type fall back to JSON strings.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from demo.contracts import PHASES
from demo.store import DemoStore

TABLES = ("opportunities", "trades", "counterfactuals")


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            if v:
                out.update(flatten(v, key + "_"))
            else:
                out[key] = None
        elif isinstance(v, (list, tuple)):
            out[key] = json.dumps(v, sort_keys=True, default=str)
        else:
            out[key] = v
    return out


def rows_to_table(rows: list[dict[str, Any]]) -> pa.Table:
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    arrays = {}
    for c in cols:
        vals = [r.get(c) for r in rows]
        try:
            arrays[c] = pa.array(vals)
        except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError):
            arrays[c] = pa.array(
                [None if v is None else json.dumps(v, default=str) for v in vals], type=pa.string()
            )
    return pa.table(arrays)


def write_parquet_atomic(table: pa.Table, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        pq.write_table(table, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def opportunity_rows(store: DemoStore, phase: str) -> list[dict[str, Any]]:
    rows = []
    for snap in store.list_snapshots(phase=phase):
        row = flatten(snap.to_dict())
        dec = store.get_decision(snap.opportunity_id)
        if dec is not None:
            row.update(
                decision_accepted=dec.accepted,
                decision_reasons=json.dumps(list(dec.reasons)),
                decision_policy_id=dec.policy_id,
                decision_decided_utc=dec.decided_utc,
                decision_shadow=json.dumps(dec.shadow, sort_keys=True, default=str),
            )
        intent = store.intent_for_opportunity(snap.opportunity_id)
        row["intent_state"] = None if intent is None else intent["state"]
        cf = store.get_counterfactual(snap.opportunity_id)
        if cf is not None:
            row.update(
                cf_hypothetical_r=cf.hypothetical_r,
                cf_hypothetical_mfe_r=cf.hypothetical_mfe_r,
                cf_hypothetical_mae_r=cf.hypothetical_mae_r,
                cf_target_before_stop=cf.target_before_stop,
                cf_horizon_end_utc=cf.horizon_end_utc,
            )
        rows.append(row)
    return rows


def trade_rows(store: DemoStore, phase: str) -> list[dict[str, Any]]:
    rows = []
    for rec in store.learning_records(phase):
        row: dict[str, Any] = {"opportunity_id": rec.opportunity_id, "phase": rec.phase}
        for name in ("snapshot", "decision", "risk", "execution", "outcome"):
            row.update(flatten(getattr(rec, name).to_dict(), f"{name}_"))
        rows.append(row)
    return rows


def counterfactual_rows(store: DemoStore, phase: str) -> list[dict[str, Any]]:
    return [flatten(c.to_dict()) for c in store.list_counterfactuals(phase)]


_BUILDERS = {
    "opportunities": opportunity_rows,
    "trades": trade_rows,
    "counterfactuals": counterfactual_rows,
}


def export_all(
    store: DemoStore, out_dir: str | os.PathLike[str], phase: str | None = None
) -> dict[str, list[Path]]:
    """Write every non-empty (table, phase) partition. Returns {table: [written file paths]}."""
    root = Path(out_dir)
    written: dict[str, list[Path]] = {t: [] for t in TABLES}
    for t in TABLES:
        for ph in (phase,) if phase else PHASES:
            rows = _BUILDERS[t](store, ph)
            if not rows:
                continue
            written[t].append(
                write_parquet_atomic(rows_to_table(rows), root / t / f"phase={ph}" / "data.parquet")
            )
    return written
