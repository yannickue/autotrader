# ruff: noqa: E501
"""Gate C of the Market Structure Observer: preregistered single-feature enrichment against matched controls (OFFLINE / RESEARCH ONLY).

    uv run python scripts/observer_gate_c.py --root <backfill dir> [--markets GER40 NAS100 ...] [--out DIR] [--stage fit|validate|oos]
        [--jobs 2] [--prereg docs/OBSERVER_GATE_C_PREREGISTRATION.md] [--dry-run] [--force] [--confirm-oos-once]

Never touches the live trader, ``artifacts/``, MT5, schedulers or the trading DB. The backfill directory (``--root``) is opened READ-ONLY; the
registry, the per-market caches and the report go to ``--out`` (default ``%LOCALAPPDATA%\\Temp\\observer_gate_c``, refused if it lies inside ``--root``).

What it does (see ``docs/OBSERVER_GATE_C_PREREGISTRATION.md``, the single source of truth, parsed here):

* reads the preregistration (hypothesis family, cells, signs, thresholds) from the machine-readable block of that document and records the file
  hash and the block hash in the report;
* per market (``--jobs`` worker processes, default 2, max 2; results are independent of the number of workers and bit-identical to a serial run):
  loads ONLY the rows of the stage's partition (``fit`` = TRAIN, ``validate`` = VALIDATION, ``oos`` = OOS, plus the PURGED / EMBARGO tags so they are
  counted), builds the derived features, freezes the cells on TRAIN (``fit``) or applies the stored ones, and calls
  ``observer_lab.enrichment.incremental_ablation`` with the predeclared contrasts;
* registers every (hypothesis x market) of the stage in its OWN persistent registry (``observer_gate_c_registry.json``) BEFORE any is evaluated,
  records the bootstrap p-values and applies Holm within the family stage x scope x label x feature group;
* decides survivors / the stop rule, runs the deterministic negative control in every stage, and writes ``observer_gate_c_report.{md,json}``.

The forward period (>= 2026-09-01) is never read: a Parquet file whose ``decision_ts_ns`` statistics reach it is refused before any row is read, there is
no forward stage, and the enrichment guards of the lab are called on every table. Nothing here is an edge claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCRIPT_VERSION = "observer-gate-c-1"
DEFAULT_ROOT = Path(os.environ.get("LOCALAPPDATA", ".")) / "Temp" / "observer_backfill"
DEFAULT_OUT = Path(os.environ.get("LOCALAPPDATA", ".")) / "Temp" / "observer_gate_c"
DEFAULT_PREREG = ROOT / "docs" / "OBSERVER_GATE_C_PREREGISTRATION.md"
REGISTRY_FILE = "observer_gate_c_registry.json"
REPORT_STEM = "observer_gate_c_report"
MAX_JOBS = 2  # 8 GB machine
FILE_PARTITION = {"TRAIN": "TRAIN", "VALIDATION": "VALIDATION", "OOS": "FROZEN_OOS"}  # partition names as written by the backfill
UNUSABLE_TAGS = ("PURGED", "EMBARGO")
BASE_COLS = ("event_id", "is_control", "control_of", "decision_ts_ns", "direction", "warmup_ok", "partition")
DAY_COL = "m_local_day"
BERLIN = "Europe/Berlin"
CELL_UNDEFINED = "CELL_UNDEFINED"
NEEDS = {  # derived feature -> raw columns it is computed from (decision-time columns and the event direction only)
    "f_swings__x_ema_trend_aligned": ("f_swings__ema_trend",),
    "f_swings__x_m15_sequence_aligned": ("f_swings__m15_sequence",),
    "f_ctrl__x_random_uniform": (),
}
MARKER_BEGIN, MARKER_END = "<!-- PREREG-JSON-BEGIN -->", "<!-- PREREG-JSON-END -->"
V_FIT_OK, V_NONE, V_INCONCLUSIVE, V_INVALID = "ENRICHMENT_CANDIDATES_TO_VALIDATE", "NO_ENRICHMENT", "INCONCLUSIVE_INSUFFICIENT_EVIDENCE", "INVALID_NEGATIVE_CONTROL_FAILED"
V_VAL_OK, V_OOS_OK, V_NOT_CONFIRMED = "CONFIRMED_ON_VALIDATION", "REPLICATED_ON_OOS", "NOT_CONFIRMED"


class StopRule(RuntimeError):
    """A preregistered stop rule forbids this stage (documented outcome, exit code 3)."""


# ---------------------------------------------------------------------------------------------- preregistration
@dataclass(frozen=True)
class Prereg:
    spec: dict[str, Any]
    file_sha256: str
    json_sha256: str
    path: str


def _canon(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def load_prereg(path: str | Path) -> Prereg:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if MARKER_BEGIN not in text or MARKER_END not in text:
        raise ValueError(f"{p}: preregistration markers {MARKER_BEGIN} / {MARKER_END} not found")
    block = text.split(MARKER_BEGIN, 1)[1].split(MARKER_END, 1)[0]
    m = re.search(r"```json\s*(\{.*\})\s*```", block, re.DOTALL)
    if not m:
        raise ValueError(f"{p}: no ```json block between the preregistration markers")
    spec = json.loads(m.group(1))
    _validate_prereg(spec)
    return Prereg(spec, hashlib.sha256(text.encode("utf-8")).hexdigest(), hashlib.sha256(_canon(spec).encode()).hexdigest(), str(p))


def _validate_prereg(s: dict[str, Any]) -> None:
    for k in ("prereg_version", "registry_name", "label", "markets", "stages", "test", "min_evidence", "min_effect_abs", "hypotheses", "negative_controls"):
        if k not in s:
            raise ValueError(f"preregistration lacks {k!r}")
    if not str(s["label"]).startswith("y_"):
        raise ValueError("label must be a y_* column")
    if set(s["stages"]) != {"fit", "validate", "oos"}:
        raise ValueError("stages must be exactly fit / validate / oos (there is no forward stage)")
    if s["test"]["kind"] != "incremental_ablation" or s["test"]["adjust"] not in ("holm", "bh") or s["test"]["p_method"] != "bootstrap":
        raise ValueError("unsupported test definition")
    ids = [h["id"] for h in (*s["hypotheses"], *s["negative_controls"])]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate hypothesis ids")
    keys = [(h["group"], h["feature"], h["cell"]) for h in (*s["hypotheses"], *s["negative_controls"])]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate (group, feature, cell) contrast")
    for h in s["hypotheses"]:
        if h["sign"] not in (-1, 1):
            raise ValueError(f"{h['id']}: sign must be +1 or -1")
    for h in (*s["hypotheses"], *s["negative_controls"]):
        f = h["feature"]
        if not f.startswith("f_") or f.endswith("_ts_ns"):
            raise ValueError(f"{h['id']}: {f!r} is not a decision-feature column")
        if f.split("__", 1)[0][2:] != h["group"]:
            raise ValueError(f"{h['id']}: group {h['group']!r} does not match the column group of {f!r}")
        if "__x_" in f and f not in NEEDS:
            raise ValueError(f"{h['id']}: derived feature {f!r} has no implementation in this script")
    if set(s["markets"]["core"]) & set(s["markets"]["explore"]):
        raise ValueError("a market cannot be both core and explore")


def market_scope(prereg: Prereg, market: str) -> str:
    for scope, ms in prereg.spec["markets"].items():
        if market in ms:
            return scope
    raise ValueError(f"unknown market {market!r}; preregistered: {prereg.spec['markets']}")


# ---------------------------------------------------------------------------------------------- data (read-only, partition-filtered)
def forward_start_ns() -> int:
    from alpha.common.market_data import FORWARD_HOLDOUT_START

    return int(pd.Timestamp(FORWARD_HOLDOUT_START, tz=BERLIN).value)


def assert_file_dev_only(path: Path) -> None:
    """Refuse a Parquet file that reaches the forward period, from the FOOTER statistics of ``decision_ts_ns`` (no row data is read)."""
    import pyarrow.parquet as pq

    from alpha.common.market_data import ForwardHoldoutError

    pf = pq.ParquetFile(path)
    names = list(pf.schema_arrow.names)
    if "decision_ts_ns" not in names:
        raise ValueError(f"{path}: no decision_ts_ns column")
    idx = names.index("decision_ts_ns")
    limit, mx = forward_start_ns(), None
    for rg in range(pf.num_row_groups):
        st = pf.metadata.row_group(rg).column(idx).statistics
        if st is None or not st.has_min_max:
            mx = None
            break
        mx = st.max if mx is None else max(mx, st.max)
    if mx is None and pf.num_row_groups:  # no footer statistics: read that single timestamp column only
        mx = int(pq.read_table(path, columns=["decision_ts_ns"]).column(0).to_numpy().max()) if pf.metadata.num_rows else 0
    if mx is not None and int(mx) >= limit:
        raise ForwardHoldoutError(f"{path}: decision_ts_ns reaches {pd.Timestamp(int(mx), tz='UTC')} >= forward holdout start; Gate C never reads the forward period")


def berlin_day_ordinal(ts_ns: np.ndarray) -> np.ndarray:
    from coverage_analysis.observer_lab import splits as SP

    return pd.to_datetime(SP.berlin_dates(ts_ns)).values.astype("datetime64[D]").astype("int64")


def load_stage_frame(root: str | Path, market: str, partition: str, columns: list[str]) -> pd.DataFrame:
    """Events (``table.parquet``) and controls (``controls.parquet``) of ONE market restricted to the stage partition (+ PURGED/EMBARGO tags), only ``columns``."""
    import pyarrow.parquet as pq

    mdir = Path(root) / market
    frames = []
    for fname in ("table.parquet", "controls.parquet"):
        p = mdir / fname
        if not p.is_file():
            raise FileNotFoundError(f"{p} not found (the backfill must be complete: events and controls)")
        assert_file_dev_only(p)
        have = set(pq.read_schema(p).names)
        missing = [c for c in columns if c not in have]
        if missing:
            raise ValueError(f"{p}: columns missing {missing[:5]}")
        frames.append(pd.read_parquet(p, columns=columns, filters=[("partition", "in", [FILE_PARTITION[partition], *UNUSABLE_TAGS])]))
    df = pd.concat(frames, ignore_index=True)
    df["partition"] = df["partition"].replace({"FROZEN_OOS": "OOS"})
    return df


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    d = df["direction"].to_numpy()
    n = len(df)
    if "f_swings__ema_trend" in df.columns:
        t = df["f_swings__ema_trend"].astype(object).to_numpy()
        out = np.full(n, None, dtype=object)
        up, dn = t == "up", t == "down"
        out[(up & (d > 0)) | (dn & (d < 0))] = "with"
        out[(up & (d < 0)) | (dn & (d > 0))] = "against"
        out[t == "flat"] = "flat"
        df["f_swings__x_ema_trend_aligned"] = out
    if "f_swings__m15_sequence" in df.columns:
        t = df["f_swings__m15_sequence"].astype(object).to_numpy()
        out = np.full(n, None, dtype=object)
        up, dn = t == "UP_SEQUENCE", t == "DOWN_SEQUENCE"
        out[(up & (d > 0)) | (dn & (d < 0))] = "with"
        out[(up & (d < 0)) | (dn & (d > 0))] = "against"
        out[t == "MIXED_TRANSITION"] = "mixed"
        out[t == "RANGE_OR_UNDEFINED"] = "range"
        df["f_swings__x_m15_sequence_aligned"] = out
    df["f_ctrl__x_random_uniform"] = np.fromiter((zlib.crc32(f"gatec|nc|{e}".encode()) for e in df["event_id"]), dtype=np.uint64, count=n) / float(2**32)
    return df


def prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reset_index(drop=True)
    df[DAY_COL] = berlin_day_ordinal(df["decision_ts_ns"].to_numpy(dtype="int64")) if len(df) else np.array([], dtype="int64")
    df["is_control"] = df["is_control"].astype(bool)
    df["warmup_ok"] = df["warmup_ok"].fillna(False).astype(bool)
    return add_derived(df)


# ---------------------------------------------------------------------------------------------- per-market worker (pure, deterministic)
def _defs_to_json(defs: dict[str, Any]) -> dict[str, Any]:
    return {f: {"feature": d.feature, "kind": d.kind, "edges": list(d.edges), "categories": list(d.categories), "has_missing": d.has_missing} for f, d in defs.items()}


def _defs_from_json(js: dict[str, Any]) -> dict[str, Any]:
    from coverage_analysis.observer_lab.enrichment import CellDef

    return {f: CellDef(v["feature"], v["kind"], tuple(v["edges"]), tuple(v["categories"]), bool(v["has_missing"])) for f, v in js.items()}


def _num(x: Any) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def run_market(task: dict[str, Any]) -> dict[str, Any]:
    """Evaluate every contrast of ONE market for ONE stage. Pure function of ``task`` and the Parquet files; no registry, no shared state."""
    from coverage_analysis.observer_lab import enrichment as EN
    from coverage_analysis.observer_lab import stats as ST

    market, stage, label, part = task["market"], task["stage"], task["label"], task["partition"]
    contrasts = task["contrasts"]
    df = prepare_frame(load_stage_frame(task["root"], market, part, required_columns_from_task(task)))
    me = task["min_evidence"]
    cfg = EN.EnrichmentConfig(
        n_quantiles=task["n_quantiles"], min_evidence=ST.MinEvidence(me["events"], me["controls"], me["blocks"], 20), B=task["B"], seed=task["seed"], alpha=task["alpha"],
        adjust=task["adjust"], adjust_scope="family", p_method="bootstrap", day_col=DAY_COL,
    )
    ev = df[(~df["is_control"]) & (df["partition"] == part) & df["warmup_ok"]]
    given = _defs_from_json(task["cell_defs"]) if task.get("cell_defs") is not None else None
    defs: dict[str, Any] = {}
    note: dict[str, str] = {}
    for f in dict.fromkeys(c["feature"] for c in contrasts):
        if given is not None:
            if f in given:
                defs[f] = given[f]
            else:
                note[f] = "no stored cell definition from the fit stage"
        elif len(ev) == 0:
            note[f] = "no TRAIN events"
        else:
            try:
                defs[f] = EN.fit_cell_def(ev[f], cfg)
            except ValueError as e:
                note[f] = f"cells not definable: {e}"
    rows: list[dict[str, Any]] = []
    testable = []
    for c in contrasts:
        d = defs.get(c["feature"])
        if d is None or c["cell"] not in d.labels():
            rows.append({"id": c["id"], "group": c["group"], "feature": c["feature"], "cell": c["cell"], "evidence": CELL_UNDEFINED, "n_event": 0, "n_control": 0, "p_event": None, "p_control": None,
                         "delta": None, "base_delta": None, "ci_low": None, "ci_high": None, "n_blocks": 0, "p_boot": None, "note": note.get(c["feature"], f"cell {c['cell']!r} not in the frozen definition {d.labels() if d else '-'}")})
        else:
            testable.append(c)
    counts = {"n_rows": len(df), "n_events_stage": int(((~df["is_control"]) & (df["partition"] == part)).sum()), "n_controls_stage": int(((df["is_control"]) & (df["partition"] == part)).sum()),
              "n_events_warm": len(ev), "n_purged_embargo_rows": int(df["partition"].isin(UNUSABLE_TAGS).sum())}
    warns: list[str] = []
    if testable and len(ev):
        groups: dict[str, list[str]] = {}
        for c in testable:
            groups.setdefault(c["group"], [])
            if c["feature"] not in groups[c["group"]]:
                groups[c["group"]].append(c["feature"])
        rep = EN.incremental_ablation(
            df, [label], np.ones(len(df), dtype=bool), groups, ST.HypothesisRegistry(f"gate-c-worker-{market}-{stage}"), cfg, purpose=task["purpose"],
            cell_defs={f: defs[f] for g in groups.values() for f in g},
            contrasts=[EN.PredeclaredContrast(c["group"], c["feature"], c["cell"]) for c in testable],
        )
        by = {(r.group, r.feature, r.cell.removeprefix("base AND ")): r for r in rep.results}
        for c in testable:
            r = by[(c["group"], c["feature"], c["cell"])]
            insuff = r.status == ST.INSUFFICIENT_EVIDENCE
            rows.append({"id": c["id"], "group": c["group"], "feature": c["feature"], "cell": c["cell"], "evidence": ST.INSUFFICIENT_EVIDENCE if insuff else ST.OK, "n_event": int(r.n_event), "n_control": int(r.n_control),
                         "p_event": _num(r.p_event), "p_control": _num(r.p_control), "delta": _num(r.delta), "base_delta": _num(r.base_delta), "ci_low": _num(r.ci_low), "ci_high": _num(r.ci_high), "n_blocks": int(r.n_blocks),
                         "p_boot": None if insuff else _num(r.p_boot), "note": ""})
        counts.update({"n_events_used": rep.n_events_used, "n_controls_used": rep.n_controls_used, "n_excluded_warmup_events": rep.n_excluded_warmup_events, "n_orphan_controls_dropped": rep.n_orphan_controls_dropped})
        warns = [w for w in rep.warnings if "power_limited" not in w]
    elif testable:  # nothing to test on
        for c in testable:
            rows.append({"id": c["id"], "group": c["group"], "feature": c["feature"], "cell": c["cell"], "evidence": "INSUFFICIENT_EVIDENCE", "n_event": 0, "n_control": 0, "p_event": None, "p_control": None,
                         "delta": None, "base_delta": None, "ci_low": None, "ci_high": None, "n_blocks": 0, "p_boot": None, "note": "no events in the stage partition"})
    order = {c["id"]: i for i, c in enumerate(contrasts)}
    rows.sort(key=lambda r: order[r["id"]])
    return {"market": market, "stage": stage, "rows": rows, "cell_defs": _defs_to_json(defs), "counts": counts, "warnings": warns}


def required_columns_from_task(task: dict[str, Any]) -> list[str]:
    cols = [*BASE_COLS, task["label"]]
    for c in task["contrasts"]:
        cols.extend(NEEDS.get(c["feature"], (c["feature"],)))
    return list(dict.fromkeys(cols))


# ---------------------------------------------------------------------------------------------- plan / registry / cache
def _family(stage: str, scope: str, label: str, group: str) -> str:
    return f"gatec|{stage}|{scope}|{label}|{group}"


def _name(stage: str, market: str, label: str, item: dict[str, Any]) -> str:
    return f"gatec|{stage}|{market}|{label}|{item['group']}|{item['feature']}|{item['cell']}"


def build_plan(prereg: Prereg, stage: str, markets: list[str], survivors: list[str] | None) -> list[dict[str, Any]]:
    """[(market x hypothesis) items]. ``survivors`` (keys ``MARKET|Hxx``) restricts validate / oos to the previous stage's survivors (+ the negative control of those markets)."""
    s = prereg.spec
    plan: list[dict[str, Any]] = []
    surv_markets = {k.split("|")[0] for k in survivors} if survivors is not None else None
    for m in markets:
        scope = market_scope(prereg, m)
        for kind, items in (("hyp", s["hypotheses"]), ("nc", s["negative_controls"])):
            for h in items:
                if survivors is not None:
                    if kind == "hyp" and f"{m}|{h['id']}" not in survivors:
                        continue
                    if kind == "nc" and m not in surv_markets:
                        continue
                plan.append({**h, "kind": kind, "market": m, "scope": scope, "key": f"{m}|{h['id']}", "name": _name(stage, m, s["label"], h), "family": _family(stage, scope, s["label"], h["group"])})
    return plan


