from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha.common.dataset import POINT, load_research_dataset
from alpha.common.metrics import compute_metrics
from alpha.common.protocol import Partition, SplitPlan, stable_hash
from alpha.common.sim import COST_SCENARIOS, SimRules, SizingSpec
from alpha.fast.registry import discover
from alpha.fast.sim import simulate_fast
from alpha.fast.store import FeatureStore
from research.runners import ar2_fast


def _plan() -> SplitPlan:
    return SplitPlan(
        Partition("train", "2025-01-01", "2025-01-02"),
        Partition("validation", "2025-01-03", "2025-01-03"),
        Partition("oos", "2025-01-04", "2025-01-05"),
    )


def test_dev_frame_physically_excludes_oos() -> None:
    frame = pd.DataFrame({"ts": pd.date_range("2025-01-01", periods=5, freq="D", tz="UTC")})
    got = ar2_fast.dev_frame(frame, _plan())
    local_days = pd.DatetimeIndex(got["ts"]).tz_convert("Europe/Berlin").normalize()
    assert len(got) == 3
    assert local_days.max().tz_localize(None) <= pd.Timestamp("2025-01-03")
    assert not (local_days.tz_localize(None) >= pd.Timestamp("2025-01-04")).any()


def test_freeze_rule_rejected_early_positive_both_and_samples() -> None:
    cfg = {
        "freeze_rule": {
            "min_train_trades": 3,
            "min_validation_trades": 2,
            "min_profit_factor_each_partition": 1.05,
            "max_drawdown_pct_le": 20.0,
            "expectancy_r_gt_0_after_removing_top_n_winners": 2,
            "expectancy_r_gt_0_under": ["SPREAD_STRESS", "SLIPPAGE_STRESS"],
            "sibling_variant_pooled_expectancy_r_gt": 0.0,
            "regime_restriction": {"min_train_trades": 2, "min_validation_trades": 1},
        }
    }

    def rec(strategy: str, variant: int, train: list[float], valid: list[float]) -> dict:
        return ar2_fast.synthetic_freeze_record(strategy, variant, train, valid)

    records = [
        rec("EARLY", 0, [-1, -0.5, -0.2], [-0.4, -0.3]),
        rec("ONE_SIDE", 0, [0.4, 0.3, 0.2], [-0.1, -0.2]),
        rec("TOO_SMALL", 0, [0.3, 0.2], [0.2]),
        rec("KEEP", 0, [0.4, 0.3, -0.1, 0.2, 0.1], [0.3, -0.1, 0.2]),
    ]
    frozen, rejected = ar2_fast.freeze_rule(cfg, records)
    assert rejected == [
        {"strategy_id": "EARLY", "reason": "REJECTED_EARLY: negative in TRAIN and VALIDATION"}
    ]
    assert {(item["strategy_id"], item["role"]) for item in frozen} == {("KEEP", "WHOLE_VARIANT")}
    assert not any(item["strategy_id"] in {"ONE_SIDE", "TOO_SMALL"} for item in frozen)


