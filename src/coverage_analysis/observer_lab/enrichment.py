# ruff: noqa: E501
"""Single-feature enrichment and incremental ablation over an observer table (OFFLINE; OBSERVATION_ONLY / NOT_ALPHA_VALIDATED).

Input: a pandas table with ``event_id``, ``is_control``, ``control_of`` (the event a control matches), ``decision_ts_ns``, a local-day
column (default ``m_local_day``; the independent block), a REQUIRED ``partition`` column (TRAIN / VALIDATION / OOS / FORWARD / PURGED /
EMBARGO, as produced by ``splits.assign_partitions``), optional ``m_structure_event_id`` (event cluster), optional ``warmup_ok``, feature
columns ``f_<group>__<name>`` and binary label columns ``y_*`` (1 / 0 / None = censored, excluded from the rates).

PARTITION / FORWARD-HOLDOUT GUARDS (called, not just tested): every entry point REQUIRES ``purpose`` in ``fit | validate | oos_test |
forward_monitor``. ``splits.assert_partition_use`` is applied to the partitions of the event AND control rows (fit -> TRAIN only, validate ->
VALIDATION only, oos_test -> OOS only, forward_monitor -> FORWARD only; FORWARD is refused for every other purpose) and
``splits.guard_dev_only`` to the decision timestamps of event AND control rows for every non-forward purpose (nothing after the dev end).
``forward_monitor`` additionally refuses rows before the forward start. PURGED / EMBARGO / UNASSIGNED rows are dropped and COUNTED. A control
whose partition differs from its event's partition is an error (controls are matched within the partition).

CELLS are frozen on the TRAIN partition: ``CellDef`` (quantile edges / categories) is fitted from the TRAIN event rows ONLY (``purpose='fit'``)
and returned in the report; validation / OOS / forward calls must pass those ``cell_defs`` and apply them unchanged.

TWO CONTRASTS per feature cell (kept apart on purpose, ``EnrichmentResult.contrast``):

* ``event_vs_same_cell_controls`` (PRIMARY single-feature test): P(y | event with feature in the cell) - P(y | control whose OWN feature value is in the
  same cell). Both sides sit in the same cell, so a difference says what the feature adds beyond "event vs control";
* ``lift_within_cell`` (the former test): P(y | events in the cell) - P(y | the controls MATCHED to exactly those events). The controls' own feature values
  are ignored, so this is the event lift inside the cell (it will be non-zero in every cell if events differ from controls at all) - NOT what the
  feature itself tells you. Compare it with ``base_delta`` (the overall lift).

MULTIPLICITY: every hypothesis is registered in the ``HypothesisRegistry`` BEFORE anything is evaluated, inside a FAMILY (default: one family per
contrast x purpose x label x feature group). Holm / BH are applied within the family (``m`` = its size) - the registry also reports the number of
hypotheses EVER tested (persistent registry), and the registry-wide adjusted p for reference. The exploratory 83 x 4 cells table is therefore not
one giant family, but it is also not significance-ready: use ``incremental_ablation(..., contrasts=[PredeclaredContrast(...)])`` with a small list
of contrasts written down BEFORE looking at the data (one family per label x group). ``B`` is chosen automatically (``stats.choose_B``: B >= 20 m /
alpha, capped); families in which the smallest attainable p cannot pass Holm are marked ``power_limited`` / ``POWER_LIMITED`` (never a silent
"not significant").

Strict separation (asserted, not assumed): feature columns and label columns are disjoint (``schema.assert_disjoint``) and every feature
timestamp column (``f_*_ts_ns``) is <= that row's ``decision_ts_ns`` (``CausalityError`` otherwise).

``incremental_ablation`` = BASE FAMILY + group: for a caller-given base mask (e.g. one setup family) and a caller-listed set of feature
groups it compares ``delta(base AND cell)`` with ``delta(base)`` (paired day-block bootstrap of the DIFFERENCE).

Warm-up: rows with ``warmup_ok`` False (the feature was still inside its warm-up) are excluded and COUNTED in the report; controls of an
excluded event are dropped with it (counted as orphans).
"""

from __future__ import annotations

