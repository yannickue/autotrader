# ruff: noqa: E501
"""Report (3 sections, caveat, right tail), entry/exit adapter and CLI (plan/fast/report, resource guard).

HEAVY (feature build + numba + exit lab): should be designated slow in tests/conftest.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

import research_workbench
from research_workbench.entry_exit_adapter import CAVEAT, _policy_right_tails, right_tail

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "research_strategy.py"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("research_strategy_cli", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._harden = lambda jobs: jobs  # no priority/job-object changes inside pytest
    return module


@pytest.fixture(scope="module")
def smoke(cli, tmp_path_factory):
    root = str(tmp_path_factory.mktemp("wb_cli"))
    cli._live_trader_running = lambda: False
    common = ["--demo-synthetic", "--artifact-root", root]
    out = {"root": root}
    out["plan_cold_rc"] = cli.main(["plan", *common, "--json"])
    out["fast_rc"] = cli.main(["fast", *common, "--entry-exit", "--json"])
    out["plan_warm_rc"] = cli.main(["plan", *common, "--json"])
    out["common"] = common
    return out


def test_right_tail_measurement_is_exact() -> None:
    tail = right_tail([3.0, -1.0, 5.0, 0.5], [3.5, 0.5, 6.0, 2.5])
    two = tail["levels"]["2R"]
    assert tail["n"] == 4 and two["n_realized_ge"] == 2 and two["p_realized_ge"] == 0.5
    assert two["n_mfe_ge"] == 3 and two["mean_realized_given_mfe_ge"] == pytest.approx(
        (3 + 5 + 0.5) / 3
    )
    assert two["median_realized_given_mfe_ge"] == 3.0
    assert (
        tail["levels"]["5R"]["n_mfe_ge"] == 1
        and tail["levels"]["5R"]["mean_realized_given_mfe_ge"] == 5.0
    )


def _lab(r_base, cap_base, r_p, cap_p):
    def rec(r, cap):
        return {"applicable": True, "r": r, "capture_ratio": cap}

    return {"lab": {"policies": {"fixed_1_5r": rec(r_base, cap_base), "pol": rec(r_p, cap_p)}}}


def test_higher_capture_but_lower_right_tail_is_flagged() -> None:
    rows = [_lab(3.0, 0.5, 1.0, 0.9), _lab(3.5, 0.5, 1.2, 0.9), _lab(0.5, 0.5, 0.4, 0.9)]
    out = _policy_right_tails(rows, [3.5, 4.0, 1.0])
    paired = out["pol"]["paired_vs_baseline"]
    assert paired["FLAG_higher_capture_lower_right_tail"] is True
    assert "2R" in paired["lower_tail_levels"]
    # same capture => no flag
    rows = [_lab(3.0, 0.9, 1.0, 0.9)]
    assert (
        _policy_right_tails(rows, [3.5])["pol"]["paired_vs_baseline"][
            "FLAG_higher_capture_lower_right_tail"
        ]
        is False
    )


def test_cli_smoke_plan_fast_plan(smoke, capsys) -> None:
    assert (smoke["plan_cold_rc"], smoke["fast_rc"], smoke["plan_warm_rc"]) == (0, 0, 0)


def test_cli_plan_cold_vs_warm_and_report_contents(smoke, cli, capsys) -> None:
    capsys.readouterr()
    assert cli.main(["plan", *smoke["common"], "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    stages = plan["markets"]["SYN_A"]["stages"]
    assert {s: v["cache"] for s, v in stages.items()} == {
        "FEATURES": "HIT", "SIGNALS": "HIT", "SIMULATION": "HIT", "METRICS": "HIT",
    }  # fmt: skip
    assert plan["classification"] == "LIGHT" and plan["workers"]["requested"] == 1
    assert "available_mb" in plan["ram_guard"]

    assert cli.main(["report", *smoke["common"], "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    for key in (
        "EXPERIMENT_ID",
        "STRATEGY_ID",
        "SPEC_HASH",
        "MARKETS",
        "FEATURE_VERSION",
        "COST_MODEL",
        "SIZING",
        "SPLIT",
    ):
        assert key in report
    market = report["MARKETS"]["SYN_A"]
    for key in (
        "DATASET_HASH",
        "FAST_STATUS",
        "FIDELITY_STATUS",
        "DIFFERENTIAL_STATUS",
        "ARTIFACTS",
    ):
        assert key in market
    assert market["ROBUSTNESS_STATUS"] == "NOT_RUN" and market["OOS_STATUS"] == "NOT_READ"
    assert report["PARTITIONS_READ"] == {
        "TRAIN": True,
        "VALIDATION": True,
        "OOS": False,
        "FORWARD": False,
    }
    contract = market["JOINT_STRATEGY_RESULT"]
    for key in (
        "trade_count", "days", "trades_per_day", "expectancy_R", "median_R", "win_rate", "gross_PnL", "net_PnL",
        "cost_burden", "max_drawdown_R", "MFE", "MAE", "capture", "giveback", "favorable_before_adverse",
    ):  # fmt: skip
        assert key in contract
    assert contract["oos"] == "NOT_READ"
    exit_q = market["EXIT_QUALITY_CAPTURE"]
    levels = exit_q["strategy_exit"]["right_tail_realized_mfe"]["levels"]
    assert set(levels) == {"2R", "3R", "5R"}
    assert {
        "p_realized_ge",
        "n_mfe_ge",
        "mean_realized_given_mfe_ge",
        "median_realized_given_mfe_ge",
    } <= set(levels["2R"])
    assert exit_q["caveat"] == CAVEAT
    assert exit_q["random_entry_control"]["status"] == "NOT_RUN"
    assert market["ENTRY_QUALITY"]["status"] == "COMPLETE"
    assert market["ENTRY_QUALITY"]["independent_of_real_exit"] is True
    comparison = exit_q["same_entry_comparison"]
    assert comparison["status"] in {"COMPLETE", "NOT_AVAILABLE"}
    if comparison["status"] == "COMPLETE":
        assert comparison["summary"]["same_entry_assertion"] is True
        assert "right_tail_by_policy" in comparison
    else:
        assert comparison["reason"]


def test_markdown_report_has_three_separated_sections_and_caveat(smoke, cli, capsys) -> None:
    capsys.readouterr()
    assert cli.main(["report", *smoke["common"]]) == 0
    text = capsys.readouterr().out
    i, j, k = (
        text.index(h)
        for h in ("### ENTRY QUALITY", "### EXIT QUALITY (CAPTURE)", "### JOINT STRATEGY RESULT")
    )
    assert i < j < k
    assert CAVEAT in text
    assert "Right tail" in text and "P(R>=L)" in text and "N(MFE>=L)" in text
    for label in (
        "EXPERIMENT_ID",
        "SPEC_HASH",
        "DATASET_HASH",
        "FAST STATUS",
        "FIDELITY STATUS",
        "DIFFERENTIAL STATUS",
        "ROBUSTNESS STATUS",
        "OOS STATUS",
    ):
        assert label in text


def test_heavy_command_refused_when_live_trader_detected(cli, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_live_trader_running", lambda: True)
    monkeypatch.delenv("RESEARCH_SPEED_ALLOW_WITH_LIVE", raising=False)
    spec = {
        "strategy": {
            "strategy_id": "S", "version": "1", "direction": "LONG",
            "entry_rules": [{"feature": "m5_rsi14", "op": "<", "threshold": 35.0}],
            "stop": {"kind": "atr_multiple", "feature": "m5_atr14", "multiple": 2.0},
            "target": {"kind": "fixed_r", "r": 1.5},
        },
        "markets": ["A"],
        "dataset": {"kind": "csv", "path": str(tmp_path / "{market}.csv")},
        "artifact_root": str(tmp_path / "art"),
    }  # fmt: skip
    path = tmp_path / "exp.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert cli.main(["fast", "--spec", str(path)]) == 3  # heavy: real data
    assert cli.main(["compare", "--spec", str(path)]) == 3
    assert (
        cli.main(["fast", "--demo-synthetic", "--artifact-root", str(tmp_path / "a2")]) == 3
    )  # light needs the flag
    assert not (tmp_path / "art").exists() and not (tmp_path / "a2").exists()
    experiment = cli._load_experiment(cli.build_parser().parse_args(["fast", "--demo-synthetic"]))
    assert cli._guard(experiment, "fast", True) is None  # explicitly LIGHT is allowed
    # plan is read-only/LIGHT and never refused
    assert cli.main(["plan", "--demo-synthetic", "--artifact-root", str(tmp_path / "a3")]) == 0


def test_compare_is_blocked_with_clear_message_when_differential_is_missing(
    cli, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(cli, "_live_trader_running", lambda: False)
    monkeypatch.setitem(sys.modules, "research_workbench.differential", None)
    monkeypatch.delattr(research_workbench, "differential", raising=False)
    rc = cli.main(["compare", "--demo-synthetic", "--artifact-root", str(tmp_path / "x")])
    assert rc == 6
    assert "BLOCKED" in capsys.readouterr().err


def test_entry_exit_artifact_is_cached(smoke, cli) -> None:
    from research_workbench import dag
    from research_workbench.experiment import demo_synthetic_experiment
    from research_workbench.fastrun import run_market

    exp = demo_synthetic_experiment(smoke["root"])
    rec = run_market(exp, "SYN_A", entry_exit=True)
    assert rec["entry_exit"]["cache"] == "HIT" and rec["entry_exit"]["computed"] is False
    assert np.isclose(1.0, 1.0) and dag.STAGES[4] == "ENTRY_EXIT"
