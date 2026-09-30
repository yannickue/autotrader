# ruff: noqa: E501
"""Real-data (GER40 dev frame) loaders, ~40 hand-written temporal specs, and the real-EventSet benchmark.

Shared with tests/temporal/test_real_events_*.py (``from scripts.bench_temporal_real import ...``).

    PYTHONPATH=src:. python scripts/bench_temporal_real.py --specs 2000 --workers 4 --screen 200
"""

from __future__ import annotations

import argparse
import io
import json
import random
import shutil
import time
from contextlib import redirect_stdout
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from alpha.common.dataset import POINT
from alpha.events.store import EventSet, build_events, load_or_build_events
from alpha.fast.store import FeatureStore
from alpha.temporal.frame import build_market_frame, frame_for_specs
from alpha.temporal.reference import MarketFrame
from alpha.temporal.spec import (
    Capture,
    Clause,
    StateMachineStrategySpec,
    StopRule,
    TargetRule,
    Transition,
    mirror,
)

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data/feature_store/v2i"
CONFIG = ROOT / "research/configs/ad1_discovery.json"


# ------------------------------------------------------------------------------ real data
@dataclass
class Real:
    cfg: dict
    plan: object
    df: object  # dev DataFrame (OOS bars removed)
    features: object
    events: EventSet
    frame: MarketFrame  # lazy arrays, TRAIN-only thresholds


def build_pipeline(df) -> tuple[object, EventSet]:
    """Uncached features + events for a bar DataFrame (used for prefixes / perturbed / slices)."""
    with redirect_stdout(io.StringIO()):
        features = FeatureStore.build(df.reset_index(drop=True), {"point_size": POINT})
    return features, build_events(features)


@lru_cache(maxsize=1)
def load_real() -> Real:
    from alpha.common.dataset import load_research_dataset
    from research.runners import ar2_fast

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(ROOT / cfg["dataset_root"])
    df = ar2_fast.dev_frame(ds.frame, plan)
    with redirect_stdout(io.StringIO()):
        features = FeatureStore.load_or_build(df, {"point_size": POINT}, CACHE_DIR)
    events = load_or_build_events(features, None, CACHE_DIR / "events")
    return Real(cfg, plan, df, features, events, build_market_frame(features, events, plan=plan))


def cleanup_cache() -> None:
    shutil.rmtree(CACHE_DIR, ignore_errors=True)


# ------------------------------------------------------------------------------ hand specs
def _c(kind, name, tf, **kw) -> Clause:
    return Clause(kind, name, tf, **kw)


def _ev(name, tf, variant="", **kw) -> Clause:
    return Clause("event", name, tf, variant=variant, **kw)


def _st(name, tf, **kw) -> Clause:
    return Clause("state", name, tf, **kw)


def _bd(name, reg, tol=0.0, variant="") -> Clause:
    return Clause("bound", name, "M5", reg=reg, tol_atr=tol, variant=variant)


def _ft(name, cmp, q) -> Clause:
    return Clause("feature", name, "M5", cmp=cmp, q=q)


def _spec(sid, anchor, acap, states, stop, target, *, context=(), expires=48, sw=None):
    return StateMachineStrategySpec(
        strategy_id=sid, version=1, direction="LONG", anchor=tuple(anchor),
        anchor_capture=tuple(acap), states=tuple(states), context=tuple(context),
        expires_after=expires, session_window=sw, stop=stop, target=target,
    )


_H1_UP = _st("TREND_UP", "H1")
_ZONE15 = _ev("ZONE_ENTER", "M15", "swing_cluster")
_NS = TargetRule("next_structure", levels=("h1_swing_high", "pdh", "session_high"),
                 fallback_r=2.0, min_space_r=1.5)
_NS2 = TargetRule("next_structure", levels=("m15_swing_high", "session_high"),
                  fallback_r=2.0, min_space_r=1.0)
_R2 = TargetRule("fixed_r", r=2.0)
_R15 = TargetRule("fixed_r", r=1.5)


def _section2(sid, src, w, stop_buf, target, n_tr=4, sw=None, expires=60):
    t = [
        Transition(_ev("SWEEP_LOW", "M5", src), within=w[0], invalidate=(_bd("BREAK_DN", "R0"),),
                   capture=(Capture("R1", "evx", "SWEEP_LOW"),)),
        Transition(_bd("RECLAIM_UP", "R0", variant="k3"), within=w[1],
                   invalidate=(_bd("BREAK_DN", "R1"),)),
        Transition(_ev("BOS_UP", "M5"), within=w[2], capture=(Capture("R2", "evl", "BOS_UP"),)),
        Transition(_bd("RETEST_HOLD_UP", "R2", tol=0.1), within=w[3]),
    ][:n_tr]
    return _spec(sid, (_H1_UP, _ZONE15), (Capture("R0", "lv", "ZONE_LO"),), t,
                 StopRule("register", reg="R1", buffer_atr=stop_buf, max_risk_atr=3.0),
                 target, sw=sw, expires=expires)