import json
import zlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from alpha.common.market_data import FORWARD_HOLDOUT_START, ForwardHoldoutError
from coverage_analysis.observer_lab import LAB_VERSION
from coverage_analysis.observer_lab import splits as SP
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

PURPOSES = ("fit", "validate", "oos_test", "forward_monitor")
CONTRAST_SAME_CELL = "event_vs_same_cell_controls"
CONTRAST_LIFT = "lift_within_cell"
UNUSABLE_PARTITIONS = (SP.PURGED, SP.EMBARGO, SP.UNASSIGNED)


@dataclass(frozen=True)
class EnrichmentConfig:
    n_quantiles: int = 3
    max_categories: int = 8
    min_evidence: ST.MinEvidence = ST.DEFAULT_MIN_EVIDENCE
    B: int | None = None  # None = automatic (stats.choose_B); an explicit value is honoured and checked for power
    B_min: int = 2000
    B_max: int = ST.DEFAULT_B_MAX
    seed: int = 0
    alpha: float = ST.DEFAULT_ALPHA
    adjust: str = "holm"  # or "bh"
    adjust_scope: str = "family"  # "family" (predeclared small families, default) | "registry" (everything ever registered)
    p_method: str = "bootstrap"  # "bootstrap" | "normal" (block-SE normal approximation, unlimited resolution)
    family_by: str = "label_group"  # "label_group" | "run"
    day_col: str = "m_local_day"
    cluster_col: str = "m_structure_event_id"
    warmup_col: str = "warmup_ok"
    partition_col: str = "partition"
    # ``observer-stats-1`` (default, unchanged lab behaviour): a control is blocked by its OWN day column, the evidence check counts the union of blocks.
    # ``observer-stats-2``: a control is blocked by the day (or week) of its EVENT (``control_of``), the evidence check counts blocks per arm.
    stats_version: str = ST.STATS_LEGACY
    block_unit: str = "day"  # "day" | "week" (ISO week of the block day; ``day_col`` must then hold integer day ordinals)


@dataclass(frozen=True)
class PredeclaredContrast:
    """One contrast written down BEFORE looking at the data: the cell ``cell`` (label as produced by ``CellDef``: ``=A``, ``Q3/3``, ``MISSING``) of ``feature``."""

    group: str
    feature: str
    cell: str


@dataclass(frozen=True)
class CellDef:
    """Frozen cell definition of one feature, fitted on TRAIN event rows only and applied unchanged elsewhere."""

    feature: str
    kind: str  # "quantile" | "category"
    edges: tuple[float, ...] = ()
    categories: tuple = ()
    has_missing: bool = False

    def labels(self) -> list[str]:
        base = [f"Q{k + 1}/{len(self.edges) + 1}" for k in range(len(self.edges) + 1)] if self.kind == "quantile" else [f"={v}" for v in self.categories]
        return base + (["MISSING"] if self.has_missing else [])


@dataclass(frozen=True)
class EnrichmentReport:
    results: list[ST.EnrichmentResult]
    n_hypotheses: int  # size of the registry AFTER this run (== hypotheses ever tested when the registry is persistent)
    n_rows: int
    n_events_used: int
    n_controls_used: int
    n_excluded_warmup_events: int
    n_excluded_warmup_controls: int
    n_orphan_controls_dropped: int
    warmup_column_present: bool
    adjust_method: str
    versions: Mapping[str, str] = field(default_factory=dict)
    purpose: str = "fit"
    n_hypotheses_ever: int = 0
    n_families: int = 0
    B: int = 0
    p_method: str = "bootstrap"
    n_excluded_partition_events: int = 0
    n_excluded_partition_controls: int = 0
    cell_defs: Mapping[str, CellDef] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    power_limited_families: tuple[str, ...] = ()


# ---------------------------------------------------------------------------------------------- validation / preparation
def _exceeds(col: pd.Series, decision: pd.Series) -> np.ndarray:
    if col.dtype == object:
        return np.array([v is not None and v == v and int(v) > int(d) for v, d in zip(col, decision, strict=True)], dtype=bool)
    ok = col.notna().to_numpy()
    return ok & (col.to_numpy(dtype="float64", na_value=np.nan) > decision.to_numpy(dtype="float64")) if col.dtype.kind == "f" else ok & (col.to_numpy() > decision.to_numpy())


