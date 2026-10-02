# ruff: noqa: E501
"""Thesis study adapter (RESEARCH / OFFLINE ONLY): MarketMap -> SetupEngine -> cohort -> matched controls -> ablation.

A THIN adapter over existing parts; nothing here has trade authority and nothing production-reachable may import it.

* **Setup events**: ``replay_setup_events`` drives ``SetupReplayer`` (one per direction) over a causal MarketMap sequence
  (``maps_from_bars`` = ``MarketMapReplay``; reads bars <= i only) with ``derive_main_thesis`` alignment. A setup event is the bar on
  which the setup first reaches TRIGGERED. After a terminal state a NEW lifecycle starts on the next bar (the replayers never re-arm);
  triggers closer than ``min_gap_bars`` to an earlier event are dropped and counted (no overlapping outcome windows).
* **DAG**: ``thesis_study_key`` reuses ``dag.code_hash`` / ``config_hash`` exactly like ``dag.entry_exit_key`` (parent stage key + config +
  code closure) and adds the SEMANTIC versions: ``MARKETMAP_VERSION``, marketmap definition hash, ``MAIN_THESIS_VERSION``, ``SetupSpec.spec_hash``
  (also of the ablation spec) and ``STUDY_VERSION``. ``run_study_cached`` publishes under the existing ``ArtifactStore`` as stage
  ``THESIS_STUDY`` (stage names are free strings there) - no new DAG.
* **Controls**: ``observer_lab.controls.match_controls`` (matched on session / time-of-day / ATR rank / spread, within the event partition,
  excluded near any event). Controls inherit the event direction and are labelled with the same label code / risk convention.
* **Ablation**: 2 x 2 over {full spec | spec WITHOUT its LEVEL_BEHAVIOUR requirements} x {any alignment | main-thesis ALIGNED}. The
  statistics are ``observer_lab.enrichment.incremental_ablation`` (BASE FAMILY = every setup event; group "level" and group "thesis"
  with PREDECLARED contrasts; paired day-block bootstrap), on a frame in the lab's own row schema.
* **Multiplicity**: every tested variant is registered in a ``HypothesisRegistry`` BEFORE its result is recorded (the 4 descriptive arms +
  the predeclared ablation contrasts); ``n_hypotheses`` is part of the output. Re-running the same study on the same registry is refused.
* **Status**: never above a research candidate. ``PromotionStatus`` has no live member; this module only ever returns REJECT_FAST or
  PROMOTE_TO_FIDELITY (the workbench's "research candidate"), and ``no_promotion_claim`` is always True.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np
import pandas as pd

from coverage_analysis.observer_lab import enrichment as EN
from coverage_analysis.observer_lab import stats as ST
from coverage_analysis.observer_lab.controls import (
    CONTROL_METHOD_VERSION,
    MatchSpec,
    bar_partitions,
    match_controls,
)
from coverage_analysis.observer_lab.labels import LABEL_CONVENTION_VERSION, EventSpec, label_event
from market_observer.schema import ObserverBars, first_passage_label_name, label_key
from research_speed.artifact import config_hash
from research_workbench import dag
from research_workbench.status import PromotionStatus
from research_workbench.thesis.contracts import (
    Alignment,
    Direction,
    EvidenceClass,
    MarketMap,
    SetupSpec,
    SetupState,
)
from research_workbench.thesis.main_thesis import MAIN_THESIS_VERSION, alignment, derive_main_thesis
from research_workbench.thesis.marketmap import (
    MARKETMAP_CONFIG,
    MARKETMAP_VERSION,
    MarketMapConfig,
    MarketMapReplay,
    geometry_for_entry,
    marketmap_definition_hash,
)
from research_workbench.thesis.position_thesis import HypotheticalExit, Variant, whipsaw
from research_workbench.thesis.setup_engine import SetupReplayer
from research_workbench.thesis.specs import CONTINUATION_RETEST

STUDY_VERSION = "thesis-study-1"
THESIS_STUDY_STAGE = "THESIS_STUDY"
THESIS_STUDY_CODE = (
    "research_workbench/thesis/study.py",
)  # import closure hashed by dag.code_hash
MAIN_THESIS_HISTORY_BARS = 12  # frozen: maps of context handed to derive_main_thesis
DEFAULT_MIN_GAP_BARS = 48  # == controls.DEFAULT_EXCLUSION_BARS == label horizon
LABEL = label_key(first_passage_label_name(0.50, 0.50))  # y_fav050_before_adv050
RESEARCH_CEILING = PromotionStatus.PROMOTE_TO_FIDELITY  # the workbench's "research candidate"
_ALLOWED_STATUS = frozenset({PromotionStatus.REJECT_FAST, PromotionStatus.PROMOTE_TO_FIDELITY})
F_LEVEL, F_THESIS = "f_level__condition", "f_thesis__alignment"
WITH_LEVEL, WITHOUT_LEVEL = "WITH", "WITHOUT"
ALIGNED, NOT_ALIGNED = "ALIGNED", "NOT_ALIGNED"
ARMS = (
    ("FULL", False),
    ("FULL+ALIGNED", True),
    ("NO_LEVEL", False),
    ("NO_LEVEL+ALIGNED", True),
)


@dataclass(frozen=True)
class StudyConfig:
    directions: tuple[Direction, ...] = (Direction.LONG, Direction.SHORT)
    min_gap_bars: int = DEFAULT_MIN_GAP_BARS
    risk_atr_mult: float = (
        1.0  # label risk R = risk_atr_mult * ATR[i] for events AND controls (same convention)
    )
    seed: int = 0
    purpose: str = "fit"
    n_controls: int = 1
    bootstrap_B: int | None = None  # None = enrichment automatic choice
    causal_controls: bool = False  # True: control percentile ranks are expanding PAST-ONLY (candidate pool stays two-sided)

    def as_dict(self) -> dict[str, Any]:
        return {
            "directions": [d.value for d in self.directions],
            "min_gap_bars": self.min_gap_bars,
            "risk_atr_mult": self.risk_atr_mult,
            "seed": self.seed,
            "purpose": self.purpose,
            "n_controls": self.n_controls,
            "bootstrap_B": self.bootstrap_B,
            "causal_controls": self.causal_controls,
            "main_thesis_history_bars": MAIN_THESIS_HISTORY_BARS,
            "label": LABEL,
        }


# ------------------------------------------------------------------------------------------------ versions + DAG key
def without_level_behaviour(spec: SetupSpec) -> SetupSpec:
    """Ablation spec: the same setup with every LEVEL_BEHAVIOUR requirement removed (different ``spec_hash`` by construction)."""
    req = {k: v for k, v in spec.requirements.items() if k is not EvidenceClass.LEVEL_BEHAVIOUR}
    return replace(spec, requirements=req, params={**spec.params, "level_behaviour_started": ()})


def study_versions(spec: SetupSpec, mm_config: MarketMapConfig | None = None) -> dict[str, str]:
    """Semantic versions that are part of the stage key (read at call time so a changed constant changes the key)."""
    return {
        "study_version": STUDY_VERSION,
        "marketmap_version": MARKETMAP_VERSION,
        "marketmap_definition_hash": marketmap_definition_hash(mm_config or MARKETMAP_CONFIG),
        "main_thesis_version": MAIN_THESIS_VERSION,
        "spec_version": spec.spec_version,
        "spec_hash": spec.spec_hash,
        "ablation_spec_hash": without_level_behaviour(spec).spec_hash,
        "control_method": CONTROL_METHOD_VERSION,
        "label_convention": LABEL_CONVENTION_VERSION,
    }


def thesis_study_key(
    parent_key: str,
    spec: SetupSpec,
    config: StudyConfig | Mapping[str, Any] | None = None,
    *,
    mm_config: MarketMapConfig | None = None,
    inputs: Mapping[str, Any] | None = None,
) -> str:
    """Stage key of the study: ``dag.entry_exit_key`` pattern (parent stage key + config + code closure) plus the semantic version hashes.

    ``parent_key``: the dataset/features identity the bars come from (e.g. ``StageKeys.features``); a different dataset => a different key."""
    cfg = (
        config.as_dict()
        if isinstance(config, StudyConfig)
        else dict(config or StudyConfig().as_dict())
    )
    return config_hash(
        {
            "parent_key": parent_key,
            "versions": study_versions(spec, mm_config),
            "config": cfg,
            "inputs": dict(inputs or {}),
            "code": dag.code_hash(THESIS_STUDY_CODE),
        }
    )


# ------------------------------------------------------------------------------------------------ input digests (cache key)
UNVERSIONED_GEOMETRY = "UNVERSIONED_OVERRIDE"


def _sha(*chunks: bytes) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.hexdigest()


def bars_digest(bars: ObserverBars) -> str:
    arrays = (
        bars.ts_ns,
        bars.o,
        bars.h,
        bars.l,
        bars.c,
        bars.tick_volume,
        bars.spread,
        bars.atr,
        bars.segment_id,
        bars.local_minute,
        bars.local_day,
    )
    return _sha(
        bars.market.encode(),
        repr((bars.tick_size, bars.bar_seconds, bars.session)).encode(),
        *(np.ascontiguousarray(a).tobytes() for a in arrays),
    )


def events_digest(events_by_ts: Mapping[int, Sequence[str]]) -> str:
    return config_hash({str(k): list(v) for k, v in sorted(events_by_ts.items())})


def maps_digest(maps: Sequence[MarketMap] | None) -> str:
    return "REPLAY_FROM_BARS" if maps is None else config_hash([m.content_hash() for m in maps])


def partition_digest(partition: Any) -> str:
    if partition is None or isinstance(partition, str):
        return f"literal:{partition}"
    return _sha(np.asarray(partition, dtype=object).astype(str).tobytes())


def registry_digest(reg: ST.HypothesisRegistry) -> dict[str, Any]:
    """Identity + state AT START: the same inputs on a registry that already holds hypotheses is a different study (multiplicity differs)."""
    return {
        "name": reg.name,
        "path": None if reg.path is None else str(reg.path),
        "n_hypotheses": reg.n_hypotheses,
        "n_families": reg.n_families,
        # names, family assignment, recorded p-values and family definitions (NOT the declaration timestamps): equal counts with
        # different content are a different registry state
        "hypotheses": sorted((h, reg._family[h], reg._p.get(h)) for h in reg._names),
        "families": {
            f: [v.get("definition"), v.get("n_results_recorded_at_declaration")]
            for f, v in sorted(reg._families.items())
        },
    }


def exit_variants_digest(variants: Mapping[Variant, Sequence[HypotheticalExit]] | None) -> str:
    if variants is None:
        return "NONE"
    return config_hash(
        {
            str(v): [
                (r.entry_id, r.kind, bool(r.triggered), repr(r.r), bool(r.complete)) for r in rows
            ]
            for v, rows in sorted(variants.items(), key=lambda kv: str(kv[0]))
        }
    )


def study_inputs(
    bars: ObserverBars,
    events_by_ts: Mapping[int, Sequence[str]],
    *,
    maps: Sequence[MarketMap] | None,
    geometry_fn: Callable[[MarketMap, Direction], Any] | None,
    geometry_version: str | None,
    partition: Any,
    registry: ST.HypothesisRegistry,
    exit_variants: Mapping[Variant, Sequence[HypotheticalExit]] | None,
) -> dict[str, Any]:
    """Content digests of EVERY actual input of the study (part of the stage key)."""
    if geometry_fn is None:
        geo = "geometry_from_bars" if maps is None else "none"
    else:
        geo = f"override:{geometry_version}" if geometry_version else UNVERSIONED_GEOMETRY
    return {
        "bars": bars_digest(bars),
        "events": events_digest(events_by_ts),
        "maps": maps_digest(maps),
        "geometry": geo,
        "partition": partition_digest(partition),
        "registry": config_hash(registry_digest(registry)),
        "exit_variants": exit_variants_digest(exit_variants),
    }


LIMITATIONS = (
    "EXPLORATORY ONLY: this adapter performs no end-to-end OOS / embargo validation; its output is never validation evidence",
    "controls are matched retrospectively: candidate bars may lie on BOTH sides of the event in time, and unless causal_controls=True the volatility / spread percentile ranks are partition-wide (use later bars of the partition)",
    "setup trigger events (e.g. STRUCT_RETEST_LONG) are caller-supplied; the adapter does not recompute them",
    "label risk R = risk_atr_mult x ATR is a study convention, not the thesis structural stop",
    "exit-variant contrasts are descriptive (no p-values); they are registered for multiplicity accounting only",
)


def _resolve_registry(
    registry: ST.HypothesisRegistry | None, market: str
) -> tuple[ST.HypothesisRegistry, str, list[str]]:
    """Registry + multiplicity scope. A missing or in-memory registry is RUN_LOCAL_ONLY (multiplicity across runs is NOT counted)."""
    notes: list[str] = []
    if registry is None:
        registry = ST.HypothesisRegistry(f"thesis-study-{market}")
        notes.append(
            "no registry supplied: a fresh in-memory registry was created; multiplicity counts THIS RUN only"
        )
    if registry.path is None:
        if not notes:
            notes.append("in-memory registry: multiplicity counts THIS RUN only")
        return registry, "RUN_LOCAL_ONLY", notes
    return registry, "PERSISTENT", notes


# ------------------------------------------------------------------------------------------------ setup events
@dataclass(frozen=True)
class SetupEvent:
    idx: int  # decision bar index (== MarketMap.bar_index)
    decision_ts_ns: int
    direction: Direction
    thesis_id: str
    spec_hash: str
    alignment: Alignment  # main-thesis alignment AT the trigger bar


@dataclass(frozen=True)
class ReplayOutcome:
    events: tuple[SetupEvent, ...]
    trace: tuple[
        tuple[int, str, str, str], ...
    ]  # (decision_ts, direction, status, state) per bar x direction
    n_overlap_dropped: int
    n_maps: int


def maps_from_bars(
    bars: ObserverBars, config: MarketMapConfig | None = None
) -> Iterator[MarketMap]:
    """Causal MarketMap sequence (``MarketMapReplay``; the map at i reads bars <= i only)."""
    rep = MarketMapReplay(config)
    for i in range(len(bars)):
        yield rep.step(bars, i)


def geometry_from_bars(
    bars: ObserverBars, config: MarketMapConfig | None = None
) -> Callable[[MarketMap, Direction], Any]:
    """Geometry callback for a PROPOSED entry at the decision bar close (prefix-only frame), memoised per (bar, direction)."""
    memo: dict[tuple[int, Direction], Any] = {}

    def fn(mm: MarketMap, d: Direction) -> Any:
        key = (mm.bar_index, d)
        if key not in memo:
            memo[key] = geometry_for_entry(
                bars, mm.bar_index, d, float(bars.c[mm.bar_index]), config=config
            )
        return memo[key]

    return fn


def replay_setup_events(
    maps: Iterable[MarketMap],
    events_by_ts: Mapping[int, Sequence[str]],
    spec: SetupSpec,
    directions: Sequence[Direction] = (Direction.LONG, Direction.SHORT),
    *,
    geometry_fn: Callable[[MarketMap, Direction], Any] | None = None,
    min_gap_bars: int = DEFAULT_MIN_GAP_BARS,
) -> ReplayOutcome:
    """Setup events (first TRIGGERED bar of each lifecycle) of ``spec`` over a causal MarketMap sequence."""
    history: deque[MarketMap] = deque(maxlen=MAIN_THESIS_HISTORY_BARS)
    reps: dict[Direction, SetupReplayer] = {d: SetupReplayer(spec, d) for d in directions}
    events: list[SetupEvent] = []
    trace: list[tuple[int, str, str, str]] = []
    last_idx: int | None = None
    dropped = n_maps = 0
    for mm in maps:
        n_maps += 1
        mt = derive_main_thesis(mm, tuple(history))
        for d in directions:
            rep = reps[d]
            open_gate = rep.thesis is not None or mm.market_phase in spec.allowed_market_phases
            geo = geometry_fn(mm, d) if (geometry_fn is not None and open_gate) else None
            res = rep.step(
                mm,
                events_by_ts.get(mm.decision_ts_ns, ()),
                alignment=alignment(d, mt),
                geometry=geo,
            )
            th = res.thesis
            trace.append(
                (
                    mm.decision_ts_ns,
                    d.value,
                    res.status.value,
                    "-" if th is None else th.state.value,
                )
            )
            if res.evaluation is not None and any(
                t.dst is SetupState.TRIGGERED for t in res.evaluation.transitions
            ):
                if last_idx is not None and mm.bar_index - last_idx < min_gap_bars:
                    dropped += 1
                else:
                    last_idx = mm.bar_index
                    events.append(
                        SetupEvent(
                            mm.bar_index,
                            mm.decision_ts_ns,
                            d,
                            th.thesis_id,
                            th.spec_hash,
                            th.daily_thesis_alignment,
                        )
                    )
            if th is not None and th.state in (
                SetupState.TRIGGERED,
                SetupState.INVALIDATED,
                SetupState.EXPIRED,
            ):
                reps[d] = SetupReplayer(
                    spec, d, mm.market
                )  # a new lifecycle starts on the NEXT bar
        history.append(mm)
    return ReplayOutcome(tuple(events), tuple(trace), dropped, n_maps)


# ------------------------------------------------------------------------------------------------ frame (observer-lab row schema)
def _label_row(
    bars: ObserverBars, idx: int, direction: Direction, cfg: StudyConfig
) -> float | str | None:
    """Binary first-passage label or ``"UNLABELABLE"`` (unknown ATR / no following bar)."""
    atr = float(bars.atr[idx])
    if not (np.isfinite(atr) and atr > 0) or idx + 1 >= len(bars):
        return "UNLABELABLE"
    lab = label_event(
        bars, EventSpec(idx, direction.sign, float(bars.c[idx]), cfg.risk_atr_mult * atr)
    )
    v = lab.columns.get(LABEL)
    return None if v is None else float(v)


def build_frame(
    bars: ObserverBars,
    rows: Sequence[tuple[SetupEvent, str, str]],
    cfg: StudyConfig,
    partition: np.ndarray | Sequence[str] | str | None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Events (+ matched controls) in the lab's row schema. ``rows`` = (event, level condition, thesis cell)."""
    idx = [e.idx for e, _, _ in rows]
    cs = match_controls(
        bars,
        idx,
        spec=MatchSpec(n_controls=cfg.n_controls),
        seed=cfg.seed,
        partition=partition,
        rank_mode="causal" if cfg.causal_controls else "partition",
        exclude_idx=idx,
    )
    part = (
        bar_partitions(bars)
        if isinstance(partition, str) and partition == "auto"
        else (None if partition is None else np.asarray(partition, dtype=object))
    )
    ctl_by_event: dict[int, list[int]] = {}
    for pos, cj in zip(cs.event_pos.tolist(), cs.control_idx.tolist(), strict=True):
        ctl_by_event.setdefault(pos, []).append(cj)

    def row(i: int, feats: Mapping[str, str], **kw: Any) -> dict[str, Any]:
        return {
            "decision_ts_ns": int(bars.decision_ts_ns(i)), "m_local_day": int(bars.local_day[i]), "m_structure_event_id": None,
            "partition": "TRAIN" if part is None else str(part[i]), "warmup_ok": True, **feats, **kw,
        }  # fmt: skip

    out: list[dict[str, Any]] = []
    n_unlabelable = 0
    for pos, (ev, lvl, th_cell) in enumerate(rows):
        y = _label_row(bars, ev.idx, ev.direction, cfg)
        if isinstance(y, str):
            n_unlabelable += 1
            continue
        eid = f"{ev.thesis_id}@{ev.decision_ts_ns}"
        feats = {F_LEVEL: lvl, F_THESIS: th_cell}
        out.append(
            row(
                ev.idx,
                feats,
                event_id=eid,
                is_control=False,
                control_of=None,
                direction=ev.direction.value,
                **{LABEL: y},
            )
        )
        for k, cj in enumerate(ctl_by_event.get(pos, [])):
            yc = _label_row(bars, cj, ev.direction, cfg)
            if isinstance(yc, str):
                continue
            out.append(
                row(
                    cj,
                    feats,
                    event_id=f"c{k}_{eid}",
                    is_control=True,
                    control_of=eid,
                    direction=ev.direction.value,
                    **{LABEL: yc},
                )
            )
    info = {
        "n_events_in": len(rows), "n_unlabelable_events": n_unlabelable, "n_controls": len(cs.control_idx),
        "match_rate": cs.report.match_rate, "n_unmatched_events": len(cs.report.unmatched_event_pos),
        "control_method": cs.report.method, "smd": dict(cs.report.smd),
    }  # fmt: skip
    return pd.DataFrame(out), info


