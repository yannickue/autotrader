"""Metrics arithmetic, split plan, OOS gate, dataset admission policy, import isolation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha.common.dataset import _admit
from alpha.common.metrics import breakdown, compute_metrics, material_difference
from alpha.common.protocol import (
    OosAccessError,
    OosGate,
    Partition,
    SplitPlan,
    run_record,
    stable_hash,
)

D = np.datetime64


def make_trades(pnl: list[float], days: list[str]) -> pd.DataFrame:
    n = len(pnl)
    return pd.DataFrame(
        {
            "entry_idx": np.arange(n), "pnl_eur": pnl, "r_multiple": np.array(pnl) / 50.0,
            "gross_pnl_eur": np.array(pnl) + 2.0, "cost_eur": 2.0, "date": [D(d) for d in days],
            "bars_held": 4, "holding_minutes": 20, "leverage": 3.0,
            "leverage_capped": False, "entry_spread_pts": 2.5,
            "spread_cost_pts": 2.5, "slippage_cost_pts": 1.0, "exit_reason": "STOP",
            "mfe_r": 0.5, "mae_r": 0.5, "crossed_rollover": False, "side": 1,
        }
    )  # fmt: skip


def test_metrics_match_hand_computation():
    pnl = [50.0, -50.0, -25.0, 100.0, -50.0, -50.0]
    days = ["2025-03-03", "2025-03-03", "2025-03-04", "2025-03-05", "2025-03-05", "2025-03-05"]
    all_days = np.array([D(f"2025-03-0{d}") for d in (3, 4, 5, 6, 7)])
    m = compute_metrics(make_trades(pnl, days), trading_days=all_days, window_bars=1000, seed=1)
    assert m["trades"] == 6 and m["trading_days"] == 5
    assert m["net_pnl_eur"] == pytest.approx(-25.0)
    assert m["win_rate"] == pytest.approx(2 / 6)
    assert m["profit_factor"] == pytest.approx(150.0 / 175.0, abs=1e-6)
    assert m["avg_winner_eur"] == pytest.approx(75.0) and m["avg_loser_eur"] == pytest.approx(
        -43.75
    )
    assert m["payoff_ratio"] == pytest.approx(75.0 / 43.75, abs=1e-5)
    assert m["max_consecutive_losses"] == 2
    eq = 10000 + np.concatenate([[0], np.cumsum(pnl)])
    assert m["max_drawdown_eur"] == pytest.approx((np.maximum.accumulate(eq) - eq).max())
    assert m["zero_trade_days"] == 2 and m["max_trades_in_a_day"] == 3
    assert m["worst_day_eur"] == pytest.approx(-25.0) and m["best_day_eur"] == pytest.approx(0.0)
    assert m["trades_per_day"] == pytest.approx(6 / 5) and m["median_trades_per_day"] == 1.0
    assert m["exposure_time_frac"] == pytest.approx(24 / 1000)
    assert m["expectancy_r_ci95_trade_iid"][0] is not None
    assert m["expectancy_r_ci95_day_clustered"][0] is not None
    assert m["avg_holding_minutes"] == 20


def test_metrics_empty_partition_is_all_zero_trade_days():
    m = compute_metrics(
        make_trades([], []).iloc[0:0], trading_days=np.array([D("2025-03-03")]), window_bars=10
    )
    assert m["trades"] == 0 and m["zero_trade_days"] == 1


def test_bootstrap_is_seed_deterministic():
    t = make_trades([10.0, -5.0, 20.0, -50.0, 5.0, 12.0], ["2025-03-03"] * 6)
    days = np.array([D("2025-03-03")])
    a = compute_metrics(t, trading_days=days, window_bars=10, seed=5)
    b = compute_metrics(t, trading_days=days, window_bars=10, seed=5)
    assert a == b


def test_breakdown_and_material_difference():
    t = make_trades([50.0] * 40 + [-50.0] * 40, ["2025-03-03"] * 80)
    t["trend"] = ["A"] * 40 + ["B"] * 40
    tab = breakdown(t, "trend")
    assert set(tab["trend"]) == {"A", "B"}
    assert material_difference(tab)["material"] is True


def test_split_plan_requires_disjoint_ordered_partitions():
    ok = SplitPlan(
        Partition("train", "2025-01-01", "2025-06-30"),
        Partition("validation", "2025-07-01", "2025-09-30"),
        Partition("oos", "2025-10-01", "2025-12-31"),
    )
    dates = np.array([D("2025-06-30"), D("2025-07-01"), D("2025-10-01")])
    assert ok.mask(dates, ok.train).tolist() == [True, False, False]
    assert ok.mask(dates, ok.oos).tolist() == [False, False, True]
    with pytest.raises(ValueError):
        SplitPlan(
            Partition("train", "2025-01-01", "2025-07-15"),
            Partition("validation", "2025-07-01", "2025-09-30"),
            Partition("oos", "2025-10-01", "2025-12-31"),
        )


def test_oos_gate_allows_identical_fingerprint_refuses_change_and_reset_starts_new_epoch(tmp_path):
    gate = OosGate(tmp_path / "log.json")
    h1, h2 = stable_hash([{"a": 1}]), stable_hash([{"a": 2}])
    components = {"candidates": "a", "config": "b", "datasets": {"m": "c"}, "sources": {}}
    gate.evaluate(h1, "first", components=components)
    gate.evaluate(h1, "reproducibility re-run")
    with pytest.raises(OosAccessError):
        gate.evaluate(h2, "tuned after peeking")
    gate.reset("approved new experiment after code review")
    gate.evaluate(h2, "new experiment")
    log = json.loads((tmp_path / "log.json").read_text())
    assert [entry["kind"] for entry in log] == ["evaluation", "evaluation", "reset", "evaluation"]
    assert log[0]["components"] == components
    with pytest.raises(ValueError, match="20"):
        gate.reset("too short")


def test_run_record_stores_source_hashes_and_experiment_fingerprint(tmp_path):
    source_hashes = {"src/alpha/common/sim.py": "abc", "research/runners/ar1_compare.py": "def"}
    record = run_record(
        repo=tmp_path,
        config={"seed": 1},
        dataset_provenance={"months": [], "n_bars": 0},
        seed=1,
        source_hashes=source_hashes,
        experiment_fingerprint="fingerprint",
    )
    assert record["source_hashes"] == source_hashes
    assert record["experiment_fingerprint"] == "fingerprint"


def test_dataset_admission_policy():
    assert _admit({"status": "PASSED"})[0]
    assert not _admit({"status": "REJECTED"})[0]
    assert _admit({"status": "PASSED_WITH_WARNINGS", "summary": {"SPREAD_ANOMALY": 3}})[0]
    assert not _admit({"status": "PASSED_WITH_WARNINGS", "summary": {"OHLC_INCONSISTENT": 1}})[0]
    unreviewed = {
        "status": "PASSED_WITH_WARNINGS",
        "summary": {"SUSPICIOUS_GAP": 1},
        "suspicious_gaps": [["2025-06-02T10:00:00+00:00", "2025-06-02T12:00:00+00:00"]],
    }
    assert not _admit(unreviewed)[0]
    reviewed = dict(
        unreviewed, suspicious_gaps=[["2025-04-17T19:55:00+00:00", "2025-04-22T00:15:00+00:00"]]
    )
    assert _admit(reviewed)[0]


def test_research_package_is_isolated_from_production_and_vice_versa():
    src = Path(__file__).resolve().parents[3] / "src"
    forbidden_in_alpha = re.compile(
        r"^\s*(?:from|import)\s+(adapters|nautilus_mt5|nautilus_kernel|execution|risk|portfolio|"
        r"strategies|pipeline|persistence)\b",
        re.M,
    )
    for py in (src / "alpha").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert not forbidden_in_alpha.search(text), f"{py} couples research to production"
    importing_alpha = re.compile(r"^\s*(?:from|import)\s+alpha\b", re.M)
    for py in src.rglob("*.py"):
        if "alpha" in py.relative_to(src).parts[:1]:
            continue
        assert not importing_alpha.search(py.read_text(encoding="utf-8")), (
            f"{py} imports research code"
        )