def _check_purpose(purpose: str | None) -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"purpose is required and must be one of {PURPOSES}, got {purpose!r}")
    return purpose


def _prepare(df: pd.DataFrame, feature_cols: Sequence[str], label_cols: Sequence[str], cfg: EnrichmentConfig, purpose: str | None):
    purpose = _check_purpose(purpose)
    for c in ("event_id", "is_control", "control_of", "decision_ts_ns", cfg.day_col):
        if c not in df.columns:
            raise ValueError(f"missing required column {c!r}")
    if cfg.partition_col not in df.columns:
        raise ValueError(f"missing required column {cfg.partition_col!r}: the table must carry the split partition of every row (splits.assign_partitions)")
    if df[cfg.partition_col].isna().any():
        raise ValueError(f"{cfg.partition_col}: every row needs a partition label")
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
    # ---- partition rules: purged/embargo rows out (counted), controls must sit in their event's partition, then the forward-holdout guards
    pc = cfg.partition_col
    unusable = work[pc].isin(UNUSABLE_PARTITIONS).to_numpy()
    n_part_ev, n_part_ctl = int((unusable & ~is_ctl.to_numpy()).sum()), int((unusable & is_ctl.to_numpy()).sum())
    work = work[~unusable].reset_index(drop=True)
    is_ctl = work["is_control"].astype(bool)
    ev_part = dict(zip(work.loc[~is_ctl, "event_id"], work.loc[~is_ctl, pc], strict=True))
    ctl = work[is_ctl]
    if ctl["control_of"].isna().any():
        raise ValueError("a control row without control_of")
    cross = [(cid, p) for cid, p, e in zip(ctl["event_id"], ctl[pc], ctl["control_of"], strict=True) if e in ev_part and ev_part[e] != p]
    if cross:
        raise ValueError(f"{len(cross)} control(s) lie in a different partition than their event (first: {cross[0][0]}): controls must be matched within the partition")
    SP.assert_partition_use(set(work[pc].tolist()), purpose)  # events AND controls
    ts = work["decision_ts_ns"].to_numpy(dtype="int64")
    if purpose == "forward_monitor":
        if len(ts) and str(SP.berlin_dates(ts).min()) < FORWARD_HOLDOUT_START:
            raise ForwardHoldoutError(f"forward_monitor rows must start at {FORWARD_HOLDOUT_START}")
    else:
        SP.guard_dev_only(ts)  # events AND controls: nothing after the dev end
    events, controls = work[~is_ctl].copy(), work[is_ctl].copy()
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
    all_event_ids = set(df.loc[~df["is_control"].astype(bool), "event_id"])
    truly_unknown = ~controls["control_of"].isin(all_event_ids)
    if truly_unknown.any():
        raise ValueError(f"{int(truly_unknown.sum())} control(s) refer to an event that is not in the table")
    gone = ~controls["control_of"].isin(set(events["event_id"]))  # its event was removed as PURGED/EMBARGO (or by warm-up above): an orphan, counted
    if gone.any():
        controls = controls[~gone]
        n_orphan += int(gone.sum())
    prep = {"n_rows": len(df), "warm": warm, "n_ev_ex": n_ev_ex, "n_ctl_ex": n_ctl_ex, "n_orphan": n_orphan, "n_part_ev": n_part_ev, "n_part_ctl": n_part_ctl}
    return events.reset_index(drop=True), controls.reset_index(drop=True), prep


