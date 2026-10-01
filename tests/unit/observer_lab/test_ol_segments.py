# ruff: noqa: E501
"""Segmented backfill (research_speed): bit-identical to the monolithic run, CACHE HIT on an identical fingerprint, explicit invalidation on a changed
data / code / config / version, abort -> no half artifacts -> resume recomputes only the unfinished segment."""

from __future__ import annotations

import contextlib
import hashlib
import shutil
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from ol_backfill_support import (
    CODE,
    MARKET,
    make_frame,
    make_inputs,
    make_mspec,
    patched_generators,
)

from coverage_analysis.observer_lab import backfill as BF
from coverage_analysis.observer_lab import backfill_controls3 as B3
from coverage_analysis.observer_lab import backfill_segments as SEG
from coverage_analysis.observer_lab import controls_sametime as CS
from research_speed.segments import SegmentStore

WIDE = CS.SameTimeSpec(atr_pct_band=0.35, spread_pct_band=0.6)


def _long_world():
    frame = make_frame(n=45 * 288, t0=pd.Timestamp("2026-06-08 00:00", tz="UTC"), seed=21)
    mi = make_inputs(frame)
    ev_bars = []
    rng = np.random.default_rng(4)
    for d in range(2, 44):
        ev_bars += sorted(int(d * 288 + 100 + x) for x in rng.choice(60, size=1, replace=False))
    return frame, mi, ev_bars


@contextlib.contextmanager
def _patched(ev_bars):
    old = BF.generate_candidates, BF.describe_candidate

    def gen(d, spec, thr):
        idx = np.array(ev_bars, dtype=np.int64)
        dirs = np.where(np.arange(len(idx)) % 2 == 0, 1, -1).astype(np.int8)
        stops = d.c[idx] - dirs * 2.0 * d.atr[idx]
        return SimpleNamespace(decision_idx=idx, direction=dirs, stop=stops, target=np.full(len(idx), np.nan), target_r=np.full(len(idx), np.nan), exit_kind=np.zeros(len(idx), dtype=np.int8))

    BF.generate_candidates, BF.describe_candidate = gen, (lambda d, spec, i, direction: {"break_bar_offset": 2})  # type: ignore[assignment]
    try:
        yield
    finally:
        BF.generate_candidates, BF.describe_candidate = old


def _spec(out, stage, **kw):
    return {"market": MARKET, "stage": stage, "out": str(out), "seed": 1, "limit": None, "data_root": None, "p2root": None, "force": False, "adopt": False, "with_b": True, "sametime_spec": WIDE, **kw}


