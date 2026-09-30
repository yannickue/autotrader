# ruff: noqa: E501
"""Survival stage on SYNTHETIC markets: planted edge survives, noise fails, call-once is enforced, dry run is inert."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from alpha.common.sim import CostScenario, SimRules, SizingSpec
from alpha.discovery import v2_select as vs
from alpha.discovery.folds import make_folds
from alpha.discovery.temporal_archetypes import random_genome
from alpha.discovery.temporal_genome import EventPool, canonical_hash, canonicalize
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays
from research.runners import v2_survival as sv

BARS, N_DAYS = 288, 100
N = BARS * N_DAYS
DIST = 2.0
CFG_PATH = Path(sv.DEFAULT_CONFIG)


def _cfg() -> dict:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    cfg["markets"] = ["P", "N"]
    cfg["test_fold_indices"] = [1, 2]
    cfg["baselines"]["draws"] = 8
    cfg["neighbours"]["max_per_finalist"] = 10
    return cfg


def _decisions() -> np.ndarray:
    i = np.arange(N)
    minute = (i % BARS) * 5
    return i[(i % 24 == 0) & (minute >= 570) & (minute < 1000) & (i < N - 40)]


def _synthetic(planted: bool, seed: int) -> tuple[sv.SurvivalContext, CandidateArrays]:
    rng = np.random.default_rng(seed)
    dec = _decisions()
    direction = np.where(np.arange(len(dec)) % 2 == 0, 1, -1).astype(np.int8)
    step = rng.normal(0.0, 0.2, N)
    if planted:  # after every decision the price runs 1.5/bar (10 bars) in the trade direction
        for i, d in zip(dec, direction, strict=True):
            step[i + 1:i + 11] = 1.5 * d
    c = 1000.0 + np.cumsum(step)
    o = np.r_[c[0], c[:-1]]
    h, low = np.maximum(o, c) + 0.05, np.minimum(o, c) - 0.05
    idx = np.arange(N)
    market = MarketArrays(o, h, low, c, np.full(N, 0.05), ((idx % BARS) * 5).astype(np.int64),
                          (idx // BARS).astype(np.int64), np.ones(N, dtype=bool))
    dates = (np.datetime64("2025-02-03", "D") + (idx // BARS)).astype("datetime64[D]")
    folds = make_folds(dates, 3, 5, 0, initial_train_frac=0.4, min_test_days=5)
    costs = {"BASE": CostScenario("BASE", 1.0, 0.02, 0.0), "COMBINED_ADVERSE": CostScenario("COMBINED_ADVERSE", 2.0, 0.05, 0.0)}
    sctx = sv.SurvivalContext("P" if planted else "N", None, market, folds, SizingSpec(min_lot=0.01, lot_step=0.01, min_risk_pts=0.5, max_risk_pts=60.0),
                              SimRules(max_trades_per_day=6, max_entry_spread_pts=10.0), costs, None, EventPool.full(),
                              np.full(N, 2.0), 0.05)
    cands = CandidateArrays(dec, direction, c[dec] - direction * DIST, np.full(len(dec), np.nan), np.full(len(dec), 1.5),
                            np.full(len(dec), EXIT_FIXED_R, dtype=np.int8))
    return sctx, cands


@pytest.fixture(scope="module")
def world():
    p, cp = _synthetic(True, 1)
    n, cn = _synthetic(False, 2)
    g = random_genome(np.random.default_rng(0), EventPool.full())
    return {"P": (p, cp), "N": (n, cn), "genome": g, "hash": canonical_hash(canonicalize(g))}


@pytest.fixture()
def patched(monkeypatch, world):
    """genome_candidates -> the planted candidate stream of the market being evaluated (any genome / neighbour)."""
    by_ctx = {id(world["P"][0]): world["P"][1], id(world["N"][0]): world["N"][1]}
    monkeypatch.setattr(sv, "genome_candidates", lambda sctx, genome: by_ctx[id(sctx)])
    return world


def _frozen(world, markets=("P", "N")):
    sels = {m: {"config": vs.SelectConfig().to_dict(), "n_eligible": 1, "n_clusters": 1,
                "finalists": [{"rank": 0, "hash": world["hash"], "cluster_id": 0, "objectives": {"e_adv": 0.1}}]}
            for m in markets}
    return vs.build_frozen_payload(sels)


def _run(world, tmp_path, execute=True, cfg=None):
    cfg = cfg or _cfg()
    frozen = _frozen(world)
    genomes = {m: {world["hash"]: world["genome"]} for m in ("P", "N")}
    ctxs = {"P": world["P"][0], "N": world["N"][0]}
    led = sv.SurvivalLedger(tmp_path / "ledger.json")
    return sv.run_survival(cfg, frozen, genomes, lambda m: ctxs[m], led, tmp_path / "out", execute), led, frozen


def test_planted_edge_survives_and_noise_fails(patched, tmp_path):
    out, _, _ = _run(patched, tmp_path)
    assert out["label"] == "survival stage - not for selection"
    rp = out["results"]["P"][patched["hash"]]
    rn = out["results"]["N"][patched["hash"]]
    assert rp["evaluable"] and all(v for k, v in rp["checks"].items() if k != "cross_market_any_positive"), rp["checks"]
    assert rp["variants"]["ADVERSE"]["pooled"]["e"] > 0.5 and rp["variants"]["ADVERSE"]["persistence"]["folds_positive"] == 2
    assert rp["neighbours"]["share_pos_test"] == 1.0 and rp["neighbours"]["generated"] > 0
    assert rp["baseline"]["z_vs_random_long_short"] > 1.645
    # the planted structure does NOT transfer to the noise market; the noise market's own structure fails
    assert rp["cross_market"]["per_market"]["N"]["test_pooled"]["e"] < 0.1
    assert rp["cross_market"]["markets_positive_pooled_test"] == 0
    assert rn["n_core_checks_passed"] < len(rn["checks"]) - 1
    assert not rn["checks"]["pooled_t_ge_min"] and not rn["checks"]["fold_sign_consistent"]


def test_cost_stress_and_entry_delay_order(patched, tmp_path):
    out, _, _ = _run(patched, tmp_path)
    v = out["results"]["P"][patched["hash"]]["variants"]
    assert v["BASE"]["pooled"]["e"] >= v["ADVERSE"]["pooled"]["e"] >= v["EXIT_SHOCK"]["pooled"]["e"] - 1e-9
    assert v["DELAY"]["pooled"]["n_trades"] > 0


def test_trials_are_counted_in_the_cumulative_ledger(patched, tmp_path):
    out, _, _ = _run(patched, tmp_path)
    led = out["cumulative_ledger"]
    assert led["prior_trials"] == 30310
    assert led["cumulative_trials"] == led["trials_before_survival"] + led["survival_evaluations"]
    n_p = out["results"]["P"][patched["hash"]]["neighbours"]["generated"]
    n_n = out["results"]["N"][patched["hash"]]["neighbours"]["generated"]
    assert led["survival_evaluations"] == (1 + n_p) + (1 + n_n) + 2  # two homes (+neighbours) + two cross-market instances
    assert led["survival_unique_specs_new_upper_bound"] >= 1 + n_p
    assert (tmp_path / "out" / "survival_results.json").exists() and (tmp_path / "out" / "survival_summary.md").exists()


def test_call_once_per_finalist_hash_is_enforced_and_persisted(patched, tmp_path):
    _, led, frozen = _run(patched, tmp_path)
    assert led.calls[frozen["finalist_hash"]]["status"] == "completed"
    with pytest.raises(sv.SurvivalAlreadyRun):
        _run(patched, tmp_path)
    # a fresh ledger object reading the same file also refuses
    with pytest.raises(sv.SurvivalAlreadyRun):
        sv.SurvivalLedger(tmp_path / "ledger.json").acquire(frozen["finalist_hash"])
    # a DIFFERENT frozen list is a different call
    sv.SurvivalLedger(tmp_path / "ledger.json").acquire("another-hash")


def test_crash_still_consumes_the_call(patched, tmp_path):
    cfg = _cfg()
    frozen = _frozen(patched)
    genomes = {m: {patched["hash"]: patched["genome"]} for m in ("P", "N")}
    led = sv.SurvivalLedger(tmp_path / "ledger.json")

    def boom(m):
        raise RuntimeError("data load failed")

    with pytest.raises(RuntimeError):
        sv.run_survival(cfg, frozen, genomes, boom, led, tmp_path / "out", True)
    assert led.calls[frozen["finalist_hash"]]["status"] == "started"
    with pytest.raises(sv.SurvivalAlreadyRun):
        sv.run_survival(cfg, frozen, genomes, lambda m: None, led, tmp_path / "out", True)


def test_dry_run_reads_no_data_and_writes_no_ledger(world, tmp_path):
    cfg = _cfg()
    frozen = _frozen(world)

    def loader(m):
        raise AssertionError("dry run must not load market data")

    led = sv.SurvivalLedger(tmp_path / "ledger.json")
    out = sv.run_survival(cfg, frozen, {}, loader, led, tmp_path / "out", False)
    assert out["dry_run"] and out["plan"]["finalists_total"] == 2 and not (tmp_path / "ledger.json").exists()
    assert out["plan"]["test_fold_indices"] == [1, 2]
    # the dry run can be repeated freely and does not consume the call
    sv.run_survival(cfg, frozen, {}, loader, led, tmp_path / "out", False)


def test_tampered_frozen_list_is_refused_before_the_ledger_is_touched(world, tmp_path):
    frozen = _frozen(world)
    frozen["markets"]["P"]["finalists"][0]["hash"] = "tampered"
    led = sv.SurvivalLedger(tmp_path / "ledger.json")
    with pytest.raises(vs.FrozenSelectionError):
        sv.run_survival(_cfg(), frozen, {}, lambda m: None, led, tmp_path / "out", True)
    assert not (tmp_path / "ledger.json").exists()


def test_delayed_shifts_decisions_and_drops_the_tail():
    dec = np.array([1, 5, 9])
    c = CandidateArrays(dec, np.ones(3, np.int8), np.zeros(3), np.full(3, np.nan), np.full(3, 1.5), np.full(3, EXIT_FIXED_R, np.int8))
    d = sv.delayed(c, 1, 11)
    assert d.decision_idx.tolist() == [2, 6]  # 9 + 1 = 10 has no fill bar (n = 11)
    assert sv.delayed(c, 0, 11) is c


def test_bundle_persistence_counts(world):
    sctx, cands = world["P"]
    from alpha.fast.sim import simulate_fast

    tr = simulate_fast(sctx.market, cands, sctx.costs["COMBINED_ADVERSE"], sctx.sizing, sctx.rules, None)
    b = sv.bundle(tr, sctx, [1, 2])
    assert len(b["per_fold"]) == 2 and b["persistence"]["folds"] == 2
    assert b["pooled"]["n_trades"] == sum(f["n_trades"] for f in b["per_fold"])


def test_neighbours_are_pm_one_step_and_distinct(world):
    g = world["genome"]
    neigh = sv.neighbour_genomes(g, EventPool.full(), 50)
    assert neigh and len({canonical_hash(n) for n in neigh}) == len(neigh) and world["hash"] not in {canonical_hash(n) for n in neigh}
    assert len(sv.neighbour_genomes(g, EventPool.full(), 3)) <= 3


def test_real_kernel_path_runs_end_to_end_on_a_synthetic_frame():
    """Unpatched kernel: the evaluation plumbing works with real genomes on a synthetic MarketFrame."""
    from alpha.discovery.temporal_evaluate import market_from_frame
    from scripts.bench_temporal import synth_frame

    frame = synth_frame(60_000, seed=3, density=12.0, run_len=(60, 200))
    market = market_from_frame(frame)
    dates = (np.datetime64("2025-02-03", "D") + (np.arange(60_000) // 288)).astype("datetime64[D]")
    folds = make_folds(dates, 2, 5, 6, initial_train_frac=0.4, min_test_days=40)
    sctx = sv.SurvivalContext("S", frame, market, folds, SizingSpec(min_risk_pts=0.5, max_risk_pts=60.0), SimRules(),
                              {"BASE": CostScenario("BASE", 1.0, 0.5, 0.0), "COMBINED_ADVERSE": CostScenario("COMBINED_ADVERSE", 2.0, 1.5, 1.0)},
                              None, EventPool.full(), np.asarray(frame.atr), 0.05)
    cfg = _cfg()
    cfg["test_fold_indices"] = [1]
    rng = np.random.default_rng(4)
    seen_eval = 0
    for _ in range(6):
        g = random_genome(rng, EventPool.full())
        rec, n_ev = sv.evaluate_home(sctx, g, cfg, 1)
        assert n_ev >= 1
        if rec["evaluable"]:
            seen_eval += 1
            assert set(rec["variants"]) == {"ADVERSE", "BASE", "EXIT_SHOCK", "DELAY"} and "checks" in rec
            away = sv.evaluate_away(sctx, g, cfg)
            assert away["evaluable"] and "test_pooled" in away
    assert seen_eval >= 1