# ---------------------------------------------------------------------------------------------- cells (frozen on TRAIN)
def fit_cell_def(col: pd.Series, cfg: EnrichmentConfig) -> CellDef:
    """Predeclared cells of one feature from the (TRAIN) event rows: quantile terciles (default) for numeric features, the categories otherwise."""
    name = str(col.name)
    missing = bool(col.isna().any())
    nunique = col.dropna().nunique()
    numeric = pd.api.types.is_numeric_dtype(col) and not pd.api.types.is_bool_dtype(col)
    if numeric and nunique > cfg.max_categories:
        x = col.to_numpy(dtype="float64", na_value=np.nan)
        edges = np.unique(np.nanquantile(x, np.linspace(0, 1, cfg.n_quantiles + 1)[1:-1]))
        return CellDef(name, "quantile", tuple(float(e) for e in edges), (), missing)
    if nunique > cfg.max_categories:
        raise ValueError(f"{name}: {nunique} categories > max_categories={cfg.max_categories}; predeclare coarser cells")
    return CellDef(name, "category", (), tuple(sorted(col.dropna().unique().tolist(), key=str)), missing)


def apply_cell_def(col: pd.Series, d: CellDef) -> list[tuple[str, np.ndarray]]:
    """[(cell label, bool mask)] of ``col`` under the FROZEN definition (values outside every cell, e.g. an unseen category, fall in no cell)."""
    missing = col.isna().to_numpy()
    cells: list[tuple[str, np.ndarray]] = []
    if d.kind == "quantile":
        x = col.to_numpy(dtype="float64", na_value=np.nan)
        q = np.searchsorted(np.asarray(d.edges), x, side="left")  # value <= edge goes to the lower cell
        for k in range(len(d.edges) + 1):
            cells.append((f"Q{k + 1}/{len(d.edges) + 1}", (q == k) & ~missing))
    else:
        for v in d.categories:
            cells.append((f"={v}", (col == v).to_numpy() & ~missing))
    if d.has_missing:
        cells.append(("MISSING", missing))
    return cells


def _resolve_cell_defs(events: pd.DataFrame, feats: Sequence[str], cfg: EnrichmentConfig, purpose: str, cell_defs: Mapping[str, CellDef] | None) -> dict[str, CellDef]:
    if cell_defs is None:
        if purpose != "fit":
            raise ValueError(f"purpose={purpose!r} needs the frozen cell_defs fitted on TRAIN (EnrichmentReport.cell_defs of the fit run); edges are never refitted outside TRAIN")
        return {f: fit_cell_def(events[f], cfg) for f in feats}
    absent = [f for f in feats if f not in cell_defs]
    if absent:
        raise ValueError(f"cell_defs has no definition for {absent[:5]}")
    return {f: cell_defs[f] for f in feats}


def _group_of(feature: str) -> str | None:
    return feature[len(FEATURE_PREFIX) :].split("__")[0] if feature.startswith(FEATURE_PREFIX) and "__" in feature else None


def _seed_for(cfg: EnrichmentConfig, name: str) -> int:
    return (cfg.seed + zlib.crc32(name.encode())) & 0x7FFFFFFF


def _versions(feature: str) -> dict[str, str]:
    g = _group_of(feature)
    return {g: GROUP_VERSIONS[g]} if g in GROUP_VERSIONS else {}


def _report_versions(cfg: EnrichmentConfig | None = None) -> dict[str, str]:
    cfg = cfg or EnrichmentConfig()
    return {"observer": OBSERVER_VERSION, "schema": SCHEMA_VERSION, "lab": LAB_VERSION, "labels": LABEL_CONVENTION_VERSION, "controls": CONTROL_METHOD_VERSION,
            "stats": cfg.stats_version, "block_unit": cfg.block_unit}


def _block_arrays(events: pd.DataFrame, controls: pd.DataFrame, cfg: EnrichmentConfig) -> tuple[np.ndarray, np.ndarray]:
    """Independent-block id of every event row and every control row (aligned to ``events`` / ``controls``).

    observer-stats-1: each row uses its own ``day_col``. observer-stats-2: a control takes the block of its EVENT (``control_of``), because a control
    is a paired draw for that event and its own decision day may fall in another block (a block must contain both halves of every pair).
    ``block_unit='week'`` maps day ordinals to ISO weeks (Monday start; ordinal 0 = Thursday 1970-01-01)."""
    if cfg.stats_version not in (ST.STATS_LEGACY, ST.STATS_V2):
        raise ValueError(f"unknown stats_version {cfg.stats_version!r}")
    if cfg.block_unit not in ("day", "week"):
        raise ValueError(f"block_unit must be 'day' or 'week', got {cfg.block_unit!r}")
    ev_day = events[cfg.day_col].to_numpy()
    if cfg.stats_version == ST.STATS_V2:
        by_event = dict(zip(events["event_id"], ev_day, strict=True))
        ct_day = np.asarray([by_event[e] for e in controls["control_of"]], dtype=ev_day.dtype)
    else:
        ct_day = controls[cfg.day_col].to_numpy()
    if cfg.block_unit == "week":
        if ev_day.dtype.kind not in "iu" or ct_day.dtype.kind not in "iu":
            raise ValueError("block_unit='week' needs integer day ordinals in day_col")
        return (ev_day.astype("int64") + 3) // 7, (ct_day.astype("int64") + 3) // 7
    return ev_day, ct_day