# ------------------------------------------------------------------------------------------------ study
@dataclass
class StudyResult:
    market: str
    key: str
    versions: dict[str, str]
    config: dict[str, Any]
    events: dict[str, int]
    arms: list[dict[str, Any]]
    ablation: list[dict[str, Any]]
    controls: dict[str, Any]
    multiplicity: dict[str, Any]
    promotion_status: str
    reasons: list[str] = field(default_factory=list)
    no_promotion_claim: bool = True
    research_only: bool = True
    cached: bool = False
    multiplicity_scope: str = "RUN_LOCAL_ONLY"
    no_promotion_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=lambda: list(LIMITATIONS))
    exit_variants: list[dict[str, Any]] = field(default_factory=list)
    registry_state_at_start: str = (
        ""  # digest of the registry (names, families, results) the run started from
    )
    registry_effect: dict[str, Any] = field(
        default_factory=dict
    )  # complete registry effect (families, registrations, recorded p-values)
    registry_entries: list[list[str]] = field(
        default_factory=list
    )  # [hypothesis, family] registered by this run (re-registered on a cache hit)

    def to_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        return json.loads(json.dumps(d, default=str))


def _arm_stats(frame: pd.DataFrame, level: str | None, aligned: bool) -> dict[str, Any]:
    ev = frame[~frame["is_control"].astype(bool)]
    ct = frame[frame["is_control"].astype(bool)]
    sel = pd.Series(True, index=ev.index)
    if level is not None:
        sel &= ev[F_LEVEL] == level
    if aligned:
        sel &= ev[F_THESIS] == ALIGNED
    ev_arm = ev[sel]
    ct_arm = ct[ct["control_of"].isin(set(ev_arm["event_id"]))]
    ye, yc = ev_arm[LABEL].dropna(), ct_arm[LABEL].dropna()
    pe = float(ye.mean()) if len(ye) else None
    pc = float(yc.mean()) if len(yc) else None
    return {
        "n_events": len(ev_arm), "n_controls": len(ct_arm), "p_event": pe, "p_control": pc,
        "delta": None if pe is None or pc is None else pe - pc, "descriptive_only": True,
    }  # fmt: skip