def hand_long_specs() -> list[StateMachineStrategySpec]:
    S: list[StateMachineStrategySpec] = []
    # section-2 example structure and variants
    S.append(_section2("h_ex_full", "prior20", (48, 24, 24, 12), 0.1, _NS, expires=96))
    S.append(_section2("h_ex_swing", "swing_low", (48, 24, 24, 12), 0.1, _R2, expires=96))
    S.append(_section2("h_ex_sess3", "session_low", (24, 12, 12, 8), 0.0, _R15, n_tr=3, expires=96))
    S.append(_section2("h_ex_pdl2", "pdl", (24, 8, 5, 3), 0.25, _R2, n_tr=2, expires=90))
    S.append(_section2("h_ex_p48", "prior48", (48, 24, 24, 12), 0.1, _NS2, n_tr=4, sw=(540, 1200), expires=96))
    S.append(_spec(
        "h_m5zone_sweep", (_ev("ZONE_ENTER", "M5", "prior_range"),), (Capture("R0", "lv", "ZONE_LO"),),
        (Transition(_ev("SWEEP_LOW", "M5", "prior20"), within=12, capture=(Capture("R1", "evx", "SWEEP_LOW"),)),
         Transition(_bd("RECLAIM_UP", "R0", variant="k6"), within=8, invalidate=(_bd("BREAK_DN", "R1"),))),
        StopRule("zone_edge", reg="R0", buffer_atr=0.1, max_risk_atr=3.0), _R2, expires=40))
    S.append(_spec(
        "h_swing_bos", (_H1_UP,), (Capture("R0", "bar_low"),),
        (Transition(_ev("SWING_LOW_CONF", "M5"), within=24, capture=(Capture("R1", "evl", "SWING_LOW_CONF"),)),
         Transition(_ev("BOS_UP", "M5"), within=12, invalidate=(_bd("BREAK_DN", "R1"),),
                    capture=(Capture("R2", "evl", "BOS_UP"),))),
        StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0), _R2, expires=60))
    S.append(_spec(
        "h_mom_resume", (_st("TREND_UP", "M15"),), (Capture("R0", "bar_low"),),
        (Transition(_ev("MOMENTUM_RESUME_UP", "M5"), within=12, capture=(Capture("R1", "evl", "MOMENTUM_RESUME_UP"),)),),
        StopRule("register", reg="R1", buffer_atr=0.25, max_risk_atr=3.0), _R2, expires=24))
    S.append(_spec(
        "h_dbl_bottom", (_ev("PATTERN_COMPLETE", "M5", "double_bottom"),), (Capture("R0", "evx", "PATTERN_COMPLETE"),),
        (Transition(_bd("BREAK_UP", "R0", tol=0.0), within=12),),
        StopRule("register", reg="R0", buffer_atr=0.1, max_risk_atr=4.0), _R2, expires=30))
    S.append(_spec(
        "h_tl_break", (_H1_UP,), (Capture("R0", "bar_low"),),
        (Transition(_ev("TRENDLINE_BREAK", "M5", "t10"), within=24,
                    capture=(Capture("R1", "evl", "TRENDLINE_BREAK"), Capture("R2", "lv", "TRENDLINE_VALUE"))),),
        StopRule("atr", atr_mult=1.5, max_risk_atr=3.0), _R2, expires=48))
    S.append(_spec(
        "h_zone_feat", (_ZONE15, _ft("atr_pct", "lt", 0.5)), (Capture("R0", "lv", "ZONE_LO"),),
        (Transition(_ev("SWEEP_LOW", "M5", "prior20"), within=12, capture=(Capture("R1", "evx", "SWEEP_LOW"),)),
         Transition(_bd("RECLAIM_UP", "R0", variant="k3"), within=5, invalidate=(_bd("BREAK_DN", "R1"),))),
        StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0), _NS2, expires=50))
    S.append(_spec(
        "h_range_retest", (_ev("ZONE_ENTER", "M15", "m15_range"), _H1_UP), (Capture("R0", "lv", "ZONE_LO"),),
        (Transition(_ev("BOS_UP", "M5"), within=24, capture=(Capture("R1", "evl", "BOS_UP"),)),
         Transition(_bd("RETEST_HOLD_UP", "R1", tol=0.1), within=8)),
        StopRule("swing", of="SWING_LOW_LVL", tf="M5", buffer_atr=0.1, max_risk_atr=3.0), _NS2, expires=60))
    S.append(_spec(
        "h_zone_exit_bos", (_ev("ZONE_ENTER", "M5", "prior_range"),), (Capture("R0", "lv", "ZONE_LO"),),
        (Transition(_ev("ZONE_EXIT", "M5", "prior_range"), within=12),
         Transition(_ev("BOS_UP", "M5"), within=12)),
        StopRule("zone_edge", reg="R0", buffer_atr=0.0, max_risk_atr=4.0), _R15, expires=48))
    S.append(_spec(
        "h_m15_sweep_choch", (_st("TREND_UP", "M15"), _ev("SWEEP_LOW", "M15", "prior20")),
        (Capture("R0", "evx", "SWEEP_LOW"),),
        (Transition(_ev("CHOCH_UP", "M5"), within=24, capture=(Capture("R1", "evl", "CHOCH_UP"),)),),
        StopRule("register", reg="R0", buffer_atr=0.1, max_risk_atr=4.0), _R2, expires=48))
    S.append(_spec(
        "h_pdl_sweep_win", (_H1_UP,), (Capture("R0", "bar_low"),),
        (Transition(_ev("SWEEP_LOW", "M5", "pdl"), within=48, capture=(Capture("R1", "evx", "SWEEP_LOW"),)),),
        StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0), _R15, expires=60, sw=(540, 1020)))
    S.append(_spec(
        "h_sess_bos_ctx", (_H1_UP,), (Capture("R0", "bar_low"),),
        (Transition(_ev("SWEEP_LOW", "M5", "session_low"), within=24, capture=(Capture("R1", "evx", "SWEEP_LOW"),)),
         Transition(_ev("BOS_UP", "M5"), within=12)),
        StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0), _R2,
        context=(_st("TREND_UP", "D1"),), expires=60))
    S.append(_spec(
        "h_mom_guard", (_st("TREND_UP", "H1", op="HOLD", arg=3),), (Capture("R0", "bar_low"),),
        (Transition(_ev("MOMENTUM_RESUME_UP", "M5"), within=8, guards=(_ft("atr_pct", "gt", 0.3),),
                    capture=(Capture("R1", "evl", "MOMENTUM_RESUME_UP"),)),),
        StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0), _R2, expires=24))
    S.append(_spec(
        "h_touch_bos", (_H1_UP, _ZONE15), (Capture("R0", "lv", "ZONE_LO"),),
        (Transition(_bd("TOUCH", "R0", tol=0.1), within=12, capture=(Capture("R1", "bar_low"),)),
         Transition(_ev("BOS_UP", "M5"), within=12, invalidate=(_bd("BREAK_DN", "R1"),))),
        StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0), _R2, expires=50))
    S.append(_spec(
        "h_five_chain", (_H1_UP, _ZONE15), (Capture("R0", "lv", "ZONE_LO"),),
        (Transition(_ev("SWEEP_LOW", "M5", "swing_low"), within=12, capture=(Capture("R1", "evx", "SWEEP_LOW"),)),
         Transition(_bd("RECLAIM_UP", "R0", variant="k3"), within=5, invalidate=(_bd("BREAK_DN", "R1"),)),
         Transition(_ev("BOS_UP", "M5"), within=8, guards=(_st("TREND_UP", "M15", op="HOLD", arg=2),),
                    capture=(Capture("R2", "evl", "BOS_UP"),)),
         Transition(_bd("RETEST_HOLD_UP", "R2", tol=0.1), within=6),
         Transition(_ev("MOMENTUM_RESUME_UP", "M5", op="BEFORE", arg=8), within=12)),
        StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0), _NS, expires=90))
    S.append(_spec(
        "h_since_enter", (_H1_UP,), (Capture("R0", "bar_low"),),
        (Transition(_ev("SWING_LOW_CONF", "M5"), within=24, capture=(Capture("R1", "evl", "SWING_LOW_CONF"),)),
         Transition(_ev("MOMENTUM_RESUME_UP", "M5"), within=24, guards=(_ev("SWING_LOW_CONF", "M5", op="SINCE_ENTER"),))),
        StopRule("register", reg="R1", buffer_atr=0.1, max_risk_atr=3.0), _R2, expires=60))
    return S