def _family_name(cfg: EnrichmentConfig, kind: str, purpose: str, lab: str, grp: str | None) -> str:
    return f"{kind}|{purpose}|{lab}|{grp or '?'}" if cfg.family_by == "label_group" else f"{kind}|{purpose}|run"


def _declare(registry: ST.HypothesisRegistry, plan_families: Mapping[str, list[str]]) -> None:
    for fam, names in plan_families.items():
        registry.declare_family(fam, definition=json.dumps(sorted(names)))


def _evaluate_p(est: ST.DeltaEstimate, cfg: EnrichmentConfig) -> float:
    return est.p_boot if cfg.p_method == "bootstrap" else est.p_norm


def _finalise(
    raw: list[tuple[ST.EnrichmentResult, float | None]], registry: ST.HypothesisRegistry, cfg: EnrichmentConfig, choice: ST.BChoice,
) -> tuple[list[ST.EnrichmentResult], tuple[str, ...]]:
    B = choice.B
    for res, p in raw:
        registry.record(res.hypothesis, p)  # type: ignore[arg-type]
    registry.flush()
    adj_fam = registry.adjust(cfg.adjust, "family")
    adj_reg = registry.adjust(cfg.adjust, "registry")
    out = []
    limited: set[str] = set()
    for res, _ in raw:
        fam = registry.family_of(res.hypothesis)  # type: ignore[arg-type]
        m = registry.family_size(fam)
        m_eff = registry.n_hypotheses if cfg.adjust_scope == "registry" else m  # the multiplicity the chosen correction actually applies
        pl = cfg.p_method == "bootstrap" and ST.is_power_limited(m_eff, B, cfg.alpha)
        if pl:
            limited.add(fam)
        a = (adj_fam if cfg.adjust_scope == "family" else adj_reg).get(res.hypothesis)  # type: ignore[arg-type]
        out.append(replace(
            res, adjusted_p=a, adjusted_p_registry=adj_reg.get(res.hypothesis), status=ST.final_status(res.status, a, res.ci_low, res.ci_high, cfg.alpha, power_limited=pl),  # type: ignore[arg-type]
            family=fam, m_family=m, n_hypotheses_ever=registry.n_hypotheses, power_limited=pl, resolution_limited=pl and choice.capped, p_floor=ST.p_floor(B), B=B,
        ))
    return out, tuple(sorted(limited))


def _overall_delta(y_event: np.ndarray, y_control: np.ndarray) -> float:
    """Context only: delta of ALL used events vs ALL used controls for the label (a cell must differ from THIS to carry information)."""
    ye, yc = y_event[~np.isnan(y_event)], y_control[~np.isnan(y_control)]
    return float(ye.mean() - yc.mean()) if len(ye) and len(yc) else float("nan")


def _clusters(events: pd.DataFrame, cfg: EnrichmentConfig, mask: np.ndarray) -> np.ndarray | None:
    return events[cfg.cluster_col].to_numpy()[mask] if cfg.cluster_col in events.columns and events[cfg.cluster_col].notna().any() else None


def _choose_B(plan_families: Mapping[str, list[str]], cfg: EnrichmentConfig) -> ST.BChoice:
    m_max = max((len(v) for v in plan_families.values()), default=1)
    return ST.choose_B(m_max, cfg.alpha, requested=cfg.B, b_min=cfg.B_min, b_max=cfg.B_max)


