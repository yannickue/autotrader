# ruff: noqa: E501
"""Single-feature enrichment and incremental ablation over an observer table (OFFLINE; OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

Input: a pandas table with ``event_id``, ``is_control``, ``control_of`` (the event a control matches), ``decision_ts_ns``, a local-day
column (default ``m_local_day``; the independent block), optional ``m_structure_event_id`` (event cluster), optional ``warmup_ok``,
feature columns ``f_<group>__<name>`` and binary label columns ``y_*`` (1 / 0 / None = censored, excluded from the rates).

Question answered per cell: does P(label | event in cell) differ from P(label | the controls MATCHED to exactly those events)? Controls are
selected through ``control_of`` so the matching is preserved inside every cell. Cells are predeclared (quantile terciles by default, or the
categories), edges come from the event rows of the table the caller passes (pass the TRAIN partition). Every (feature, cell, label) is
registered in the ``HypothesisRegistry`` BEFORE anything is evaluated; Holm / BH are applied with the full registered count.

Strict separation (asserted, not assumed): feature columns and label columns are disjoint (``schema.assert_disjoint``) and every feature
timestamp column (``f_*_ts_ns``) is <= that row's ``decision_ts_ns`` (``CausalityError`` otherwise).

``incremental_ablation`` = BASE FAMILY + group: for a caller-given base mask (e.g. one setup family) and a caller-listed set of feature
groups it compares ``delta(base AND cell)`` with ``delta(base)`` (paired day-block bootstrap of the DIFFERENCE). No brute-force
combinations: only the groups the caller lists, one feature cell at a time.

Warm-up: rows with ``warmup_ok`` False (the feature was still inside its warm-up) are excluded and COUNTED in the report; controls of an
excluded event are dropped with it (counted as orphans).
"""

from __future__ import annotations

import zlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from coverage_analysis.observer_lab import LAB_VERSION
from coverage_analysis.observer_lab import stats as ST
from coverage_analysis.observer_lab.controls import CONTROL_METHOD_VERSION
from coverage_analysis.observer_lab.labels import LABEL_CONVENTION_VERSION
from market_observer.schema import (
    FEATURE_PREFIX,
    GROUP_VERSIONS,
    OBSERVER_VERSION,
    SCHEMA_VERSION,
    TS_SUFFIX,
    CausalityError,
    assert_disjoint,
)


@dataclass(frozen=True)
class EnrichmentConfig:
    n_quantiles: int = 3
    max_categories: int = 8
    min_evidence: ST.MinEvidence = ST.DEFAULT_MIN_EVIDENCE
    B: int = 1000
    seed: int = 0
    alpha: float = ST.DEFAULT_ALPHA
    adjust: str = "holm"  # or "bh"
    day_col: str = "m_local_day"
    cluster_col: str = "m_structure_event_id"
    warmup_col: str = "warmup_ok"


@dataclass(frozen=True)
class EnrichmentReport:
    results: list[ST.EnrichmentResult]
    n_hypotheses: int  # size of the registry AFTER this run
    n_rows: int
    n_events_used: int
    n_controls_used: int
    n_excluded_warmup_events: int
    n_excluded_warmup_controls: int
    n_orphan_controls_dropped: int
    warmup_column_present: bool
    adjust_method: str
    versions: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------- validation / preparation
def _exceeds(col: pd.Series, decision: pd.Series) -> np.ndarray:
    if col.dtype == object:
        return np.array([v is not None and v == v and int(v) > int(d) for v, d in zip(col, decision, strict=True)], dtype=bool)
    ok = col.notna().to_numpy()
    return ok & (col.to_numpy(dtype="float64", na_value=np.nan) > decision.to_numpy(dtype="float64")) if col.dtype.kind == "f" else ok & (col.to_numpy() > decision.to_numpy())


