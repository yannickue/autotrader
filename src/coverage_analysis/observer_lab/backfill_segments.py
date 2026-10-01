# ruff: noqa: E501
"""Segmented, checkpointed, cacheable execution of the observer backfill (OFFLINE / RESEARCH ONLY; development tooling, changes no computed number).

A run is split into SEGMENTS = market x stage (``events``, ``controls3_a``, ``controls3_b``). Each segment

* has an ARTIFACT_ID = sha256 of (DATA_HASH, FEATURE_CODE_HASH, CONTROL_CODE_HASH, CONFIG_HASH, LABEL_VERSION, PREREG_VERSION) (``research_speed.artifact``),
* is a CACHE HIT when its segment manifest (``<out>/_segments/<MARKET>__<stage>.json``, written last and atomically) carries the same ARTIFACT_ID and every
  output file still has its recorded size and sha256; otherwise it is recomputed (``force=True`` into the step, so the step's own coarser idempotence can never
  resurrect stale files) and the miss REASON is logged,
* is independent of the others except through files: ``controls3_a`` / ``controls3_b`` read the committed ``events`` segment of the same market. The dependency is
  part of their CONFIG_HASH (upstream ARTIFACT_ID), so a recomputed events step invalidates its controls.

The step functions are the unchanged ``run_events_step`` / ``run_controls3_step(only_set=...)``: segmenting and parallelising reorders WHEN work runs, never WHAT is computed
(bit-equality of the parquet outputs against the serial run is asserted in tests/unit/observer_lab/test_ol_segments.py and measured in docs/RESEARCH_SPEED.md).
"""

from __future__ import annotations

import time
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from research_speed.artifact import NOT_APPLICABLE, ArtifactFingerprint, config_hash
from research_speed.importgraph import code_hash, tree_hash
from research_speed.segments import SegmentStore

REPO = Path(__file__).resolve().parents[3]
ROOTS = (REPO / "src", REPO / "scripts")
STAGES = ("events", "controls3_a", "controls3_b")
SEGMENTS_VERSION = "observer-backfill-segments-1"


def segment_id(market: str, stage: str) -> str:
    return f"{market}/{stage}"


def plan_segments(markets: list[str], steps: tuple[str, ...], with_b: bool) -> list[tuple[str, str, tuple[str, ...]]]:
    """(market, stage, dependencies) in a stable order: all events segments first (they gate the controls), then the control sets."""
    out: list[tuple[str, str, tuple[str, ...]]] = []
    if "events" in steps:
        out += [(m, "events", ()) for m in markets]
    if "controls" in steps:
        for m in markets:
            dep = (segment_id(m, "events"),) if "events" in steps else ()
            out.append((m, "controls3_a", dep))
            if with_b:
                out.append((m, "controls3_b", dep))
    return out


# ---------------------------------------------------------------------------------------------- code hashes (content of the static import closure)
def _p(rel: str) -> Path:
    return REPO / rel


_CONTROL_ONLY = ("src/coverage_analysis/observer_lab/backfill_controls3.py", "src/coverage_analysis/observer_lab/controls_sametime.py", "src/coverage_analysis/observer_lab/backfill_segments.py")


@lru_cache(maxsize=1)
def feature_code_hash() -> str:
    """Everything the events step (generators, observer features, labels, partitions, file layout) can depend on; the control-only modules are excluded."""
    entries = [_p("src/coverage_analysis/observer_lab/backfill.py"), _p("scripts/entry_exit_quality.py")]
    return code_hash(entries, ROOTS, REPO, exclude=[_p(x) for x in _CONTROL_ONLY])


@lru_cache(maxsize=1)
def control_code_hash() -> str:
    """The controls-3 selection / diagnostics / gate code including everything it imports (the feature code too: control rows carry the observer features)."""
    entries = [_p("src/coverage_analysis/observer_lab/backfill_controls3.py"), _p("src/coverage_analysis/observer_lab/controls_sametime.py")]
    return code_hash(entries, ROOTS, REPO)