def test_oos_refuses_missing_and_tampered_frozen_file(tmp_path: Path) -> None:
    cfg = json.loads(ar2_fast.DEFAULT_CONFIG.read_text(encoding="utf-8"))
    with pytest.raises(SystemExit, match="no frozen_candidates"):
        ar2_fast.run_oos(cfg, tmp_path)
    payload = {"set": [{"strategy_id": "X"}], "hash": stable_hash([])}
    (tmp_path / "frozen_candidates.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit, match="hash mismatch"):
        ar2_fast.run_oos(cfg, tmp_path)


def test_main_writes_exit_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected")

    monkeypatch.setattr(ar2_fast, "run_dev", boom)
    code = ar2_fast.cli(["--out", str(tmp_path)])
    assert code == 1
    assert (tmp_path / "EXIT").read_text(encoding="utf-8") == "1\n"


def test_metric_projection_matches_reference_for_core_values() -> None:
    r = np.asarray([0.5, -0.25, 1.0, -0.5, 0.1])
    trades = ar2_fast.synthetic_trade_arrays(r, np.asarray([0, 0, 1, 2, 2]))
    got = ar2_fast.complete_metrics(
        trades,
        np.ones(len(r), dtype=bool),
        trading_days=np.asarray([0, 1, 2]),
        window_bars=100,
        equity_eur=10_000.0,
        min_trades=30,
        seed=7,
    )
    assert got["trades"] == 5
    assert got["expectancy_r"] == pytest.approx(float(r.mean()), abs=1e-9)
    assert got["profit_factor"] == pytest.approx(round(1.6 / 0.75, 6), abs=1e-9)
    assert got["insufficient_sample"] is True


def test_result_cache_reuses_identical_fingerprint_and_invalidates(tmp_path: Path) -> None:
    cache = ar2_fast.ResultCache(tmp_path)
    calls = 0

    def evaluate() -> ar2_fast.TradeArrays:
        nonlocal calls
        calls += 1
        return ar2_fast.synthetic_trade_arrays(np.asarray([0.2]), np.asarray([0]))

    first, reused_first = cache.get_or_run({"component": "a"}, evaluate)
    second, reused_second = cache.get_or_run({"component": "a"}, evaluate)
    changed, reused_changed = cache.get_or_run({"component": "b"}, evaluate)
    assert calls == 2
    assert (reused_first, reused_second, reused_changed) == (False, True, False)
    np.testing.assert_array_equal(first.as_matrix(), second.as_matrix())
    np.testing.assert_array_equal(first.as_matrix(), changed.as_matrix())


def _reference_frame(trades: ar2_fast.TradeArrays, dates: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "entry_idx": trades.entry_idx,
            "date": dates[trades.entry_idx],
            "pnl_eur": trades.net_pnl_eur,
            "gross_pnl_eur": trades.gross_pnl_eur,
            "r_multiple": trades.r_multiple,
            "holding_minutes": trades.holding_bars * 5,
            "leverage": trades.leverage,
            "leverage_capped": trades.leverage_capped,
            "entry_spread_pts": trades.spread_cost_pts,
            "spread_cost_pts": trades.spread_cost_pts,
            "slippage_cost_pts": trades.slippage_cost_pts,
            "cost_eur": trades.cost_eur,
            "crossed_rollover": np.zeros(len(trades), dtype=bool),
            "exit_reason": trades.exit_reason_labels,
            "mfe_r": trades.mfe_r,
            "mae_r": trades.mae_r,
            "side": trades.side,
        }
    )


def test_fast_metrics_match_common_metrics_for_three_golden_variants(tmp_path: Path) -> None:
    cfg = json.loads(ar2_fast.DEFAULT_CONFIG.read_text(encoding="utf-8"))
    ds = load_research_dataset(ar2_fast.REPO_ROOT / cfg["dataset_root"])
    frame = ar2_fast.dev_frame(ds.frame, ar2_fast._plan(cfg))
    features = FeatureStore.load_or_build(frame, {"point_size": POINT}, tmp_path / "features")
    market = ar2_fast._market(features)
    dates = ar2_fast._dates(features)
    sizing = SizingSpec(**cfg["sizing"])
    rules = SimRules(**cfg["rules"])
    variants = [
        (family, params) for _, family in sorted(discover().items()) for params in family.variants
    ]
    days = np.unique(features["berlin_day_id"])
    checked = 0
    for family, params in variants:
        trades = simulate_fast(
            market, family.generate(features, params), COST_SCENARIOS["BASE"], sizing, rules
        )
        if not len(trades):
            continue
        reference = compute_metrics(
            _reference_frame(trades, dates),
            trading_days=days,
            window_bars=len(frame),
            sizing=sizing,
            seed=cfg["seed"],
        )
        if reference.get("profit_factor") is None:
            continue
        got = ar2_fast.complete_metrics(
            trades,
            np.ones(len(trades), dtype=bool),
            trading_days=days,
            window_bars=len(frame),
            equity_eur=sizing.equity_eur,
            min_trades=cfg["sample_rules"]["min_trades_flag"],
            seed=cfg["seed"],
        )
        assert got["trades"] == reference["trades"]
        assert got["expectancy_r"] == pytest.approx(reference["expectancy_r"], abs=1e-9)
        assert got["profit_factor"] == pytest.approx(reference["profit_factor"], abs=1e-9)
        checked += 1
        if checked == 3:
            break
    assert checked == 3


def test_e2e_golden_slice_writes_every_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = json.loads(ar2_fast.DEFAULT_CONFIG.read_text(encoding="utf-8"))
    ds = load_research_dataset(ar2_fast.REPO_ROOT / cfg["dataset_root"])
    frame = ds.frame.iloc[46000:58000].reset_index(drop=True)
    out = tmp_path / "out"
    monkeypatch.setattr(
        ar2_fast,
        "load_research_dataset",
        lambda _root: dataclasses.replace(ds, frame=frame),
    )
    code = ar2_fast.cli(["--out", str(out), "--cache-dir", str(tmp_path / "cache")])
    expected = {
        "dev_results.json",
        "regime_context_matrix.csv",
        "session_matrix.csv",
        "cadence.csv",
        "conflicts.json",
        "frozen_candidates.json",
        "dev_run_record.json",
        "report.md",
        "EXIT",
    }
    assert code == 0
    assert expected <= {path.name for path in out.iterdir()}
    assert (out / "report.md").stat().st_size > 100
