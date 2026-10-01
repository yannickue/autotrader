# ruff: noqa: E501
"""Historical BACKFILL of the Market Structure Observer (OFFLINE / RESEARCH ONLY; OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

Builds, per market, ONE event table with observer features, matched controls and post-event labels from the development frames.

* EVENTS come from the production family generators (``alpha.families.registry.generate_candidates`` on the frozen specs of the market, exactly the
  generators ``scripts/entry_exit_quality.py`` / the live engine use); one row per (family, variant, direction, decision bar). No second replay engine.
* FEATURES come ONLY from ``market_observer`` (the incremental ``MarketStructureObserver`` stepped once through the bars; the SAME ``bars_adapter`` as the
  live hook). A record is emitted at every event bar and every control bar.
* CONTROLS: ``observer_lab.controls.match_controls`` (market, session bucket, time of day, ATR percentile, spread band, direction inherited, +-48 bars
  exclusion around every event, seeded). Their features are computed by the same observer pass, ``is_control=True``, ``control_of=<event_id>``.
* LABELS: ``observer_lab.labels.label_event`` for events AND controls (bars strictly after the decision bar only; a control inherits the risk distance
  ``R`` (price units) of its event).

Output per market directory (Parquet, zstd): ``table.parquet`` (= ``ObserverRecord.to_row()`` + ``warmup_ok`` + ``run_id``), and the SAME rows split by
column group so a model step can load features without labels: ``events.parquet`` (identity + generator fields + matching covariates),
``features.parquet`` (identity + versions + ``f_*`` + ``m_*``; NO ``y_*``, NO ``horizon_end_ts_ns``), ``labels.parquet`` (``event_id`` + ``y_*`` +
``horizon_end_ts_ns``). ``manifest.json`` is written last (atomic completion marker); ``backfill.log`` is the run log.

``load_event_table(path, with_labels=False)`` physically cannot return ``y_*`` columns unless asked: it never opens ``labels.parquet`` otherwise.
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
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from alpha.common.market_data import DEV_END, assert_no_forward_holdout, gap_summary
from alpha.families.registry import describe_candidate, generate_candidates
from coverage_analysis.observer_lab.controls import (
    CONTROL_METHOD_VERSION,
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
from coverage_analysis.observer_lab.splits import guard_dev_only
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

BACKFILL_VERSION = "observer-backfill-1"
DEFAULT_CHUNK_ROWS = 2000
EVENT_BASE_COLS = ("event_id", "market", "family", "variant", "direction", "is_control", "control_of", "decision_ts_ns", "run_id")
EVENT_EXTRA_COLS = (
    "decision_idx", "strategy_id", "structure_event_id", "entry", "stop", "risk", "risk_atr", "target",
    "match_session", "match_local_minute", "match_atr_pct", "match_spread_pct",
)
LABEL_ONLY_COLS = ("horizon_end_ts_ns",)
FILES = ("table", "events", "features", "labels")
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
    seen: dict[tuple, int] = {}
    rows: list[EventRow] = []
    for fs in mi.specs:
        spec = fs.spec
        cands = generate_candidates(d, spec, fs.thr)
        variant = _variant_of(spec)
        for ci in range(len(cands.decision_idx)):
            i = int(cands.decision_idx[ci])
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
    return rows, dict(excl)


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
        "method": rep.method,
    }


def _fingerprint(code: dict[str, Any], market: str, seed: int, limit: int | None, config_hash: str, data_fp: str, spec: MatchSpec, max_bars: int) -> str:
    payload = json.dumps(
        {"v": BACKFILL_VERSION, "code": code["git_sha"], "src": code["backfill_source_hash"], "market": market, "seed": seed, "limit": limit, "cfg": config_hash,
         "data": data_fp, "match": asdict(spec), "ctl": CONTROL_METHOD_VERSION, "lab": LABEL_CONVENTION_VERSION, "obs": OBSERVER_VERSION, "max_bars": max_bars},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def run_market_backfill(
    mi: MarketInputs, mspec: Any, out_dir: str | Path, *, seed: int = 0, limit: int | None = None, force: bool = False, chunk_rows: int = DEFAULT_CHUNK_ROWS,
    match_spec: MatchSpec | None = None, max_label_bars: int = DEFAULT_MAX_BARS, code: dict[str, Any] | None = None, extra_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backfill ONE market into ``<out_dir>/<MARKET>/``. Idempotent: a complete directory with the same fingerprint is left untouched (``status`` ->
    ``SKIPPED_COMPLETE``); anything else is rebuilt from scratch (the observer is sequential, so the unit of resumption is the market)."""
    t0 = time.time()
    market = mi.market
    mdir = Path(out_dir) / market
    match_spec = match_spec or MatchSpec()
    code = code or code_identity()
    frame = mi.frame
    assert_no_forward_holdout(frame)  # existing guard: no bar after the Berlin dev end
    bars = build_bars(frame, mspec, market)
    guard_dev_only(bars.ts_ns)  # observer-lab guard (same rule, independent implementation)
    cfg = observer_config_for(mspec)
    data_fp = frame_fingerprint(frame)
    fp = _fingerprint(code, market, seed, limit, cfg.config_hash(), data_fp, match_spec, max_label_bars)
    run_id = fp[:12]
    mf_path = mdir / "manifest.json"
    if mf_path.is_file() and not force:
        try:
            old = json.loads(mf_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        if old.get("status") == "COMPLETE" and old.get("fingerprint") == fp and all((mdir / f"{k}.parquet").is_file() for k in FILES):
            old["status_this_call"] = "SKIPPED_COMPLETE"
            return old
    if mdir.exists():
        shutil.rmtree(mdir)
    parts_dir = mdir / "_parts"
    parts_dir.mkdir(parents=True)
    fh = logging.FileHandler(mdir / "backfill.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(fh)
    log.setLevel(logging.INFO)
    try:
        log.info("start market=%s run_id=%s seed=%s limit=%s bars=%d data_fp=%s code=%s", market, run_id, seed, limit, len(bars), data_fp, code)
        events, excl = generate_events(mi, limit=limit)
        log.info("events %d (excl %s)", len(events), excl)
        ev_idx = np.array([e.idx for e in events], dtype=np.int64)
        eval_idx = int(pd.DatetimeIndex(frame["ts"]).searchsorted(mi.eval_from))
        eligible = np.arange(len(bars)) >= eval_idx
        if limit is not None and len(events):
            eligible &= np.arange(len(bars)) <= int(ev_idx.max()) + CONTROL_TAIL_BARS
        cs = match_controls(bars, ev_idx, spec=match_spec, seed=seed, eligible=eligible)
        log.info("controls %d match_rate=%.4f", len(cs.control_idx), cs.report.match_rate)
        cov = bar_covariates(bars)

        ev_ids = [event_id_for(market, bars.decision_ts_ns(e.idx), e.family, e.variant, e.direction) for e in events]
        if len(set(ev_ids)) != len(ev_ids):
            raise RuntimeError("duplicate event ids among events")
        items: dict[int, list[_Item]] = defaultdict(list)
        for e, eid in zip(events, ev_ids, strict=True):
            items[e.idx].append(_Item("event", e.idx, e.direction, e.risk, e.family, e.variant, eid, None, e))
        for pos, cidx in zip(cs.event_pos.tolist(), cs.control_idx.tolist(), strict=True):
            e = events[pos]
            items[int(cidx)].append(_Item("control", int(cidx), e.direction, e.risk, e.family, e.variant,
                                          event_id_for(market, bars.decision_ts_ns(int(cidx)), e.family, e.variant, e.direction), ev_ids[pos], e))
        all_ids = [it.event_id for lst in items.values() for it in lst]
        if len(set(all_ids)) != len(all_ids):
            raise RuntimeError("duplicate event ids among events + controls")

        obs = MarketStructureObserver(cfg)
        rows: list[dict[str, Any]] = []
        extras: list[dict[str, Any]] = []
        n_parts = 0
        n_rows = 0
        t_obs = time.time()

        def flush() -> None:
            nonlocal rows, extras, n_parts
            if not rows:
                return
            pq.write_table(_rows_to_table(rows), parts_dir / f"table-{n_parts:05d}.parquet", compression="zstd")
            pq.write_table(_rows_to_table(extras, list(EVENT_BASE_COLS) + list(EVENT_EXTRA_COLS)), parts_dir / f"events-{n_parts:05d}.parquet", compression="zstd")
            n_parts += 1
            rows, extras = [], []

        for i in sorted(items):
            obs.advance(bars, i)
            for it in sorted(items[i], key=lambda x: (x.kind, x.family or "", x.variant or "", x.direction)):
                src = it.event
                oe = ObservedEvent(
                    it.direction, float(bars.c[i]), family=it.family, variant=it.variant,
                    structure_event_id=src.structure_event_id if (it.kind == "event" and src is not None) else None,
                    is_control=it.kind == "control", control_of=it.control_of,
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
                is_ev = it.kind == "event"
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
        t_obs = time.time() - t_obs
        log.info("observer pass done: %d rows in %.1fs (%.2f ms/row incl. registry)", n_rows, t_obs, 1000 * t_obs / max(n_rows, 1))

        # ---- merge the part files into the final column-group files
        tbl_parts = sorted(parts_dir.glob("table-*.parquet"))
        ev_parts = sorted(parts_dir.glob("events-*.parquet"))
        counts: dict[str, int] = {}
        if tbl_parts:
            n_tbl = _merge_parts(tbl_parts, mdir / "table.parquet")
            full_cols = pq.read_schema(mdir / "table.parquet").names
            label_cols = [c for c in full_cols if c.startswith(LABEL_PREFIX) or c in LABEL_ONLY_COLS]
            feat_cols = [c for c in full_cols if c not in label_cols]
            lab_cols = ["event_id", "is_control", "control_of", *label_cols]
            counts["table"] = n_tbl
            counts["features"] = _split_file(mdir / "table.parquet", mdir / "features.parquet", feat_cols)
            counts["labels"] = _split_file(mdir / "table.parquet", mdir / "labels.parquet", lab_cols)
            counts["events"] = _merge_parts(ev_parts, mdir / "events.parquet", list(EVENT_BASE_COLS) + list(EVENT_EXTRA_COLS))
        else:  # no event at all: write empty but well-formed files
            for k in FILES:
                pq.write_table(pa.table({"event_id": pa.array([], pa.string())}), mdir / f"{k}.parquet")
                counts[k] = 0
        shutil.rmtree(parts_dir)

        fam_counts: dict[str, int] = {}
        for e in events:
            fam_counts[f"{e.family}|{e.variant or ''}|{'long' if e.direction > 0 else 'short'}"] = fam_counts.get(f"{e.family}|{e.variant or ''}|{'long' if e.direction > 0 else 'short'}", 0) + 1
        warm = _warmup_counts(mdir / "features.parquet")
        manifest = {
            "status": "COMPLETE", "fingerprint": fp, "run_id": run_id, "backfill_version": BACKFILL_VERSION, "market": market, "observation_only": True,
            "code": code, "observer_version": OBSERVER_VERSION, "schema_version": SCHEMA_VERSION, "group_versions": {k: v for k, v in GROUP_VERSIONS.items() if k != "fib"},
            "definition_hashes": cfg.definition_hashes(), "observer_config_hash": cfg.config_hash(), "label_convention_version": LABEL_CONVENTION_VERSION,
            "label_max_bars": max_label_bars, "control_method_version": CONTROL_METHOD_VERSION, "match_spec": asdict(match_spec), "seed": seed, "limit": limit,
            "dev_end_guard": {"dev_end_berlin_date": DEV_END, "assert_no_forward_holdout": "PASS", "guard_dev_only": "PASS", "last_bar_utc": pd.Timestamp(int(bars.ts_ns[-1]), tz="UTC").isoformat()},
            "data": {**_coverage(frame, mi.tz), "frame_fingerprint": data_fp, "eval_from_utc": pd.Timestamp(mi.eval_from).isoformat(), "eval_from_index": eval_idx},
            "events": {"n_events": len(events), "n_controls": len(cs.control_idx), "by_family_variant_direction": dict(sorted(fam_counts.items())), "exclusions": excl,
                       "warmup": warm},
            "match_report": _matching_report_dict(cs.report), "rows": counts, "files": {k: f"{k}.parquet" for k in FILES},
            "runtime_s": round(time.time() - t0, 1), "observer_pass_s": round(t_obs, 1), "peak_memory_mb": peak_memory_mb(), "limit_mode": limit is not None,
            "caveats": [
                "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED; no edge claim", "events = production generator opportunities WITHOUT the live operating policy filters",
                "a control inherits the risk distance R (price units) and direction of its event; control family/variant are inherited labels, not generator output",
                "match_* columns of events.parquet are descriptive full-sample percentile ranks (NOT causal features; never feed them to a model as features)",
            ],
            **(extra_manifest or {}),
        }
        mf_path.write_text(json.dumps(manifest, indent=1, sort_keys=False, default=str), encoding="utf-8")
        log.info("done market=%s rows=%s runtime=%.1fs peak_mb=%s", market, counts, time.time() - t0, manifest["peak_memory_mb"])
        manifest["status_this_call"] = "BUILT"
        return manifest
    finally:
        log.removeHandler(fh)
        fh.close()


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
    df = pq.read_table(features_path, columns=["warmup_ok", "is_control"]).to_pandas()
    out: dict[str, Any] = {}
    for name, sub in (("events", df[~df["is_control"].astype(bool)]), ("controls", df[df["is_control"].astype(bool)])):
        out[name] = {"n": len(sub), "warmup_false": int((~sub["warmup_ok"].astype(bool)).sum())}
    return out


# ---------------------------------------------------------------------------------------------- loader (feature/label separation)
def load_event_table(path: str | Path, with_labels: bool = False, *, columns: list[str] | None = None) -> pd.DataFrame:
    """Events + features of one market directory (or of every market directory below ``path``), joined on ``event_id``.

    Labels are PHYSICALLY separate: ``labels.parquet`` is opened only when ``with_labels=True``; ``features.parquet`` / ``events.parquet`` contain no
    ``y_*`` column and no ``horizon_end_ts_ns`` (asserted here as well, so a corrupt directory raises instead of leaking)."""
    root = Path(path)
    dirs = [root] if (root / "features.parquet").is_file() else sorted(p.parent for p in root.glob("*/features.parquet"))
    if not dirs:
        raise FileNotFoundError(f"no features.parquet below {root}")
    frames = []
    for d in dirs:
        feats = pd.read_parquet(d / "features.parquet")
        events = pd.read_parquet(d / "events.parquet")
        bad = [c for c in (*feats.columns, *events.columns) if c.startswith(LABEL_PREFIX) or c in LABEL_ONLY_COLS]
        if bad:
            raise RuntimeError(f"label columns found in the feature/event files of {d}: {bad[:5]}")
        extra = [c for c in events.columns if c not in feats.columns or c == "event_id"]
        df = feats.merge(events[extra], on="event_id", how="left", validate="one_to_one")
        if with_labels:
            lab = pd.read_parquet(d / "labels.parquet")
            keep = ["event_id", *[c for c in lab.columns if c not in df.columns]]
            df = df.merge(lab[keep], on="event_id", how="left", validate="one_to_one")
        frames.append(df)
    out = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    if columns is not None:
        if not with_labels and any(c.startswith(LABEL_PREFIX) or c in LABEL_ONLY_COLS for c in columns):
            raise ValueError("label columns requested with with_labels=False")
        out = out[columns]
    if not with_labels:
        assert not any(c.startswith(LABEL_PREFIX) or c in LABEL_ONLY_COLS for c in out.columns)
    return out
