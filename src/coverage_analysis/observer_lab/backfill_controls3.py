# ruff: noqa: E501
"""Controls step ``observer-controls-3`` of the observer backfill (OFFLINE / RESEARCH ONLY; same-clock-time other-day controls + blocking balance gate).

Re-runnable alone (``scripts/observer_backfill.py --step controls3``); READS the events step, never rewrites it, never touches the controls-2 files:

    <out>/<MARKET>/controls3/    controls{,_events,_features,_labels}.parquet + controls_diag.parquet + controls_manifest.json   (set A)
    <out>/<MARKET>/controls3_b/  same files for the disjoint A/A set B (``with_b``)

``controls_diag.parquet`` (one row per control): ``event_id`` (the control's id), ``control_of`` (its EVENT's id), ``control_set``, ``partition`` (the event's
tag), ``control_partition``, ``day_offset``, ``dist_to_opportunity_bars`` (diagnostic, NOT an exclusion rule), ``event_decision_idx``, ``control_decision_idx`` and the
matching covariates of both bars (``ev_*`` / ``c_*``: session, local minute, partition-internal ATR / spread percentile rank). The blocking balance gate
(``controls_sametime.balance_gate``) reads this file plus label AVAILABILITY (censoring) and the event partition counts - never a feature-vs-label distribution.
"""

from __future__ import annotations

import json
import shutil
import time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from alpha.common.market_data import assert_no_forward_holdout
from coverage_analysis.observer_lab import backfill as BF
from coverage_analysis.observer_lab import controls_sametime as CS
from coverage_analysis.observer_lab.controls import bar_covariates, bar_partitions
from coverage_analysis.observer_lab.labels import DEFAULT_MAX_BARS, LABEL_CONVENTION_VERSION
from coverage_analysis.observer_lab.splits import guard_dev_only
from market_observer.observer import event_id_for

CONTROLS3_PIPELINE_VERSION = "observer-backfill-controls3-1"
SUBDIRS = {"a": "controls3", "b": "controls3_b"}
DIAG_FILE = "controls_diag.parquet"
CENSOR_LABEL = "y_fav050_before_adv050"  # the 0.5 R / 0.5 R first-passage label: None = censored (neither barrier inside the horizon)
CENSOR_LABELS = ("y_fav025_before_adv025", "y_fav050_before_adv050", "y_fav075_before_adv050", "y_fav100_before_adv050")