def _prepare(df: pd.DataFrame, feature_cols: Sequence[str], label_cols: Sequence[str], cfg: EnrichmentConfig):
    for c in ("event_id", "is_control", "control_of", "decision_ts_ns", cfg.day_col):
        if c not in df.columns:
            raise ValueError(f"missing required column {c!r}")
    assert_disjoint(set(feature_cols), set(label_cols))
    missing = [c for c in (*feature_cols, *label_cols) if c not in df.columns]
    if missing:
        raise ValueError(f"columns not in the table: {missing[:5]}")
    for c in df.columns:  # causality over ALL timestamp feature columns of the table, not only the listed ones
        if c.startswith(FEATURE_PREFIX) and c.endswith(TS_SUFFIX):
            bad = _exceeds(df[c], df["decision_ts_ns"])
            if bad.any():
                raise CausalityError(f"{c} is later than decision_ts_ns in {int(bad.sum())} row(s) (first: {df.loc[bad, 'event_id'].iloc[0]})")
    work = df.reset_index(drop=True).copy()
    for lab in label_cols:
        work[lab] = pd.to_numeric(work[lab], errors="coerce")
        vals = set(work[lab].dropna().unique().tolist())
        if not vals <= {0.0, 1.0}:
            raise ValueError(f"label {lab} is not a binary outcome (values {sorted(vals)[:5]}); threshold it explicitly first")
    is_ctl = work["is_control"].astype(bool)
    events, controls = work[~is_ctl].copy(), work[is_ctl].copy()
    if controls["control_of"].isna().any():
        raise ValueError("a control row without control_of")
    warm = cfg.warmup_col in work.columns
    n_ev_ex = n_ctl_ex = n_orphan = 0
    if warm:
        ok_ev = events[cfg.warmup_col].fillna(False).astype(bool)
        dropped_ids = set(events.loc[~ok_ev, "event_id"])
        n_ev_ex = int((~ok_ev).sum())
        events = events[ok_ev]
        orphan = controls["control_of"].isin(dropped_ids)
        n_orphan = int(orphan.sum())
        controls = controls[~orphan]
        ok_ct = controls[cfg.warmup_col].fillna(False).astype(bool)
        n_ctl_ex = int((~ok_ct).sum())
        controls = controls[ok_ct]
    unknown = ~controls["control_of"].isin(set(work.loc[~is_ctl, "event_id"]))
    if unknown.any():
        raise ValueError(f"{int(unknown.sum())} control(s) refer to an event that is not in the table")
    prep = {"n_rows": len(work), "warm": warm, "n_ev_ex": n_ev_ex, "n_ctl_ex": n_ctl_ex, "n_orphan": n_orphan}
    return events.reset_index(drop=True), controls.reset_index(drop=True), prep


def _cells(col: pd.Series, cfg: EnrichmentConfig) -> list[tuple[str, np.ndarray]]:
    """Predeclared cells of one feature over the EVENT rows: [(cell label, bool mask)]."""
    missing = col.isna().to_numpy()
    cells: list[tuple[str, np.ndarray]] = []
    nunique = col.dropna().nunique()
    numeric = pd.api.types.is_numeric_dtype(col) and not pd.api.types.is_bool_dtype(col)
    if numeric and nunique > cfg.max_categories:
        x = col.to_numpy(dtype="float64", na_value=np.nan)
        edges = np.unique(np.nanquantile(x, np.linspace(0, 1, cfg.n_quantiles + 1)[1:-1]))
        q = np.searchsorted(edges, x, side="left")  # value <= edge goes to the lower cell
        for k in range(len(edges) + 1):
            cells.append((f"Q{k + 1}/{len(edges) + 1}", (q == k) & ~missing))
    else:
        if nunique > cfg.max_categories:
            raise ValueError(f"{col.name}: {nunique} categories > max_categories={cfg.max_categories}; predeclare coarser cells")
        for v in sorted(col.dropna().unique().tolist(), key=str):
            cells.append((f"={v}", (col == v).to_numpy() & ~missing))
    if missing.any():
        cells.append(("MISSING", missing))
    return cells


def _group_of(feature: str) -> str | None:
    return feature[len(FEATURE_PREFIX) :].split("__")[0] if feature.startswith(FEATURE_PREFIX) and "__" in feature else None


def _seed_for(cfg: EnrichmentConfig, name: str) -> int:
    return (cfg.seed + zlib.crc32(name.encode())) & 0x7FFFFFFF


def _versions(feature: str) -> dict[str, str]:
    g = _group_of(feature)
    return {g: GROUP_VERSIONS[g]} if g in GROUP_VERSIONS else {}


