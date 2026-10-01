# ruff: noqa: E501
"""Gate C of the Market Structure Observer: preregistered single-feature enrichment against matched controls (OFFLINE / RESEARCH ONLY).

    uv run python scripts/observer_gate_c.py --root <backfill dir> [--markets GER40 NAS100 ...] [--out DIR] [--stage fit|validate|oos]
        [--jobs 2] [--prereg docs/OBSERVER_GATE_C_PREREGISTRATION.md] [--dry-run] [--preflight] [--force] [--confirm-oos-once]

Version ``observer-gate-c-3``: runs preregistration ``observer-gate-c-prereg-2`` (``observer-stats-2``, day blocks) AND the draft ``observer-gate-c-prereg-3``
(``observer-stats-3``, contiguous blocks of >= 21 trading days, own registry namespace) from the same code; the preregistration text selects the statistics,
the registry namespace and the file names. prereg-1 is history. The new ``--attest`` step pins the SHA-256 of controls files whose manifest predates the
``controls_sha256`` field (read-only, written next to --out, never into --root).

Never touches the live trader, ``artifacts/``, MT5, schedulers or the trading DB. The backfill directory (``--root``) is opened READ-ONLY; the
registry, the per-market caches and the report go to ``--out`` (default ``%LOCALAPPDATA%\\Temp\\observer_gate_c``, refused if it lies inside ``--root``).

What it does (see ``docs/OBSERVER_GATE_C_PREREGISTRATION.md``, the single source of truth, parsed here):

* reads the preregistration from the machine-readable block of that document and records the file hash and the block hash in the report;
* PREFLIGHT (always first; ``--preflight`` runs only this): reads ONLY counts and the per-market ``controls_manifest.json`` (control method version, content
  fingerprint, balance gate). A failing preflight registers NOTHING and does not consume the stop rule (exit code 5);
* per market (``--jobs`` worker processes, default 2, max 2; results independent of the number of workers): loads ONLY the rows of the stage's partition,
  builds the derived features, freezes the cells on TRAIN (``fit``) or applies the stored ones, and calls ``observer_lab.enrichment.incremental_ablation``
  (``observer-stats-2``: controls blocked by the day of their EVENT, blocks counted per arm) for the predeclared contrasts, the random negative control,
  the A/A test (NC-A), the shift placebo (NC-B) and, for explore markets, the controls-3 vs controls-2 bridge (NC-C, descriptive);
* registers every confirmatory (hypothesis x market) of the stage in its OWN persistent registry BEFORE any is evaluated, applies Holm over ALL hypotheses
  of the stage and scope (one family), decides survivors / stop rule / validity of the negative controls, writes ``observer_gate_c2_report.{md,json}``;
* ``fit`` runs exactly once per preregistration version: a lock file OUTSIDE ``--out`` (``LOCK_DIR``) pins the fit report hash and forbids a second
  ``fit`` with another ``--out`` (no best-of-N); a stage already in the report is never overwritten with different content.

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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SCRIPT_VERSION = "observer-gate-c-3"
DEFAULT_ROOT = Path(os.environ.get("LOCALAPPDATA", ".")) / "Temp" / "observer_backfill"
DEFAULT_OUT = Path(os.environ.get("LOCALAPPDATA", ".")) / "Temp" / "observer_gate_c"
LOCK_DIR = Path(os.environ.get("LOCALAPPDATA", ".")) / "Temp" / "observer_gate_c_locks"  # fixed anchor OUTSIDE --out (tests monkeypatch this module attribute only)
DEFAULT_PREREG = ROOT / "docs" / "OBSERVER_GATE_C_PREREGISTRATION.md"
REGISTRY_FILE = "observer_gate_c2_registry.json"
REPORT_STEM = "observer_gate_c2_report"
PREFLIGHT_STEM = "observer_gate_c2_preflight"
MAX_JOBS = 2  # 8 GB machine
FILE_PARTITION = {"TRAIN": "TRAIN", "VALIDATION": "VALIDATION", "OOS": "FROZEN_OOS"}  # partition names as written by the backfill
UNUSABLE_TAGS = ("PURGED", "EMBARGO")
BASE_COLS = ("event_id", "is_control", "control_of", "decision_ts_ns", "direction", "warmup_ok", "partition")
DAY_COL = "m_local_day"
BERLIN = "Europe/Berlin"
CELL_UNDEFINED = "CELL_UNDEFINED"
DESCRIPTIVE_ONLY = "DESCRIPTIVE_ONLY"
NEEDS = {  # derived feature -> raw columns it is computed from (decision-time columns and the event direction only)
    "f_swings__x_ema_trend_aligned": ("f_swings__ema_trend",),
    "f_swings__x_m15_sequence_aligned": ("f_swings__m15_sequence",),
    "f_ctrl__x_random_uniform": (),
}
MARKER_BEGIN, MARKER_END = "<!-- PREREG-JSON-BEGIN -->", "<!-- PREREG-JSON-END -->"
V_FIT_OK, V_NONE, V_INCONCLUSIVE, V_INVALID = "ENRICHMENT_CANDIDATES_TO_VALIDATE", "NO_ENRICHMENT", "INCONCLUSIVE_INSUFFICIENT_EVIDENCE", "INVALID_NEGATIVE_CONTROL_FAILED"
V_VAL_OK, V_OOS_OK, V_NOT_CONFIRMED = "CONFIRMED_ON_VALIDATION", "REPLICATED_ON_OOS", "NOT_CONFIRMED"
KINDS = ("hyp", "nc", "nc_a", "nc_b")  # confirmatory hypothesis | random-feature control | A/A test | shift placebo
EXIT_STOP, EXIT_NAN, EXIT_PREFLIGHT = 3, 4, 5
EXIT_ATTEST = 6
V_NOT_ASKED = "INCONCLUSIVE_NOT_ASKED"  # no market passed the balance gate / no confirmatory test eligible: the question was never asked (NOT "no enrichment")
ATTEST_FILE = "observer_controls_attestation.json"  # written next to the report in --out (never into --root, the artifacts stay byte-identical)
ATTEST_VERSION = "observer-controls-attest-1"
BALANCE_FIELDS = ("match_rate", "smd_local_minute", "smd_atr_pct", "smd_spread_pct", "censoring_diff_pp")
MANIFEST_NAME = "controls_manifest.json"
CONTROLS3_A_DIR, CONTROLS3_B_DIR = "controls3", "controls3_b"  # on-disk layout written by scripts/observer_backfill.py --step controls3


def resolve_file(root: str | Path, market: str, logical: str) -> Path:
    """Layout adapter (path mapping only): the preregistration names ``controls.parquet`` / ``controls_b.parquet`` / ``controls_v2.parquet`` /
    ``controls_manifest.json`` next to ``table.parquet``. The controls-3 builder writes ``<M>/controls3/{controls.parquet, controls_manifest.json}`` (set A) and
    ``<M>/controls3_b/controls.parquet`` (set B); the flat ``<M>/controls.parquet`` of that layout is the controls-2 set (= the NC-C bridge input).
    If a ``controls3`` directory exists it is authoritative (no silent mixing with the flat controls-2 files); without it the flat layout is used unchanged."""
    mdir = Path(root) / market
    a_dir, b_dir = mdir / CONTROLS3_A_DIR, mdir / CONTROLS3_B_DIR
    if logical == "controls.parquet" and a_dir.is_dir():
        return a_dir / "controls.parquet"
    if logical == MANIFEST_NAME and a_dir.is_dir():
        return a_dir / MANIFEST_NAME
    if logical == "controls_b.parquet" and b_dir.is_dir():
        return b_dir / "controls.parquet"
    if logical == "controls_b_manifest.json":
        return b_dir / MANIFEST_NAME if b_dir.is_dir() else mdir / logical  # flat layout: no manifest of set B (the hash then comes from the attestation / ``controls_b_sha256``)
    if logical == "controls_v2.parquet" and not (mdir / logical).is_file() and a_dir.is_dir():
        return mdir / "controls.parquet"  # controls-2 set next to the events
    return mdir / logical


def normalize_balance_gate(prereg_balance: dict[str, Any], bg: Any) -> Any:
    """Schema adapter for the manifest of the controls-3 builder (``observer-controls-3-balance-1``): ``balance_gate = {gate_version, thresholds, partitions: {P: {...,
    verdict}}, ...}`` -> the per-partition shape of the preregistration (``{P: {match_rate, smd_*, censoring_diff_pp, passed, status, n_controls}}``). Pure re-labelling:
    nothing is recomputed or loosened. A threshold of the builder that differs from the preregistered one makes the entry unusable (returns an error marker), and
    the builder's own verdict becomes ``passed`` (the script still compares it with its recomputation under the preregistered thresholds)."""
    if not isinstance(bg, dict) or not isinstance(bg.get("partitions"), dict):
        return bg  # already the preregistered shape (or not a dict: the structural check reports it)
    th = bg.get("thresholds") or {}
    # UNIT: the builder writes shares and the censoring threshold as FRACTIONS (0.05); the preregistration speaks percentage points (5.0). The conversion is explicit and
    # fail-closed: a builder that declares another unit, or a diff outside [-1, 1] (= already percentage points), is refused instead of being read 100x too loose.
    unit = bg.get("censored_share_unit")
    if unit is not None and unit != "fraction":
        return {"__adapter_error__": f"builder declares censored_share_unit {unit!r}; only 'fraction' is understood (the adapter converts fraction -> percentage points)"}
    want_den = prereg_balance.get("censoring_denominator")
    have_den = bg.get("censored_share_denominator")
    if want_den is not None and have_den is not None and have_den != want_den:
        return {"__adapter_error__": f"censoring denominator differs: builder {have_den!r}, preregistration {want_den!r}"}
    want = {"match_rate_min": prereg_balance["min_match_rate"], "smd_abs_max": prereg_balance["max_abs_smd"], "censored_share_diff_max": prereg_balance["max_abs_censoring_diff_pp"] / 100.0}
    off = {k: (th.get(k), v) for k, v in want.items() if not isinstance(th.get(k), (int, float)) or abs(float(th[k]) - v) > 1e-12}
    if off:
        return {"__adapter_error__": f"builder thresholds differ from the preregistered ones (builder, prereg): {off}"}
    out: dict[str, Any] = {}
    for p, e in bg["partitions"].items():
        smd, cen = (e.get("smd") or {}), (e.get("censored_share") or {})
        diff = cen.get("diff")
        if diff is not None and (not isinstance(diff, (int, float)) or isinstance(diff, bool) or not math.isfinite(float(diff)) or abs(float(diff)) > 1.0):
            return {"__adapter_error__": f"balance_gate[{p}].censored_share.diff={diff!r} is not a fraction in [-1, 1] (unit mismatch: percentage points?)"}
        out[p] = {"match_rate": e.get("match_rate"), "smd_local_minute": smd.get("local_minute"), "smd_atr_pct": smd.get("atr_pct"), "smd_spread_pct": smd.get("spread_pct"),
                  "censoring_diff_pp": None if diff is None else 100.0 * float(diff), "passed": e.get("verdict") == "PASS", "status": "passed" if e.get("verdict") == "PASS" else "descriptive_only",
                  "n_controls": e.get("n_controls"),
                  # builder-only information (the preregistered gate may or may not contain these criteria; see ``balance_passes`` / ``_check_manifest``)
                  "builder_verdict": e.get("verdict"), "session_share_diff_max": e.get("session_share_diff_max"), "n_events": e.get("n_events"),
                  "builder_thresholds": {k: th.get(k) for k in ("session_share_diff_max", "min_events")}}
    return out