def _tag(res: ST.EnrichmentResult, est: ST.DeltaEstimate, purpose: str, partition: str, contrast: str, cfg: EnrichmentConfig | None = None) -> ST.EnrichmentResult:
    return replace(res, p_norm=est.p_norm, contrast=contrast, purpose=purpose, partition=partition, n_blocks_event=est.n_blocks_event, n_blocks_control=est.n_blocks_control,
                   n_nan_draws=est.n_nan_draws, stats_version=(cfg.stats_version if cfg else ST.STATS_LEGACY))


def _evidence(est: ST.DeltaEstimate, cfg: EnrichmentConfig, n_clusters: int | None) -> str:
    v2 = cfg.stats_version == ST.STATS_V2  # per-arm block counts only under observer-stats-2
    return ST.evidence_status(
        n_event=est.n_event, n_control=est.n_control, n_blocks=est.n_blocks, n_clusters=n_clusters, min_evidence=cfg.min_evidence,
        n_blocks_event=est.n_blocks_event if v2 else None, n_blocks_control=est.n_blocks_control if v2 else None,
    )


def _partition_of(events: pd.DataFrame, cfg: EnrichmentConfig) -> str:
    return "+".join(sorted(set(events[cfg.partition_col].tolist()))) if len(events) else "-"


# ---------------------------------------------------------------------------------------------- single-feature enrichment
def single_feature_enrichment(
    df: pd.DataFrame, feature_cols: Sequence[str], label_cols: Sequence[str], registry: ST.HypothesisRegistry, cfg: EnrichmentConfig | None = None,
    *, purpose: str | None = None, cell_defs: Mapping[str, CellDef] | None = None, contrast: str = CONTRAST_SAME_CELL,
) -> EnrichmentReport:
    """Per (feature cell, label): ``contrast='event_vs_same_cell_controls'`` (primary) or ``'lift_within_cell'`` (see module docstring).
    ``purpose`` is required; ``cell_defs`` must be passed for every purpose except ``fit`` (cells are fitted on the TRAIN events only)."""
    cfg = cfg or EnrichmentConfig()
    if contrast not in (CONTRAST_SAME_CELL, CONTRAST_LIFT):
        raise ValueError(f"contrast must be {CONTRAST_SAME_CELL!r} or {CONTRAST_LIFT!r}")
    events, controls, prep = _prepare(df, feature_cols, label_cols, cfg, purpose)
    purpose = _check_purpose(purpose)
    feats = [f for f in feature_cols if not f.endswith(TS_SUFFIX)]
    defs = _resolve_cell_defs(events, feats, cfg, purpose, cell_defs)
    plan = []
    for lab in label_cols:
        for f in feats:
            ev_cells, ct_cells = apply_cell_def(events[f], defs[f]), dict(apply_cell_def(controls[f], defs[f]))
            for cell, mask in ev_cells:
                plan.append((lab, f, cell, mask, ct_cells[cell]))
    names = [f"{contrast}|{purpose}|{lab}|{f}|{cell}" for lab, f, cell, _, _ in plan]
    fams = [_family_name(cfg, contrast, purpose, lab, _group_of(f)) for lab, f, _, _, _ in plan]
    plan_families: dict[str, list[str]] = {}
    for n, fam in zip(names, fams, strict=True):
        plan_families.setdefault(fam, []).append(n)
    choice = _choose_B(plan_families, cfg)
    _declare(registry, plan_families)
    registry.register_many(names, fams)  # all hypotheses are registered BEFORE any is evaluated
    raw: list[tuple[ST.EnrichmentResult, float | None]] = []
    overall = {lab: _overall_delta(events[lab].to_numpy(), controls[lab].to_numpy()) for lab in label_cols}
    ev_blk, ct_blk = _block_arrays(events, controls, cfg)
    part = _partition_of(events, cfg)
    for (lab, f, cell, mask, ctl_cell), name in zip(plan, names, strict=True):
        if contrast == CONTRAST_SAME_CELL:
            cm = ctl_cell
        else:
            cm = controls["control_of"].isin(set(events.loc[mask, "event_id"])).to_numpy()
        est = ST.block_bootstrap_delta(
            events[lab].to_numpy()[mask], ev_blk[mask], controls[lab].to_numpy()[cm], ct_blk[cm],
            B=choice.B, seed=_seed_for(cfg, name), alpha=cfg.alpha, cluster_event=_clusters(events, cfg, mask),
        )
        status = _evidence(est, cfg, est.n_clusters)
        res = ST.EnrichmentResult(
            feature=f, group=_group_of(f), label=lab, cell=cell, n_event=est.n_event, n_control=est.n_control, p_event=est.p_event, p_control=est.p_control,
            delta=est.delta, ci_low=est.ci_low, ci_high=est.ci_high, n_blocks=est.n_blocks, adjusted_p=None, status=status, versions=_versions(f),
            n_clusters=est.n_clusters, p_boot=est.p_boot, kind="single", base_delta=overall[lab], hypothesis=name,
        )
        raw.append((_tag(res, est, purpose, part, contrast, cfg), _evaluate_p(est, cfg) if status == ST.OK else None))
    return _wrap(raw, registry, events, controls, prep, cfg, purpose, choice, defs)


