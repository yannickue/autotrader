# ruff: noqa: E501
"""END-TO-END: promoted market -> `compare` through the REAL Nautilus differential -> verified FIDELITY artifact -> report.

SLOW (real Nautilus BacktestEngine run): designate this file slow in tests/conftest.py.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from research_workbench import dag
from research_workbench.compare import fidelity_identity, read_fidelity
from research_workbench.experiment import experiment_from_dict

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "research_strategy.py"
LOOSE = ["--min-train-trades", "1", "--min-validation-trades", "1", "--allow-negative-expectancy"]


def _spec(root: Path, cost=None) -> dict:
    return {
        "strategy": {
            "strategy_id": "WB_E2E", "version": "1", "direction": "LONG",
            "entry_rules": [{"feature": "m5_rsi14", "op": "<", "threshold": 30.0}],
            "stop": {"kind": "atr_multiple", "feature": "m5_atr14", "multiple": 2.0},
            "target": {"kind": "fixed_r", "r": 1.5},
        },
        "markets": ["SYN_E2E"],
        "dataset": {"kind": "synthetic", "seed": 21, "days": 14, "start": "2024-03-04"},
        "split": {
            "train": ["2024-03-04", "2024-03-12"], "validation": ["2024-03-13", "2024-03-21"],
            "oos": ["2024-03-22", "2024-03-31"], "embargo_days": 0,
        },
        "artifact_root": str(root),
        **({"cost": cost} if cost else {}),
    }  # fmt: skip


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("research_strategy_cli_e2e", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._harden = lambda jobs: jobs
    module._live_trader_running = lambda: False
    return module


@pytest.fixture(scope="module")
def e2e(cli, tmp_path_factory):
    root = tmp_path_factory.mktemp("wb_e2e")
    path = root / "exp.json"
    path.write_text(json.dumps(_spec(root)), encoding="utf-8")
    rc_fast = cli.main(["fast", "--spec", str(path), *LOOSE])
    rc_compare = cli.main(["compare", "--spec", str(path), *LOOSE])
    return {"root": root, "path": path, "rc_fast": rc_fast, "rc_compare": rc_compare}


def _report(cli, capsys, path):
    capsys.readouterr()
    assert cli.main(["report", "--spec", str(path), "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def _record(e2e):
    experiment = experiment_from_dict(json.loads(e2e["path"].read_text("utf-8")))
    store = dag.ArtifactStore(e2e["root"])
    return experiment, store, store.read_run_record(experiment.experiment_id, "SYN_E2E")


def test_market_promotes_and_compare_runs_the_real_differential(e2e) -> None:
    assert e2e["rc_fast"] == 0
    experiment, store, record = _record(e2e)
    assert record["status"] == "PROMOTE_TO_FIDELITY"
    ident = fidelity_identity(experiment, record["keys"])
    assert (
        ident["key"] and ident["nautilus_version"] and ident["replay_config"]["timeframe"] == "5m"
    )
    result = read_fidelity(store, "SYN_E2E", ident["key"])
    assert result is not None, "FIDELITY artifact must verify under the current key"
    # PINNED (seed 21, deterministic after the replay timing fix): the real engines agree
    assert result["status"] == "PASS", (result["status"], result.get("blocked_reason"))
    assert (result["fast_trade_count"], result["fidelity_trade_count"]) == (33, 33)
    summary = result["summary"]
    assert summary["nautilus_ignored_candidates"] == []
    assert summary["netting"]["overlap_violations"] == 0
    assert summary["netting"]["same_bar_reentries"] == 15
    assert summary["class_counts"]["BUG_SUSPECTED"] == 0
    assert result["promotion_status"] == "READY_FOR_ROBUSTNESS"
    manifest = store.lookup("SYN_E2E", "FIDELITY", ident["key"]).manifest
    assert (
        manifest["complete"] is True
        and manifest["stage"] == "FIDELITY"
        and manifest["artifact_sha256"]
    )
    assert e2e["rc_compare"] == 0


def test_report_shows_fidelity_with_scope_and_by_construction_disclosure(e2e, cli, capsys) -> None:
    report = _report(cli, capsys, e2e["path"])
    market = report["MARKETS"]["SYN_E2E"]
    assert market["FIDELITY_STATUS"] == "FIDELITY_PASS"
    assert market["DIFFERENTIAL_STATUS"] == "PASS"
    assert market["PROMOTION_STATUS"] == "READY_FOR_ROBUSTNESS"
    joint = market["JOINT_STRATEGY_RESULT"]
    assert joint["fast_trades"] == 33 and isinstance(joint["candidates_blocked_while_busy"], int)
    assert (
        market["FIDELITY"]["netting"]["candidates_blocked_by_open_position"]
        == joint["candidates_blocked_while_busy"]
    )
    fid = market["FIDELITY"]
    assert "EXECUTION semantics" in fid["scope"] and "Does NOT validate" in fid["scope"]
    assert set(fid["by_construction_fields"]) >= {"direction", "stop", "target", "qty"}
    assert "mismatch_leg_counts" in fid
    assert market["DATA_SCOPE"]["partitions_feeding_metrics"] == ["TRAIN", "VALIDATION"]
    capsys.readouterr()
    assert cli.main(["report", "--spec", str(e2e["path"])]) == 0
    text = capsys.readouterr().out
    assert (
        "DIFFERENTIAL SCOPE" in text
        and "BY_CONSTRUCTION" in text
        and "CAUSALITY / LIMITATION" in text
    )


def test_stale_or_rejected_state_never_shows_an_old_pass(e2e, cli, capsys, tmp_path) -> None:
    # (1) the same market is now REJECT_FAST under a strict gate: no fidelity status may be shown
    assert cli.main(["fast", "--spec", str(e2e["path"]), "--min-train-trades", "100000"]) == 0
    market = _report(cli, capsys, e2e["path"])["MARKETS"]["SYN_E2E"]
    assert market["FAST_STATUS"] == "REJECT_FAST"
    assert market["FIDELITY_STATUS"] == "NOT_RUN" and market["DIFFERENTIAL_STATUS"] == "NOT_RUN"
    # (2) back to the loose gate: the old artifact verifies under unchanged keys
    assert cli.main(["fast", "--spec", str(e2e["path"]), *LOOSE]) == 0
    assert _report(cli, capsys, e2e["path"])["MARKETS"]["SYN_E2E"]["FIDELITY_STATUS"] != "NOT_RUN"
    # (3) a different cost scenario changes the simulation key: the stored artifact no longer matches => STALE
    other = tmp_path / "exp2.json"
    other.write_text(
        json.dumps(
            _spec(e2e["root"], cost={"name": "WIDE", "spread_mult": 2.0, "slippage_pts": 0.5})
        ),
        encoding="utf-8",
    )
    assert cli.main(["fast", "--spec", str(other), *LOOSE]) == 0
    stale = _report(cli, capsys, other)["MARKETS"]["SYN_E2E"]
    assert stale["FIDELITY_STATUS"] in {
        "NOT_RUN",
        "STALE",
    }  # never the earlier experiment's PASS/MISMATCH
    # (4) tampering with the stored FIDELITY payload => STALE (the verified load rejects it)
    experiment, store, record = _record(e2e)
    key = fidelity_identity(experiment, record["keys"])["key"]
    victim = store.stage_dir("SYN_E2E", "FIDELITY", key) / "differential.json"
    victim.write_text(victim.read_text("utf-8").replace("PASS", "PASX", 1) + " ", encoding="utf-8")
    tampered = _report(cli, capsys, e2e["path"])["MARKETS"]["SYN_E2E"]
    assert tampered["FIDELITY_STATUS"] == "STALE"


def test_inexpressible_cost_scenario_is_blocked_with_the_exact_reason(
    cli, tmp_path, capsys
) -> None:
    path = tmp_path / "exp.json"
    cost = {
        "name": "COMMISSION",
        "spread_mult": 1.0,
        "slippage_pts": 0.5,
        "commission_eur_per_lot": 1.0,
    }
    path.write_text(json.dumps(_spec(tmp_path, cost=cost)), encoding="utf-8")
    assert cli.main(["fast", "--spec", str(path), *LOOSE]) == 0
    assert cli.main(["compare", "--spec", str(path), *LOOSE]) == 1
    experiment, store, record = _record({"root": tmp_path, "path": path})
    assert record["status"] == "PROMOTE_TO_FIDELITY"  # BLOCKED never promotes
    result = read_fidelity(store, "SYN_E2E", fidelity_identity(experiment, record["keys"])["key"])
    assert result["status"] == "BLOCKED"
    assert "commission_eur_per_lot" in result["blocked_reason"]
    assert result["promotion_status"] == "PROMOTE_TO_FIDELITY"
    report = _report(cli, capsys, path)["MARKETS"]["SYN_E2E"]
    assert (
        report["FIDELITY_STATUS"] == "BLOCKED"
        and report["PROMOTION_STATUS"] == "PROMOTE_TO_FIDELITY"
    )