class StopRule(RuntimeError):
    """A preregistered stop rule forbids this stage (documented outcome, exit code 3)."""


class NoConfirmatoryTestLeft(StopRule):
    """Stop rule 'no confirmatory test left': raised BEFORE the registry is written and BEFORE the fit lock is taken. The question was never asked (INCONCLUSIVE_NOT_ASKED), which is NOT 'no enrichment'."""


class NanBootstrapAbort(RuntimeError):
    """Bootstrap draws were NaN: counted and reported, the stage is aborted (exit code 4), never silently dropped."""


class PreflightFailed(RuntimeError):
    """The preflight found a structural / provenance defect: nothing is registered, the stop rule is not consumed (exit code 5)."""


# ---------------------------------------------------------------------------------------------- preregistration
@dataclass(frozen=True)
class Prereg:
    spec: dict[str, Any]
    file_sha256: str
    json_sha256: str
    path: str


def _canon(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def _sha(o: Any) -> str:
    return hashlib.sha256(_canon(o).encode()).hexdigest()


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
    return Prereg(spec, hashlib.sha256(text.encode("utf-8")).hexdigest(), _sha(spec), str(p))


def _validate_prereg(s: dict[str, Any]) -> None:
    for k in ("prereg_version", "registry_name", "label", "markets", "stages", "test", "min_evidence", "min_effect_abs", "hypotheses", "negative_controls", "stats", "controls", "placebos"):
        if k not in s:
            raise ValueError(f"preregistration lacks {k!r}")
    if not str(s["label"]).startswith("y_"):
        raise ValueError("label must be a y_* column")
    if set(s["stages"]) != {"fit", "validate", "oos"}:
        raise ValueError("stages must be exactly fit / validate / oos (there is no forward stage)")
    if s["test"]["kind"] != "incremental_ablation" or s["test"]["adjust"] not in ("holm", "bh") or s["test"]["p_method"] != "bootstrap":
        raise ValueError("unsupported test definition")
    if s["test"].get("holm_scope") != "stage_all":
        raise ValueError("test.holm_scope must be 'stage_all' (Holm over ALL hypotheses of the stage and scope, one family)")
    from coverage_analysis.observer_lab import stats as ST

    st = s["stats"]
    if st.get("version") == ST.STATS_V2:
        if st.get("block_unit") != "day" or st.get("sensitivity_block_unit") != "week":
            raise ValueError("stats must declare version observer-stats-2, block_unit day, sensitivity_block_unit week")
    elif st.get("version") == ST.STATS_V3:
        n, sn = st.get("block_len_days"), st.get("sensitivity_block_len_days")
        if st.get("block_unit") != "tdays" or st.get("control_block") != "event_day" or not isinstance(n, int) or isinstance(n, bool) or n < ST.MIN_BLOCK_TRADING_DAYS                 or not isinstance(sn, int) or isinstance(sn, bool) or sn < n:
            raise ValueError(f"stats must declare observer-stats-3 with block_unit 'tdays', control_block 'event_day', block_len_days >= {ST.MIN_BLOCK_TRADING_DAYS} and sensitivity_block_len_days >= block_len_days")
    else:
        raise ValueError(f"unsupported stats version {st.get('version')!r} (observer-stats-2 | observer-stats-3)")
    bal = s["controls"].get("balance") or {}
    if not {"min_match_rate", "max_abs_smd", "max_abs_censoring_diff_pp"} <= set(bal) or not s["controls"].get("required_method_version"):
        raise ValueError("controls must declare required_method_version and balance thresholds")
    if not {"nc_a", "nc_b", "nc_c"} <= set(s["placebos"]):
        raise ValueError("placebos must declare nc_a, nc_b and nc_c")
    nca = s["placebos"].get("alpha")
    if not isinstance(nca, (int, float)) or not 0 < nca <= s["test"]["alpha"]:
        raise ValueError("placebos.alpha (the significance level of the negative controls, 0 < alpha <= test.alpha) is required")
    if not isinstance(s["placebos"]["nc_b"].get("tolerance_minutes"), int) or s["placebos"]["nc_b"]["tolerance_minutes"] <= 0:
        raise ValueError("placebos.nc_b.tolerance_minutes (positive int) is required")
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


def ns(prereg: Prereg) -> str:
    """Registry / hypothesis-name namespace of the preregistration (prereg-2: ``gatec2``; prereg-3 declares its own, so no registered name can collide)."""
    return str(prereg.spec.get("namespace", "gatec2"))


def registry_file(prereg: Prereg) -> str:
    return str((prereg.spec.get("files") or {}).get("registry", REGISTRY_FILE))


def report_stem(prereg: Prereg) -> str:
    return str((prereg.spec.get("files") or {}).get("report", REPORT_STEM))


def preflight_stem(prereg: Prereg) -> str:
    return str((prereg.spec.get("files") or {}).get("preflight", PREFLIGHT_STEM))


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


def berlin_seconds_of_day(ts_ns: np.ndarray) -> np.ndarray:
    idx = pd.DatetimeIndex(pd.to_datetime(ts_ns, unit="ns", utc=True)).tz_convert(BERLIN)
    return (idx.hour * 3600 + idx.minute * 60 + idx.second).to_numpy(dtype="int64")


def load_part(root: str | Path, market: str, fname: str, partition: str, columns: list[str]) -> pd.DataFrame:
    """ONE Parquet file of ONE market restricted to the stage partition (+ PURGED/EMBARGO tags), only ``columns``."""
    import pyarrow.parquet as pq

    p = resolve_file(root, market, fname)
    if not p.is_file():
        raise FileNotFoundError(f"{p} not found (the backfill must be complete)")
    assert_file_dev_only(p)
    have = set(pq.read_schema(p).names)
    missing = [c for c in columns if c not in have]
    if missing:
        raise ValueError(f"{p}: columns missing {missing[:5]}")
    df = pd.read_parquet(p, columns=columns, filters=[("partition", "in", [FILE_PARTITION[partition], *UNUSABLE_TAGS])])
    df["partition"] = df["partition"].replace({"FROZEN_OOS": "OOS"})
    return df


def load_stage_frame(root: str | Path, market: str, partition: str, columns: list[str]) -> pd.DataFrame:
    """Events (``table.parquet``) and controls (``controls.parquet``) of ONE market restricted to the stage partition (+ PURGED/EMBARGO tags), only ``columns``."""
    return pd.concat([load_part(root, market, f, partition, columns) for f in ("table.parquet", "controls.parquet")], ignore_index=True)


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


# ---------------------------------------------------------------------------------------------- preflight (counts + manifests only)
def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_facts(path: Path, label: str, part: str) -> dict[str, Any]:
    """Content hash, row count, schema hash and per-partition counts of one Parquet file. Reads only the ``partition`` and label columns (counts)."""
    import pyarrow.parquet as pq

    assert_file_dev_only(path)
    pf = pq.ParquetFile(path)
    names = list(pf.schema_arrow.names)
    t = pq.read_table(path, columns=["partition", label]).to_pandas()
    pcount = {str(k): int(v) for k, v in t["partition"].value_counts().sort_index().items()}
    stage_rows = t[t["partition"] == FILE_PARTITION[part]]
    n_stage = len(stage_rows)
    return {"sha256": file_sha256(path), "rows": int(pf.metadata.num_rows), "schema_sha256": hashlib.sha256(str(pf.schema_arrow).encode()).hexdigest(), "columns": names,
            "partition_counts": pcount, "n_stage": n_stage, "n_stage_censored": int(stage_rows[label].isna().sum())}


def balance_passes(thr: dict[str, Any], b: dict[str, Any]) -> bool:
    """Recomputed balance gate (the manifest's own ``passed`` must agree): match rate, standardised mean differences, censoring balance."""
    ok = (b["match_rate"] >= thr["min_match_rate"] and all(abs(b[k]) <= thr["max_abs_smd"] for k in ("smd_local_minute", "smd_atr_pct", "smd_spread_pct"))
          and abs(b["censoring_diff_pp"]) <= thr["max_abs_censoring_diff_pp"])
    # Criteria that exist in the controls-3 BUILDER gate but not in prereg-2: they are enforced here ONLY when the preregistration declares them (prereg-3).
    if "max_session_share_diff" in thr:
        s = b.get("session_share_diff_max")
        ok = ok and isinstance(s, (int, float)) and math.isfinite(float(s)) and float(s) <= thr["max_session_share_diff"]
    if "min_events" in thr:
        n = b.get("n_events")
        ok = ok and isinstance(n, int) and not isinstance(n, bool) and n >= thr["min_events"]
    return ok


def builder_only_reasons(thr: dict[str, Any], e: dict[str, Any]) -> list[str]:
    """Why the BUILDER verdict is stricter than the preregistered gate (explicit marking of the prereg-2 / builder inconsistency; never resolved silently)."""
    out: list[str] = []
    bt = e.get("builder_thresholds") or {}
    s = e.get("session_share_diff_max")
    if "max_session_share_diff" not in thr and isinstance(s, (int, float)) and isinstance(bt.get("session_share_diff_max"), (int, float)) and s > bt["session_share_diff_max"]:
        out.append(f"session_share_diff_max {s:.4f} > builder tolerance {bt['session_share_diff_max']} (criterion not in this preregistration)")
    n = e.get("n_events")
    if "min_events" not in thr and isinstance(n, int) and isinstance(bt.get("min_events"), int) and n < bt["min_events"]:
        out.append(f"n_events {n} < builder minimum {bt['min_events']} (INSUFFICIENT_N is not in this preregistration)")
    return out


def _check_manifest(prereg: Prereg, man: Any, part: str, market: str | None = None, control_set: str = "a") -> tuple[dict[str, Any] | None, list[str]]:
    """Schema check of ``controls_manifest.json``; returns (balance entry of the stage partition | None, structural problems)."""
    spec = prereg.spec["controls"]
    errs: list[str] = []
    if not isinstance(man, dict):
        return None, ["controls manifest is not a JSON object"]
    if man.get("control_method_version") != spec["required_method_version"]:
        errs.append(f"control_method_version {man.get('control_method_version')!r} != required {spec['required_method_version']!r}")
    if market is not None and man.get("market") is not None and man.get("market") != market:
        errs.append(f"manifest belongs to market {man.get('market')!r}, not {market!r} (controls of another market)")
    if man.get("control_set") is not None and man.get("control_set") != control_set:
        errs.append(f"manifest describes control set {man.get('control_set')!r}, expected {control_set!r} (set A and set B must not be mixed)")
    bg = normalize_balance_gate(spec["balance"], man.get("balance_gate"))
    if not isinstance(bg, dict):
        return None, [*errs, "manifest has no balance_gate object"]
    if "__adapter_error__" in bg:
        return None, [*errs, bg["__adapter_error__"]]
    entry: dict[str, Any] | None = None
    for fp in FILE_PARTITION.values():
        e = bg.get(fp)
        if not isinstance(e, dict):
            errs.append(f"balance_gate[{fp}] missing")
            continue
        bad = [k for k in BALANCE_FIELDS if not isinstance(e.get(k), (int, float)) or isinstance(e.get(k), bool) or not math.isfinite(float(e[k]))]
        if bad:
            errs.append(f"balance_gate[{fp}]: missing / non-finite {bad}")
            continue
        if not isinstance(e.get("passed"), bool) or e.get("status") not in ("passed", "descriptive_only"):
            errs.append(f"balance_gate[{fp}]: 'passed' (bool) and 'status' (passed | descriptive_only) required")
            continue
        recomputed = balance_passes(spec["balance"], e)
        if recomputed != e["passed"] or (e["status"] == "passed") != e["passed"]:
            bo = builder_only_reasons(spec["balance"], e) if not recomputed or not e["passed"] else []
            note = f" PREREG/BUILDER INCONSISTENCY (not bent either way; owner decision needed): {'; '.join(bo)}" if bo and not e["passed"] and recomputed else ""
            errs.append(f"balance_gate[{fp}]: passed={e['passed']} / status={e['status']} (builder verdict {e.get('builder_verdict')}) disagree with the recomputed gate ({recomputed}) under the preregistered thresholds.{note}")
        if fp == FILE_PARTITION[part]:
            entry = e
    return entry, errs


def matched_censoring_diff_pp(table_path: Path, controls_path: Path, label: str, part: str) -> float | None:
    """Censoring balance over the MATCHED PAIRS of the stage partition (the denominator of the controls-3 builder gate and of prereg-3): every control row of the
    stage is joined to its event (``control_of``); result = share of censored (NaN) labels among those controls minus among their events, in percentage points
    (controls minus events, the sign of the builder's ``censored_share.diff``). Reads event ids, control_of and the label's availability only."""
    import pyarrow.parquet as pq

    ev = pq.read_table(table_path, columns=["event_id", "partition", label]).to_pandas()
    ev = ev[ev["partition"] == FILE_PARTITION[part]][["event_id", label]].rename(columns={"event_id": "control_of", label: "_ev"})
    ct = pq.read_table(controls_path, columns=["control_of", label]).to_pandas()
    pairs = ct.merge(ev, on="control_of", how="inner")
    if not len(pairs):
        return None
    return 100.0 * (float(pairs[label].isna().mean()) - float(pairs["_ev"].isna().mean()))


def load_attestation(out: Path) -> dict[str, Any] | None:
    p = Path(out) / ATTEST_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def hash_sources(root: Path, market: str, man: Any, attest_entry: dict[str, Any] | None) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Every declared SHA-256 of the control files, by logical file and by source: ``manifest`` (``controls_sha256`` of set A), ``manifest_b`` (``controls_sha256`` of
    the manifest in ``controls3_b``), ``manifest_a_b`` (``controls_b_sha256`` in the flat-layout manifest), ``attestation`` (read-only ``--attest`` step)."""
    errs: list[str] = []
    src: dict[str, dict[str, str]] = {"controls.parquet": {}, "controls_b.parquet": {}}
    if isinstance(man, dict):
        if isinstance(man.get("controls_sha256"), str):
            src["controls.parquet"]["manifest"] = man["controls_sha256"]
        if isinstance(man.get("controls_b_sha256"), str):
            src["controls_b.parquet"]["manifest_a_b"] = man["controls_b_sha256"]
    mb = resolve_file(root, market, "controls_b_manifest.json")
    if mb.is_file():
        try:
            bm = json.loads(mb.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errs.append(f"controls_b manifest is not valid JSON: {e}")
        else:
            if isinstance(bm, dict) and isinstance(bm.get("controls_sha256"), str):
                src["controls_b.parquet"]["manifest_b"] = bm["controls_sha256"]
    for logical, e in ((attest_entry or {}).get("files") or {}).items():
        if logical in src and isinstance(e, dict) and isinstance(e.get("sha256"), str):
            src[logical]["attestation"] = e["sha256"]
    return src, errs


def hash_errors(logical_files: list[str], files: dict[str, Any], src: dict[str, dict[str, str]]) -> list[str]:
    """``controls_sha256`` is MANDATORY (manifest or attestation): a control file without a declared hash, with conflicting declarations or with a hash that is not
    the file's own is a structural defect (the file might be another control set, e.g. controls-2, not the one the manifest describes)."""
    errs: list[str] = []
    for lf in logical_files:
        if lf not in files:
            continue
        s = src.get(lf, {})
        if not s:
            errs.append(f"{lf}: no declared SHA-256 (controls_sha256 is REQUIRED: the manifest of the builder writes it, older artifacts need the read-only --attest step)")
        elif len(set(s.values())) > 1:
            errs.append(f"{lf}: conflicting declared SHA-256 values {sorted(s)} (manifest vs attestation)")
        elif next(iter(s.values())) != files[lf]["sha256"]:
            errs.append(f"{lf}: declared sha256 ({', '.join(sorted(s))}) does not match the file (the control set is not the one that was manifested / attested; controls-2 and controls-3 are never mixed)")
    return errs


def attest(prereg: Prereg, root: Path, out: Path, markets: list[str]) -> int:
    """READ-ONLY attestation for controls artifacts whose manifest predates ``controls_sha256``: hashes the file bytes of the current control files and writes
    ``<out>/observer_controls_attestation.json`` (never into ``--root``: the artifacts stay byte-identical, nothing is recomputed). It is a pin (trust on first
    attest, tamper-evident afterwards: any change of a hashed file or of the manifest breaks the preflight); it does not prove WHO built the files. An existing
    attestation is never overwritten with different content (exit code 6)."""
    ent: dict[str, Any] = {}
    for m in markets:
        scope = market_scope(prereg, m)
        mp = resolve_file(root, m, MANIFEST_NAME)
        names = ["controls.parquet"] + (["controls_b.parquet"] if scope == "core" else [])
        paths = {n: resolve_file(root, m, n) for n in names}
        missing = [str(p) for p in (mp, *paths.values()) if not p.is_file()]
        if missing:
            print(f"ATTEST REFUSED {m}: not found: {missing}")
            return EXIT_ATTEST
        try:
            man = json.loads(mp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"ATTEST REFUSED {m}: manifest is not valid JSON: {e}")
            return EXIT_ATTEST
        if not isinstance(man, dict) or man.get("control_method_version") != prereg.spec["controls"]["required_method_version"]:
            print(f"ATTEST REFUSED {m}: control_method_version {man.get('control_method_version') if isinstance(man, dict) else None!r} != required {prereg.spec['controls']['required_method_version']!r} (only controls-3 is attested)")
            return EXIT_ATTEST
        ent[m] = {"manifest_sha256": file_sha256(mp), "control_method_version": man["control_method_version"],
                  "files": {n: {"path": str(p.relative_to(root)).replace("\\", "/"), "sha256": file_sha256(p), "bytes": p.stat().st_size} for n, p in paths.items()}}
    prev = load_attestation(out) or {"attest_version": ATTEST_VERSION, "attested_utc": datetime.now(UTC).isoformat(timespec="seconds"), "markets": {}}
    for m, e in ent.items():
        if m in prev["markets"] and prev["markets"][m] != e:
            print(f"ATTEST REFUSED {m}: an attestation with different content already exists in {Path(out) / ATTEST_FILE} (never overwritten)")
            return EXIT_ATTEST
        prev["markets"][m] = e
    prev["markets"] = dict(sorted(prev["markets"].items()))
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / ATTEST_FILE).write_text(json.dumps(prev, indent=1), encoding="utf-8")
    print(f"ATTESTED {', '.join(ent)} -> {Path(out) / ATTEST_FILE}")
    return 0