def run_study(
    bars: ObserverBars,
    events_by_ts: Mapping[int, Sequence[str]],
    *,
    spec: SetupSpec = CONTINUATION_RETEST,
    config: StudyConfig | None = None,
    registry: ST.HypothesisRegistry | None = None,
    maps: Sequence[MarketMap] | None = None,
    geometry_fn: Callable[[MarketMap, Direction], Any] | None = None,
    partition: np.ndarray | Sequence[str] | str | None = "auto",
    parent_key: str = "-",
    mm_config: MarketMapConfig | None = None,
    geometry_version: str | None = None,
    exit_variants: Mapping[Variant, Sequence[HypotheticalExit]] | None = None,
) -> StudyResult:
    """The whole study for one market. ``maps`` / ``geometry_fn`` default to the causal MarketMapReplay over ``bars``.

    ``registry``: pass a PERSISTENT ``HypothesisRegistry(path=...)`` for cross-run multiplicity; without one the result is marked
    ``multiplicity_scope="RUN_LOCAL_ONLY"`` with a visible warning and a no-promotion reason.
    ``geometry_version``: version token of a ``geometry_fn`` override (part of the key; required for caching).
    ``exit_variants``: optional ``run_variants`` output; every variant A-D x contrast vs CONTROL is registered."""
    cfg = config or StudyConfig()
    reg, scope, scope_notes = _resolve_registry(registry, bars.market)
    n_start = (reg.n_hypotheses, len(reg._families))
    inputs = study_inputs(
        bars,
        events_by_ts,
        maps=maps,
        geometry_fn=geometry_fn,
        geometry_version=geometry_version,
        partition=partition,
        registry=reg,
        exit_variants=exit_variants,
    )
    key = thesis_study_key(parent_key, spec, cfg, mm_config=mm_config, inputs=inputs)
    mlist = list(maps) if maps is not None else list(maps_from_bars(bars, mm_config))
    geo = (
        geometry_fn
        if geometry_fn is not None or maps is not None
        else geometry_from_bars(bars, mm_config)
    )
    full = replay_setup_events(
        mlist, events_by_ts, spec, cfg.directions, geometry_fn=geo, min_gap_bars=cfg.min_gap_bars
    )
    abl = replay_setup_events(
        mlist,
        events_by_ts,
        without_level_behaviour(spec),
        cfg.directions,
        geometry_fn=geo,
        min_gap_bars=cfg.min_gap_bars,
    )
    in_full = {(e.idx, e.direction) for e in full.events}
    union: dict[tuple[int, Direction], SetupEvent] = {(e.idx, e.direction): e for e in abl.events}
    union.update({(e.idx, e.direction): e for e in full.events})
    ordered = sorted(union.values(), key=lambda e: (e.idx, e.direction.value))
    rows = [
        (
            e,
            WITH_LEVEL if (e.idx, e.direction) in in_full else WITHOUT_LEVEL,
            ALIGNED if e.alignment is Alignment.ALIGNED else NOT_ALIGNED,
        )
        for e in ordered
    ]
    out = StudyResult(
        market=bars.market, key=key, versions=study_versions(spec, mm_config), config=cfg.as_dict(),
        events={"full_spec": len(full.events), "no_level_spec": len(abl.events), "union": len(ordered),
                "overlap_dropped_full": full.n_overlap_dropped, "overlap_dropped_no_level": abl.n_overlap_dropped, "maps": full.n_maps},
        arms=[], ablation=[], controls={}, multiplicity={}, promotion_status=str(PromotionStatus.REJECT_FAST),
        multiplicity_scope=scope, warnings=list(scope_notes), registry_state_at_start=inputs["registry"],
    )  # fmt: skip
    out.no_promotion_reasons.append(
        "exploratory adapter: no end-to-end OOS / embargo validation (see limitations)"
    )
    if scope != "PERSISTENT":
        out.no_promotion_reasons.append(
            "multiplicity scope RUN_LOCAL_ONLY: hypotheses tested in other runs are not counted"
        )
    arm_names = [f"thesis_study|{key[:12]}|arm|{a}" for a, _ in ARMS]
    arm_family = f"thesis_study_arms|{key[:12]}"
    reg.declare_family(arm_family, definition=json.dumps(arm_names))
    reg.register_many(arm_names, arm_family)  # every tested variant counted BEFORE any result
    n_arm = len(arm_names)
    n_exit = 0
    if exit_variants is not None:
        out.exit_variants, n_exit = _exit_variant_stats(reg, key, exit_variants)
    if not rows:
        out.reasons.append(
            "no setup event in the sample: nothing to test (REJECT_FAST, not a negative finding)"
        )
        out.multiplicity = _multiplicity(reg, n_arm, 0, cfg, n_exit=n_exit, scope=scope)
        _finish(out, reg, n_start)
        return out
    frame, info = build_frame(bars, rows, cfg, partition)
    out.controls = info
    for (name, aligned), lvl in zip(ARMS, (WITH_LEVEL, WITH_LEVEL, None, None), strict=True):
        out.arms.append({"arm": name, **_arm_stats(frame, lvl, aligned)})
    contrasts = [
        EN.PredeclaredContrast("level", F_LEVEL, f"={WITH_LEVEL}"),
        EN.PredeclaredContrast("thesis", F_THESIS, f"={ALIGNED}"),
    ]
    base = ~frame["is_control"].astype(bool)
    ecfg = EN.EnrichmentConfig(B=cfg.bootstrap_B, seed=cfg.seed)
    rep = EN.incremental_ablation(
        frame,
        [LABEL],
        base,
        {"level": [F_LEVEL], "thesis": [F_THESIS]},
        reg,
        ecfg,
        purpose=cfg.purpose,
        contrasts=contrasts,
    )
    out.ablation = [
        {k: getattr(r, k) for k in ("group", "feature", "cell", "label", "n_event", "n_control", "delta", "base_delta", "ci_low", "ci_high", "adjusted_p", "status", "hypothesis", "family", "m_family")}
        for r in rep.results
    ]  # fmt: skip
    out.multiplicity = _multiplicity(
        reg, n_arm, len(rep.results), cfg, rep.warnings, n_exit=n_exit, scope=scope
    )
    out.warnings += list(rep.warnings)
    wins = [r for r in rep.results if r.status == ST.SIGNIFICANT_ADJUSTED and r.delta > 0]
    if wins:
        out.promotion_status = str(PromotionStatus.PROMOTE_TO_FIDELITY)
        out.reasons.append(
            f"{len(wins)} ablation contrast(s) significant after multiplicity adjustment: a RESEARCH CANDIDATE only, never a live claim"
        )
    else:
        out.reasons.append(
            "no ablation contrast significant after multiplicity adjustment (insufficient evidence is not evidence of no effect)"
        )
    assert (
        PromotionStatus(out.promotion_status) in _ALLOWED_STATUS
    )  # never above a research candidate
    _finish(out, reg, n_start)
    return out


