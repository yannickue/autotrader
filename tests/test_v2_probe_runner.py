# ruff: noqa: E501
"""V2 probe runner on a SYNTHETIC frame: determinism, ledger header with prior counts, sealing, report schema."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import numpy as np
import pytest

from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec
from alpha.discovery.folds import SearchSplitPlan, fold_report, make_folds
from alpha.discovery.temporal_evaluate import market_from_frame
from alpha.discovery.temporal_genome import EventPool
from research.runners import v2_probe
from scripts.bench_temporal import synth_frame

N = 60_000
CFG_PATH = Path(v2_probe.DEFAULT_CONFIG)


def _cfg() -> dict:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    cfg["search"].update(deap_pop=20, deap_gens=2)
    cfg["min_train_trades"] = 15
    cfg["baselines"]["draws"] = 3
    cfg["confluence"]["top_k"] = 10
    cfg["folds"].update(n_folds=2, purge_bars=6)
    return cfg


@pytest.fixture(scope="module")
def frame():
    return synth_frame(N, seed=3, density=12.0, run_len=(60, 200))


def _ctx(frame, cfg) -> v2_probe.ProbeContext:
    dates = (np.datetime64("2025-02-03", "D") + (np.arange(N) // 288)).astype("datetime64[D]")
    f = cfg["folds"]
    folds = make_folds(dates, f["n_folds"], f["embargo_days"], f["purge_bars"],
                       initial_train_frac=f["initial_train_frac"], min_test_days=f["min_test_days"])
    plan = SearchSplitPlan.from_folds(dates, folds)
    return v2_probe.ProbeContext(
        "SYNTH", lambda: frame, market_from_frame(frame), dates, plan, fold_report(dates, folds, f["embargo_days"]),
        EventPool.full(), SizingSpec(min_risk_pts=0.5, max_risk_pts=60.0), SimRules(),
        {n: COST_SCENARIOS[n] for n in ("BASE", "COMBINED_ADVERSE")}, 1.0, "synthetic", np.asarray(frame.atr), {})


def _run(frame, tmp_path, name="a", n=100, seed=5, resume=False):
    cfg = _cfg()
    return v2_probe.run_probe(_ctx(frame, cfg), cfg, n, seed, tmp_path / name, None, resume)


def test_probe_smoke_deterministic_and_ledger_header(frame, tmp_path):
    s1 = _run(frame, tmp_path, "a")
    s2 = _run(frame, tmp_path, "b")
    assert s1["counts"]["unique_specs"] >= 100
    for key in ("counts", "zero_trade", "train_decisions", "families", "sim_skips", "confluence_cofire",
                "drift_baselines_train", "niches", "n_tf_sets", "n_event_signatures"):
        assert s1[key] == s2[key], key
    led = json.loads((tmp_path / "a" / "ledger.json").read_text(encoding="utf-8"))
    assert led["header"]["v1_cumulative"] == {"trials": 30310, "unique_specs": 29798, "label": "v1 cumulative"}
    assert s1["cumulative"]["cumulative_trials"] == 30310 + s1["counts"]["evaluations_total"]
    assert s1["cumulative"]["cumulative_unique_specs"] == 29798 + s1["counts"]["unique_specs"]
    assert led["ledger"]["total_trials"] == s1["counts"]["evaluations_total"]


def test_resume_carries_ledger_counts(frame, tmp_path):
    s1 = _run(frame, tmp_path, "r", n=60)
    s2 = _run(frame, tmp_path, "r", n=120, resume=True)
    assert s2["ledger_carried_in"]["trials"] == s1["counts"]["evaluations_total"]
    assert s2["counts"]["evaluations_total"] > s1["counts"]["evaluations_total"]
    assert s2["counts"]["unique_specs"] >= 120


def test_report_schema(frame, tmp_path):
    s = _run(frame, tmp_path, "s")
    for key in ("summary_version", "market", "scope", "folds", "counts", "cumulative", "zero_trade",
                "train_decisions", "train_trades_passers", "train_trades_per_day_passers", "families",
                "tf_sets", "event_signatures_top", "niches", "sim_skips", "confluence_cofire",
                "drift_baselines_train", "runtime_s", "peak_rss_mb"):
        assert key in s, key
    for key in ("unique_specs", "duplicate_rejects", "invalid_rejects", "behavioral_twins", "reject_reasons"):
        assert key in s["counts"]
    assert set(s["sim_skips"]) >= {"stop_invalid", "risk_out_of_range", "target_crossed_at_fill"}
    for key in ("always_long", "always_short", "random_long_short", "same_session_random"):
        assert key in s["drift_baselines_train"]
    for key in ("search_deap_s", "search_random_s", "cofire_s", "baselines_s", "total_s"):
        assert key in s["runtime_s"]
    assert (tmp_path / "s" / "probe_summary.md").read_text(encoding="utf-8").startswith("# V2 probe")
    assert json.loads((tmp_path / "s" / "probe_summary.json").read_text(encoding="utf-8"))["market"] == "SYNTH"
    pool = json.loads((tmp_path / "s" / "candidate_pool.json").read_text(encoding="utf-8"))
    assert pool["meta"]["fold_digest"] == s["folds"]["digest"]


def test_no_validation_in_search_path_and_summary(frame, tmp_path, monkeypatch):
    src = inspect.getsource(v2_probe)
    assert "temporal_validation_gate_view" not in src and "ensure_full" not in src
    assert "need_base=True" not in src
    monkeypatch.setattr(v2_probe.TemporalEvaluator, "ensure_full",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("sealed view touched")))
    seen = []
    orig = v2_probe.ProbeEvaluator._compute

    def spy(self, canon, ghash, spec, bkey, need_full):
        seen.append(need_full)
        return orig(self, canon, ghash, spec, bkey, need_full)

    monkeypatch.setattr(v2_probe.ProbeEvaluator, "_compute", spy)
    s = _run(frame, tmp_path, "v")
    assert seen and not any(seen)  # every computation was lean (no BASE / sealed side)
    blob = json.dumps(s).lower()
    assert blob.count("validation") == 1  # only the scope sentence names it ("no Validation ...")
    ctx = _ctx(frame, _cfg())
    ev = v2_probe.ProbeEvaluator(lambda: frame, market_from_frame(frame), ctx.dates, ctx.plan,
                                 min_train_trades=15)
    from alpha.discovery.temporal_archetypes import random_genome

    with pytest.raises(RuntimeError):
        ev.evaluate(random_genome(np.random.default_rng(0)), need_base=True)


def test_search_split_uses_train_only_and_no_dev_bar_after_dev_end(frame):
    ctx = _ctx(frame, _cfg())
    tm = ctx.plan.mask(ctx.dates, ctx.plan.train)
    te = ctx.plan.mask(ctx.dates, ctx.plan.validation)
    assert tm.any() and te.any() and not (tm & te).any()
    assert ctx.dates.max() <= np.datetime64("2026-08-31")
    assert ctx.dates[tm].max() < ctx.dates[te].min()


def test_market_sim_params_ger40_is_v1_and_others_scale():
    from markets.spec import load_market_spec

    cfg = _cfg()
    sizing, rules, costs, scale = v2_probe.market_sim_params(load_market_spec("GER40"), cfg, 3.0)
    assert scale == 1.0 and sizing.min_risk_pts == 5.0 and sizing.max_risk_pts == 400.0
    assert rules.max_entry_spread_pts == 8.0 and costs["COMBINED_ADVERSE"] == COST_SCENARIOS["COMBINED_ADVERSE"]
    cfg["reference"]["ger40_median_atr_price"] = None
    with pytest.raises(ValueError):
        v2_probe.market_sim_params(load_market_spec("XAUUSD"), cfg, 1.0)
    cfg["reference"]["ger40_median_atr_price"] = 4.0
    s2, r2, c2, sc = v2_probe.market_sim_params(load_market_spec("XAUUSD"), cfg, 1.0)
    assert sc == pytest.approx(0.25) and s2.min_risk_pts == pytest.approx(1.25)
    assert c2["COMBINED_ADVERSE"].slippage_pts == pytest.approx(1.5 * 0.25) and s2.contract_size == 100.0
    assert r2.max_entry_spread_pts == load_market_spec("XAUUSD").max_entry_spread_price