def _parquet_hashes(mdir: Path) -> dict[str, str]:
    return {p.relative_to(mdir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(mdir.rglob("*.parquet"))}


@lru_cache(maxsize=1)
def _world_runs():
    """One monolithic run and one segmented run of the same synthetic world (shared by the tests of this module)."""
    import tempfile

    frame, mi, ev_bars = _long_world()
    ms = make_mspec()
    mono, seg = Path(tempfile.mkdtemp(prefix="seg_mono_")), Path(tempfile.mkdtemp(prefix="seg_seg_"))
    with _patched(ev_bars):
        BF.run_events_step(mi, ms, mono, code=CODE)
        B3.run_controls3_step(frame, ms, MARKET, mono, seed=1, code=CODE, spec=WIDE)
        res = {s: SEG.run_segment_with_inputs(_spec(seg, s), mi, ms) for s in SEG.STAGES}
    return {"mono": mono, "seg": seg, "res": res, "frame": frame, "mi": mi, "ms": ms, "ev_bars": ev_bars}


def test_segmented_run_is_bit_identical_to_the_monolithic_run():
    w = _world_runs()
    assert {k: v["status"] for k, v in w["res"].items()} == {"events": "BUILT", "controls3_a": "BUILT", "controls3_b": "BUILT"}
    a, b = _parquet_hashes(w["mono"] / MARKET), _parquet_hashes(w["seg"] / MARKET)
    assert a == b and len(a) >= 14  # events (5 incl. opportunity bars) + 2 x (4 control files + diag)
    for sub in ("controls3", "controls3_b"):
        ma = pd.read_json(w["mono"] / MARKET / sub / "controls_manifest.json", typ="series")
        mb = pd.read_json(w["seg"] / MARKET / sub / "controls_manifest.json", typ="series")
        assert ma["n_controls"] == mb["n_controls"] and ma["market_status"] == mb["market_status"] and ma["fingerprint"] == mb["fingerprint"]
    assert not any(p.name.startswith(("_stage", "_parts")) or p.name.endswith("._tmp") for p in (w["seg"] / MARKET).iterdir())  # no staging leftovers


def test_second_run_is_a_cache_hit_without_touching_the_files():
    w = _world_runs()
    mdir = w["seg"] / MARKET
    before = {p: p.stat().st_mtime_ns for p in mdir.rglob("*.parquet")}
    with _patched(w["ev_bars"]):
        again = {s: SEG.run_segment_with_inputs(_spec(w["seg"], s), w["mi"], w["ms"]) for s in SEG.STAGES}
    assert {k: v["status"] for k, v in again.items()} == {s: "CACHE_HIT" for s in SEG.STAGES}
    assert {p: p.stat().st_mtime_ns for p in mdir.rglob("*.parquet")} == before
    assert again["events"]["artifact_id"] == w["res"]["events"]["artifact_id"]


def test_changed_data_code_config_or_version_never_hits(monkeypatch):
    w = _world_runs()
    store = SegmentStore(w["seg"])
    ocfg = BF.observer_config_for(w["ms"]).config_hash()
    base_data = SEG.data_identity(w["mi"])  # frame bytes + eval_from
    spec = _spec(w["seg"], "events")
    ok = SEG.events_fingerprint(spec, base_data, ocfg)
    assert store.lookup(f"{MARKET}/events", ok).hit
    # data: one close value differs
    f2 = w["frame"].copy()
    f2.loc[100, "close"] += 0.01
    miss = store.lookup(f"{MARKET}/events", SEG.events_fingerprint(spec, SEG.data_identity(SimpleNamespace(frame=f2, eval_from=w["mi"].eval_from)), ocfg))
    assert not miss.hit and miss.reason == "FINGERPRINT_CHANGED:data_hash"
    # feature code
    monkeypatch.setattr(SEG, "feature_code_hash", lambda: "edited-feature-code")
    miss = store.lookup(f"{MARKET}/events", SEG.events_fingerprint(spec, base_data, ocfg))
    assert not miss.hit and miss.reason == "FINGERPRINT_CHANGED:feature_code_hash"
    monkeypatch.undo()
    # config (limit) and observer config
    assert store.lookup(f"{MARKET}/events", SEG.events_fingerprint({**spec, "limit": 5}, base_data, ocfg)).reason == "FINGERPRINT_CHANGED:config_hash"
    assert store.lookup(f"{MARKET}/events", SEG.events_fingerprint(spec, base_data, "other-observer-config")).reason == "FINGERPRINT_CHANGED:config_hash"
    # version of the labels
    monkeypatch.setattr(BF, "EVENTS_PIPELINE_VERSION", "observer-backfill-events-NEXT")
    assert store.lookup(f"{MARKET}/events", SEG.events_fingerprint(spec, base_data, ocfg)).reason == "FINGERPRINT_CHANGED:label_version"
    monkeypatch.undo()
    # controls: control code, seed, and the upstream events artifact
    up = store.read(f"{MARKET}/events")["artifact_id"]
    cspec = _spec(w["seg"], "controls3_a")
    assert store.lookup(f"{MARKET}/controls3_a", SEG.controls_fingerprint(cspec, base_data, ocfg, up)).hit
    monkeypatch.setattr(SEG, "control_code_hash", lambda: "edited-control-code")
    assert store.lookup(f"{MARKET}/controls3_a", SEG.controls_fingerprint(cspec, base_data, ocfg, up)).reason == "FINGERPRINT_CHANGED:control_code_hash"
    monkeypatch.undo()
    assert store.lookup(f"{MARKET}/controls3_a", SEG.controls_fingerprint({**cspec, "seed": 2}, base_data, ocfg, up)).reason == "FINGERPRINT_CHANGED:config_hash"
    assert store.lookup(f"{MARKET}/controls3_a", SEG.controls_fingerprint(cspec, base_data, ocfg, "a-new-events-artifact")).reason == "FINGERPRINT_CHANGED:config_hash"


def test_feature_code_closure_excludes_control_only_modules_and_the_control_closure_includes_them():
    from research_speed.importgraph import closure

    feat = {p.name for p in closure([SEG._p("src/coverage_analysis/observer_lab/backfill.py")], SEG.ROOTS, exclude=[SEG._p(x) for x in SEG._CONTROL_ONLY])}
    ctl = {p.name for p in closure([SEG._p("src/coverage_analysis/observer_lab/backfill_controls3.py")], SEG.ROOTS)}
    assert "controls_sametime.py" not in feat and "backfill_controls3.py" not in feat
    assert {"observer.py", "labels.py", "controls.py"} <= feat
    assert {"controls_sametime.py", "backfill.py", "observer.py"} <= ctl


def test_aborted_events_step_leaves_no_final_files_and_no_manifest(tmp_path, monkeypatch):
    frame = make_frame()
    mi, ms = make_inputs(frame), make_mspec()

    def boom(*a, **k):
        raise RuntimeError("simulated abort")

    with patched_generators():
        monkeypatch.setattr(BF, "_assemble_files", boom)
        with pytest.raises(RuntimeError, match="simulated abort"):
            SEG.run_segment_with_inputs(_spec(tmp_path, "events"), mi, ms)
        mdir = tmp_path / MARKET
        assert not any((mdir / f).exists() for f in (*BF.EVENT_FILES.values(), "manifest.json"))
        assert SegmentStore(tmp_path).lookup(f"{MARKET}/events", SEG.events_fingerprint(_spec(tmp_path, "events"), BF.frame_fingerprint(frame), BF.observer_config_for(ms).config_hash())).reason == "NO_MANIFEST"
        monkeypatch.undo()
        ok = SEG.run_segment_with_inputs(_spec(tmp_path, "events"), mi, ms)  # resume = a clean rebuild
    assert ok["status"] == "BUILT" and (mdir / "manifest.json").is_file()


def test_abort_in_one_control_set_keeps_the_finished_segments_and_a_rerun_builds_only_the_missing_one(tmp_path, monkeypatch):
    w = _world_runs()
    out = tmp_path / "run"
    shutil.copytree(w["seg"], out)  # start from the finished events + set A segments of the shared world, drop set B (= the segment that gets aborted)
    shutil.rmtree(out / MARKET / "controls3_b")
    (out / "_segments" / f"{MARKET}__controls3_b.json").unlink()
    with _patched(w["ev_bars"]):
        real = BF._observe_pass

        def failing(*a, **k):
            raise RuntimeError("simulated abort in set B")

        monkeypatch.setattr(BF, "_observe_pass", failing)
        with pytest.raises(RuntimeError, match="set B"):
            SEG.run_segment_with_inputs(_spec(out, "controls3_b"), w["mi"], w["ms"])
        monkeypatch.setattr(BF, "_observe_pass", real)
        mdir = out / MARKET
        assert not (mdir / "controls3_b").exists() and not (out / "_segments" / f"{MARKET}__controls3_b.json").exists()  # no half set, no commit marker
        done = {k: v for k, v in _parquet_hashes(mdir).items() if not k.startswith("controls3_b")}
        res = {s: SEG.run_segment_with_inputs(_spec(out, s), w["mi"], w["ms"]) for s in SEG.STAGES}
    assert [res[s]["status"] for s in SEG.STAGES] == ["CACHE_HIT", "CACHE_HIT", "BUILT"]
    assert {k: v for k, v in _parquet_hashes(mdir).items() if not k.startswith("controls3_b")} == done  # finished segments untouched
    assert _parquet_hashes(mdir)["controls3_b/controls.parquet"] == _parquet_hashes(w["seg"] / MARKET)["controls3_b/controls.parquet"]  # the resumed set equals the uninterrupted one


def test_plan_orders_events_before_their_controls_and_splits_sets():
    plan = SEG.plan_segments(["A", "B"], ("events", "controls"), True)
    assert [(m, s) for m, s, _ in plan] == [("A", "events"), ("B", "events"), ("A", "controls3_a"), ("A", "controls3_b"), ("B", "controls3_a"), ("B", "controls3_b")]
    assert dict(((m, s), d) for m, s, d in plan)[("A", "controls3_b")] == ("A/events",)
    assert [s for _, s, _ in SEG.plan_segments(["A"], ("controls",), False)] == ["controls3_a"]
