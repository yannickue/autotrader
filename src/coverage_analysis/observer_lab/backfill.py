# ruff: noqa: E501
"""Historical BACKFILL of the Market Structure Observer (OFFLINE / RESEARCH ONLY; OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

Per market: an event table with observer features and post-event labels (STEP 1, ``run_events_step``) and, as a SEPARATE independently re-runnable
step keyed by ``event_id``, matched controls with the same features and labels (STEP 2, ``run_controls_step``).

* EVENTS come from the production family generators (``alpha.families.registry.generate_candidates`` on the frozen specs of the market, exactly the
  generators ``scripts/entry_exit_quality.py`` / the live engine use); one row per (family, variant, direction, decision bar). No second replay engine.
* FEATURES come ONLY from ``market_observer`` (the incremental ``MarketStructureObserver`` stepped once through the bars; the SAME ``bars_adapter`` as the
  live hook). A record is emitted at every event bar (step 1) and every control bar (step 2).
* CONTROLS: ``select_controls`` -> ``observer_lab.controls.match_controls`` (market, session bucket, time of day, ATR percentile, spread band, direction
  inherited, +-48 bars exclusion around EVERY generator opportunity bar, seeded), ``is_control=True``, ``control_of=<event_id>``. The matching is the version
  partition-aware revision (``CONTROL_MATCHING_REVISION``, ``observer-controls-2``); a matching change re-runs only step 2.
* LABELS: ``observer_lab.labels.label_event`` for events AND controls (bars strictly after the decision bar only; a control inherits the risk distance
  ``R`` (price units) of its event).
* PARTITION: every row carries ``partition`` (TRAIN / VALIDATION / FROZEN_OOS / PURGED / EMBARGO from ``observer_lab.splits``; a split tag that uses the label
  horizon: it is in the events/table files, never in the feature file).

Output per market directory (Parquet, zstd). Events: ``table.parquet`` (= ``ObserverRecord.to_row()`` + ``warmup_ok`` + ``run_id`` + ``partition``) and the
SAME rows split by column group so a model step can load features without labels: ``events.parquet`` (identity + generator fields + matching covariates +
partition), ``features.parquet`` (identity + versions + ``f_*`` + ``m_*``; NO ``y_*``, NO ``horizon_end_ts_ns``), ``labels.parquet`` (``event_id`` + ``y_*`` +
``horizon_end_ts_ns``), ``opportunity_bars.parquet``, ``manifest.json``. Controls: ``controls.parquet`` + ``controls_{events,features,labels}.parquet`` +
``controls_manifest.json``. The manifest of a step is written last (atomic completion marker); ``backfill.log`` is the run log.

``load_event_table(path, with_labels=False)`` physically cannot return ``y_*`` columns unless asked: it never opens a labels file otherwise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from alpha.common.market_data import (
    DEV_END,
    FORWARD_HOLDOUT_START,
    assert_no_forward_holdout,
    gap_summary,
)
from alpha.families.registry import describe_candidate, generate_candidates
from coverage_analysis.observer_lab.controls import (
    CONTROL_METHOD_VERSION,
    ControlSet,
    MatchSpec,
    bar_covariates,
    match_controls,
)
from coverage_analysis.observer_lab.labels import (
    DEFAULT_MAX_BARS,
    LABEL_CONVENTION_VERSION,
    EventSpec,
    label_event,
)
from coverage_analysis.observer_lab.splits import (
    OOS,
    ObserverSplitPlan,
    assign_partitions,
    berlin_dates,
    build_plan,
    core_fit_end,
    guard_dev_only,
)
from market_observer import bars_adapter as BA
from market_observer.observer import (
    MarketStructureObserver,
    ObservedEvent,
    ObserverConfig,
    event_id_for,
)
from market_observer.schema import (
    GROUP_VERSIONS,
    LABEL_PREFIX,
    OBSERVER_VERSION,
    SCHEMA_VERSION,
    ObserverBars,
)

if TYPE_CHECKING:  # pragma: no cover
    from coverage_analysis.entry_exit import MarketInputs

BACKFILL_VERSION = "observer-backfill-2"
EVENTS_PIPELINE_VERSION = "observer-backfill-events-1"  # bump on ANY change of event generation / feature pass / labels / file layout of the events step
CONTROLS_PIPELINE_VERSION = "observer-backfill-controls-1"  # bump on ANY change of the control step (selection wrapper, files)
CONTROL_MATCHING_REVISION = "partition-aware (match_controls partition=auto, rank_mode=partition, exclude_idx = every generator opportunity)"
DEFAULT_CHUNK_ROWS = 2000
EVENT_BASE_COLS = ("event_id", "market", "family", "variant", "direction", "is_control", "control_of", "decision_ts_ns", "run_id")
EVENT_EXTRA_COLS = (
    "decision_idx", "strategy_id", "structure_event_id", "entry", "stop", "risk", "risk_atr", "target",
    "match_session", "match_local_minute", "match_atr_pct", "match_spread_pct",
)
PARTITION_COLS = ("partition",)  # split tag (uses the label horizon): lives in events / table files, never in the feature file
LABEL_ONLY_COLS = ("horizon_end_ts_ns",)
EVENT_FILES = {"table": "table.parquet", "events": "events.parquet", "features": "features.parquet", "labels": "labels.parquet"}
CONTROL_FILES = {"table": "controls.parquet", "events": "controls_events.parquet", "features": "controls_features.parquet", "labels": "controls_labels.parquet"}
FILES = tuple(EVENT_FILES)
CONTROL_TAIL_BARS = 200  # limit mode: controls may lie at most this many bars after the last kept event

log = logging.getLogger("observer_backfill")


# ---------------------------------------------------------------------------------------------- events
@dataclass(frozen=True)
class EventRow:
    """One opportunity of a production family generator at decision bar ``idx`` (the last CLOSED bar)."""

    idx: int
    market: str
    family: str
    variant: str | None
    strategy_id: str
    direction: int
    entry: float  # event price = the decision bar's close (== what the live hook observes)
    stop: float
    risk: float  # |entry - stop| in price units, > 0
    risk_atr: float | None
    target: float | None
    structure_event_id: str | None


def _variant_of(spec: Any) -> str | None:
    mode = getattr(spec, "mode", None)
    return None if mode is None else str(mode)


def generate_events(mi: MarketInputs, *, limit: int | None = None) -> tuple[list[EventRow], dict[str, Any]]:
    """Events and exclusion counters (see ``generate_events_full``)."""
    rows, excl, _opp = generate_events_full(mi, limit=limit)
    return rows, excl


def generate_events_full(mi: MarketInputs, *, limit: int | None = None) -> tuple[list[EventRow], dict[str, Any], np.ndarray]:
    """Events of one market from the production family generators (deterministic; one row per (decision bar, family, variant, direction)).

    A candidate is dropped (and COUNTED, never silent) when it lies before ``eval_from`` (family warm-up), has no following bar, has no finite stop or
    its stop is not on the loss side of the decision close. The live operating policy (entry windows, flatten, runway) is NOT applied: events are the
    generators' opportunities, the observer's question is about structure, not about tradability.
    """
    d = mi.data
    n = len(d)
    ts_index = pd.DatetimeIndex(mi.frame["ts"])
    eval_idx = int(ts_index.searchsorted(mi.eval_from))
    ts_ns = np.asarray(d.ts_ns, dtype=np.int64)
    excl: Counter[str] = Counter()
    opp: set[int] = set()  # EVERY generator candidate bar (also those filtered out below): controls stay away from all of them
    seen: dict[tuple, int] = {}
    rows: list[EventRow] = []
    for fs in mi.specs:
        spec = fs.spec
        cands = generate_candidates(d, spec, fs.thr)
        variant = _variant_of(spec)
        for ci in range(len(cands.decision_idx)):
            i = int(cands.decision_idx[ci])
            opp.add(i)
            excl["candidates"] += 1
            if i < eval_idx:
                excl["before_eval_start"] += 1
                continue
            if i >= n - 1:
                excl["no_following_bar"] += 1
                continue
            direction = int(cands.direction[ci])
            stop = float(cands.stop[ci])
            if not math.isfinite(stop):
                excl["no_stop"] += 1
                continue
            entry = float(d.c[i])
            risk = (entry - stop) if direction > 0 else (stop - entry)
            if not (math.isfinite(risk) and risk > 0):
                excl["stop_not_on_loss_side"] += 1
                continue
            key = (i, fs.family, variant, direction)
            if key in seen:
                excl["duplicate_family_variant_direction"] += 1
                continue
            seen[key] = len(rows)
            sid = None
            if fs.family == "STRUCT":
                ev = describe_candidate(d, spec, i, direction)
                off = ev.get("break_bar_offset") if ev else None
                if off is not None and i - int(off) >= 0:
                    sid = f"{mi.market}:{direction}:{pd.Timestamp(int(ts_ns[i - int(off)]), tz='UTC').isoformat()}"
            atr = float(d.atr[i])
            tgt = float(cands.target[ci])
            rows.append(EventRow(
                idx=i, market=mi.market, family=fs.family, variant=variant, strategy_id=fs.strategy_id, direction=direction, entry=entry, stop=stop,
                risk=float(risk), risk_atr=(risk / atr) if atr > 0 and math.isfinite(atr) else None, target=tgt if math.isfinite(tgt) else None,
                structure_event_id=sid,
            ))
    rows.sort(key=lambda e: (e.idx, e.family, e.variant or "", e.direction))
    total = len(rows)
    if limit is not None:
        rows = rows[: int(limit)]
    excl["emitted"] = len(rows)
    excl["emitted_before_limit"] = total
    return rows, dict(excl), np.array(sorted(opp), dtype=np.int64)


# ---------------------------------------------------------------------------------------------- bars
def build_bars(frame: pd.DataFrame, mspec: Any, market: str) -> ObserverBars:
    """THE adapter path of the live hook: closed M5 frame -> ObserverBars (``session_for`` / ``config_for`` of the live hook are reused)."""
    from demo.opportunity.observer_hook import session_for

    return BA.bars_from_frame(market, frame, point_size=float(mspec.point_size), tick_size=float(mspec.tick_size), session=session_for(mspec))


def observer_config_for(mspec: Any) -> ObserverConfig:
    from demo.opportunity.observer_hook import config_for

    return config_for(mspec)


def frame_fingerprint(frame: pd.DataFrame) -> str:
    ts = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8.astype(np.int64)
    h = hashlib.sha256()
    h.update(ts.tobytes())
    for col in ("open", "high", "low", "close", "tick_volume", "spread_pts"):
        h.update(frame[col].to_numpy(float).tobytes())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------------------------- parquet helpers
def _py(v: Any) -> Any:
    return v.item() if isinstance(v, np.generic) else v


def _arr(values: list[Any]) -> pa.Array:
    vals = [_py(v) for v in values]
    try:
        return pa.array(vals)
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError):
        return pa.array([None if v is None else str(v) for v in vals], type=pa.string())


def _rows_to_table(rows: list[dict[str, Any]], columns: list[str] | None = None) -> pa.Table:
    cols = columns if columns is not None else list(dict.fromkeys(k for r in rows for k in r))
    return pa.table({c: _arr([r.get(c) for r in rows]) for c in cols})


def _unify_type(types: list[pa.DataType]) -> pa.DataType:
    real = [t for t in types if not pa.types.is_null(t)]
    if not real:
        return pa.null()
    if all(t == real[0] for t in real):
        return real[0]
    if all(pa.types.is_integer(t) or pa.types.is_floating(t) for t in real):
        return pa.float64()
    return pa.string()


def _merge_parts(parts: list[Path], dest: Path, columns: list[str] | None = None) -> int:
    """Stream the part files into ONE Parquet file with a unified schema (missing columns -> null, mixed numeric -> float64). Returns the row count."""
    names: list[str] = []
    types: dict[str, list[pa.DataType]] = defaultdict(list)
    for p in parts:
        for f in pq.read_schema(p):
            if f.name not in types:
                names.append(f.name)
            types[f.name].append(f.type)
    if columns is not None:
        names = [c for c in columns if c in types]
    schema = pa.schema([(c, _unify_type(types[c])) for c in names])
    tmp = dest.with_suffix(".parquet.tmp")
    n = 0
    with pq.ParquetWriter(tmp, schema, compression="zstd") as w:
        for p in parts:
            t = pq.read_table(p)
            arrays = []
            for f in schema:
                if f.name in t.column_names:
                    col = t[f.name]
                    arrays.append(col.cast(f.type) if col.type != f.type else col)
                else:
                    arrays.append(pa.nulls(len(t), type=f.type))
            w.write_table(pa.table(arrays, schema=schema))
            n += len(t)
    os.replace(tmp, dest)
    return n


def peak_memory_mb() -> float | None:
    """Peak working set of this process in MB (Windows via ctypes, else ``resource``); None when unavailable."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            h = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return round(pmc.PeakWorkingSetSize / 1e6, 1)
            return None
        import resource

        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except Exception:
        return None


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    except Exception:
        return None