def family_sizes(plan: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in plan:
        out[p["family"]] = out.get(p["family"], 0) + 1
    return out


def choose_B(prereg: Prereg, fams: dict[str, int]) -> Any:
    from coverage_analysis.observer_lab import stats as ST

    t = prereg.spec["test"]
    return ST.choose_B(max(fams.values(), default=1), t["alpha"], b_min=t["b_min"], b_max=t["b_max"])


def registry_recorded(path: Path) -> dict[str, tuple[bool, float | None]]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {h: (bool(v.get("recorded")), v.get("p")) for h, v in data["hypotheses"].items()}


def register_stage(reg: Any, plan: list[dict[str, Any]], existing: dict[str, Any]) -> bool:
    """Register the whole stage BEFORE evaluation. Returns True when the stage was already fully registered (a re-run / resume). A partial overlap is an error."""
    from coverage_analysis.observer_lab.stats import HypothesisReuseError

    names = [p["name"] for p in plan]
    have = [n in existing for n in names]
    if all(have) and names:
        return True
    if any(have):
        raise HypothesisReuseError("the stage is only partly registered in the registry; refusing to continue (no best-of-N)")
    fams: dict[str, list[str]] = {}
    for p in plan:
        fams.setdefault(p["family"], []).append(p["name"])
    for f, ns in fams.items():
        reg.declare_family(f, definition=json.dumps(sorted(ns)))
    reg.register_many(names, [p["family"] for p in plan])
    return False


def fingerprint(prereg: Prereg, task: dict[str, Any], root: Path) -> str:
    files = []
    for fname in ("table.parquet", "controls.parquet"):
        p = root / task["market"] / fname
        st = p.stat() if p.is_file() else None
        files.append([fname, None if st is None else st.st_size, None if st is None else st.st_mtime_ns])
    body = {"v": SCRIPT_VERSION, "prereg": prereg.json_sha256, "stage": task["stage"], "market": task["market"], "B": task["B"], "seed": task["seed"], "files": files,
            "contrasts": [(c["id"], c["feature"], c["cell"]) for c in task["contrasts"]], "cell_defs": hashlib.sha256(_canon(task.get("cell_defs")).encode()).hexdigest()}
    return hashlib.sha256(_canon(body).encode()).hexdigest()


def _evaluate_markets(tasks: list[dict[str, Any]], jobs: int) -> list[dict[str, Any]]:
    if jobs > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks))) as ex:
            return list(ex.map(run_market, tasks))  # order preserved: the result never depends on completion order
    return [run_market(t) for t in tasks]