def lift_within_cell(
    df: pd.DataFrame, feature_cols: Sequence[str], label_cols: Sequence[str], registry: ST.HypothesisRegistry, cfg: EnrichmentConfig | None = None,
    *, purpose: str | None = None, cell_defs: Mapping[str, CellDef] | None = None,
) -> EnrichmentReport:
    """The former single-feature test: events in the cell vs the controls matched to exactly those events (the controls' own feature values are
    ignored). Measures the event lift inside the cell, not what the feature tells you - see the module docstring."""
    return single_feature_enrichment(df, feature_cols, label_cols, registry, cfg, purpose=purpose, cell_defs=cell_defs, contrast=CONTRAST_LIFT)


# ---------------------------------------------------------------------------------------------- incremental ablation
def incremental_ablation(
    df: pd.DataFrame, label_cols: Sequence[str], base_mask: pd.Series | np.ndarray, groups: Mapping[str, Sequence[str]],
    registry: ST.HypothesisRegistry, cfg: EnrichmentConfig | None = None,
    *, purpose: str | None = None, cell_defs: Mapping[str, CellDef] | None = None, contrasts: Sequence[PredeclaredContrast] | None = None,
) -> EnrichmentReport:
    """BASE FAMILY + group. ``base_mask`` is aligned to ``df`` (rows of events; controls follow their event through ``control_of``).
    Result.delta = delta(base AND cell) - delta(base); ``base_delta`` is the base delta; p/CI from the paired day-block bootstrap.

    ``contrasts`` (recommended for any confirmatory use): the PREDECLARED small list of (group, feature, cell) to test; one family per label x group,
    declared in the registry before evaluation. Without it every cell of every listed feature is tested (exploratory; families can be large and
    are then reported ``power_limited`` when the p resolution cannot support them)."""
    cfg = cfg or EnrichmentConfig()
    all_feats = [f for fs in groups.values() for f in fs]
    bm = pd.Series(np.asarray(base_mask, dtype=bool), index=df.index)
    work = df.copy()
    work["_base"] = bm.to_numpy()
    events, controls, prep = _prepare(work, all_feats, label_cols, cfg, purpose)
    purpose = _check_purpose(purpose)
    defs = _resolve_cell_defs(events, all_feats, cfg, purpose, cell_defs)
    base_ev = events["_base"].to_numpy(dtype=bool)
    base_ids = set(events.loc[base_ev, "event_id"])
    cm_base = controls["control_of"].isin(base_ids).to_numpy()
    wanted = None
    if contrasts is not None:
        wanted = {(c.group, c.feature, c.cell) for c in contrasts}
        known = {(g, f, cell) for g, fs in groups.items() for f in fs for cell in defs[f].labels()}
        unknown = sorted(wanted - known)
        if unknown:
            raise ValueError(f"predeclared contrast(s) not defined by the groups / cell definitions: {unknown[:3]}")
    plan = []
    for lab in label_cols:
        for grp, fs in groups.items():
            for f in fs:
                for cell, m in apply_cell_def(events[f], defs[f]):
                    if wanted is not None and (grp, f, cell) not in wanted:
                        continue
                    plan.append((lab, grp, f, cell, m & base_ev))
    names = [f"incr|{purpose}|{lab}|{grp}|{f}|{cell}" for lab, grp, f, cell, _ in plan]
    fams = [_family_name(cfg, "incr", purpose, lab, grp) for lab, grp, _, _, _ in plan]
    plan_families: dict[str, list[str]] = {}
    for n, fam in zip(names, fams, strict=True):
        plan_families.setdefault(fam, []).append(n)
    choice = _choose_B(plan_families, cfg)
    _declare(registry, plan_families)
    registry.register_many(names, fams)
    raw: list[tuple[ST.EnrichmentResult, float | None]] = []
    part = _partition_of(events, cfg)
    ev_blk, ct_blk = _block_arrays(events, controls, cfg)
    for (lab, grp, f, cell, mask), name in zip(plan, names, strict=True):
        ids = set(events.loc[mask, "event_id"])
        cm = controls["control_of"].isin(ids).to_numpy()
        arm_a = (events[lab].to_numpy()[mask], ev_blk[mask], controls[lab].to_numpy()[cm], ct_blk[cm])
        arm_b = (events[lab].to_numpy()[base_ev], ev_blk[base_ev], controls[lab].to_numpy()[cm_base], ct_blk[cm_base])
        est = ST.block_bootstrap_contrast(arm_a, arm_b, B=choice.B, seed=_seed_for(cfg, name), alpha=cfg.alpha)
        cl = _clusters(events, cfg, mask)
        n_cl = len(set(cl[~np.isnan(arm_a[0])].tolist())) if cl is not None else None
        status = _evidence(est, cfg, n_cl)
        base_delta = _overall_delta(arm_b[0], arm_b[2])
        res = ST.EnrichmentResult(
            feature=f, group=grp, label=lab, cell=f"base AND {cell}", n_event=est.n_event, n_control=est.n_control, p_event=est.p_event,
            p_control=est.p_control, delta=est.delta, ci_low=est.ci_low, ci_high=est.ci_high, n_blocks=est.n_blocks, adjusted_p=None, status=status,
            versions=_versions(f), n_clusters=n_cl, p_boot=est.p_boot, kind="incremental", base_delta=base_delta, hypothesis=name,
        )
        raw.append((_tag(res, est, purpose, part, "incremental", cfg), _evaluate_p(est, cfg) if status == ST.OK else None))
    return _wrap(raw, registry, events, controls, prep, cfg, purpose, choice, defs)