@lru_cache(maxsize=1)
def configs_hash() -> str:
    return tree_hash(REPO / "configs", REPO)


def events_fingerprint(spec: dict[str, Any], data_hash: str, observer_config_hash: str) -> ArtifactFingerprint:
    from coverage_analysis.observer_lab import backfill as BF
    from coverage_analysis.observer_lab.labels import DEFAULT_MAX_BARS, LABEL_CONVENTION_VERSION
    from market_observer.schema import OBSERVER_VERSION, SCHEMA_VERSION

    cfg = {"stage": "events", "market": spec["market"], "limit": spec["limit"], "observer_config": observer_config_hash, "max_label_bars": DEFAULT_MAX_BARS, "configs": configs_hash(),
           "segments": SEGMENTS_VERSION}
    label = "|".join([LABEL_CONVENTION_VERSION, OBSERVER_VERSION, SCHEMA_VERSION, BF.EVENTS_PIPELINE_VERSION, BF.BACKFILL_VERSION])
    return ArtifactFingerprint(data_hash, feature_code_hash(), NOT_APPLICABLE, config_hash(cfg), label, NOT_APPLICABLE)


def controls_fingerprint(spec: dict[str, Any], data_hash: str, observer_config_hash: str, upstream_artifact_id: str) -> ArtifactFingerprint:
    from coverage_analysis.observer_lab import backfill as BF
    from coverage_analysis.observer_lab import backfill_controls3 as C3
    from coverage_analysis.observer_lab import controls_sametime as CS
    from coverage_analysis.observer_lab.labels import DEFAULT_MAX_BARS, LABEL_CONVENTION_VERSION
    from market_observer.schema import OBSERVER_VERSION, SCHEMA_VERSION

    st = spec["sametime_spec"] or CS.SameTimeSpec()
    cfg = {"stage": spec["stage"], "market": spec["market"], "seed": spec["seed"], "limit": spec["limit"], "spec": asdict(st), "with_b": spec["with_b"], "observer_config": observer_config_hash,
           "max_label_bars": DEFAULT_MAX_BARS, "upstream_events": upstream_artifact_id, "configs": configs_hash(), "segments": SEGMENTS_VERSION}
    label = "|".join([LABEL_CONVENTION_VERSION, OBSERVER_VERSION, SCHEMA_VERSION, C3.CONTROLS3_PIPELINE_VERSION, CS.CONTROL_METHOD_VERSION, CS.GATE_VERSION, BF.BACKFILL_VERSION])
    return ArtifactFingerprint(data_hash, feature_code_hash(), control_code_hash(), config_hash(cfg), label, NOT_APPLICABLE)


def output_files(market: str, stage: str) -> list[str]:
    from coverage_analysis.observer_lab import backfill as BF
    from coverage_analysis.observer_lab import backfill_controls3 as C3

    if stage == "events":
        return [f"{market}/{f}" for f in (*BF.EVENT_FILES.values(), "opportunity_bars.parquet", "manifest.json")]
    sub = C3.SUBDIRS["a" if stage == "controls3_a" else "b"]
    return [f"{market}/{sub}/{f}" for f in (*BF.CONTROL_FILES.values(), C3.DIAG_FILE, "controls_manifest.json")]


# ---------------------------------------------------------------------------------------------- the worker entry (top-level => picklable; one process per segment)
def run_segment(spec: dict[str, Any]) -> dict[str, Any]:
    """spec: market, stage, out, seed, limit, data_root, p2root, force, adopt, with_b, sametime_spec (or None). Returns a small result dict (never raises for CACHE MISS)."""
    t0 = time.monotonic()
    market, stage = spec["market"], spec["stage"]
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    import entry_exit_quality as X

    from markets.phase2 import load_phase2_spec
    from markets.spec import CANONICALS, PHASE2_CANONICALS, load_market_spec

    mi = X.build_market_inputs(market, 0, spec["data_root"], spec["p2root"])
    if mi is None:
        return {"market": market, "stage": stage, "status": "NO_DATA", "note": "no data root with markets/manifest_<M>.json found (pass --phase2-root)"}
    ms = load_market_spec(market) if market in CANONICALS else load_phase2_spec(market) if market in PHASE2_CANONICALS else None
    if ms is None:
        raise ValueError(f"unknown market {market!r}")
    return run_segment_with_inputs(spec, mi, ms, t0=t0)