def preflight(prereg: Prereg, root: Path, stage: str, markets: list[str], attestation: dict[str, Any] | None = None) -> dict[str, Any]:
    """Counts and manifests only. Never registers anything, never evaluates an outcome. Structural defects -> ``ok`` False; a failed balance gate is NOT a
    defect: that market is then ``descriptive_only`` (preregistered fallback)."""
    s = prereg.spec
    part = s["stages"][stage]["partition"]
    label = s["label"]
    need_cols = list(dict.fromkeys([*BASE_COLS, label, *[c for h in (*s["hypotheses"], *s["negative_controls"]) for c in NEEDS.get(h["feature"], (h["feature"],))]]))
    out: dict[str, Any] = {"stage": stage, "partition": part, "script_version": SCRIPT_VERSION, "prereg_json_sha256": prereg.json_sha256, "markets": {}, "ok": True, "errors": []}
    for m in markets:
        scope = market_scope(prereg, m)
        errs: list[str] = []
        files: dict[str, Any] = {}
        want = ["table.parquet", "controls.parquet"] + (["controls_b.parquet"] if scope == "core" else [])
        if scope == "explore" and resolve_file(root, m, "controls_v2.parquet").is_file():
            want.append("controls_v2.parquet")  # optional NC-C bridge input
        for fname in want:
            p = resolve_file(root, m, fname)
            if not p.is_file():
                errs.append(f"{fname} not found")
                continue
            files[fname] = _file_facts(p, label, part)
            missing = [c for c in need_cols if c not in files[fname]["columns"]]
            if missing:
                errs.append(f"{fname}: columns missing {missing[:5]}")
        mp = resolve_file(root, m, MANIFEST_NAME)
        entry, man_sha, man_version, hash_src = None, None, None, {}
        if not mp.is_file():
            errs.append(f"{MANIFEST_NAME} not found (controls-3 manifest is required)")
        else:
            man_sha = file_sha256(mp)
            try:
                man = json.loads(mp.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                man, entry = None, None
                errs.append(f"{MANIFEST_NAME} is not valid JSON: {e}")
            if man is not None:
                man_version = man.get("control_method_version") if isinstance(man, dict) else None
                entry, merrs = _check_manifest(prereg, man, part, market=m)
                errs += merrs
                att = ((attestation or {}).get("markets") or {}).get(m)
                if att is not None and att.get("manifest_sha256") not in (None, man_sha):
                    errs.append("the manifest changed since it was attested (manifest_sha256 of the attestation differs)")
                src, herrs = hash_sources(root, m, man, att)
                errs += herrs + hash_errors(["controls.parquet"] + (["controls_b.parquet"] if scope == "core" else []), files, src)
                hash_src = {k: sorted(v) for k, v in src.items() if v}
                mb = resolve_file(root, m, "controls_b_manifest.json")
                if scope == "core" and mb.is_file():
                    try:
                        _, berrs = _check_manifest(prereg, json.loads(mb.read_text(encoding="utf-8")), part, market=m, control_set="b")
                        errs += [e for e in berrs if "balance_gate" not in e]  # B is only used as the A/A partner: provenance (version / set / market), not its own balance gate
                    except json.JSONDecodeError:
                        pass  # reported by hash_sources
                nbg = normalize_balance_gate(s["controls"]["balance"], man.get("balance_gate", {})) if isinstance(man, dict) else {}
                for fp_, ent in (nbg.items() if isinstance(nbg, dict) and "__adapter_error__" not in nbg else ()):
                    if isinstance(ent, dict) and isinstance(ent.get("n_controls"), int) and "controls.parquet" in files:
                        have = files["controls.parquet"]["partition_counts"].get(fp_)
                        if have is not None and int(ent["n_controls"]) != have:
                            errs.append(f"balance_gate[{fp_}].n_controls={ent['n_controls']} != {have} rows of controls.parquet")
        obs = None
        if "table.parquet" in files and "controls.parquet" in files and files["table.parquet"]["n_stage"] and files["controls.parquet"]["n_stage"]:
            ev, ct = files["table.parquet"], files["controls.parquet"]
            if s["controls"]["balance"].get("censoring_denominator") == "matched_pairs":  # prereg-3: ONE denominator, identical to the builder gate
                obs = matched_censoring_diff_pp(resolve_file(root, m, "table.parquet"), resolve_file(root, m, "controls.parquet"), label, part)
            else:  # prereg-2: all stage events vs all stage controls (differs from the builder's matched-pairs share; see docs/OBSERVER_GATE_C_PREREGISTRATION_V3.md)
                obs = 100.0 * (ev["n_stage_censored"] / ev["n_stage"] - ct["n_stage_censored"] / ct["n_stage"])
        thr = s["controls"]["balance"]
        status = "descriptive_only"
        if not errs and entry is not None:
            ok_obs = obs is not None and abs(obs) <= thr["max_abs_censoring_diff_pp"]
            status = "passed" if entry["passed"] and ok_obs else "descriptive_only"
        elif not errs:
            errs.append("no balance entry for the stage partition")
        data_fp = _sha({"manifest": man_sha, "files": {k: {kk: v[kk] for kk in ("sha256", "rows", "schema_sha256", "partition_counts")} for k, v in files.items()}}) if not errs else None
        out["markets"][m] = {
            "scope": scope, "ok": not errs, "errors": errs, "status": status, "manifest_sha256": man_sha, "control_method_version": man_version, "balance_gate": entry,
            "observed_censoring_diff_pp": obs, "n_stage_events": files.get("table.parquet", {}).get("n_stage"), "n_stage_controls": files.get("controls.parquet", {}).get("n_stage"),
            "data_fingerprint": data_fp, "files": {k: {kk: v[kk] for kk in ("sha256", "rows", "schema_sha256", "partition_counts")} for k, v in files.items()},
            "has_controls_b": "controls_b.parquet" in files, "has_controls_v2": "controls_v2.parquet" in files, "hash_sources": hash_src,
        }
        if errs:
            out["ok"] = False
            out["errors"] += [f"{m}: {e}" for e in errs]
    return out


# ---------------------------------------------------------------------------------------------- per-market worker (pure, deterministic)
def _defs_to_json(defs: dict[str, Any]) -> dict[str, Any]:
    return {f: {"feature": d.feature, "kind": d.kind, "edges": list(d.edges), "categories": list(d.categories), "has_missing": d.has_missing} for f, d in defs.items()}


def _defs_from_json(js: dict[str, Any]) -> dict[str, Any]:
    from coverage_analysis.observer_lab.enrichment import CellDef

    return {f: CellDef(v["feature"], v["kind"], tuple(v["edges"]), tuple(v["categories"]), bool(v["has_missing"])) for f, v in js.items()}


def _num(x: Any) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else float(x)


def _blank_row(c: dict[str, Any], evidence: str, note: str) -> dict[str, Any]:
    return {"id": c["id"], "kind": c["kind"], "group": c["group"], "feature": c["feature"], "cell": c["cell"], "evidence": evidence, "n_event": 0, "n_control": 0, "p_event": None, "p_control": None,
            "delta": None, "base_delta": None, "ci_low": None, "ci_high": None, "n_blocks": 0, "n_blocks_event": 0, "n_blocks_control": 0, "n_nan_draws": 0, "p_boot": None,
            "week_ci_low": None, "week_ci_high": None, "week_p_boot": None, "week_n_blocks_event": None, "note": note}


def _ablate(frame: pd.DataFrame, items: list[dict[str, Any]], defs: dict[str, Any], cfg: Any, label: str, purpose: str, tag: str) -> dict[str, dict[str, Any]]:
    """incremental_ablation of the predeclared ``items`` on ``frame``; a throw-away in-memory registry (the persistent one lives in the parent process)."""
    from coverage_analysis.observer_lab import enrichment as EN
    from coverage_analysis.observer_lab import stats as ST

    groups: dict[str, list[str]] = {}
    for c in items:
        groups.setdefault(c["group"], [])
        if c["feature"] not in groups[c["group"]]:
            groups[c["group"]].append(c["feature"])
    rep = EN.incremental_ablation(
        frame, [label], np.ones(len(frame), dtype=bool), groups, ST.HypothesisRegistry(f"gate-c-worker-{tag}"), cfg, purpose=purpose,
        cell_defs={f: defs[f] for g in groups.values() for f in g}, contrasts=[EN.PredeclaredContrast(c["group"], c["feature"], c["cell"]) for c in items],
    )
    by = {(r.group, r.feature, r.cell.removeprefix("base AND ")): r for r in rep.results}
    out: dict[str, dict[str, Any]] = {}
    for c in items:
        r = by[(c["group"], c["feature"], c["cell"])]
        insuff = r.status == ST.INSUFFICIENT_EVIDENCE
        row = _blank_row(c, ST.INSUFFICIENT_EVIDENCE if insuff else ST.OK, "")
        row.update({"n_event": int(r.n_event), "n_control": int(r.n_control), "p_event": _num(r.p_event), "p_control": _num(r.p_control), "delta": _num(r.delta), "base_delta": _num(r.base_delta),
                    "ci_low": _num(r.ci_low), "ci_high": _num(r.ci_high), "n_blocks": int(r.n_blocks), "n_blocks_event": int(r.n_blocks_event or 0), "n_blocks_control": int(r.n_blocks_control or 0),
                    "n_nan_draws": int(r.n_nan_draws), "p_boot": None if insuff else _num(r.p_boot)})
        out[c["id"]] = row
    n_nan = sum(r["n_nan_draws"] for r in out.values())
    if n_nan > 0:  # M5: counted and reported, never silently dropped
        raise NanBootstrapAbort(f"{tag}: {n_nan} NaN bootstrap draw(s) in {sum(1 for r in out.values() if r['n_nan_draws'])} contrast(s); aborting (an empty arm in a resample means the cell is too thin for this design)")
    return out


def build_nc_a_frame(df: pd.DataFrame, cb: pd.DataFrame, part: str) -> tuple[pd.DataFrame, dict[str, int]]:
    """A/A table: the SECOND disjoint control set ``controls_b`` plays the pseudo-event, the first control set stays the control. Pairing is by the original event
    (``control_of``) and by rank inside that event, so every pseudo-event has exactly one partner control; the block of both is the day of the ORIGINAL event."""
    ev = df[(~df["is_control"]) & (df["partition"] == part)]
    ev_day = dict(zip(ev["event_id"], ev[DAY_COL], strict=True))
    c1 = df[df["is_control"] & (df["partition"] == part) & df["control_of"].isin(ev_day)].sort_values("event_id", kind="stable").copy()
    b = cb[(cb["partition"] == part) & cb["control_of"].isin(ev_day)].sort_values("event_id", kind="stable").copy()
    c1["_rk"], b["_rk"] = c1.groupby("control_of").cumcount(), b.groupby("control_of").cumcount()
    mg = b.merge(c1[["control_of", "_rk", "event_id"]].rename(columns={"event_id": "_c1_id"}), on=["control_of", "_rk"], how="inner")
    pe = mg.drop(columns=["_c1_id", "_rk"]).copy()
    pe["event_id"] = "b::" + pe["event_id"].astype(str)
    orig = pe["control_of"].to_numpy()
    pe["control_of"] = None
    pe["is_control"] = False
    pe[DAY_COL] = [ev_day[o] for o in orig]
    pid = dict(zip(mg["_c1_id"], pe["event_id"], strict=True))
    cc = c1[c1["event_id"].isin(pid)].copy()
    cc["control_of"] = cc["event_id"].map(pid)
    cc[DAY_COL] = [ev_day[o] for o in c1.loc[cc.index, "control_of"]]
    frame = pd.concat([pe, cc.drop(columns=["_rk"])], ignore_index=True)
    return frame, {"n_pairs": len(pe), "n_controls_b_stage": int(((cb["partition"] == part)).sum()), "n_unpaired_dropped": int(((cb["partition"] == part)).sum()) - len(pe)}


def shifted_labels(df: pd.DataFrame, label: str, part: str, tol_s: int) -> tuple[np.ndarray, float]:
    """NC-B: every row keeps its features and gets the label of the row of the SAME arm (event / control) that lies closest to the SAME Berlin time of day on
    the NEXT trading day present in the data, within +-``tol_s`` seconds (NaN if there is none). Events are sparse in time, so an exact-bar match would
    leave almost no pairs; the tolerance is preregistered. Returns (shifted label, share of stage rows that received one)."""
    d = df[DAY_COL].to_numpy(dtype="int64")
    tod = berlin_seconds_of_day(df["decision_ts_ns"].to_numpy(dtype="int64"))
    y = df[label].to_numpy(dtype="float64")
    ctl = df["is_control"].to_numpy()
    in_part = (df["partition"] == part).to_numpy()
    out = np.full(len(df), np.nan)
    for arm in (False, True):
        src = in_part & (ctl == arm) & np.isfinite(y)
        rows = ctl == arm
        if not src.any() or not rows.any():
            continue
        sk = d[src] * 86400 + tod[src]
        order = np.argsort(sk, kind="stable")
        sk, sl = sk[order], y[src][order]
        days = np.unique(d[src])
        pos = np.searchsorted(days, d[rows], side="right")
        has = pos < len(days)
        tk = days[np.minimum(pos, len(days) - 1)] * 86400 + tod[rows]
        j = np.searchsorted(sk, tk)
        left, right = np.clip(j - 1, 0, len(sk) - 1), np.clip(j, 0, len(sk) - 1)
        pick = np.where(np.abs(sk[left] - tk) <= np.abs(sk[right] - tk), left, right)
        ok = has & (np.abs(sk[pick] - tk) <= tol_s)
        out[rows] = np.where(ok, sl[pick], np.nan)
    cov = float(np.isfinite(out[in_part]).mean()) if in_part.any() else 0.0
    return out, cov


def _block_fn(task: dict[str, Any], event_days: np.ndarray) -> Any:
    """Block id of a day ordinal for the ad-hoc bootstraps (NC-A base delta, NC-C bridge): the day itself under observer-stats-2, the contiguous block of
    >= ``block_len_days`` trading days under observer-stats-3 (same mapping the enrichment uses)."""
    from coverage_analysis.observer_lab import stats as ST

    if task["stats"]["version"] != ST.STATS_V3:
        return lambda x: np.asarray(x)
    mp = ST.contiguous_day_blocks(event_days, int(task["stats"]["block_len_days"]))
    return lambda x: np.asarray([mp[d] for d in np.asarray(x).tolist()], dtype="int64")


def run_market(task: dict[str, Any]) -> dict[str, Any]:
    """Evaluate every contrast of ONE market for ONE stage. Pure function of ``task`` and the Parquet files; no registry, no shared state."""
    from dataclasses import replace

    from coverage_analysis.observer_lab import enrichment as EN
    from coverage_analysis.observer_lab import stats as ST

    market, stage, label, part = task["market"], task["stage"], task["label"], task["partition"]
    contrasts = task["contrasts"]
    cols = required_columns_from_task(task)
    df = prepare_frame(load_stage_frame(task["root"], market, part, cols))
    me = task["min_evidence"]
    cfg = EN.EnrichmentConfig(
        n_quantiles=task["n_quantiles"], min_evidence=ST.MinEvidence(me["events"], me["controls"], me["blocks"], 20), B=task["B"], seed=task["seed"], alpha=task["alpha"],
        adjust=task["adjust"], adjust_scope="family", p_method="bootstrap", day_col=DAY_COL, stats_version=task["stats"]["version"], block_unit=task["stats"]["block_unit"],
        block_len_days=int(task["stats"].get("block_len_days", ST.MIN_BLOCK_TRADING_DAYS)),
    )
    stats3 = task["stats"]["version"] == ST.STATS_V3
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
    testable: list[dict[str, Any]] = []
    for c in contrasts:
        d = defs.get(c["feature"])
        if d is None or c["cell"] not in d.labels():
            rows.append(_blank_row(c, CELL_UNDEFINED, note.get(c["feature"], f"cell {c['cell']!r} not in the frozen definition {d.labels() if d else '-'}")))
        else:
            testable.append(c)
    counts: dict[str, Any] = {"n_rows": len(df), "n_events_stage": int(((~df["is_control"]) & (df["partition"] == part)).sum()), "n_controls_stage": int(((df["is_control"]) & (df["partition"] == part)).sum()),
                              "n_events_warm": len(ev), "n_purged_embargo_rows": int(df["partition"].isin(UNUSABLE_TAGS).sum()), "n_nan_draws": 0}
    warns: list[str] = []
    extra: dict[str, Any] = {"nc_a_base": None, "nc_a_info": None, "nc_b_coverage": None, "bridge": None}
    main_items = [c for c in testable if c["kind"] in ("hyp", "nc")]
    if main_items and len(ev):
        res = _ablate(df, main_items, defs, cfg, label, task["purpose"], f"{market}-{stage}-main")
        wk_items = [c for c in main_items if c["kind"] == "hyp"]
        if wk_items:  # sensitivity (descriptive, never part of the correction): stats-2: ISO-week blocks of the event day; stats-3: LONGER contiguous blocks (``sensitivity_block_len_days``)
            scfg = replace(cfg, block_len_days=int(task["stats"]["sensitivity_block_len_days"]), B=task["B_week"]) if stats3 else replace(cfg, block_unit="week", B=task["B_week"])
            wres = _ablate(df, wk_items, defs, scfg, label, task["purpose"], f"{market}-{stage}-week")
            for i, w in wres.items():
                res[i].update({"week_ci_low": w["ci_low"], "week_ci_high": w["ci_high"], "week_p_boot": w["p_boot"], "week_n_blocks_event": w["n_blocks_event"]})
        rows += list(res.values())
    elif main_items:  # nothing to test on
        rows += [_blank_row(c, ST.INSUFFICIENT_EVIDENCE, "no events in the stage partition") for c in main_items]
    # ---- NC-A: A/A test with the second disjoint control set
    a_items = [c for c in testable if c["kind"] == "nc_a"]
    if a_items and len(ev):
        cb = prepare_frame(load_part(task["root"], market, "controls_b.parquet", part, cols))
        frame_a, info = build_nc_a_frame(df, cb, part)
        extra["nc_a_info"] = info
        if info["n_pairs"]:
            res_a = _ablate(frame_a, a_items, defs, cfg, label, task["purpose"], f"{market}-{stage}-nca")
            rows += list(res_a.values())
            pe, cc = frame_a[~frame_a["is_control"] & frame_a["warmup_ok"]], frame_a[frame_a["is_control"]]
            bf = _block_fn(task, pe[DAY_COL].to_numpy())  # stats-2: the day itself; stats-3: contiguous block of >= 21 trading days (controls follow their event)
            blk = dict(zip(pe["event_id"], bf(pe[DAY_COL].to_numpy()), strict=True))
            cc = cc[cc["control_of"].isin(blk)]
            est = ST.block_bootstrap_delta(pe[label].to_numpy(), bf(pe[DAY_COL].to_numpy()), cc[label].to_numpy(), np.asarray([blk.get(e, -1) for e in cc["control_of"]]), B=task["B"],
                                           seed=(task["seed"] + 17) & 0x7FFFFFFF, alpha=task["nc_base_alpha"])
            if est.n_nan_draws:
                raise NanBootstrapAbort(f"{market}-{stage}-nca-base: {est.n_nan_draws} NaN bootstrap draw(s)")
            extra["nc_a_base"] = {"delta": _num(est.delta), "ci_low": _num(est.ci_low), "ci_high": _num(est.ci_high), "alpha_used": task["nc_base_alpha"], "n_event": est.n_event, "n_control": est.n_control,
                                  "null": bool(est.n_event and est.ci_low <= 0 <= est.ci_high)}
        else:
            rows += [_blank_row(c, ST.INSUFFICIENT_EVIDENCE, "no pseudo-event / control pairs (controls_b empty for this partition)") for c in a_items]
    elif a_items:
        rows += [_blank_row(c, ST.INSUFFICIENT_EVIDENCE, "no events in the stage partition") for c in a_items]
    # ---- NC-B: shift placebo (features of the row, label of the same time of day on the next trading day)
    b_items = [c for c in testable if c["kind"] == "nc_b"]
    if b_items and len(ev):
        new, cov = shifted_labels(df, label, part, int(task["nc_b_tol_s"]))
        extra["nc_b_coverage"] = cov
        fb = df.copy()
        fb[label] = new
        rows += list(_ablate(fb, b_items, defs, cfg, label, task["purpose"], f"{market}-{stage}-ncb").values())
    elif b_items:
        rows += [_blank_row(c, ST.INSUFFICIENT_EVIDENCE, "no events in the stage partition") for c in b_items]
    # ---- NC-C: bridge controls-3 vs controls-2 (explore markets, descriptive: balance and base delta only, no feature cells)
    if task.get("bridge") and len(ev):
        extra["bridge"] = bridge_controls(task, df, ev, cols, cfg)
    counts["n_events_used"] = len(ev)
    order = {c["id"]: i for i, c in enumerate(contrasts)}
    rows.sort(key=lambda r: order[r["id"]])
    return {"market": market, "stage": stage, "rows": rows, "cell_defs": _defs_to_json(defs), "counts": counts, "warnings": warns, **extra}


def bridge_controls(task: dict[str, Any], df: pd.DataFrame, ev: pd.DataFrame, cols: list[str], cfg: Any) -> dict[str, Any]:
    """NC-C: base delta (events vs matched controls) under controls-3 and under controls-2 for the SAME events, and their difference with a paired day-block CI."""
    from coverage_analysis.observer_lab import stats as ST

    label, part = task["label"], task["partition"]
    p2 = resolve_file(task["root"], task["market"], "controls_v2.parquet")
    if not p2.is_file():
        return {"available": False, "note": "controls_v2.parquet not present: no controls-2 set to bridge to"}
    c2 = prepare_frame(load_part(task["root"], task["market"], "controls_v2.parquet", part, cols))
    c2 = c2[(c2["partition"] == part) & c2["is_control"]]
    c3 = df[df["is_control"] & (df["partition"] == part)]
    both = set(c3["control_of"]) & set(c2["control_of"]) & set(ev["event_id"])
    e, c3, c2 = ev[ev["event_id"].isin(both)], c3[c3["control_of"].isin(both)], c2[c2["control_of"].isin(both)]
    bf = _block_fn(task, e[DAY_COL].to_numpy())
    day = dict(zip(e["event_id"], bf(e[DAY_COL].to_numpy()), strict=True))
    d3, d2 = np.asarray([day[x] for x in c3["control_of"]]), np.asarray([day[x] for x in c2["control_of"]])
    ye, de = e[label].to_numpy(), bf(e[DAY_COL].to_numpy())
    est = ST.block_bootstrap_contrast((ye, de, c3[label].to_numpy(), d3), (ye, de, c2[label].to_numpy(), d2), B=task["B"], seed=(task["seed"] + 29) & 0x7FFFFFFF, alpha=task["alpha"])
    cens3 = float(c3[label].isna().mean()) if len(c3) else float("nan")
    cens2 = float(c2[label].isna().mean()) if len(c2) else float("nan")
    cens_e = float(e[label].isna().mean()) if len(e) else float("nan")
    return {"available": True, "n_events": len(e), "n_controls3": len(c3), "n_controls2": len(c2), "base_delta_controls3": _num(est.p_event - est.p_control), "base_delta_controls2": _num(est.p_event - est.p_control - est.delta),
            "difference_3_minus_2": _num(est.delta), "ci_low": _num(est.ci_low), "ci_high": _num(est.ci_high), "n_nan_draws": est.n_nan_draws,
            "censoring_diff_pp_controls3": _num(100 * (cens_e - cens3)), "censoring_diff_pp_controls2": _num(100 * (cens_e - cens2)),
            "note": "descriptive only: balance and base delta, no feature cells, no verdict effect"}


def required_columns_from_task(task: dict[str, Any]) -> list[str]:
    cols = [*BASE_COLS, task["label"]]
    for c in task["contrasts"]:
        cols.extend(NEEDS.get(c["feature"], (c["feature"],)))
    return list(dict.fromkeys(cols))


# ---------------------------------------------------------------------------------------------- plan / registry / cache / lock
def _family(nsp: str, stage: str, scope: str, label: str, kind: str) -> str:
    return f"{nsp}|{stage}|{scope}|{label}|{ {'hyp': 'all', 'nc': 'ctrl', 'nc_a': 'nc_a', 'nc_b': 'nc_b'}[kind] }"


def _name(nsp: str, stage: str, market: str, label: str, kind: str, item: dict[str, Any]) -> str:
    return f"{nsp}|{stage}|{market}|{label}|{kind}|{item['group']}|{item['feature']}|{item['cell']}"


def build_plan(prereg: Prereg, stage: str, markets: list[str], survivors: list[str] | None, descriptive: frozenset[str] | set[str] = frozenset()) -> list[dict[str, Any]]:
    """[(market x contrast) items]. ``survivors`` (keys ``MARKET|Hxx``) restricts validate / oos to the previous stage's survivors (+ their nc / nc_a / nc_b
    controls). Markets in ``descriptive`` (balance gate failed) get their hypotheses evaluated but they are NOT registered, not in m, never survivors."""
    s = prereg.spec
    nsp = ns(prereg)
    plan: list[dict[str, Any]] = []
    for m in markets:
        scope = market_scope(prereg, m)
        desc = m in descriptive
        for h in s["hypotheses"]:
            if survivors is not None and f"{m}|{h['id']}" not in survivors:
                continue
            plan.append({**h, "kind": "hyp", "market": m, "scope": scope, "descriptive": desc, "key": f"{m}|{h['id']}", "name": _name(nsp, stage, m, s["label"], "hyp", h), "family": None if desc else _family(nsp, stage, scope, s["label"], "hyp")})
            if scope == "core" and not desc:
                for kind, pre in (("nc_a", "A_"), ("nc_b", "B_")):
                    ih = {**h, "id": pre + h["id"], "sign": 0}
                    plan.append({**ih, "kind": kind, "market": m, "scope": scope, "descriptive": False, "key": f"{m}|{ih['id']}", "name": _name(nsp, stage, m, s["label"], kind, ih), "family": _family(nsp, stage, scope, s["label"], kind)})
        if desc or (survivors is not None and not any(k.startswith(m + "|") for k in survivors)):
            continue
        for h in s["negative_controls"]:
            plan.append({**h, "kind": "nc", "market": m, "scope": scope, "descriptive": False, "key": f"{m}|{h['id']}", "name": _name(nsp, stage, m, s["label"], "nc", h), "family": _family(nsp, stage, scope, s["label"], "nc")})
    return plan


def confirmatory_eligibility(stage: str, plan: list[dict[str, Any]], markets: list[str], descriptive: set[str] | frozenset[str]) -> list[dict[str, Any]]:
    """ORDER OF DECISIONS (fixed): 1. balance eligibility per market (preflight: ``descriptive`` = gate not passed) -> 2. THIS check: is any CONFIRMATORY test left
    (a registered hypothesis of a CORE market; explore markets can never confirm, so an eligible BRENT / BTCUSD does not count) -> 3. only then the registry and the
    fit lock. When nothing is left the stop rule 'no confirmatory test left' fires here: nothing is registered, no lock is taken, the verdict is
    INCONCLUSIVE_NOT_ASKED (the controls were not good enough to ask the question), which is NOT 'no enrichment'."""
    items = [p for p in plan if p["kind"] == "hyp" and p["scope"] == "core" and p["family"] is not None]
    if items:
        return items
    if stage == "fit":
        raise NoConfirmatoryTestLeft(
            f"no confirmatory test left: no core market is eligible (balance gate not passed -> descriptive_only: {sorted(descriptive & set(markets))}; explore markets never confirm); "
            f"nothing registered, no fit lock taken. {V_NOT_ASKED} (kein no-enrichment): the controls are not good enough to ask the confirmatory question.")
    raise StopRule(f"no confirmatory test left: stage {stage!r} has no survivors of the previous stage to replicate; nothing registered")


def family_sizes(plan: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in plan:
        if p["family"] is not None:
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
    """Register the whole (confirmatory) stage BEFORE evaluation. Returns True when it was already fully registered (a re-run / resume). A partial overlap is an error."""
    from coverage_analysis.observer_lab.stats import HypothesisReuseError

    reg_items = [p for p in plan if p["family"] is not None]
    names = [p["name"] for p in reg_items]
    have = [n in existing for n in names]
    if all(have) and names:
        return True
    if any(have):
        raise HypothesisReuseError("the stage is only partly registered in the registry; refusing to continue (no best-of-N)")
    fams: dict[str, list[str]] = {}
    for p in reg_items:
        fams.setdefault(p["family"], []).append(p["name"])
    for f, ns in fams.items():
        reg.declare_family(f, definition=json.dumps(sorted(ns)))
    reg.register_many(names, [p["family"] for p in reg_items])
    return False


def fingerprint(prereg: Prereg, task: dict[str, Any]) -> str:
    """Cache key: script version, prereg block, stage/market/parameters AND the CONTENT fingerprint of the data (file hashes, row counts, schema hash,
    partition counts, manifest hash), never just size / mtime."""
    body = {"v": SCRIPT_VERSION, "prereg": prereg.json_sha256, "stage": task["stage"], "market": task["market"], "B": task["B"], "B_week": task["B_week"], "seed": task["seed"], "data": task["data_fingerprint"],
            "contrasts": [(c["id"], c["kind"], c["feature"], c["cell"]) for c in task["contrasts"]], "cell_defs": _sha(task.get("cell_defs")), "nc_base_alpha": task["nc_base_alpha"], "nc_b_tol_s": task["nc_b_tol_s"], "bridge": task.get("bridge")}
    return _sha(body)


def _evaluate_markets(tasks: list[dict[str, Any]], jobs: int) -> list[dict[str, Any]]:
    if jobs > 1 and len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, len(tasks))) as ex:
            return list(ex.map(run_market, tasks))  # order preserved: the result never depends on completion order
    return [run_market(t) for t in tasks]