def summarise_by_group(results: Sequence[ST.EnrichmentResult]) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for r in results:
        out.setdefault(r.group or "?", Counter())[r.status] += 1
    return out


def _wrap(raw, registry, events, controls, prep, cfg, purpose, choice: ST.BChoice, defs) -> EnrichmentReport:
    results, limited = _finalise(raw, registry, cfg, choice)
    warns = (choice.warning,) if choice.warning else ()
    if limited:
        warns += (f"power_limited families (m * 2/(B+1) >= alpha): {len(limited)}; their non-significant cells are POWER_LIMITED, not evidence of no effect",)
    return EnrichmentReport(
        results=results, n_hypotheses=registry.n_hypotheses, n_rows=prep["n_rows"], n_events_used=len(events), n_controls_used=len(controls),
        n_excluded_warmup_events=prep["n_ev_ex"], n_excluded_warmup_controls=prep["n_ctl_ex"], n_orphan_controls_dropped=prep["n_orphan"],
        warmup_column_present=prep["warm"], adjust_method=cfg.adjust, versions=_report_versions(cfg), purpose=purpose,
        n_hypotheses_ever=registry.n_hypotheses, n_families=registry.n_families, B=choice.B, p_method=cfg.p_method,
        n_excluded_partition_events=prep["n_part_ev"], n_excluded_partition_controls=prep["n_part_ctl"], cell_defs=dict(defs), warnings=warns,
        power_limited_families=limited,
    )