def run_segment_with_inputs(spec: dict[str, Any], mi: Any, ms: Any, *, t0: float | None = None) -> dict[str, Any]:
    """The segment logic on already loaded inputs (tests drive this with synthetic data; ``run_segment`` loads the real data first)."""
    from coverage_analysis.observer_lab import backfill as BF

    t0 = time.monotonic() if t0 is None else t0
    market, stage, out = spec["market"], spec["stage"], spec["out"]
    load_s = time.monotonic() - t0
    store = SegmentStore(out)
    sid = segment_id(market, stage)
    data_hash = BF.frame_fingerprint(mi.frame)
    ocfg = BF.observer_config_for(ms).config_hash()
    if stage == "events":
        fp = events_fingerprint(spec, data_hash, ocfg)
    else:
        up = store.read(segment_id(market, "events"))
        if not up or up.get("status") != "COMPLETE":
            raise RuntimeError(f"{sid}: the events segment of {market} is not committed (run it first)")
        fp = controls_fingerprint(spec, data_hash, ocfg, up["artifact_id"])
    lk = store.lookup(sid, fp)
    base = {"market": market, "stage": stage, "artifact_id": fp.artifact_id, "load_s": round(load_s, 2)}
    if lk.hit and not spec["force"]:
        return {**base, "status": "CACHE_HIT", "reason": "HIT", "wall_s": round(time.monotonic() - t0, 2), "built_wall_s": lk.manifest.get("wall_s") if lk.manifest else None}
    code = BF.code_identity()
    t1 = time.monotonic()
    status = "BUILT"
    adopted = False
    if spec["adopt"] and not spec["force"] and lk.reason == "NO_MANIFEST":
        # explicit opt-in: trust a COMPLETE legacy step output (same data / pipeline fingerprint of the step itself) without recomputing; recorded as unverified-code
        res = _run_step(spec, mi, ms, force=False, code=code)
        adopted = res.get("status_this_call") == "SKIPPED_COMPLETE"
        status = "ADOPTED" if adopted else "BUILT"
    else:
        _run_step(spec, mi, ms, force=True, code=code)
    step_s = time.monotonic() - t1
    files = output_files(market, stage)
    wall = time.monotonic() - t0
    store.commit(sid, fp, files, wall_s=wall, extra={"load_s": round(load_s, 2), "step_s": round(step_s, 2), "peak_memory_mb": BF.peak_memory_mb(), "miss_reason": lk.reason,
                                                    "adopted_unverified_code": adopted, "code": code})
    return {**base, "status": status, "reason": lk.reason, "wall_s": round(wall, 2), "step_s": round(step_s, 2), "peak_memory_mb": BF.peak_memory_mb()}


def _run_step(spec: dict[str, Any], mi: Any, ms: Any, *, force: bool, code: dict[str, Any]) -> dict[str, Any]:
    from coverage_analysis.observer_lab import backfill as BF
    from coverage_analysis.observer_lab import backfill_controls3 as C3
    from coverage_analysis.observer_lab.controls_sametime import SameTimeSpec

    stage = spec["stage"]
    if stage == "events":
        return BF.run_events_step(mi, ms, spec["out"], limit=spec["limit"], force=force, code=code)
    return C3.run_controls3_step(mi.frame, ms, mi.market, spec["out"], seed=spec["seed"], force=force, spec=spec["sametime_spec"] or SameTimeSpec(), with_b=spec["with_b"], code=code,
                                 only_set="a" if stage == "controls3_a" else "b")