def code_identity(root: Path | None = None) -> dict[str, Any]:
    root = root or Path(__file__).resolve().parents[3]
    sha = _git(root, "rev-parse", "HEAD")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no")
    src = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    return {"git_sha": sha, "git_dirty": bool(dirty) if dirty is not None else None, "backfill_source_hash": src}


# ---------------------------------------------------------------------------------------------- the backfill
@dataclass(frozen=True)
class _Item:
    kind: str  # "event" | "control"
    idx: int
    direction: int
    risk: float
    family: str | None
    variant: str | None
    event_id: str
    control_of: str | None
    event: EventRow | None


def _coverage(frame: pd.DataFrame, tz: str) -> dict[str, Any]:
    ts = pd.DatetimeIndex(frame["ts"])
    months = pd.period_range(ts[0].tz_convert("UTC").tz_localize(None).to_period("M"), ts[-1].tz_convert("UTC").tz_localize(None).to_period("M"), freq="M")
    present = set(ts.tz_convert("UTC").tz_localize(None).to_period("M").unique())
    d = np.diff(ts.as_unit("s").asi8)
    big = [(ts[k].isoformat(), round(float(d[k]) / 3600, 1)) for k in np.flatnonzero(d > 3 * 86400)]
    return {
        "first_bar_utc": ts[0].isoformat(), "last_bar_utc": ts[-1].isoformat(), "n_bars": len(ts), "missing_months_utc": [str(m) for m in months if m not in present],
        "gaps_longer_than_3_days": big, **gap_summary(frame, tz),
    }