def _multiplicity(
    reg: ST.HypothesisRegistry,
    n_arm: int,
    n_abl: int,
    cfg: StudyConfig,
    warnings: Sequence[str] = (),
    *,
    n_exit: int = 0,
    scope: str = "RUN_LOCAL_ONLY",
) -> dict[str, Any]:
    return {
        "n_hypotheses": reg.n_hypotheses, "n_families": reg.n_families, "n_arm_hypotheses": n_arm, "n_ablation_hypotheses": n_abl,
        "n_exit_variant_hypotheses": n_exit, "multiplicity_scope": scope,
        "adjust": EN.EnrichmentConfig().adjust, "registry": reg.name, "warnings": list(warnings), "seed": cfg.seed,
    }  # fmt: skip


EXIT_CONTRASTS = ("mean_r_delta_vs_control", "whipsaw_rate")


def _exit_variant_stats(
    reg: ST.HypothesisRegistry, key: str, variants: Mapping[Variant, Sequence[HypotheticalExit]]
) -> tuple[list[dict[str, Any]], int]:
    """Register every exit variant A-D x contrast vs CONTROL BEFORE its result is computed; results are descriptive (no p-values).

    Pending (incomplete) rows are excluded from the statistics and counted, never treated as losses."""
    tested = [v for v in Variant if v is not Variant.CONTROL]
    names = [
        f"thesis_study|{key[:12]}|exit|{v.value}|{c}|vs_CONTROL"
        for v in tested
        for c in EXIT_CONTRASTS
    ]
    fam = f"thesis_study_exit_variants|{key[:12]}"
    reg.declare_family(fam, definition=json.dumps(names))
    reg.register_many(names, fam)
    control = {r.entry_id: r for r in variants.get(Variant.CONTROL, ())}
    rows: list[dict[str, Any]] = []
    for v in tested:
        pairs = [(r, control[r.entry_id]) for r in variants.get(v, ()) if r.entry_id in control]
        done = [
            (r, c)
            for r, c in pairs
            if r.complete and c.complete and np.isfinite(r.r) and np.isfinite(c.r)
        ]
        n_missing_control = sum(1 for r in variants.get(v, ()) if r.entry_id not in control)
        rows.append({
            "variant": v.value, "vs": "CONTROL", "n_pairs": len(pairs), "n_complete": len(done), "n_pending_excluded": len(pairs) - len(done),
            "n_without_control": n_missing_control, "descriptive_only": True,
            "mean_r_delta_vs_control": float(np.mean([r.r - c.r for r, c in done])) if done else None,
            "whipsaw_rate": float(np.mean([whipsaw(r, c) for r, c in done])) if done else None,
            "hypotheses": [f"thesis_study|{key[:12]}|exit|{v.value}|{c}|vs_CONTROL" for c in EXIT_CONTRASTS],
        })  # fmt: skip
    return rows, len(names)