def _report_versions() -> dict[str, str]:
    return {"observer": OBSERVER_VERSION, "schema": SCHEMA_VERSION, "lab": LAB_VERSION, "labels": LABEL_CONVENTION_VERSION, "controls": CONTROL_METHOD_VERSION}


def _finalise(raw: list[tuple[ST.EnrichmentResult, float | None]], registry: ST.HypothesisRegistry, cfg: EnrichmentConfig) -> list[ST.EnrichmentResult]:
    for res, p in raw:
        registry.record(res.hypothesis, p)  # type: ignore[arg-type]
    adj = registry.adjust(cfg.adjust)
    out = []
    for res, _ in raw:
        a = adj.get(res.hypothesis)  # type: ignore[arg-type]
        out.append(replace(res, adjusted_p=a, status=ST.final_status(res.status, a, res.ci_low, res.ci_high, cfg.alpha)))
    return out


def _overall_delta(y_event: np.ndarray, y_control: np.ndarray) -> float:
    """Context only: delta of ALL used events vs ALL used controls for the label (a cell must differ from THIS to carry information)."""
    ye, yc = y_event[~np.isnan(y_event)], y_control[~np.isnan(y_control)]
    return float(ye.mean() - yc.mean()) if len(ye) and len(yc) else float("nan")


def _clusters(events: pd.DataFrame, cfg: EnrichmentConfig, mask: np.ndarray) -> np.ndarray | None:
    return events[cfg.cluster_col].to_numpy()[mask] if cfg.cluster_col in events.columns and events[cfg.cluster_col].notna().any() else None


# ---------------------------------------------------------------------------------------------- single-feature enrichment
def single_feature_enrichment(
    df: pd.DataFrame, feature_cols: Sequence[str], label_cols: Sequence[str], registry: ST.HypothesisRegistry, cfg: EnrichmentConfig | None = None,
) -> EnrichmentReport:
    cfg = cfg or EnrichmentConfig()
    events, controls, prep = _prepare(df, feature_cols, label_cols, cfg)
    feats = [f for f in feature_cols if not f.endswith(TS_SUFFIX)]
    plan = [(lab, f, cell, mask) for lab in label_cols for f in feats for cell, mask in _cells(events[f], cfg)]
    names = [f"single|{lab}|{f}|{cell}" for lab, f, cell, _ in plan]
    registry.register_many(names)  # all hypotheses are registered BEFORE any is evaluated
    raw: list[tuple[ST.EnrichmentResult, float | None]] = []
    overall = {lab: _overall_delta(events[lab].to_numpy(), controls[lab].to_numpy()) for lab in label_cols}
    for (lab, f, cell, mask), name in zip(plan, names, strict=True):
        ids = set(events.loc[mask, "event_id"])
        cm = controls["control_of"].isin(ids).to_numpy()
        est = ST.block_bootstrap_delta(
            events[lab].to_numpy()[mask], events[cfg.day_col].to_numpy()[mask], controls[lab].to_numpy()[cm], controls[cfg.day_col].to_numpy()[cm],
            B=cfg.B, seed=_seed_for(cfg, name), alpha=cfg.alpha, cluster_event=_clusters(events, cfg, mask),
        )
        status = ST.evidence_status(n_event=est.n_event, n_control=est.n_control, n_blocks=est.n_blocks, n_clusters=est.n_clusters, min_evidence=cfg.min_evidence)
        res = ST.EnrichmentResult(
            feature=f, group=_group_of(f), label=lab, cell=cell, n_event=est.n_event, n_control=est.n_control, p_event=est.p_event, p_control=est.p_control,
            delta=est.delta, ci_low=est.ci_low, ci_high=est.ci_high, n_blocks=est.n_blocks, adjusted_p=None, status=status, versions=_versions(f),
            n_clusters=est.n_clusters, p_boot=est.p_boot, kind="single", base_delta=overall[lab], hypothesis=name,
        )
        raw.append((res, est.p_boot if status == ST.OK else None))
    return _wrap(_finalise(raw, registry, cfg), registry, events, controls, prep, cfg)