def lock_path(prereg: Prereg) -> Path:
    return LOCK_DIR / f"{prereg.spec['prereg_version']}.fit.lock.json"


def read_lock(prereg: Prereg) -> dict[str, Any] | None:
    p = lock_path(prereg)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def write_lock(prereg: Prereg, lock: dict[str, Any]) -> None:
    p = lock_path(prereg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(lock, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def stage_core(st: dict[str, Any]) -> dict[str, Any]:
    """The part of a stage result that must be reproducible (no timing, no resume flag)."""
    return {k: st.get(k) for k in ("stage", "partition", "verdict", "survivors", "negative_control_failures", "prereg_json_sha256", "B", "families", "markets", "results", "preflight")}


def stage_hash(st: dict[str, Any]) -> str:
    return _sha(_clean(stage_core(st)))


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


def previous_survivors(prereg: Prereg, stage: str, report: dict[str, Any], confirm_oos: bool, out: Path | None = None, check_lock: bool = True) -> tuple[list[str], dict[str, Any] | None]:
    prev = {"validate": "fit", "oos": "validate"}.get(stage)
    if prev is None:
        return [], None
    st = (report.get("stages") or {}).get(prev)
    if not st or st.get("status") != "COMPLETE":
        raise StopRule(f"stage {stage!r} needs a COMPLETE {prev!r} stage in the report (run it first)")
    if st.get("prereg_json_sha256") != prereg.json_sha256:
        raise StopRule(f"the {prev!r} stage was run under a different preregistration block; a new preregistration needs a new --out")
    if check_lock:
        lock = read_lock(prereg)
        fit = (report.get("stages") or {}).get("fit")
        if lock is None or not fit or lock.get("fit_stage_sha256") != stage_hash(fit) or (out is not None and lock.get("out") != str(out)):
            raise StopRule("the fit report does not match the fit lock (hash / --out): the fit stage is run exactly once per preregistration version; the stage chain is refused")
    if not st.get("survivors"):
        raise StopRule(f"STOP RULE: stage {prev!r} ended with {st.get('verdict')} and no survivors: documented as 'no enrichment' / not confirmed; {stage!r} is not run")
    if stage == "oos" and not confirm_oos:
        raise StopRule("the OOS partition is touched exactly once; pass --confirm-oos-once to proceed")
    return list(st["survivors"]), st


def stage_verdict(stage: str, results: list[dict[str, Any]], markets: dict[str, Any], families: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    from coverage_analysis.observer_lab import stats as ST

    fails = [f"{r['key']}|{r['kind']}" for r in results if r["kind"] in ("nc", "nc_a", "nc_b") and r["status"] == ST.SIGNIFICANT_ADJUSTED]
    for m, v in sorted(markets.items()):
        mrows = [r for r in results if r["market"] == m]
        base = v.get("nc_a_base")
        if base is not None and not base["null"]:
            fails.append(f"{m}|NC_A_BASE_NOT_NULL")
        for kind in ("nc_a", "nc_b"):
            kr = [r for r in mrows if r["kind"] == kind]
            if kr and not any(r["status"] not in (ST.INSUFFICIENT_EVIDENCE, CELL_UNDEFINED) for r in kr):
                fails.append(f"{m}|{kind.upper()}_NOT_EVALUABLE")  # a negative control that cannot run is not a pass
    fails += [f"{f}|POWER_LIMITED" for f, v in families.items() if v["power_limited"] and (f.endswith("|nc_a") or f.endswith("|nc_b") or f.endswith("|ctrl"))]
    surv = [r["key"] for r in results if r["kind"] == "hyp" and r["survivor"]]
    if fails:
        return V_INVALID, [], fails
    hyp = [r for r in results if r["kind"] == "hyp" and not r["descriptive"]]
    if stage == "fit":
        if surv:
            return V_FIT_OK, surv, []
        if not hyp or all(r["status"] in (ST.INSUFFICIENT_EVIDENCE, ST.POWER_LIMITED, CELL_UNDEFINED) for r in hyp):
            return V_INCONCLUSIVE, [], []
        return V_NONE, [], []
    if surv:
        return (V_VAL_OK if stage == "validate" else V_OOS_OK), surv, []
    return V_NOT_CONFIRMED, [], []


def run_stage(a: argparse.Namespace, prereg: Prereg, stage: str, markets: list[str], pre: dict[str, Any]) -> dict[str, Any]:
    from coverage_analysis.observer_lab import stats as ST

    spec = prereg.spec
    out, root = Path(a.out), Path(a.root)
    report = load_report(out / f"{report_stem(prereg)}.json")
    survivors, prev = previous_survivors(prereg, stage, report, a.confirm_oos_once, out)
    descriptive = {m for m in markets if pre["markets"][m]["status"] != "passed"}
    plan = build_plan(prereg, stage, markets, survivors if stage != "fit" else None, descriptive)
    confirmatory_eligibility(stage, plan, markets, descriptive)  # BEFORE the fit lock and the registry (raises NoConfirmatoryTestLeft / StopRule)
    fams = family_sizes(plan)
    choice = choose_B(prereg, fams)
    label, part, purpose = spec["label"], spec["stages"][stage]["partition"], spec["stages"][stage]["purpose"]
    n_core = max(1, len({p["market"] for p in plan if p["scope"] == "core" and p["kind"] == "nc_a"}))
    nc_alpha = spec["placebos"]["alpha"]  # negative controls use a stricter level (a valid stage must not be declared invalid by chance in 3 families x markets)
    nc_base_alpha = nc_alpha / n_core  # Bonferroni over the markets that carry an A/A test
    prev_defs = (prev or {}).get("markets", {})
    by_market: dict[str, list[dict[str, Any]]] = {}
    for p in plan:
        by_market.setdefault(p["market"], []).append(p)
    # ---- fit lock: exactly one fit per preregistration version, anchored OUTSIDE --out
    fps = {m: pre["markets"][m]["data_fingerprint"] for m in by_market}
    lock = read_lock(prereg)
    if stage == "fit":
        if lock is not None and (lock.get("out") != str(out) or lock.get("prereg_json_sha256") != prereg.json_sha256):
            raise StopRule(f"FIT LOCK: a fit of {spec['prereg_version']} was already started with --out {lock.get('out')} ({lock_path(prereg)}); a second fit with another --out is forbidden (no best-of-N)")
        if lock is not None and lock.get("data_fingerprints") != fps:
            raise StopRule("FIT LOCK: the data fingerprint differs from the one of the first fit of this preregistration; refusing (no best-of-N)")
        if lock is None:
            write_lock(prereg, {"prereg_version": spec["prereg_version"], "prereg_json_sha256": prereg.json_sha256, "registry_name": spec["registry_name"], "out": str(out), "script_version": SCRIPT_VERSION,
                                "created_utc": datetime.now(UTC).isoformat(timespec="seconds"), "data_fingerprints": fps, "fit_stage_sha256": None})
    else:
        for m, fp in fps.items():
            if ((lock or {}).get("data_fingerprints") or {}).get(m) != fp:
                raise StopRule(f"the data of {m} changed since the fit stage (fingerprint differs); a new preregistration needs a new fit")
    tasks = []
    for m, items in by_market.items():
        sc = market_scope(prereg, m)
        t = {"market": m, "stage": stage, "label": label, "partition": part, "purpose": purpose, "root": str(root), "B": choice.B, "B_week": min(choice.B, spec["stats"].get("sensitivity_B_max", 4000)),
             "seed": zlib.crc32(f"{ns(prereg)}|{stage}|{m}".encode()) & 0x7FFFFFFF, "stats": spec["stats"], "n_quantiles": spec["test"]["n_quantiles"], "alpha": spec["test"]["alpha"], "adjust": spec["test"]["adjust"],
             "min_evidence": spec["min_evidence"], "nc_base_alpha": nc_base_alpha, "nc_b_tol_s": 60 * int(spec["placebos"]["nc_b"]["tolerance_minutes"]), "data_fingerprint": pre["markets"][m]["data_fingerprint"],
             "bridge": bool(sc == "explore" and pre["markets"][m]["has_controls_v2"]),
             "contrasts": [{k: h[k] for k in ("id", "group", "feature", "cell", "kind")} for h in items], "cell_defs": None if stage == "fit" else (prev_defs.get(m) or {}).get("cell_defs")}
        if stage != "fit" and t["cell_defs"] is None:
            raise StopRule(f"no frozen cell definitions for {m} in the previous stage")
        tasks.append(t)
    # ---- registry: the whole confirmatory stage is registered BEFORE anything is evaluated
    reg_path = out / registry_file(prereg)
    reg = ST.HypothesisRegistry(spec["registry_name"], reg_path)
    existing = registry_recorded(reg_path)
    resumed = register_stage(reg, plan, existing)
    # ---- per-market evaluation with a cache (fingerprint = prereg block + stage + CONTENT of the inputs + parameters)
    cache_dir = out / "cache" / stage
    cache_dir.mkdir(parents=True, exist_ok=True)
    results_by_market: dict[str, dict[str, Any]] = {}
    todo = []
    for t in tasks:
        cp = cache_dir / f"{t['market']}.json"
        fp = fingerprint(prereg, t)
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
        if p["family"] is None:
            continue
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
        desc = p["descriptive"]
        m_fam = reg.family_size(p["family"]) if p["family"] else None
        a_lvl = alpha if p["kind"] == "hyp" else nc_alpha
        pl = ST.is_power_limited(m_fam, choice.B, a_lvl) if m_fam else False
        a_p = adj.get(p["name"]) if p["family"] else None
        if r["evidence"] == CELL_UNDEFINED:
            status = CELL_UNDEFINED
        elif r["evidence"] == ST.INSUFFICIENT_EVIDENCE:
            status = ST.INSUFFICIENT_EVIDENCE
        elif desc:
            status = DESCRIPTIVE_ONLY
        else:
            status = ST.final_status(ST.OK, a_p, r["ci_low"], r["ci_high"], a_lvl, power_limited=pl)
        d = r["delta"]
        sign_ok = "NA" if p["kind"] != "hyp" or d is None else ("EXPECTED" if np.sign(d) == p["sign"] else "CONTRARY")
        surv = p["kind"] == "hyp" and p["scope"] == "core" and not desc and status == ST.SIGNIFICANT_ADJUSTED and sign_ok == "EXPECTED" and d is not None and abs(d) >= min_eff and not pl
        wk = None if r["week_ci_low"] is None else bool(r["week_ci_low"] > 0 or r["week_ci_high"] < 0)
        results.append({"key": p["key"], "id": p["id"], "kind": p["kind"], "market": p["market"], "scope": p["scope"], "descriptive": desc, "group": p["group"], "feature": p["feature"], "cell": p["cell"], "expected_sign": p["sign"],
                        "family": p["family"], "m_family": m_fam, "B": choice.B, "power_limited": pl, "n_event": r["n_event"], "n_control": r["n_control"], "p_event": r["p_event"], "p_control": r["p_control"], "delta": d,
                        "base_delta": r["base_delta"], "ci_low": r["ci_low"], "ci_high": r["ci_high"], "n_blocks": r["n_blocks"], "n_blocks_event": r["n_blocks_event"], "n_blocks_control": r["n_blocks_control"],
                        "n_nan_draws": r["n_nan_draws"], "p_boot": r["p_boot"], "adjusted_p": a_p, "adjusted_p_registry": adj_reg.get(p["name"]) if p["family"] else None, "status": status, "sign_check": sign_ok,
                        "survivor": bool(surv), "week_ci_low": r["week_ci_low"], "week_ci_high": r["week_ci_high"], "week_p_boot": r["week_p_boot"], "week_ci_excludes_0": wk, "note": r["note"], "hypothesis": p["name"]})
    fam_summary = {f: {"m": n, "power_limited": ST.is_power_limited(n, choice.B, alpha if f.endswith("|all") else nc_alpha), "alpha": alpha if f.endswith("|all") else nc_alpha} for f, n in sorted(fams.items())}
    msum = results_by_market_summary(results_by_market)
    verdict, surv_keys, nc_fail = stage_verdict(stage, results, msum, fam_summary)
    contrary = [r["key"] for r in results if r["kind"] == "hyp" and not r["descriptive"] and r["status"] == ST.SIGNIFICANT_ADJUSTED and r["sign_check"] == "CONTRARY"]
    return _clean({
        "status": "COMPLETE", "stage": stage, "partition": part, "purpose": purpose, "verdict": verdict, "survivors": surv_keys, "negative_control_failures": nc_fail, "contrary_significant": contrary,
        "descriptive_only_markets": sorted(descriptive), "prereg_json_sha256": prereg.json_sha256, "script_version": SCRIPT_VERSION, "stats_version": spec["stats"]["version"], "resumed_registered_stage": resumed, "B": choice.B,
        "required_B": choice.required, "b_warning": choice.warning, "n_hypotheses_ever": reg.n_hypotheses, "families": fam_summary, "markets": msum, "results": results, "timing": timing,
        "preflight": {m: {k: v[k] for k in ("status", "manifest_sha256", "control_method_version", "data_fingerprint", "balance_gate", "observed_censoring_diff_pp")} for m, v in pre["markets"].items() if m in by_market},
    })


def results_by_market_summary(rbm: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {m: {"counts": r["counts"], "cell_defs": r["cell_defs"], "warnings": r["warnings"], "nc_a_base": r.get("nc_a_base"), "nc_a_info": r.get("nc_a_info"), "nc_b_coverage": r.get("nc_b_coverage"),
                "bridge": r.get("bridge")} for m, r in sorted(rbm.items())}


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
              f"* descriptive_only markets (balance gate not passed; not in m, NOT 'no enrichment'): {', '.join(st['descriptive_only_markets']) or 'none'}",
              f"* stats `{st['stats_version']}`; B = {st['B']} (required {st['required_B']}); hypotheses ever registered: {st['n_hypotheses_ever']}; registered-stage resume: {st['resumed_registered_stage']}"]
        if st.get("b_warning"):
            L.append(f"* WARNING: {st['b_warning']}")
        if st["verdict"] in (V_NONE, V_INCONCLUSIVE, V_INVALID, V_NOT_CONFIRMED):
            L.append("* STOP RULE applies: documented, the next stage is not run, no variants are tried inside this preregistration.")
        L += ["", "Controls and balance (preflight):", "", "| market | controls version | status | match | smd min | smd atr | smd spread | cens. diff pp (manifest / observed) | manifest sha | data fingerprint |", "|---|---|---|---|---|---|---|---|---|---|"]
        for m, v in st["preflight"].items():
            b = v["balance_gate"] or {}
            oc = "-" if v["observed_censoring_diff_pp"] is None else f"{v['observed_censoring_diff_pp']:+.2f}"
            L.append(f"| {m} | {v['control_method_version']} | {v['status']} | {b.get('match_rate')} | {b.get('smd_local_minute')} | {b.get('smd_atr_pct')} | {b.get('smd_spread_pct')} | {b.get('censoring_diff_pp')} / {oc} | `{str(v['manifest_sha256'])[:12]}` | `{str(v['data_fingerprint'])[:12]}` |")
        L += ["", "| family | m | power limited |", "|---|---|---|", *[f"| `{f}` | {v['m']} | {v['power_limited']} |" for f, v in st["families"].items()], "",
              "| market | kind | id | feature | cell | exp | n ev | n ctl | blocks ev/ctl | delta pp | CI pp | adj p | status | sign | week CI excl 0 | surv |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in st["results"]:
            ci = "-" if r["ci_low"] is None else f"[{_pp(r['ci_low'])}, {_pp(r['ci_high'])}]"
            ap = "-" if r["adjusted_p"] is None else f"{r['adjusted_p']:.3g}"
            L.append(f"| {r['market']} | {r['kind']} | {r['id']} | `{r['feature'].split('__', 1)[-1]}` | {r['cell']} | {r['expected_sign']:+d} | {r['n_event']} | {r['n_control']} | {r['n_blocks_event']}/{r['n_blocks_control']} | {_pp(r['delta'])} | {ci} | {ap} | {r['status']} | {r['sign_check']} | {r['week_ci_excludes_0'] if r['week_ci_excludes_0'] is not None else '-'} | {'YES' if r['survivor'] else ''} |")
        L += ["", "Per market:", ""]
        for m, v in st["markets"].items():
            extra = ""
            if v.get("nc_a_base"):
                b = v["nc_a_base"]
                extra += f"; NC-A base delta {_pp(b['delta'])} pp CI [{_pp(b['ci_low'])}, {_pp(b['ci_high'])}] (alpha {b['alpha_used']:.4g}) null={b['null']}, pairs {v['nc_a_info']}"
            if v.get("nc_b_coverage") is not None:
                extra += f"; NC-B shifted-label coverage {v['nc_b_coverage']:.2f}"
            if v.get("bridge"):
                extra += f"; NC-C bridge {v['bridge']}"
            L.append(f"* {m}: {v['counts']}" + (f"; warnings: {v['warnings']}" if v["warnings"] else "") + extra)
        L.append("")
    L += ["## Honest limits", "", "* One label, predeclared contrasts, terciles frozen on TRAIN; a null is not proof of no structure.", "* Bar-resolution first-passage rates, costs excluded; day blocks are the independence unit (week blocks reported as a sensitivity).",
          "* 07-01..08-31 (OOS) is frozen only relative to the observer; the forward period (>= 2026-09-01) was never read."]
    return "\n".join(L) + "\n"


def write_report(out: Path, prereg: Prereg, stage: str, stage_res: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rep = load_report(out / f"{report_stem(prereg)}.json")
    old = (rep.get("stages") or {}).get(stage)
    if old is not None and stage_hash(old) != stage_hash(stage_res):
        raise StopRule(f"the report already holds a stage {stage!r} with different content; a stage result is never overwritten (no best-of-N)")
    rep["prereg"] = {"path": prereg.path, "file_sha256": prereg.file_sha256, "json_sha256": prereg.json_sha256, "version": prereg.spec["prereg_version"]}
    rep.setdefault("stages", {})[stage] = stage_res
    (out / f"{report_stem(prereg)}.json").write_text(json.dumps(_clean(rep), indent=1), encoding="utf-8")
    (out / f"{report_stem(prereg)}.md").write_text(render_md(rep), encoding="utf-8")


def pin_fit_hash(prereg: Prereg, stage_res: dict[str, Any]) -> None:
    lock = read_lock(prereg)
    if lock is None:
        raise StopRule("fit lock missing after the fit stage")
    h = stage_hash(stage_res)
    if lock.get("fit_stage_sha256") not in (None, h):
        raise StopRule("the fit result differs from the fit report hash pinned in the lock (no best-of-N)")
    lock["fit_stage_sha256"] = h
    lock["fit_completed_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    write_lock(prereg, lock)


# ---------------------------------------------------------------------------------------------- CLI
def dry_run(a: argparse.Namespace, prereg: Prereg, stage: str, markets: list[str]) -> int:
    """Prints the plan. Reads NO backfill data (not even a Parquet footer): only checks that the expected files exist."""
    survivors: list[str] | None = None
    note = ""
    if stage != "fit":
        rep = load_report(Path(a.out) / f"{report_stem(prereg)}.json")
        try:
            survivors, _ = previous_survivors(prereg, stage, rep, True, Path(a.out).resolve())
        except StopRule as e:
            note = f"(would stop: {e})"
            survivors = []
    elif not set(prereg.spec["markets"]["core"]) <= set(markets):
        note = "(a real fit is REFUSED: --markets must cover the full preregistered core set)"
    plan = build_plan(prereg, stage, markets, survivors)
    fams = family_sizes(plan)
    ch = choose_B(prereg, fams) if fams else None
    print(f"DRY RUN stage={stage} partition={prereg.spec['stages'][stage]['partition']} markets={markets} jobs={a.jobs} {note}")
    print(f"prereg {prereg.spec['prereg_version']} block sha256 {prereg.json_sha256[:16]}; label {prereg.spec['label']}")
    print(f"tests: {len(plan)}; families: {len(fams)}; B: {ch.B if ch else '-'}")
    for f, n in sorted(fams.items()):
        print(f"  family {f}: m={n}")
    for m in markets:
        scope = market_scope(prereg, m)
        names = ["table.parquet", "controls.parquet", MANIFEST_NAME] + (["controls_b.parquet"] if scope == "core" else [])
        have = {n: resolve_file(a.root, m, n).is_file() for n in names}
        print(f"  market {m} ({scope}): " + " ".join(f"{n}={v}" for n, v in have.items()) + " (existence only, nothing read)")
    lk = read_lock(prereg)
    print(f"fit lock {lock_path(prereg)}: {'present (out ' + str(lk.get('out')) + ')' if lk else 'absent'}")
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
    ap.add_argument("--preflight", action="store_true", help="run only the preflight (counts + controls manifests); registers nothing")
    ap.add_argument("--attest", action="store_true", help=f"read-only: pin the SHA-256 of the current control files (artifacts whose manifest has no controls_sha256) into <out>/{ATTEST_FILE}; nothing in --root is written")
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
    if a.stage == "fit" and not set(prereg.spec["markets"]["core"]) <= set(markets):
        ap.error(f"--stage fit needs the full preregistered core set {prereg.spec['markets']['core']} (partial fits are not allowed); explore markets may be added")
    a.out, a.root = str(out), str(root)
    if a.attest:
        return attest(prereg, root, out, markets)
    # ---- preflight FIRST: a failure registers nothing and consumes no stop rule
    pre = preflight(prereg, root, a.stage, markets, load_attestation(out))
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{preflight_stem(prereg)}_{a.stage}.json").write_text(json.dumps(_clean(pre), indent=1), encoding="utf-8")
    if not pre["ok"]:
        print("PREFLIGHT FAILED (nothing registered, stop rule not consumed):")
        for e in pre["errors"]:
            print(f"  - {e}")
        return EXIT_PREFLIGHT
    if a.preflight:
        print("PREFLIGHT OK: " + "; ".join(f"{m}={v['status']}" for m, v in pre["markets"].items()))
        return 0
    try:
        res = run_stage(a, prereg, a.stage, markets, pre)
        write_report(out, prereg, a.stage, res)
        if a.stage == "fit":
            pin_fit_hash(prereg, res)
    except NoConfirmatoryTestLeft as e:
        print(f"STOP: {e}")
        print(f"{V_NOT_ASKED} (kein no-enrichment): no registry written, no fit lock taken")
        return EXIT_STOP
    except StopRule as e:
        print(f"STOP: {e}")
        return EXIT_STOP
    except NanBootstrapAbort as e:
        print(f"ABORT (NaN bootstrap draws): {e}")
        return EXIT_NAN
    print(f"stage {a.stage}: {res['verdict']}; survivors: {res['survivors'] or 'none'}; B={res['B']}; report -> {out / (report_stem(prereg) + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