def _finish(out: StudyResult, reg: ST.HypothesisRegistry, n_start: tuple[int, int]) -> None:
    """Record what this run registered (replayed into the registry on a cache hit) and surface the scope warning."""
    n_hyp, n_fam = n_start
    out.registry_entries = [[n, reg.family_of(n)] for n in reg._names[n_hyp:]]
    # the COMPLETE registry effect of this run (replayed exactly on a cache hit): family declarations incl. definition and the number
    # of results recorded at declaration, and every new hypothesis with its recorded p-value
    out.registry_effect = {
        "families": {f: dict(reg._families[f]) for f in list(reg._families)[n_fam:]},
        "hypotheses": [
            [n, reg.family_of(n), n in reg._p, reg._p.get(n)] for n in reg._names[n_hyp:]
        ],
    }
    if out.multiplicity_scope != "PERSISTENT":
        out.warnings.append(
            "MULTIPLICITY RUN_LOCAL_ONLY: pass a persistent HypothesisRegistry(path=...) to count hypotheses across runs"
        )
    out.no_promotion_claim = True


def _registry_compatible(reg: ST.HypothesisRegistry, res: StudyResult, state: str) -> bool:
    """A stored result may be restored only into a registry in the SAME state it started from, with none of its names present."""
    return (
        bool(res.registry_effect)
        and res.registry_state_at_start == state
        and not any(n in reg._family for n, _ in res.registry_entries)
    )