def hand_specs() -> list[StateMachineStrategySpec]:
    """20 realistic LONG specs plus their 20 SHORT mirrors (40 total)."""
    longs = hand_long_specs()
    return [*longs, *(mirror(s) for s in longs)]


# ------------------------------------------------------------------------------ benchmark
def _mb(b: int) -> str:
    return f"{b / 2**20:.0f} MB"


def main() -> None:
    from alpha.common.protocol import SplitPlan  # noqa: F401
    from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec
    from alpha.fast.screen import light_screen
    from alpha.temporal import batch
    from alpha.temporal.evaluate import evaluate_temporal_many
    from research.runners import ar2_fast
    from scripts.bench_temporal import random_specs

    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--screen", type=int, default=200)
    ap.add_argument("--cache-mb", type=int, default=512)
    ap.add_argument("--keep-cache", action="store_true")
    a = ap.parse_args()

    t0 = time.perf_counter()
    real = load_real()
    n = len(real.frame)
    specs = random_specs(a.specs, a.seed)
    frame = frame_for_specs(real.frame, specs)
    mb = sum(np.asarray(v).nbytes for v in frame.arrays.values()) / 2**20
    print(f"real dev frame: {n} bars, {len(specs)} specs, batch frame {len(frame.arrays)} arrays "
          f"({mb:.0f} MB) of {len(real.events)} event arrays, {len(frame.thresholds)} thresholds "
          f"(setup {time.perf_counter() - t0:.1f}s)")
    evaluate_temporal_many(specs[:8], frame)  # numba warm-up / cache load

    st = batch.BatchStats()
    t0 = time.perf_counter()
    res = evaluate_temporal_many(specs, frame, cache_bytes=a.cache_mb << 20, stats=st)
    dt = time.perf_counter() - t0
    counts = np.array([len(r.candidates.decision_idx) for r in res])
    days = len(np.unique(real.features["berlin_day_id"]))
    print(f"1 worker, prefix cache: {len(specs) / dt:.1f} specs/s ({dt:.1f}s) hit_rate={st.hit_rate:.2f} "
          f"stages {st.stages_computed}/{st.stage_demands} peak cache {_mb(st.peak_cache_bytes)} "
          f"peak RSS {_mb(batch.peak_rss_bytes())}")
    print(f"specs with >=1 candidate: {(counts > 0).sum()}/{len(specs)} = {(counts > 0).mean():.1%}; "
          f"candidates total {counts.sum()}, median {np.median(counts):.0f}, p90 {np.percentile(counts, 90):.0f}, "
          f"max {counts.max()}; implied candidates/day among non-empty specs: median "
          f"{np.median(counts[counts > 0]) / days:.2f}, p90 {np.percentile(counts[counts > 0], 90) / days:.2f} "
          f"(over {days} days)")

    if a.workers > 1:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor

        with batch.SharedFrame(frame) as sh, ProcessPoolExecutor(
            max_workers=a.workers, mp_context=mp.get_context("spawn")
        ) as ex:
            evaluate_temporal_many(specs[: a.workers * 2], frame, workers=a.workers, executor=ex, shared=sh)
            st3 = batch.BatchStats()
            t0 = time.perf_counter()
            res3 = evaluate_temporal_many(specs, frame, workers=a.workers, executor=ex, shared=sh,
                                          cache_bytes=a.cache_mb << 20, stats=st3)
            dt3 = time.perf_counter() - t0
        same = all(x.to_bytes() == y.to_bytes() for x, y in zip(res, res3, strict=True))
        print(f"{a.workers} workers: {len(specs) / dt3:.1f} specs/s ({dt3:.1f}s) hit_rate={st3.hit_rate:.2f} "
              f"identical={same} worker peak RSS {[_mb(x) for x in st3.worker_peak_rss]}")

    # compile-target contract: candidates -> simulate_fast -> light_screen (informational only)
    if a.screen:
        cfg, plan = real.cfg, real.plan
        market, dates = ar2_fast._market(real.features), ar2_fast._dates(real.features)
        sizing, rules = SizingSpec(**cfg["sizing"]), SimRules(**cfg["rules"])
        rng = random.Random(a.seed)
        pick = [i for i in range(len(res)) if counts[i] > 0]
        rng.shuffle(pick)
        pick = pick[: a.screen]
        t0 = time.perf_counter()
        rows = []
        for i in pick:
            r = light_screen(market, res[i].candidates, COST_SCENARIOS["BASE"], plan,
                             dates=dates, sizing=sizing, rules=rules)
            rows.append((r.train,))
        dts = time.perf_counter() - t0
        tr = [x[0] for x in rows]
        ntr = np.array([x.n_trades for x in tr])
        ex = np.array([x.expectancy_r for x in tr if x.expectancy_r is not None], dtype=float)
        pf = np.array([x.profit_factor for x in tr if x.profit_factor is not None], dtype=float)
        print(f"light_screen on {len(pick)} candidate sets ({dts:.1f}s, INFORMATIONAL, no selection): "
              f"train trades median {np.median(ntr):.0f} (p10 {np.percentile(ntr, 10):.0f}, p90 {np.percentile(ntr, 90):.0f}); "
              f"train expectancy_r median {np.median(ex):+.3f} (p10 {np.percentile(ex, 10):+.3f}, p90 {np.percentile(ex, 90):+.3f}); "
              f"train PF median {np.median(pf):.2f}; sets with >=60 train trades {(ntr >= 60).sum()}")
    if not a.keep_cache:
        cleanup_cache()


if __name__ == "__main__":
    main()