# ---------------------------------------------------------------------------------------------- incremental ablation
def incremental_ablation(
    df: pd.DataFrame, label_cols: Sequence[str], base_mask: pd.Series | np.ndarray, groups: Mapping[str, Sequence[str]],
    registry: ST.HypothesisRegistry, cfg: EnrichmentConfig | None = None,
) -> EnrichmentReport:
    """BASE FAMILY + group. ``base_mask`` is aligned to ``df`` (rows of events; controls follow their event through ``control_of``).
    Result.delta = delta(base AND cell) - delta(base); ``base_delta`` is the base delta; p/CI from the paired day-block bootstrap."""
    cfg = cfg or EnrichmentConfig()
    all_feats = [f for fs in groups.values() for f in fs]
    bm = pd.Series(np.asarray(base_mask, dtype=bool), index=df.index)
    work = df.copy()
    work["_base"] = bm.to_numpy()
    events, controls, prep = _prepare(work, all_feats, label_cols, cfg)
    base_ev = events["_base"].to_numpy(dtype=bool)
    base_ids = set(events.loc[base_ev, "event_id"])
    cm_base = controls["control_of"].isin(base_ids).to_numpy()
    plan = []
    for lab in label_cols:
        for grp, fs in groups.items():
            for f in fs:
                sub = events.loc[base_ev, f].reset_index(drop=True)
                for cell, m in _cells(sub, cfg):
                    full = np.zeros(len(events), dtype=bool)
                    full[np.flatnonzero(base_ev)[m]] = True
                    plan.append((lab, grp, f, cell, full))
    names = [f"incr|{lab}|{grp}|{f}|{cell}" for lab, grp, f, cell, _ in plan]
    registry.register_many(names)
    raw: list[tuple[ST.EnrichmentResult, float | None]] = []
    for (lab, grp, f, cell, mask), name in zip(plan, names, strict=True):
        ids = set(events.loc[mask, "event_id"])
        cm = controls["control_of"].isin(ids).to_numpy()
        arm_a = (events[lab].to_numpy()[mask], events[cfg.day_col].to_numpy()[mask], controls[lab].to_numpy()[cm], controls[cfg.day_col].to_numpy()[cm])
        arm_b = (events[lab].to_numpy()[base_ev], events[cfg.day_col].to_numpy()[base_ev], controls[lab].to_numpy()[cm_base], controls[cfg.day_col].to_numpy()[cm_base])
        est = ST.block_bootstrap_contrast(arm_a, arm_b, B=cfg.B, seed=_seed_for(cfg, name), alpha=cfg.alpha)
        cl = _clusters(events, cfg, mask)
        n_cl = len(set(cl[~np.isnan(arm_a[0])].tolist())) if cl is not None else None
        status = ST.evidence_status(n_event=est.n_event, n_control=est.n_control, n_blocks=est.n_blocks, n_clusters=n_cl, min_evidence=cfg.min_evidence)
        base_delta = _overall_delta(arm_b[0], arm_b[2])
        res = ST.EnrichmentResult(
            feature=f, group=grp, label=lab, cell=f"base AND {cell}", n_event=est.n_event, n_control=est.n_control, p_event=est.p_event,
            p_control=est.p_control, delta=est.delta, ci_low=est.ci_low, ci_high=est.ci_high, n_blocks=est.n_blocks, adjusted_p=None, status=status,
            versions=_versions(f), n_clusters=n_cl, p_boot=est.p_boot, kind="incremental", base_delta=base_delta, hypothesis=name,
        )
        raw.append((res, est.p_boot if status == ST.OK else None))
    return _wrap(_finalise(raw, registry, cfg), registry, events, controls, prep, cfg)


def summarise_by_group(results: Sequence[ST.EnrichmentResult]) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for r in results:
        out.setdefault(r.group or "?", Counter())[r.status] += 1
    return out


def _wrap(results, registry, events, controls, prep, cfg) -> EnrichmentReport:
    return EnrichmentReport(
        results=results, n_hypotheses=registry.n_hypotheses, n_rows=prep["n_rows"], n_events_used=len(events), n_controls_used=len(controls),
        n_excluded_warmup_events=prep["n_ev_ex"], n_excluded_warmup_controls=prep["n_ctl_ex"], n_orphan_controls_dropped=prep["n_orphan"],
        warmup_column_present=prep["warm"], adjust_method=cfg.adjust, versions=_report_versions(),
    )