def _replay_registration(reg: ST.HypothesisRegistry, res: StudyResult) -> None:
    """Cache hit: leave the registry exactly as the fresh run left it (family declarations with their definition and recorded-count,
    registrations in order, recorded p-values) and flush it, so state hashes / later keys / registry-wide adjustments are identical."""
    eff = res.registry_effect
    for fam, info in eff.get("families", {}).items():
        reg._families[fam] = dict(info)
    for name, fam, recorded, p in eff.get("hypotheses", []):
        reg._names.append(name)
        reg._family[name] = fam
        if recorded:
            reg._p[name] = p
    reg.flush()


def run_study_cached(
    store: dag.ArtifactStore,
    experiment_id: str,
    bars: ObserverBars,
    events_by_ts: Mapping[int, Sequence[str]],
    *,
    parent_key: str,
    spec: SetupSpec = CONTINUATION_RETEST,
    config: StudyConfig | None = None,
    registry: ST.HypothesisRegistry | None = None,
    maps: Sequence[MarketMap] | None = None,
    geometry_fn: Callable[[MarketMap, Direction], Any] | None = None,
    geometry_version: str | None = None,
    partition: Any = "auto",
    mm_config: MarketMapConfig | None = None,
    exit_variants: Mapping[Variant, Sequence[HypotheticalExit]] | None = None,
) -> StudyResult:
    """``run_study`` under the existing artifact store (stage ``THESIS_STUDY``): HIT = the stored result, no recomputation.

    The key covers ALL actual inputs (bars, events, maps content hashes, geometry version, partition, registry state at start,
    exit variants). A ``geometry_fn`` override needs a ``geometry_version`` token, otherwise caching is refused (ValueError)."""
    if geometry_fn is not None and not geometry_version:
        raise ValueError(
            "a geometry_fn override cannot be cached without a geometry_version token (un-hashable input)"
        )
    cfg = config or StudyConfig()
    reg, _, _ = _resolve_registry(registry, bars.market)
    inputs = study_inputs(
        bars,
        events_by_ts,
        maps=maps,
        geometry_fn=geometry_fn,
        geometry_version=geometry_version,
        partition=partition,
        registry=reg,
        exit_variants=exit_variants,
    )
    key = thesis_study_key(parent_key, spec, cfg, mm_config=mm_config, inputs=inputs)
    cacheable = not key.startswith(dag.UNCACHEABLE) and not dag.code_hash(
        THESIS_STUDY_CODE
    ).startswith(dag.UNCACHEABLE)
    lk = store.lookup(bars.market, THESIS_STUDY_STAGE, key, cacheable=cacheable)
    if lk.hit:
        data = json.loads(
            (store.stage_dir(bars.market, THESIS_STUDY_STAGE, key) / "study.json").read_text(
                "utf-8"
            )
        )
        res = StudyResult(**data)
        if _registry_compatible(reg, res, inputs["registry"]):
            _replay_registration(reg, res)
            res.cached = True
            return res
        # incompatible registry state: treated as a MISS (recompute; reuse errors surface exactly as in a fresh run)
    t0 = time.perf_counter()
    res = run_study(
        bars, events_by_ts, spec=spec, config=cfg, registry=registry, maps=maps, geometry_fn=geometry_fn, geometry_version=geometry_version,
        partition=partition, parent_key=parent_key, mm_config=mm_config, exit_variants=exit_variants,
    )  # fmt: skip
    assert res.key == key
    if cacheable:
        store.publish(
            bars.market, THESIS_STUDY_STAGE, key, {"study.json": json.dumps(res.to_dict(), indent=1, sort_keys=True).encode("utf-8")},
            experiment_id=experiment_id, code=dag.code_hash(THESIS_STUDY_CODE), runtime_s=time.perf_counter() - t0,
        )  # fmt: skip
    return res


__all__ = [
    "LIMITATIONS",
    "RESEARCH_CEILING",
    "STUDY_VERSION",
    "THESIS_STUDY_CODE",
    "THESIS_STUDY_STAGE",
    "ReplayOutcome",
    "SetupEvent",
    "StudyConfig",
    "StudyResult",
    "build_frame",
    "geometry_from_bars",
    "maps_from_bars",
    "replay_setup_events",
    "run_study",
    "run_study_cached",
    "study_inputs",
    "study_versions",
    "thesis_study_key",
    "without_level_behaviour",
]
