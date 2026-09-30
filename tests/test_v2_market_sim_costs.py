# ruff: noqa: E501
"""V2 per-market cost/sizing definitions, volume_min sizing skip, archetype windows, TemporalEval diagnostics."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import numpy as np
import pytest

from alpha.common.frame import Frame
from alpha.common.market_costs import (
    REFERENCE_EUR_PER_USD,
    RESEARCH_LEVERAGE_CAP,
    MarketCostError,
    cost_scenarios_for,
    derived_table,
    market_cost_model,
    observed_median_spread_price,
    sizing_for,
)
from alpha.common.sim import COST_SCENARIOS, DEFAULT_SIZING
from alpha.discovery import temporal_archetypes as ta
from alpha.discovery.temporal_genome import EventPool
from alpha.fast.sim import SKIP_LABELS, CandidateArrays, MarketArrays, simulate_fast
from markets.spec import CANONICALS, MAX_LEVERAGE_CAP, load_all_specs
from tests._v2mm_helpers import make_frame

SPECS = load_all_specs()
REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ GER40 reproduces V1 exactly
def test_ger40_costs_and_sizing_reproduce_v1_exactly():
    ger = SPECS["GER40"]
    assert cost_scenarios_for(ger) == COST_SCENARIOS
    assert sizing_for(ger, account_eur=DEFAULT_SIZING.equity_eur) == DEFAULT_SIZING
    assert sizing_for(ger) == dataclasses.replace(DEFAULT_SIZING, equity_eur=500.0)
    assert observed_median_spread_price(ger) == pytest.approx(1.45)


# ------------------------------------------------------------------ per-market table
def test_derived_table_all_markets_and_docs_in_sync(capsys):
    table = derived_table({c: SPECS[c] for c in CANONICALS})
    print("\n" + table)
    for name in CANONICALS:
        spec, m = SPECS[name], market_cost_model(SPECS[name])
        s, costs = m.sizing, m.costs
        assert 0 < s.min_risk_pts < s.max_risk_pts
        assert s.max_leverage == min(RESEARCH_LEVERAGE_CAP, spec.max_leverage) <= MAX_LEVERAGE_CAP
        assert (s.min_lot, s.lot_step) == (spec.volume_min, spec.volume_step)
        assert s.equity_eur == 500.0
        rate = 1.0 if spec.currency_profit == "EUR" else REFERENCE_EUR_PER_USD
        assert s.contract_size == pytest.approx(spec.contract_size * rate)
        for v in (s.min_risk_pts, s.max_risk_pts, costs["BASE"].slippage_pts, costs["SLIPPAGE_STRESS"].slippage_pts):
            assert v / spec.tick_size == pytest.approx(round(v / spec.tick_size), abs=1e-6)  # tick multiples
        assert costs["GROSS_REFERENCE"].slippage_pts == 0.0
        assert 0 < costs["BASE"].slippage_pts <= costs["SLIPPAGE_STRESS"].slippage_pts
        # slippage stays a fixed fraction of the median spread (0.345 / 1.034) within one tick
        assert costs["BASE"].slippage_pts == pytest.approx(0.5 / 1.45 * m.median_spread_price, abs=spec.tick_size)
        assert costs["SLIPPAGE_STRESS"].slippage_pts == pytest.approx(1.5 / 1.45 * m.median_spread_price, abs=spec.tick_size)
        assert m.unmodelled and any(u.startswith("commission") for u in m.unmodelled)
        # spread scenarios untouched
        assert costs["SPREAD_STRESS"].spread_mult == 2.0 and costs["BASE"].spread_mult == 1.0
    docs = (REPO / "docs" / "V2_MARKETS.md").read_text(encoding="utf-8")
    assert "Derived cost/sizing" in docs and table in docs, "regenerate the docs table from derived_table()"


def test_missing_report_or_currency_fails_closed(tmp_path):
    with pytest.raises(MarketCostError):
        sizing_for(SPECS["NAS100"], report_dir=tmp_path)
    exotic = dataclasses.replace(SPECS["NAS100"], currency_profit="JPY")
    with pytest.raises(MarketCostError):
        sizing_for(exotic)
    assert sizing_for(exotic, eur_per_ccy={"JPY": 0.006}).contract_size == pytest.approx(0.006)


# ------------------------------------------------------------------ volume_min risk-based skip
def _one_day_stream(stop_dist: float):
    df = make_frame("2025-03-03T08:00", "2025-03-03T19:00", seed=2, spread_pts=100.0, price=100.0)
    fr = Frame.from_dataframe(df)
    m = MarketArrays.from_frame(fr)
    idx = np.array([30], dtype=np.int64)  # 10:30 Berlin-ish, inside 09:00-20:00
    assert 540 <= fr.minute[idx[0] + 1] < 1200
    stop = fr.c[idx] - stop_dist
    c = CandidateArrays(idx, np.array([1], np.int8), stop, np.array([np.nan]), np.array([1000.0]),
                        np.zeros(1, np.int8))
    return m, c


def test_min_lot_that_risks_more_than_approved_is_skipped_as_size_below_min():
    ger = SPECS["GER40"]
    sz = sizing_for(ger, account_eur=500.0)  # approved risk 2.5 EUR, min lot 0.25
    cost = cost_scenarios_for(ger)["BASE"]
    ok_m, ok_c = _one_day_stream(4.5)  # risk ~ 6 pts -> 0.25 lot risks 1.5 EUR <= 2.5
    t_ok = simulate_fast(ok_m, ok_c, cost, sz)
    assert len(t_ok) == 1 and t_ok.qty[0] >= ger.volume_min
    assert t_ok.qty[0] * t_ok.risk_pts[0] * sz.contract_size <= 500.0 * 0.005 + 1e-9
    big_m, big_c = _one_day_stream(38.5)  # risk ~ 40 pts -> 0.25 lot risks 10 EUR > 2.5
    t_big = simulate_fast(big_m, big_c, cost, sz)
    assert len(t_big) == 0
    assert t_big.skips["size_below_min"] == 1
    assert sum(t_big.skips.values()) == 1 and "size_below_min" in SKIP_LABELS


def test_min_lot_feasibility_flag_matches_the_kernel_rule():
    for name in CANONICALS:
        m = market_cost_model(SPECS[name], 500.0)
        assert m.min_lot_feasible_at_min_stop == (m.min_lot_risk_at_min_stop_eur <= m.approved_risk_eur + 1e-12)
    # a bigger account only ever makes the minimum lot more feasible
    assert market_cost_model(SPECS["GER40"], 10_000.0).min_lot_feasible_at_min_stop


# ------------------------------------------------------------------ archetype time windows
PINNED_DEFAULT_GENOMES = "9b8f9c3df2a91ccb9218840013e2ca5df1c569c5098cbd6bfd8e84027fcb8c84"  # HEAD c0814d6


def _genomes(**kw):
    pool = EventPool.full()
    out = []
    for seed in range(60):
        rng = np.random.default_rng(seed)
        out += [ta.random_genome(rng, pool, **kw) for _ in range(3)]
    return out


def test_default_random_genomes_identical_to_pre_change():
    for kw in ({}, {"windows": None}, {"windows": ta.WINDOWS}):
        h = hashlib.sha256()
        for g in _genomes(**kw):
            h.update(g.to_json().encode())
        assert h.hexdigest() == PINNED_DEFAULT_GENOMES, kw


def test_windows_for_ger40_keeps_inside_windows_and_removes_outside_ranges():
    w = ta.windows_for(540, 1200)
    assert (540, 1200) in w  # fully inside: unchanged
    assert all(540 <= a < b <= 1200 and b - a >= 60 for a, b in w)
    assert (0, 720) not in w and (600, 1440) not in w and (420, 900) not in w
    assert len(set(w)) == len(w) and ta.windows_for(540, 1200) == w  # deterministic


@pytest.mark.parametrize("name", CANONICALS)
def test_random_genomes_use_only_windows_inside_the_market_entry_window(name):
    cal = SPECS[name].calendar
    allowed = ta.windows_for(cal.entry_start_min, cal.entry_end_min)
    assert allowed
    rng, pool = np.random.default_rng(11), EventPool.full()
    seen = 0
    for _ in range(120):
        g = ta.random_genome(rng, pool, windows=allowed)
        if g.window is not None:
            seen += 1
            assert cal.entry_start_min <= g.window[0] < g.window[1] <= cal.entry_end_min, (name, g.window)
    assert seen > 10


def test_windows_for_rejects_bad_entry_window():
    with pytest.raises(ValueError):
        ta.windows_for(900, 900)
    with pytest.raises(ValueError):
        ta.random_genome(np.random.default_rng(0), windows=())


# ------------------------------------------------------------------ TemporalEval diagnostics
def test_temporal_eval_exposes_skip_counts_and_train_candidates_without_changing_results():
    from alpha.common.protocol import Partition, SplitPlan
    from alpha.common.sim import SizingSpec
    from alpha.discovery.temporal_evaluate import TemporalEvaluator, market_from_frame
    from alpha.fast.sim import GER40_WINDOW
    from scripts.bench_temporal import synth_frame

    n = 30_000
    days = n // 288
    d0 = np.datetime64("2024-01-01", "D")
    def day(k: int) -> str:
        return str(d0 + np.timedelta64(k, "D"))

    split = SplitPlan(Partition("train", day(0), day(int(days * 0.6))),
                      Partition("validation", day(int(days * 0.6) + 1), day(int(days * 0.8))),
                      Partition("oos", day(int(days * 0.8) + 1), day(days + 2)))
    dates = (d0 + (np.arange(n) // 288)).astype("datetime64[D]")
    frame = synth_frame(n, seed=3, density=12.0, run_len=(60, 200))
    sizing = SizingSpec(min_risk_pts=0.5, max_risk_pts=60.0)

    def ev(**kw):
        return TemporalEvaluator(lambda: frame, market_from_frame(frame), dates, split, sizing=sizing,
                                 min_train_trades=15, **kw)

    a, b = ev(), ev(window=GER40_WINDOW)
    rng, pool = np.random.default_rng(1), EventPool.full()
    for _ in range(80):
        g = ta.random_genome(rng, pool)
        ea = a.evaluate(g, need_base=False)
        if ea.rejected:
            # Stage-A rejects ran no sim; a post-sim too_few_trades reject keeps its skip diagnostics
            assert (ea.skip_counts is None or ea.reject == "too_few_trades") and ea.train_candidates is not None
            continue
        assert set(ea.skip_counts) == set(SKIP_LABELS)
        assert isinstance(ea.train_candidates, CandidateArrays)
        assert len(ea.train_candidates.decision_idx) == ea.n_train_candidates
        assert "skip_counts" not in ea.to_dict() and "train_candidates" not in ea.to_dict()
        eb = b.evaluate(g, need_base=False)
        assert eb.to_json() == ea.to_json()  # explicit GER40 window: identical numbers / hashes
        again = a.evaluate(g, need_base=False)  # cache hit: diagnostics only on fresh computation
        assert again.skip_counts is None and again.to_json() == ea.to_json()
        return
    pytest.skip("no Stage-A survivor among random genomes")