def controls3_fingerprint(events_fp: str, seed: int, spec: CS.SameTimeSpec, with_b: bool, max_bars: int) -> str:
    import hashlib

    payload = json.dumps({"v": CONTROLS3_PIPELINE_VERSION, "events": events_fp, "seed": seed, "spec": asdict(spec), "b": with_b, "ctl": CS.CONTROL_METHOD_VERSION,
                          "rev": CS.CONTROL_MATCHING_REVISION, "lab": LABEL_CONVENTION_VERSION, "max_bars": max_bars, "gate": CS.GATE_VERSION}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def build_gate_pairs(diag: pd.DataFrame, event_labels: pd.DataFrame, control_labels: pd.DataFrame, censor_label: str = CENSOR_LABEL) -> pd.DataFrame:
    """Join the diagnostic rows with the label AVAILABILITY (censored = label is None) of event and control. Only the censoring flag is taken from the labels."""
    el = event_labels[["event_id", censor_label]].rename(columns={"event_id": "control_of", censor_label: "_ev_y"})
    cl = control_labels[["event_id", censor_label]].rename(columns={censor_label: "_c_y"})
    df = diag.merge(el, on="control_of", how="left", validate="many_to_one").merge(cl, on="event_id", how="left", validate="one_to_one")
    df["ev_censored"] = df["_ev_y"].isna()
    df["c_censored"] = df["_c_y"].isna()
    return df.drop(columns=["_ev_y", "_c_y"])


def gate_for_dir(mdir: Path, sub: Path, events: pd.DataFrame | None = None) -> dict[str, Any]:
    """Recompute the balance gate of one control directory from the files (diag + event counts + label availability)."""
    ev = events if events is not None else pd.read_parquet(mdir / BF.EVENT_FILES["events"], columns=["event_id", "partition"])
    n_by_part = {str(k): int(v) for k, v in ev["partition"].value_counts().items()}
    diag = pd.read_parquet(sub / DIAG_FILE)
    pairs = build_gate_pairs(diag, pd.read_parquet(mdir / BF.EVENT_FILES["labels"], columns=["event_id", CENSOR_LABEL]),
                             pd.read_parquet(sub / BF.CONTROL_FILES["labels"], columns=["event_id", CENSOR_LABEL])) if len(diag) else diag.assign(ev_censored=pd.Series(dtype=bool), c_censored=pd.Series(dtype=bool))
    gate = CS.balance_gate(pairs, n_by_part)
    # informational (not gated): censored shares of the other first-passage labels, events vs controls over the matched pairs
    if len(diag):
        info = {}
        el = pd.read_parquet(mdir / BF.EVENT_FILES["labels"], columns=["event_id", *CENSOR_LABELS]).set_index("event_id")
        cl = pd.read_parquet(sub / BF.CONTROL_FILES["labels"], columns=["event_id", *CENSOR_LABELS]).set_index("event_id")
        for lab in CENSOR_LABELS:
            e = el[lab].reindex(diag["control_of"]).isna().mean()
            c = cl[lab].reindex(diag["event_id"]).isna().mean()
            info[lab] = {"events": float(e), "controls": float(c), "diff": float(c - e)}
        gate["censored_share_all_labels_matched_pairs"] = info
    gate["n_events_in_purged_or_embargo_tag"] = int(sum(v for k, v in n_by_part.items() if k not in CS.GATE_PARTITIONS))
    return gate


def _write_set(
    mdir: Path, subname: str, *, bars, cfg, market: str, events: pd.DataFrame, cs, cov: dict[str, np.ndarray], part: np.ndarray, spec: CS.SameTimeSpec, seed: int, fp: str,
    evm: dict[str, Any], match_spec_dict: dict[str, Any], opp: np.ndarray, eligible: np.ndarray, max_label_bars: int, chunk_rows: int, code: dict[str, Any], regular: bool, t0: float,
) -> dict[str, Any]:
    sub = mdir / subname
    shutil.rmtree(sub, ignore_errors=True)
    parts_dir = sub / "_parts"
    parts_dir.mkdir(parents=True)
    run_id = fp[:12]
    items: dict[int, list[BF._Item]] = defaultdict(list)
    for pos, cidx in zip(cs.event_pos.tolist(), cs.control_idx.tolist(), strict=True):
        e = events.iloc[pos]
        fam, var, d = e["family"], e["variant"], int(e["direction"])
        items[int(cidx)].append(BF._Item("control", int(cidx), d, float(e["risk"]), fam, var, event_id_for(market, bars.decision_ts_ns(int(cidx)), fam, var, d), str(e["event_id"]), None))
    all_ids = [it.event_id for lst in items.values() for it in lst]
    if len(set(all_ids)) != len(all_ids):
        raise RuntimeError("duplicate event ids among controls")
    plan, _ = BF.split_plan_for(bars.ts_ns)
    embargo_s = float(max_label_bars * bars.bar_seconds)
    _n_rows, t_obs = BF._observe_pass(bars, cfg, items, run_id=run_id, plan=plan, embargo_s=embargo_s, parts_dir=parts_dir, chunk_rows=chunk_rows, max_label_bars=max_label_bars)
    counts = BF._assemble_files(sub, parts_dir, BF.CONTROL_FILES)
    shutil.rmtree(parts_dir)
    # diagnostic file (one row per control, same order as the pair list)
    ev_idx = events["decision_idx"].to_numpy(np.int64)
    rows = []
    for pos, cidx, x, cpart in zip(cs.event_pos.tolist(), cs.control_idx.tolist(), cs.extra, cs.control_partition, strict=True):
        e = events.iloc[pos]
        i = int(ev_idx[pos])
        d = int(e["direction"])
        fam, var = e["family"], e["variant"]
        rows.append({
            "event_id": event_id_for(market, bars.decision_ts_ns(int(cidx)), fam, var, d), "control_of": str(e["event_id"]), "control_set": x["control_set"], "partition": str(e["partition"]),
            "control_partition": cpart, "day_offset": int(x["day_offset"]), "dist_to_opportunity_bars": int(x["dist_to_opportunity_bars"]), "event_decision_idx": i, "control_decision_idx": int(cidx),
            "ev_session": str(cov["session"][i]), "ev_minute": float(cov["minute"][i]), "ev_atr_pct": float(cov["atr_pct"][i]), "ev_spread_pct": float(cov["spread_pct"][i]),
            "c_session": str(cov["session"][cidx]), "c_minute": float(cov["minute"][cidx]), "c_atr_pct": float(cov["atr_pct"][cidx]), "c_spread_pct": float(cov["spread_pct"][cidx]),
        })
    cols = ["event_id", "control_of", "control_set", "partition", "control_partition", "day_offset", "dist_to_opportunity_bars", "event_decision_idx", "control_decision_idx",
            "ev_session", "ev_minute", "ev_atr_pct", "ev_spread_pct", "c_session", "c_minute", "c_atr_pct", "c_spread_pct"]
    pd.DataFrame(rows, columns=cols).to_parquet(sub / DIAG_FILE, index=False)
    gate = gate_for_dir(mdir, sub, events=events.assign(partition=events["partition"]))
    cpart = pd.read_parquet(sub / BF.CONTROL_FILES["events"], columns=["control_of", "partition"]) if counts.get("events") else pd.DataFrame({"control_of": [], "partition": []})
    joined = cpart.merge(events[["event_id", "partition"]].rename(columns={"event_id": "control_of", "partition": "event_partition"}), on="control_of", how="left")
    mismatch = int((joined["partition"] != joined["event_partition"]).sum()) if len(joined) else 0
    matched_pos = set(cs.event_pos.tolist())
    by_fam: dict[str, dict[str, int]] = {}
    for pos, fam in enumerate(events["family"].tolist()):
        b = by_fam.setdefault(str(fam), {"n_events": 0, "n_matched": 0})
        b["n_events"] += 1
        b["n_matched"] += int(pos in matched_pos)
    diag = pd.read_parquet(sub / DIAG_FILE)
    manifest = {
        "status": "COMPLETE", "step": "controls3", "control_set": spec.control_set, "fingerprint": fp, "run_id": run_id, "market": market, "events_fingerprint": evm["fingerprint"],
        "events_run_id": evm["run_id"], "controls_pipeline_version": CONTROLS3_PIPELINE_VERSION, "control_method_version": CS.CONTROL_METHOD_VERSION,
        "matching_revision": CS.CONTROL_MATCHING_REVISION, "partitioned_matching": regular, "match_spec": match_spec_dict, "seed": seed,
        "effective_seed": seed + (CS.B_SEED_OFFSET if spec.control_set == "b" else 0), "code": code, "observer_config_hash": cfg.config_hash(),
        "label_convention_version": LABEL_CONVENTION_VERSION, "n_controls": len(cs.control_idx), "match_report": BF._matching_report_dict(cs.report), "match_by_family": by_fam,
        "eligible_control_bars": int(eligible.sum()), "n_opportunity_bars": len(opp),
        "diagnostics": {"day_offset_abs_mean": float(diag["day_offset"].abs().mean()) if len(diag) else None, "day_offset_abs_max": int(diag["day_offset"].abs().max()) if len(diag) else None,
                        "dist_to_opportunity_bars": {k: float(v) for k, v in diag["dist_to_opportunity_bars"].describe(percentiles=[0.1, 0.5, 0.9]).items()} if len(diag) else {}},
        "balance_gate": gate, "market_status": gate["market_status"],
        "partitions": {"counts": BF._partition_summary(sub / BF.CONTROL_FILES["events"]), "controls_in_a_different_partition_than_their_event": mismatch, "plan": asdict(plan), "regular_plan": regular},
        "warmup": BF._warmup_counts(sub / BF.CONTROL_FILES["features"]), "rows": counts, "files": {**BF.CONTROL_FILES, "diag": DIAG_FILE}, "runtime_s": round(time.time() - t0, 1),
        "observer_pass_s": round(t_obs, 1), "peak_memory_mb": BF.peak_memory_mb(),
        "caveats": [
            "observer-controls-3: same Berlin bar clock time on the +-10 nearest trading days, same partition (full 48-bar horizon inside it), no exclusion radius; the control is never an opportunity bar",
            "a control inherits the risk distance R (price units), direction, family and variant of its event; it carries the id of its event in control_of (bootstrap: day block of the EVENT)",
            "market_status = descriptive_only when any partition with events does not pass the blocking balance gate (match rate, SMD, session share, censoring share)",
        ],
    }
    (sub / "controls_manifest.json").write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
    BF.log.info("controls3[%s] done market=%s n=%d match_rate=%.4f status=%s rows=%s", spec.control_set, market, len(cs.control_idx), cs.report.match_rate, gate["market_status"], counts)
    return manifest


def run_controls3_step(
    frame: pd.DataFrame, mspec: Any, market: str, out_dir: str | Path, *, seed: int = 0, force: bool = False, spec: CS.SameTimeSpec | None = None, with_b: bool = True,
    chunk_rows: int = BF.DEFAULT_CHUNK_ROWS, max_label_bars: int = DEFAULT_MAX_BARS, code: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Controls-3 selection (set A, optionally the disjoint A/A set B) + control features/labels + balance gate. Needs a COMPLETE events step of the same data; only READS it."""
    t0 = time.time()
    mdir = Path(out_dir) / market
    spec = spec or CS.SameTimeSpec()
    code = code or BF.code_identity()
    mp = mdir / "manifest.json"
    if not mp.is_file():
        raise FileNotFoundError(f"events step of {market} not complete (no manifest.json); run it first")
    evm = json.loads(mp.read_text(encoding="utf-8"))
    if evm.get("status") != "COMPLETE":
        raise RuntimeError("events step is not COMPLETE")
    assert_no_forward_holdout(frame)
    if BF.frame_fingerprint(frame) != evm["data"]["frame_fingerprint"]:
        raise ValueError("the frame differs from the one the events step used (frame fingerprint mismatch)")
    bars = BF.build_bars(frame, mspec, market)
    guard_dev_only(bars.ts_ns)
    cfg = BF.observer_config_for(mspec)
    fp = controls3_fingerprint(evm["fingerprint"], seed, spec, with_b, max_label_bars)
    if not force:
        old = BF._complete(mdir / SUBDIRS["a"], "controls_manifest.json", BF.CONTROL_FILES, fp)
        if old is not None and (not with_b or BF._complete(mdir / SUBDIRS["b"], "controls_manifest.json", BF.CONTROL_FILES, fp) is not None):
            old["status_this_call"] = "SKIPPED_COMPLETE"
            return old
    (mdir / SUBDIRS["a"]).mkdir(exist_ok=True)
    fh = BF._attach_log(mdir)
    try:
        BF.log.info("controls3 step start market=%s seed=%s events_fp=%s method=%s", market, seed, evm["fingerprint"][:12], CS.CONTROL_METHOD_VERSION)
        events = pd.read_parquet(mdir / BF.EVENT_FILES["events"], columns=["event_id", "decision_idx", "direction", "risk", "family", "variant", "partition"])
        events["variant"] = events["variant"].astype(object).where(events["variant"].notna(), None)
        opp = pd.read_parquet(mdir / "opportunity_bars.parquet")["decision_idx"].to_numpy(np.int64)
        n = len(bars)
        eligible = np.arange(n) >= int(evm["data"]["eval_from_index"])
        if evm.get("limit") is not None and len(events):
            eligible &= np.arange(n) <= int(events["decision_idx"].max()) + BF.CONTROL_TAIL_BARS
        ev_idx = events["decision_idx"].to_numpy(np.int64)
        _plan, regular = BF.split_plan_for(bars.ts_ns)
        if not regular:
            raise ValueError("observer-controls-3 needs the regular train/validation/OOS plan (>= 4 trading days up to the core fit end)")
        part = bar_partitions(bars, horizon_bars=spec.horizon_bars)
        cov = bar_covariates(bars, rank_mode="partition", partition=part)
        kw = {"eligible": eligible, "partition": part, "exclude_idx": opp}
        cs_a = CS.match_controls_sametime(bars, ev_idx, spec=spec, seed=seed, **kw)
        BF.log.info("controls3 A: %d controls match_rate=%.4f", len(cs_a.control_idx), cs_a.report.match_rate)
        common = {"bars": bars, "cfg": cfg, "market": market, "events": events, "cov": cov, "part": part, "seed": seed, "fp": fp, "evm": evm, "opp": opp, "eligible": eligible,
                  "max_label_bars": max_label_bars, "chunk_rows": chunk_rows, "code": code, "regular": regular, "t0": t0}
        out = _write_set(mdir, SUBDIRS["a"], cs=cs_a, spec=spec, match_spec_dict=asdict(spec), **common)
        if with_b:
            from dataclasses import replace

            spec_b = replace(spec, control_set="b")
            cs_b = CS.match_controls_sametime(bars, ev_idx, spec=spec_b, seed=seed, avoid_idx=cs_a.control_idx, **kw)
            if set(cs_a.control_idx.tolist()) & set(cs_b.control_idx.tolist()):
                raise RuntimeError("controls_b is not disjoint from controls_a")
            BF.log.info("controls3 B: %d controls match_rate=%.4f", len(cs_b.control_idx), cs_b.report.match_rate)
            out["controls_b"] = _write_set(mdir, SUBDIRS["b"], cs=cs_b, spec=spec_b, match_spec_dict=asdict(spec_b), **common)
        out["status_this_call"] = "BUILT"
        return out
    finally:
        BF.log.removeHandler(fh)
        fh.close()