def _matching_report_dict(rep: Any) -> dict[str, Any]:
    return {
        "n_events": rep.n_events, "n_matched": rep.n_matched, "match_rate": rep.match_rate, "n_unmatched": len(rep.unmatched_event_pos), "n_controls": rep.n_controls,
        "smd": {k: (None if (isinstance(v, float) and not math.isfinite(v)) else v) for k, v in rep.smd.items()}, "session_share_diff_max": rep.session_share_diff_max,
        "method": rep.method, "partition_mode": getattr(rep, "partition_mode", None), "rank_mode": getattr(rep, "rank_mode", None),
        "by_partition": {k: dict(v) for k, v in getattr(rep, "by_partition", {}).items()}, "n_events_in_excluded_partition": getattr(rep, "n_events_in_excluded_partition", None),
        "n_exclusion_bars_blocked": getattr(rep, "n_exclusion_bars_blocked", None), "n_rejected_partition": getattr(rep, "n_rejected_partition", None),
    }


# ---------------------------------------------------------------------------------------------- partitions
NO_FROZEN_SPLIT_MARKETS = ("BTCUSD", "BRENT")  # Phase-2 markets: no frozen train/validation split of any fitted parameter exists


def split_plan_for(ts_ns: np.ndarray) -> tuple[ObserverSplitPlan, bool]:
    """Repo split plan (core fit end / dev end constants of ``observer_lab.splits``) for the trading days of the bars. Returns (plan, regular): when the
    frame has too few days up to the core fit end (synthetic / very short data) TRAIN and VALIDATION are empty and everything up to the dev end is OOS."""
    days = sorted(set(berlin_dates(ts_ns).tolist()))
    try:
        return build_plan(days), True
    except ValueError:
        fit_end = core_fit_end()
        return ObserverSplitPlan("1970-01-01", "1970-01-01", "1970-01-02", "1970-01-02", (pd.Timestamp(fit_end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), DEV_END, FORWARD_HOLDOUT_START, fit_end), False


def partitions_for(plan: ObserverSplitPlan, decision_ts_ns: np.ndarray, horizon_end_ts_ns: np.ndarray, embargo_s: float) -> np.ndarray:
    """``TRAIN`` / ``VALIDATION`` / ``FROZEN_OOS`` / ``PURGED`` / ``EMBARGO`` per row (``observer_lab.splits.assign_partitions``; its ``OOS`` is written as
    ``FROZEN_OOS``). PURGED = the label horizon reaches into another partition; EMBARGO = inside the first ``embargo_s`` after a boundary."""
    part = assign_partitions(decision_ts_ns, horizon_end_ts_ns, plan, embargo_s=embargo_s)
    return np.where(part == OOS, "FROZEN_OOS", part).astype(object)


# ---------------------------------------------------------------------------------------------- fingerprints (one per step)
def _events_fingerprint(market: str, limit: int | None, config_hash: str, data_fp: str, max_bars: int) -> str:
    payload = json.dumps(
        {"v": EVENTS_PIPELINE_VERSION, "market": market, "limit": limit, "cfg": config_hash, "data": data_fp, "lab": LABEL_CONVENTION_VERSION,
         "obs": OBSERVER_VERSION, "schema": SCHEMA_VERSION, "max_bars": max_bars}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _controls_fingerprint(events_fp: str, seed: int, spec: MatchSpec, max_bars: int) -> str:
    payload = json.dumps(
        {"v": CONTROLS_PIPELINE_VERSION, "events": events_fp, "seed": seed, "match": asdict(spec), "ctl": CONTROL_METHOD_VERSION, "rev": CONTROL_MATCHING_REVISION,
         "lab": LABEL_CONVENTION_VERSION, "max_bars": max_bars}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _complete(mdir: Path, manifest_name: str, files: dict[str, str], fp: str) -> dict[str, Any] | None:
    p = mdir / manifest_name
    if not p.is_file():
        return None
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if old.get("status") == "COMPLETE" and old.get("fingerprint") == fp and all((mdir / f).is_file() for f in files.values()):
        return old
    return None


def _attach_log(mdir: Path) -> logging.Handler:
    fh = logging.FileHandler(mdir / "backfill.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(fh)
    log.setLevel(logging.INFO)
    return fh


# ---------------------------------------------------------------------------------------------- the observer pass (events AND controls)
def _observe_pass(
    bars: ObserverBars, cfg: ObserverConfig, items: dict[int, list[_Item]], *, run_id: str, plan: ObserverSplitPlan, embargo_s: float, parts_dir: Path,
    chunk_rows: int, max_label_bars: int,
) -> tuple[int, float]:
    """ONE sequential pass of the incremental observer through the bars; a record + labels at every item bar. Rows are written in chunks (memory-safe)."""
    market = bars.market
    cov = bar_covariates(bars)
    obs = MarketStructureObserver(cfg)
    rows: list[dict[str, Any]] = []
    extras: list[dict[str, Any]] = []
    n_parts = 0
    n_rows = 0
    t0 = time.time()

    def flush() -> None:
        nonlocal rows, extras, n_parts
        if not rows:
            return
        part = partitions_for(plan, np.array([r["decision_ts_ns"] for r in rows], dtype=np.int64), np.array([r["horizon_end_ts_ns"] for r in rows], dtype=np.int64), embargo_s)
        for r, e, p in zip(rows, extras, part.tolist(), strict=True):
            r["partition"] = p
            e["partition"] = p
        pq.write_table(_rows_to_table(rows), parts_dir / f"table-{n_parts:05d}.parquet", compression="zstd")
        pq.write_table(_rows_to_table(extras, list(EVENT_BASE_COLS) + list(EVENT_EXTRA_COLS) + list(PARTITION_COLS)), parts_dir / f"events-{n_parts:05d}.parquet", compression="zstd")
        n_parts += 1
        rows, extras = [], []

    for i in sorted(items):
        obs.advance(bars, i)
        for it in sorted(items[i], key=lambda x: (x.kind, x.family or "", x.variant or "", x.direction)):
            src = it.event
            is_ev = it.kind == "event"
            oe = ObservedEvent(
                it.direction, float(bars.c[i]), family=it.family, variant=it.variant, structure_event_id=src.structure_event_id if (is_ev and src is not None) else None,
                is_control=not is_ev, control_of=it.control_of,
            )
            rec = obs.observe(bars, i, oe)
            lab = label_event(bars, EventSpec(i, it.direction, float(bars.c[i]), it.risk), max_bars=max_label_bars)
            full = replace(rec, labels=lab)
            if full.event_id != it.event_id:
                raise RuntimeError("event id mismatch between planning and observer")
            row = full.to_row()
            row["warmup_ok"] = bool(rec.meta.get("warmup_ok"))
            row["run_id"] = run_id
            rows.append(row)
            atr_i = float(bars.atr[i])
            extras.append({
                "event_id": it.event_id, "market": market, "family": it.family, "variant": it.variant, "direction": it.direction, "is_control": not is_ev,
                "control_of": it.control_of, "decision_ts_ns": int(bars.decision_ts_ns(i)), "run_id": run_id, "decision_idx": i,
                "strategy_id": src.strategy_id if (is_ev and src) else None, "structure_event_id": src.structure_event_id if (is_ev and src) else None,
                "entry": float(bars.c[i]), "stop": (src.stop if is_ev and src else float(bars.c[i]) - it.direction * it.risk), "risk": it.risk,
                "risk_atr": (it.risk / atr_i) if (atr_i > 0 and math.isfinite(atr_i)) else None, "target": src.target if (is_ev and src) else None,
                "match_session": str(cov["session"][i]), "match_local_minute": float(cov["minute"][i]),
                "match_atr_pct": None if np.isnan(cov["atr_pct"][i]) else float(cov["atr_pct"][i]),
                "match_spread_pct": None if np.isnan(cov["spread_pct"][i]) else float(cov["spread_pct"][i]),
            })
            n_rows += 1
            if len(rows) >= chunk_rows:
                flush()
    flush()
    return n_rows, time.time() - t0


def _assemble_files(mdir: Path, parts_dir: Path, files: dict[str, str]) -> dict[str, int]:
    """Merge the chunk files into the final column-group files (table / events / features / labels)."""
    tbl_parts = sorted(parts_dir.glob("table-*.parquet"))
    ev_parts = sorted(parts_dir.glob("events-*.parquet"))
    counts: dict[str, int] = {}
    if not tbl_parts:  # no row at all: empty but well-formed files
        for k, f in files.items():
            pq.write_table(pa.table({"event_id": pa.array([], pa.string())}), mdir / f)
            counts[k] = 0
        return counts
    counts["table"] = _merge_parts(tbl_parts, mdir / files["table"])
    full_cols = pq.read_schema(mdir / files["table"]).names
    label_cols = [c for c in full_cols if c.startswith(LABEL_PREFIX) or c in LABEL_ONLY_COLS]
    feat_cols = [c for c in full_cols if c not in label_cols and c not in PARTITION_COLS]
    counts["features"] = _split_file(mdir / files["table"], mdir / files["features"], feat_cols)
    counts["labels"] = _split_file(mdir / files["table"], mdir / files["labels"], ["event_id", "is_control", "control_of", *label_cols])
    counts["events"] = _merge_parts(ev_parts, mdir / files["events"], list(EVENT_BASE_COLS) + list(EVENT_EXTRA_COLS) + list(PARTITION_COLS))
    return counts


def _partition_summary(events_path: Path) -> dict[str, int]:
    if not events_path.is_file() or "partition" not in pq.read_schema(events_path).names:
        return {}
    s = pq.read_table(events_path, columns=["partition"]).to_pandas()["partition"]
    return {str(k): int(v) for k, v in s.value_counts().sort_index().items()}


# ---------------------------------------------------------------------------------------------- step 1: events + features + labels
def run_events_step(
    mi: MarketInputs, mspec: Any, out_dir: str | Path, *, limit: int | None = None, force: bool = False, chunk_rows: int = DEFAULT_CHUNK_ROWS,
    max_label_bars: int = DEFAULT_MAX_BARS, code: dict[str, Any] | None = None, extra_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Events of ONE market (production family generators) with observer features and post-event labels -> ``<out>/<MARKET>/{table,events,features,labels}.parquet``
    + ``opportunity_bars.parquet`` (EVERY generator candidate bar, also the ones filtered out: controls must stay away from all of them) + ``manifest.json``.
    Idempotent: a complete directory with the same fingerprint is left untouched. Controls are NOT touched here (``run_controls_step``)."""
    t0 = time.time()
    market = mi.market
    mdir = Path(out_dir) / market
    code = code or code_identity()
    frame = mi.frame
    assert_no_forward_holdout(frame)  # existing guard: no bar after the Berlin dev end
    bars = build_bars(frame, mspec, market)
    guard_dev_only(bars.ts_ns)  # observer-lab guard (same rule, independent implementation)
    cfg = observer_config_for(mspec)
    data_fp = frame_fingerprint(frame)
    fp = _events_fingerprint(market, limit, cfg.config_hash(), data_fp, max_label_bars)
    run_id = fp[:12]
    if not force:
        old = _complete(mdir, "manifest.json", EVENT_FILES, fp)
        if old is not None:
            old["status_this_call"] = "SKIPPED_COMPLETE"
            return old
    mdir.mkdir(parents=True, exist_ok=True)
    for f in (*EVENT_FILES.values(), "manifest.json", "opportunity_bars.parquet"):
        (mdir / f).unlink(missing_ok=True)
    shutil.rmtree(mdir / "_parts", ignore_errors=True)
    parts_dir = mdir / "_parts"
    parts_dir.mkdir()
    fh = _attach_log(mdir)
    try:
        log.info("events step start market=%s run_id=%s limit=%s bars=%d data_fp=%s code=%s", market, run_id, limit, len(bars), data_fp, code)
        events, excl, opp_idx = generate_events_full(mi, limit=limit)
        log.info("events %d (excl %s); opportunity bars %d", len(events), excl, len(opp_idx))
        pd.DataFrame({"decision_idx": np.asarray(opp_idx, dtype=np.int64)}).to_parquet(mdir / "opportunity_bars.parquet", index=False)
        ev_ids = [event_id_for(market, bars.decision_ts_ns(e.idx), e.family, e.variant, e.direction) for e in events]
        if len(set(ev_ids)) != len(ev_ids):
            raise RuntimeError("duplicate event ids among events")
        items: dict[int, list[_Item]] = defaultdict(list)
        for e, eid in zip(events, ev_ids, strict=True):
            items[e.idx].append(_Item("event", e.idx, e.direction, e.risk, e.family, e.variant, eid, None, e))
        plan, regular = split_plan_for(bars.ts_ns)
        embargo_s = float(max_label_bars * bars.bar_seconds)
        n_rows, t_obs = _observe_pass(bars, cfg, items, run_id=run_id, plan=plan, embargo_s=embargo_s, parts_dir=parts_dir, chunk_rows=chunk_rows, max_label_bars=max_label_bars)
        log.info("observer pass done: %d rows in %.1fs (%.2f ms/row incl. registry)", n_rows, t_obs, 1000 * t_obs / max(n_rows, 1))
        counts = _assemble_files(mdir, parts_dir, EVENT_FILES)
        shutil.rmtree(parts_dir)
        fam_counts: Counter[str] = Counter(f"{e.family}|{e.variant or ''}|{'long' if e.direction > 0 else 'short'}" for e in events)
        manifest = {
            "status": "COMPLETE", "step": "events", "fingerprint": fp, "run_id": run_id, "backfill_version": BACKFILL_VERSION, "events_pipeline_version": EVENTS_PIPELINE_VERSION,
            "market": market, "observation_only": True, "code": code, "observer_version": OBSERVER_VERSION, "schema_version": SCHEMA_VERSION,
            "group_versions": {k: v for k, v in GROUP_VERSIONS.items() if k != "fib"}, "definition_hashes": cfg.definition_hashes(), "observer_config_hash": cfg.config_hash(),
            "label_convention_version": LABEL_CONVENTION_VERSION, "label_max_bars": max_label_bars, "limit": limit, "limit_mode": limit is not None,
            "dev_end_guard": {"dev_end_berlin_date": DEV_END, "assert_no_forward_holdout": "PASS", "guard_dev_only": "PASS", "last_bar_utc": pd.Timestamp(int(bars.ts_ns[-1]), tz="UTC").isoformat()},
            "data": {**_coverage(frame, mi.tz), "frame_fingerprint": data_fp, "eval_from_utc": pd.Timestamp(mi.eval_from).isoformat(),
                     "eval_from_index": int(pd.DatetimeIndex(frame["ts"]).searchsorted(mi.eval_from))},
            "partitions": {
                "scheme": "observer_lab.splits (core fit end / dev end constants); OOS written as FROZEN_OOS; PURGED = horizon reaches another partition; EMBARGO = first label horizon after a boundary",
                "plan": asdict(plan), "regular_plan": regular, "embargo_s": embargo_s, "has_frozen_split": market not in NO_FROZEN_SPLIT_MARKETS,
                "note": ("BTCUSD/BRENT: NO frozen split exists (short history, placeholder STRUCT constants); the boundaries above are the repo's generic dev dates, TRAIN/VALIDATION here "
                         "are NOT a frozen train/validation of any fitted parameter" if market in NO_FROZEN_SPLIT_MARKETS else
                         "core thresholds were fitted up to the core fit end; the dev window 2026-07-01..2026-08-31 is a weaker OOS than a never-seen period"),
                "counts": _partition_summary(mdir / EVENT_FILES["events"]),
            },
            "events": {"n_events": len(events), "by_family_variant_direction": dict(sorted(fam_counts.items())), "exclusions": excl, "n_opportunity_bars": len(opp_idx),
                       "warmup": _warmup_counts(mdir / EVENT_FILES["features"])},
            "rows": counts, "files": {**EVENT_FILES, "opportunity_bars": "opportunity_bars.parquet"}, "runtime_s": round(time.time() - t0, 1), "observer_pass_s": round(t_obs, 1),
            "peak_memory_mb": peak_memory_mb(),
            "caveats": [
                "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED; no edge claim", "events = production generator opportunities WITHOUT the live operating policy filters",
                "match_* columns of events.parquet are descriptive full-sample percentile ranks (NOT causal features; never feed them to a model as features)",
                "label horizons and the partition column use future bars of the event: they are labels / split tags, never features",
            ],
            **(extra_manifest or {}),
        }
        (mdir / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
        log.info("events step done market=%s rows=%s runtime=%.1fs peak_mb=%s", market, counts, time.time() - t0, manifest["peak_memory_mb"])
        manifest["status_this_call"] = "BUILT"
        return manifest
    finally:
        log.removeHandler(fh)
        fh.close()


# ---------------------------------------------------------------------------------------------- step 2: controls (independently re-runnable)
def select_controls(
    bars: ObserverBars, event_idx: Sequence[int], *, spec: MatchSpec, seed: int, eligible: np.ndarray, exclude_idx: Sequence[int], partitioned: bool,
) -> ControlSet:
    """THE single place that chooses controls: the partition-aware ``observer_lab.controls.match_controls`` (controls drawn INSIDE the event's partition, ranks
    within the partition, PURGED / EMBARGO / UNASSIGNED bars never controls, +-``exclusion_bars`` around EVERY generator opportunity via ``exclude_idx``).
    ``partitioned=False`` (frames with fewer than 4 trading days up to the core fit end: synthetic / very short data) uses ``partition=None`` (legacy)."""
    return match_controls(bars, event_idx, spec=spec, seed=seed, eligible=eligible, partition="auto" if partitioned else None, exclude_idx=exclude_idx)


def exclusion_mask(n: int, idx: np.ndarray, radius: int) -> np.ndarray:
    """bool[n]: True within ``radius`` bars of any index in ``idx``."""
    diff = np.zeros(n + 1, dtype=np.int64)
    for i in np.asarray(idx, dtype=np.int64):
        diff[max(0, int(i) - radius)] += 1
        diff[min(n, int(i) + radius + 1)] -= 1
    return np.cumsum(diff[:n]) > 0


def run_controls_step(
    frame: pd.DataFrame, mspec: Any, market: str, out_dir: str | Path, *, seed: int = 0, force: bool = False, match_spec: MatchSpec | None = None,
    chunk_rows: int = DEFAULT_CHUNK_ROWS, max_label_bars: int = DEFAULT_MAX_BARS, code: dict[str, Any] | None = None, extra_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Control selection + control features + control labels, keyed by ``event_id`` (``control_of``), from a COMPLETE events step of the same data. Writes
    ``controls.parquet`` (all columns) and ``controls_{events,features,labels}.parquet`` + ``controls_manifest.json``. Re-runnable without recomputing event
    features (the events step is only READ). Controls stay outside +-``exclusion_bars`` of EVERY generator candidate bar (``opportunity_bars.parquet``), also
    those that were filtered out of the event table."""
    t0 = time.time()
    mdir = Path(out_dir) / market
    match_spec = match_spec or MatchSpec()
    code = code or code_identity()
    ev_manifest_path = mdir / "manifest.json"
    if not ev_manifest_path.is_file():
        raise FileNotFoundError(f"events step of {market} not complete (no manifest.json); run it first")
    evm = json.loads(ev_manifest_path.read_text(encoding="utf-8"))
    if evm.get("status") != "COMPLETE":
        raise RuntimeError("events step is not COMPLETE")
    assert_no_forward_holdout(frame)
    data_fp = frame_fingerprint(frame)
    if data_fp != evm["data"]["frame_fingerprint"]:
        raise ValueError("the frame differs from the one the events step used (frame fingerprint mismatch)")
    bars = build_bars(frame, mspec, market)
    guard_dev_only(bars.ts_ns)
    cfg = observer_config_for(mspec)
    fp = _controls_fingerprint(evm["fingerprint"], seed, match_spec, max_label_bars)
    run_id = fp[:12]
    if not force:
        old = _complete(mdir, "controls_manifest.json", CONTROL_FILES, fp)
        if old is not None:
            old["status_this_call"] = "SKIPPED_COMPLETE"
            return old
    for f in (*CONTROL_FILES.values(), "controls_manifest.json"):
        (mdir / f).unlink(missing_ok=True)
    shutil.rmtree(mdir / "_parts_controls", ignore_errors=True)
    parts_dir = mdir / "_parts_controls"
    parts_dir.mkdir()
    fh = _attach_log(mdir)
    try:
        log.info("controls step start market=%s run_id=%s seed=%s events_fp=%s matching=%s/%s", market, run_id, seed, evm["fingerprint"][:12], CONTROL_METHOD_VERSION, CONTROL_MATCHING_REVISION)
        events = pd.read_parquet(mdir / EVENT_FILES["events"], columns=["event_id", "decision_idx", "direction", "risk", "family", "variant", "partition"])
        events["variant"] = events["variant"].astype(object).where(events["variant"].notna(), None)
        opp = pd.read_parquet(mdir / "opportunity_bars.parquet")["decision_idx"].to_numpy(np.int64)
        n = len(bars)
        eval_idx = int(evm["data"]["eval_from_index"])
        eligible = np.arange(n) >= eval_idx
        if evm.get("limit") is not None and len(events):
            eligible &= np.arange(n) <= int(events["decision_idx"].max()) + CONTROL_TAIL_BARS
        ev_idx = events["decision_idx"].to_numpy(np.int64)
        _plan0, regular0 = split_plan_for(bars.ts_ns)
        cs = select_controls(bars, ev_idx, spec=match_spec, seed=seed, eligible=eligible, exclude_idx=opp, partitioned=regular0)
        log.info("controls %d match_rate=%.4f", len(cs.control_idx), cs.report.match_rate)
        items: dict[int, list[_Item]] = defaultdict(list)
        for pos, cidx in zip(cs.event_pos.tolist(), cs.control_idx.tolist(), strict=True):
            e = events.iloc[pos]
            fam, var, d = e["family"], e["variant"], int(e["direction"])
            items[int(cidx)].append(_Item("control", int(cidx), d, float(e["risk"]), fam, var, event_id_for(market, bars.decision_ts_ns(int(cidx)), fam, var, d), str(e["event_id"]), None))
        all_ids = [it.event_id for lst in items.values() for it in lst]
        if len(set(all_ids)) != len(all_ids):
            raise RuntimeError("duplicate event ids among controls")
        plan, regular = split_plan_for(bars.ts_ns)
        embargo_s = float(max_label_bars * bars.bar_seconds)
        n_rows, t_obs = _observe_pass(bars, cfg, items, run_id=run_id, plan=plan, embargo_s=embargo_s, parts_dir=parts_dir, chunk_rows=chunk_rows, max_label_bars=max_label_bars)
        log.info("controls observer pass done: %d rows in %.1fs", n_rows, t_obs)
        counts = _assemble_files(mdir, parts_dir, CONTROL_FILES)
        shutil.rmtree(parts_dir)
        # partition agreement of control and event (the revised matching enforces it: expected 0)
        cpart = pd.read_parquet(mdir / CONTROL_FILES["events"], columns=["control_of", "partition"]) if counts.get("events") else pd.DataFrame({"control_of": [], "partition": []})
        joined = cpart.merge(events[["event_id", "partition"]].rename(columns={"event_id": "control_of", "partition": "event_partition"}), on="control_of", how="left")
        mismatch = int((joined["partition"] != joined["event_partition"]).sum()) if len(joined) else 0
        matched_pos = set(cs.event_pos.tolist())
        by_fam: dict[str, dict[str, int]] = {}
        for pos, fam in enumerate(events["family"].tolist()):
            b = by_fam.setdefault(str(fam), {"n_events": 0, "n_matched": 0})
            b["n_events"] += 1
            b["n_matched"] += int(pos in matched_pos)
        manifest = {
            "status": "COMPLETE", "step": "controls", "fingerprint": fp, "run_id": run_id, "market": market, "events_fingerprint": evm["fingerprint"], "events_run_id": evm["run_id"],
            "controls_pipeline_version": CONTROLS_PIPELINE_VERSION, "control_method_version": CONTROL_METHOD_VERSION, "matching_revision": CONTROL_MATCHING_REVISION,
            "matching_is_pre_revision": False, "partitioned_matching": regular0, "match_spec": asdict(match_spec), "seed": seed, "code": code, "observer_config_hash": cfg.config_hash(),
            "label_convention_version": LABEL_CONVENTION_VERSION, "n_controls": len(cs.control_idx), "match_report": _matching_report_dict(cs.report),
            "match_by_family": by_fam, "eligible_control_bars": int(eligible.sum()), "n_excluded_opportunity_bars": len(opp),
            "partitions": {"counts": _partition_summary(mdir / CONTROL_FILES["events"]), "controls_in_a_different_partition_than_their_event": mismatch,
                           "plan": asdict(plan), "regular_plan": regular},
            "warmup": _warmup_counts(mdir / CONTROL_FILES["features"]), "rows": counts, "files": dict(CONTROL_FILES), "runtime_s": round(time.time() - t0, 1), "observer_pass_s": round(t_obs, 1),
            "peak_memory_mb": peak_memory_mb(),
            "caveats": [
                "matching = observer_lab.controls.match_controls (observer-controls-2): controls are drawn inside the event's partition with partition-internal ranks; events in PURGED/EMBARGO bars are unmatched by construction", 
                "a control inherits the risk distance R (price units), direction, family and variant of its event (inherited labels, not generator output)",
            ],
            **(extra_manifest or {}),
        }
        (mdir / "controls_manifest.json").write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
        log.info("controls step done market=%s rows=%s runtime=%.1fs", market, counts, time.time() - t0)
        manifest["status_this_call"] = "BUILT"
        return manifest
    finally:
        log.removeHandler(fh)
        fh.close()


def run_market_backfill(
    mi: MarketInputs, mspec: Any, out_dir: str | Path, *, seed: int = 0, limit: int | None = None, force: bool = False, chunk_rows: int = DEFAULT_CHUNK_ROWS,
    match_spec: MatchSpec | None = None, max_label_bars: int = DEFAULT_MAX_BARS, code: dict[str, Any] | None = None, steps: Sequence[str] = ("events", "controls"),
) -> dict[str, Any]:
    """Both steps of ONE market. Returns the events manifest with the controls manifest under ``controls`` (when run). Idempotent per step."""
    code = code or code_identity()
    out: dict[str, Any] = {}
    if "events" in steps:
        out = run_events_step(mi, mspec, out_dir, limit=limit, force=force, chunk_rows=chunk_rows, max_label_bars=max_label_bars, code=code)
    if "controls" in steps:
        c = run_controls_step(mi.frame, mspec, mi.market, out_dir, seed=seed, force=force, match_spec=match_spec, chunk_rows=chunk_rows, max_label_bars=max_label_bars, code=code)
        out = {**out, "controls": c} if out else {"market": mi.market, "controls": c}
    return out


def _split_file(src: Path, dest: Path, columns: list[str]) -> int:
    """Write the column subset of ``src`` to ``dest`` streaming by row group batches."""
    pf = pq.ParquetFile(src)
    schema = pa.schema([pf.schema_arrow.field(c) for c in columns])
    tmp = dest.with_suffix(".parquet.tmp")
    n = 0
    with pq.ParquetWriter(tmp, schema, compression="zstd") as w:
        for g in range(pf.num_row_groups):
            t = pf.read_row_group(g, columns=columns)
            w.write_table(t)
            n += len(t)
    os.replace(tmp, dest)
    return n


def _warmup_counts(features_path: Path) -> dict[str, Any]:
    if not features_path.is_file() or "warmup_ok" not in pq.read_schema(features_path).names:
        return {}
    df = pq.read_table(features_path, columns=["warmup_ok"]).to_pandas()
    return {"n": len(df), "warmup_false": int((~df["warmup_ok"].astype(bool)).sum())}


# ---------------------------------------------------------------------------------------------- loader (feature/label separation)
def _read_set(d: Path, files: dict[str, str], with_labels: bool) -> pd.DataFrame | None:
    if not (d / files["features"]).is_file():
        return None
    feats = pd.read_parquet(d / files["features"])
    events = pd.read_parquet(d / files["events"])
    bad = [c for c in (*feats.columns, *events.columns) if c.startswith(LABEL_PREFIX) or c in LABEL_ONLY_COLS]
    if bad:
        raise RuntimeError(f"label columns found in the feature/event files of {d}: {bad[:5]}")
    extra = [c for c in events.columns if c not in feats.columns or c == "event_id"]
    df = feats.merge(events[extra], on="event_id", how="left", validate="one_to_one")
    if with_labels:
        lab = pd.read_parquet(d / files["labels"])
        keep = ["event_id", *[c for c in lab.columns if c not in df.columns]]
        df = df.merge(lab[keep], on="event_id", how="left", validate="one_to_one")
    return df


def load_event_table(path: str | Path, with_labels: bool = False, *, controls: bool = True, columns: list[str] | None = None) -> pd.DataFrame:
    """Events (+ controls unless ``controls=False``) with features, joined on ``event_id``; one market directory or every market directory below ``path``.

    Labels are PHYSICALLY separate: ``labels.parquet`` / ``controls_labels.parquet`` are opened only when ``with_labels=True``; the feature and event files
    contain no ``y_*`` column and no ``horizon_end_ts_ns`` (asserted here as well, so a corrupt directory raises instead of leaking)."""
    root = Path(path)
    dirs = [root] if (root / EVENT_FILES["features"]).is_file() else sorted(p.parent for p in root.glob(f"*/{EVENT_FILES['features']}"))
    if not dirs:
        raise FileNotFoundError(f"no {EVENT_FILES['features']} below {root}")
    frames = []
    for d in dirs:
        for use, files in ((True, EVENT_FILES), (controls, CONTROL_FILES)):
            if use:
                df = _read_set(d, files, with_labels)
                if df is not None:
                    frames.append(df)
    if not frames:
        raise FileNotFoundError(f"nothing readable below {root}")
    out = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    if columns is not None:
        if not with_labels and any(c.startswith(LABEL_PREFIX) or c in LABEL_ONLY_COLS for c in columns):
            raise ValueError("label columns requested with with_labels=False")
        out = out[columns]
    if not with_labels:
        assert not any(c.startswith(LABEL_PREFIX) or c in LABEL_ONLY_COLS for c in out.columns)
    return out