# ---------------------------------------------------------------------------------------------- stage run
def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not math.isfinite(float(o)) else float(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def previous_survivors(prereg: Prereg, stage: str, report: dict[str, Any], confirm_oos: bool) -> tuple[list[str], dict[str, Any] | None]:
    prev = {"validate": "fit", "oos": "validate"}.get(stage)
    if prev is None:
        return [], None
    st = (report.get("stages") or {}).get(prev)
    if not st or st.get("status") != "COMPLETE":
        raise StopRule(f"stage {stage!r} needs a COMPLETE {prev!r} stage in the report (run it first)")
    if st.get("prereg_json_sha256") != prereg.json_sha256:
        raise StopRule(f"the {prev!r} stage was run under a different preregistration block; a new preregistration needs a new --out")
    if not st.get("survivors"):
        raise StopRule(f"STOP RULE: stage {prev!r} ended with {st.get('verdict')} and no survivors: documented as 'no enrichment' / not confirmed; {stage!r} is not run")
    if stage == "oos" and not confirm_oos:
        raise StopRule("the OOS partition is touched exactly once; pass --confirm-oos-once to proceed")
    return list(st["survivors"]), st


def stage_verdict(stage: str, results: list[dict[str, Any]]) -> tuple[str, list[str], list[str]]:
    from coverage_analysis.observer_lab import stats as ST

    nc_fail = [r["key"] + "|" + r["id"] for r in results if r["kind"] == "nc" and r["status"] == ST.SIGNIFICANT_ADJUSTED]
    surv = [r["key"] for r in results if r["kind"] == "hyp" and r["survivor"]]
    if nc_fail:
        return V_INVALID, [], nc_fail
    hyp = [r for r in results if r["kind"] == "hyp"]
    if stage == "fit":
        if surv:
            return V_FIT_OK, surv, []
        if hyp and all(r["status"] in (ST.INSUFFICIENT_EVIDENCE, ST.POWER_LIMITED, CELL_UNDEFINED) for r in hyp):
            return V_INCONCLUSIVE, [], []
        return V_NONE, [], []
    if surv:
        return (V_VAL_OK if stage == "validate" else V_OOS_OK), surv, []
    return V_NOT_CONFIRMED, [], []


def run_stage(a: argparse.Namespace, prereg: Prereg, stage: str, markets: list[str]) -> dict[str, Any]:
    from coverage_analysis.observer_lab import stats as ST

    spec = prereg.spec
    out, root = Path(a.out), Path(a.root)
    report = load_report(out / f"{REPORT_STEM}.json")
    survivors, prev = previous_survivors(prereg, stage, report, a.confirm_oos_once)
    plan = build_plan(prereg, stage, markets, survivors if stage != "fit" else None)
    if not plan:
        raise StopRule("nothing to test for the requested markets (no survivors there)")
    fams = family_sizes(plan)
    choice = choose_B(prereg, fams)
    label, part, purpose = spec["label"], spec["stages"][stage]["partition"], spec["stages"][stage]["purpose"]
    prev_defs = (prev or {}).get("markets", {})
    by_market: dict[str, list[dict[str, Any]]] = {}
    for p in plan:
        by_market.setdefault(p["market"], []).append(p)
    tasks = []
    for m, items in by_market.items():
        t = {"market": m, "stage": stage, "label": label, "partition": part, "purpose": purpose, "root": str(root), "B": choice.B, "seed": zlib.crc32(f"gatec|{stage}|{m}".encode()) & 0x7FFFFFFF,
             "n_quantiles": spec["test"]["n_quantiles"], "alpha": spec["test"]["alpha"], "adjust": spec["test"]["adjust"], "min_evidence": spec["min_evidence"],
             "contrasts": [{k: h[k] for k in ("id", "group", "feature", "cell")} for h in items], "cell_defs": None if stage == "fit" else (prev_defs.get(m) or {}).get("cell_defs")}
        if stage != "fit" and t["cell_defs"] is None:
            raise StopRule(f"no frozen cell definitions for {m} in the previous stage")
        tasks.append(t)
    # ---- registry: the whole stage is registered BEFORE anything is evaluated
    reg_path = out / REGISTRY_FILE
    reg = ST.HypothesisRegistry(spec["registry_name"], reg_path)
    existing = registry_recorded(reg_path)
    resumed = register_stage(reg, plan, existing)
    # ---- per-market evaluation with a cache (fingerprint = prereg block + stage + inputs + parameters)
    cache_dir = out / "cache" / stage
    cache_dir.mkdir(parents=True, exist_ok=True)
    results_by_market: dict[str, dict[str, Any]] = {}
    todo = []
    for t in tasks:
        cp = cache_dir / f"{t['market']}.json"
        fp = fingerprint(prereg, t, root)
        if cp.is_file() and not a.force:
            c = json.loads(cp.read_text(encoding="utf-8"))
            if c.get("fingerprint") == fp:
                results_by_market[t["market"]] = c["result"]
                continue
        todo.append((t, fp, cp))
    t0 = time.time()
    fresh = _evaluate_markets([x[0] for x in todo], a.jobs)
    for (t, fp, cp), res in zip(todo, fresh, strict=True):
        res = _clean(res)
        cp.write_text(json.dumps({"fingerprint": fp, "result": res}, indent=1), encoding="utf-8")
        results_by_market[t["market"]] = res
    timing = {"evaluated_markets": [x[0]["market"] for x in todo], "cached_markets": [m for m in by_market if m not in {x[0]["market"] for x in todo}], "seconds": round(time.time() - t0, 2), "jobs": a.jobs}
    # ---- record p-values (a re-run must reproduce the registered ones exactly), Holm within the family
    rows: dict[str, dict[str, Any]] = {}
    for p in plan:
        rows[p["name"]] = next(r for r in results_by_market[p["market"]]["rows"] if r["id"] == p["id"])
    for p in plan:
        pv = rows[p["name"]]["p_boot"] if rows[p["name"]]["evidence"] == ST.OK else None
        if p["name"] in existing and existing[p["name"]][0]:
            if existing[p["name"]][1] != pv:
                raise ST.HypothesisReuseError(f"{p['name']}: registered result {existing[p['name']][1]} differs from the recomputed {pv}; results of a registered hypothesis are never replaced")
        else:
            reg.record(p["name"], pv)
    reg.flush()
    adj = reg.adjust(spec["test"]["adjust"], "family")
    adj_reg = reg.adjust(spec["test"]["adjust"], "registry")
    alpha, min_eff = spec["test"]["alpha"], spec["min_effect_abs"]
    results = []
    for p in plan:
        r = rows[p["name"]]
        m_fam = reg.family_size(p["family"])
        pl = ST.is_power_limited(m_fam, choice.B, alpha)
        a_p = adj.get(p["name"])
        if r["evidence"] == CELL_UNDEFINED:
            status = CELL_UNDEFINED
        elif r["evidence"] == ST.INSUFFICIENT_EVIDENCE:
            status = ST.INSUFFICIENT_EVIDENCE
        else:
            status = ST.final_status(ST.OK, a_p, r["ci_low"], r["ci_high"], alpha, power_limited=pl)
        d = r["delta"]
        sign_ok = "NA" if p["kind"] == "nc" or d is None else ("EXPECTED" if np.sign(d) == p["sign"] else "CONTRARY")
        surv = p["kind"] == "hyp" and p["scope"] == "core" and status == ST.SIGNIFICANT_ADJUSTED and sign_ok == "EXPECTED" and d is not None and abs(d) >= min_eff and not pl
        results.append({"key": p["key"], "id": p["id"], "kind": p["kind"], "market": p["market"], "scope": p["scope"], "group": p["group"], "feature": p["feature"], "cell": p["cell"], "expected_sign": p["sign"], "family": p["family"],
                        "m_family": m_fam, "B": choice.B, "power_limited": pl, "n_event": r["n_event"], "n_control": r["n_control"], "p_event": r["p_event"], "p_control": r["p_control"], "delta": d, "base_delta": r["base_delta"],
                        "ci_low": r["ci_low"], "ci_high": r["ci_high"], "n_blocks": r["n_blocks"], "p_boot": r["p_boot"], "adjusted_p": a_p, "adjusted_p_registry": adj_reg.get(p["name"]), "status": status, "sign_check": sign_ok,
                        "survivor": bool(surv), "note": r["note"], "hypothesis": p["name"]})
    verdict, surv_keys, nc_fail = stage_verdict(stage, results)
    contrary = [r["key"] for r in results if r["kind"] == "hyp" and r["status"] == ST.SIGNIFICANT_ADJUSTED and r["sign_check"] == "CONTRARY"]
    return _clean({
        "status": "COMPLETE", "stage": stage, "partition": part, "purpose": purpose, "verdict": verdict, "survivors": surv_keys, "negative_control_failures": nc_fail, "contrary_significant": contrary,
        "prereg_json_sha256": prereg.json_sha256, "resumed_registered_stage": resumed, "B": choice.B, "required_B": choice.required, "b_warning": choice.warning, "n_hypotheses_ever": reg.n_hypotheses,
        "families": {f: {"m": n, "power_limited": ST.is_power_limited(n, choice.B, alpha)} for f, n in sorted(fams.items())}, "markets": results_by_market_summary(results_by_market),
        "results": results, "timing": timing,
    })


def results_by_market_summary(rbm: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {m: {"counts": r["counts"], "cell_defs": r["cell_defs"], "warnings": r["warnings"]} for m, r in sorted(rbm.items())}


# ---------------------------------------------------------------------------------------------- report
def _pp(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:+.1f}"


def render_md(report: dict[str, Any]) -> str:
    L = ["# Observer Gate C report (preregistered single-feature enrichment)", "", "Status: `OBSERVATION_ONLY_NOT_ALPHA_VALIDATED`. Not an edge claim. Costs, slippage and the real stop are excluded.", "",
         f"Preregistration: `{report['prereg']['path']}` (file sha256 `{report['prereg']['file_sha256'][:16]}`, block sha256 `{report['prereg']['json_sha256'][:16]}`, version `{report['prereg']['version']}`). Script `{SCRIPT_VERSION}`.", ""]
    for stage in ("fit", "validate", "oos"):
        st = (report.get("stages") or {}).get(stage)
        if not st:
            continue
        L += [f"## Stage `{stage}` ({st['partition']})", "", f"* verdict: **{st['verdict']}**", f"* survivors: {', '.join(st['survivors']) or 'none'}",
              f"* negative control failures: {', '.join(st['negative_control_failures']) or 'none'}", f"* significant in the CONTRARY direction (hypothesis generation only, not survivors): {', '.join(st['contrary_significant']) or 'none'}",
              f"* B = {st['B']} (required {st['required_B']}); hypotheses ever registered: {st['n_hypotheses_ever']}; registered-stage resume: {st['resumed_registered_stage']}"]
        if st.get("b_warning"):
            L.append(f"* WARNING: {st['b_warning']}")
        if st["verdict"] in (V_NONE, V_INCONCLUSIVE, V_INVALID, V_NOT_CONFIRMED):
            L.append("* STOP RULE applies: documented, the next stage is not run, no variants are tried inside this preregistration.")
        L += ["", "| family | m | power limited |", "|---|---|---|", *[f"| `{f}` | {v['m']} | {v['power_limited']} |" for f, v in st["families"].items()], "",
              "| market | id | feature | cell | exp | n ev | n ctl | blocks | delta pp | CI pp | adj p | status | sign | surv |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in st["results"]:
            ci = "-" if r["ci_low"] is None else f"[{_pp(r['ci_low'])}, {_pp(r['ci_high'])}]"
            ap = "-" if r["adjusted_p"] is None else f"{r['adjusted_p']:.3g}"
            L.append(f"| {r['market']} | {r['id']} | `{r['feature'].split('__', 1)[-1]}` | {r['cell']} | {r['expected_sign']:+d} | {r['n_event']} | {r['n_control']} | {r['n_blocks']} | {_pp(r['delta'])} | {ci} | {ap} | {r['status']} | {r['sign_check']} | {'YES' if r['survivor'] else ''} |")
        L += ["", "Per market:", ""]
        for m, v in st["markets"].items():
            L.append(f"* {m}: {v['counts']}" + (f"; warnings: {v['warnings']}" if v["warnings"] else ""))
        L.append("")
    L += ["## Honest limits", "", "* One label, predeclared contrasts, terciles frozen on TRAIN; a null is not proof of no structure.", "* Bar-resolution first-passage rates, costs excluded; day blocks are the independence unit.",
          "* 07-01..08-31 (OOS) is frozen only relative to the observer; the forward period (>= 2026-09-01) was never read."]
    return "\n".join(L) + "\n"


def write_report(out: Path, prereg: Prereg, stage: str, stage_res: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rep = load_report(out / f"{REPORT_STEM}.json")
    rep["prereg"] = {"path": prereg.path, "file_sha256": prereg.file_sha256, "json_sha256": prereg.json_sha256, "version": prereg.spec["prereg_version"]}
    rep.setdefault("stages", {})[stage] = stage_res
    (out / f"{REPORT_STEM}.json").write_text(json.dumps(_clean(rep), indent=1), encoding="utf-8")
    (out / f"{REPORT_STEM}.md").write_text(render_md(rep), encoding="utf-8")


# ---------------------------------------------------------------------------------------------- CLI
def dry_run(a: argparse.Namespace, prereg: Prereg, stage: str, markets: list[str]) -> int:
    """Prints the plan. Reads NO backfill data (not even a Parquet footer): only checks that the expected files exist."""
    survivors: list[str] | None = None
    note = ""
    if stage != "fit":
        rep = load_report(Path(a.out) / f"{REPORT_STEM}.json")
        try:
            survivors, _ = previous_survivors(prereg, stage, rep, True)
        except StopRule as e:
            note = f"(would stop: {e})"
            survivors = []
    plan = build_plan(prereg, stage, markets, survivors)
    fams = family_sizes(plan)
    ch = choose_B(prereg, fams) if fams else None
    print(f"DRY RUN stage={stage} partition={prereg.spec['stages'][stage]['partition']} markets={markets} jobs={a.jobs} {note}")
    print(f"prereg {prereg.spec['prereg_version']} block sha256 {prereg.json_sha256[:16]}; label {prereg.spec['label']}")
    print(f"tests: {len(plan)}; families: {len(fams)}; B: {ch.B if ch else '-'}")
    for f, n in sorted(fams.items()):
        print(f"  family {f}: m={n}")
    for m in markets:
        files = [(Path(a.root) / m / f).is_file() for f in ("table.parquet", "controls.parquet")]
        print(f"  market {m} ({market_scope(prereg, m)}): table.parquet={files[0]} controls.parquet={files[1]} (existence only, nothing read)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="backfill directory (read only)")
    ap.add_argument("--markets", nargs="+", default=None, help="default: the preregistered core markets; explore markets run as a separate scope")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="registry, caches and report (never inside --root)")
    ap.add_argument("--stage", choices=("fit", "validate", "oos"), default="fit")
    ap.add_argument("--prereg", default=str(DEFAULT_PREREG))
    ap.add_argument("--jobs", type=int, default=2, help=f"worker processes over markets (1..{MAX_JOBS}; 8 GB machine)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, read no data")
    ap.add_argument("--force", action="store_true", help="recompute the per-market caches (the registry still refuses changed results of registered hypotheses)")
    ap.add_argument("--confirm-oos-once", action="store_true", help="required for --stage oos: the OOS partition is touched exactly once")
    a = ap.parse_args(argv)
    if not 1 <= a.jobs <= MAX_JOBS:
        ap.error(f"--jobs must be 1..{MAX_JOBS}")
    prereg = load_prereg(a.prereg)
    markets = a.markets or list(prereg.spec["markets"]["core"])
    for m in markets:
        market_scope(prereg, m)
    root, out = Path(a.root).resolve(), Path(a.out).resolve()
    if out == root or root in out.parents:
        ap.error("--out must not lie inside --root (the backfill directory is read only)")
    if a.dry_run:
        return dry_run(a, prereg, a.stage, markets)
    try:
        res = run_stage(a, prereg, a.stage, markets)
    except StopRule as e:
        print(f"STOP: {e}")
        return 3
    write_report(out, prereg, a.stage, res)
    print(f"stage {a.stage}: {res['verdict']}; survivors: {res['survivors'] or 'none'}; B={res['B']}; report -> {out / (REPORT_STEM + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
