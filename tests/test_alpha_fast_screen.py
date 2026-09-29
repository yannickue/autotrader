from __future__ import annotations

import json

import numpy as np
import pytest

from alpha.common.dataset import POINT, load_research_dataset
from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec
from alpha.fast.registry import discover
from alpha.fast.screen import RejectReason, light_screen, light_screen_many, reject_reason
from alpha.fast.sim import CandidateArrays, MarketArrays, fast_metrics, simulate_fast
from alpha.fast.spec import Rule, StopSpec, StrategySpec, TargetSpec
from alpha.fast.store import FeatureStore
from research.runners import ar2_fast


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="SCREEN", version="1", direction="LONG",
        entry_rules=(Rule("c", ">", threshold=0.0),),
        stop=StopSpec("atr_multiple", feature="m5_atr14", multiple=1.0),
        target=TargetSpec("fixed_r", r=2.0),
    )


def _candidates(stops: list[float]) -> CandidateArrays:
    n = len(stops)
    return CandidateArrays(
        np.arange(n, dtype=np.int64), np.ones(n, dtype=np.int8), np.asarray(stops),
        np.full(n, np.nan), np.full(n, 2.0), np.zeros(n, dtype=np.int8),
    )


def test_reject_reason_covers_stage_a_sanity_checks() -> None:
    spec = _spec()
    empty = _candidates([])
    assert reject_reason(spec, empty, min_trades=2) == RejectReason.ZERO_CANDIDATES
    assert reject_reason(spec, _candidates([95.0]), min_trades=2) == RejectReason.TOO_FEW_TRADES
    assert (
        reject_reason(
            spec, _candidates([95.0, 95.0]), min_trades=2, seen={spec.spec_hash()}
        )
        == RejectReason.DUPLICATE_SPEC
    )

    market = MarketArrays(
        *[np.full(3, 100.0) for _ in range(4)], np.zeros(3), np.full(3, 600),
        np.zeros(3), np.array([True, True, False]),
    )
    assert reject_reason(
        spec, _candidates([99.0, 99.0]), min_trades=1, market=market,
        sizing=SizingSpec(min_risk_pts=5.0),
    ) == RejectReason.INVALID_STOP
    assert reject_reason(spec, _candidates([90.0, 90.0]), min_trades=1, market=market) is None


def test_light_screen_matches_fast_metrics_for_three_real_variants(tmp_path) -> None:
    cfg = json.loads(ar2_fast.DEFAULT_CONFIG.read_text(encoding="utf-8"))
    plan = ar2_fast._plan({**cfg, "embargo_days": 1})
    ds = load_research_dataset(ar2_fast.REPO_ROOT / cfg["dataset_root"])
    frame = ar2_fast.dev_frame(ds.frame, plan)
    features = FeatureStore.load_or_build(frame, {"point_size": POINT}, tmp_path / "features")
    market = ar2_fast._market(features)
    dates = ar2_fast._dates(features)
    sizing = SizingSpec(**cfg["sizing"])
    rules = SimRules(**cfg["rules"])
    variants = [
        (family, params) for _, family in sorted(discover().items()) for params in family.variants
    ]
    checked = 0
    candidate_sets = []
    for family, params in variants:
        candidates = family.generate(features, params)
        trades = simulate_fast(market, candidates, COST_SCENARIOS["BASE"], sizing, rules)
        train_mask = plan.mask(dates[trades.entry_idx], plan.train)
        valid_mask = plan.mask(dates[trades.entry_idx], plan.validation)
        if not train_mask.any() or not valid_mask.any():
            continue
        got = light_screen(
            market, candidates, COST_SCENARIOS["BASE"], plan, dates=dates,
            sizing=sizing, rules=rules,
        )
        for part, mask in ((got.train, train_mask), (got.validation, valid_mask)):
            selected = ar2_fast._subset(trades, mask)
            bar_mask = plan.mask(dates, plan.train if part is got.train else plan.validation)
            reference = fast_metrics(selected, trading_days=np.unique(market.day[bar_mask]))
            assert part.n_trades == reference["trades"]
            assert part.expectancy_r == pytest.approx(reference["expectancy_r"], abs=1e-9)
            assert part.profit_factor == pytest.approx(reference["profit_factor"], abs=1e-9)
            assert part.win_rate == pytest.approx(reference["win_rate"], abs=1e-9)
        candidate_sets.append(candidates)
        checked += 1
        if checked == 3:
            break
    assert checked == 3
    batch = light_screen_many(
        market, candidate_sets, COST_SCENARIOS["BASE"], plan, dates=dates,
        sizing=sizing, rules=rules,
    )
    assert len(batch) == 3
