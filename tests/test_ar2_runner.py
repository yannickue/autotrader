import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from alpha.common.protocol import Partition, SplitPlan, stable_hash

ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    spec = importlib.util.spec_from_file_location(
        "ar2_compare", ROOT / "research/runners/ar2_compare.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["ar2_compare"] = module
    spec.loader.exec_module(module)
    return module


runner = load_runner()
CFG = json.loads((ROOT / "research/configs/ar2_phase2.json").read_text(encoding="utf-8"))
PLAN = SplitPlan(**{k: Partition(k, *v) for k, v in CFG["splits"].items()})


def test_discovery_finds_all_strategy_families_dynamically() -> None:
    variants = runner.discover_variants()
    ids = {v["strategy_id"] for v in variants}
    assert len(ids) >= 10
    assert all(v["strategy_version"] for v in variants)
    assert len({v["id"] for v in variants}) == len(variants)


def test_discovery_picks_up_a_dummy_package(tmp_path, monkeypatch) -> None:
    pkg = tmp_path / "dummy_strats"
    (pkg / "alpha_dummy").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "alpha_dummy" / "__init__.py").write_text(
        "from dataclasses import dataclass\n"
        "from alpha.signals import CandidateStrategyBase\n"
        "@dataclass(frozen=True)\nclass P:\n    x: int = 1\n"
        "VARIANTS = (P(), P(x=2))\n"
        "class Dummy(CandidateStrategyBase):\n"
        "    strategy_id = 'DUMMY'\n    strategy_version = '9'\n"
        "    def reset(self): pass\n"
        "    def regime_eligible(self, s): return False\n"
        "    def setup_condition(self, s): return False\n"
        "    def trigger(self, s, t): return None\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    found = runner.discover_variants("dummy_strats")
    assert [v["id"] for v in found] == ["DUMMY#0", "DUMMY#1"]


def test_development_frame_contains_no_oos_bars() -> None:
    stamps = pd.date_range("2026-04-25", "2026-05-10", freq="1D", tz="Europe/Berlin")
    df = pd.DataFrame({"ts": stamps.tz_convert("UTC"), "open": 1.0})
    dev = runner.dev_frame(df, PLAN)
    local = pd.DatetimeIndex(dev["ts"]).tz_convert("Europe/Berlin")
    assert len(dev) > 0 and local.max() <= pd.Timestamp("2026-04-30 23:59", tz="Europe/Berlin")
    assert not (local >= pd.Timestamp(PLAN.oos.start, tz="Europe/Berlin")).any()


def test_expectancy_without_top_winners_and_restrict() -> None:
    tr = pd.DataFrame({"r_multiple": [5.0, 4.0, -1.0, -1.0, 0.5]})
    assert runner.expectancy_without_top_winners(tr, 2) == pytest.approx(-0.5)
    assert runner.expectancy_without_top_winners(tr.iloc[:2], 2) is None
    cands = [
        SimpleNamespace(h1_regime={"DIRECTION": "UP"}),
        SimpleNamespace(h1_regime={"DIRECTION": "DOWN"}),
    ]
    kept = runner.restrict(cands, {"dimension": "DIRECTION", "values": ["UP"]})
    assert len(kept) == 1 and runner.restrict(cands, None) == cands


def fake_records(train_r: float, val_r: float, n_train: int = 80, n_val: int = 40):
    def trades(r: float, start: str, n: int) -> pd.DataFrame:
        dates = pd.date_range(start, periods=n, freq="D")
        return pd.DataFrame(
            {
                "date": dates.to_numpy().astype("datetime64[D]"),
                "r_multiple": np.where(np.arange(n) % 5 == 0, r * 3.0, -r * 0.5) + r,
                "pnl_eur": np.where(np.arange(n) % 5 == 0, 3.0 * r, -0.5 * r) * 50 + r * 50,
                "h1_regime": [
                    {
                        "DIRECTION": "UP",
                        "TREND_STRENGTH": "TRENDING",
                        "VOLATILITY": "NORMAL",
                        "VOL_STATE": "EXPANSION",
                    }
                ]
                * n,
            }
        )

    tr = pd.concat([trades(train_r, "2025-03-01", n_train), trades(val_r, "2025-12-15", n_val)])

    def m(part_r: float, n: int) -> dict:
        return {
            "trades": n,
            "expectancy_r": part_r,
            "profit_factor": 1.4 if part_r > 0 else 0.7,
            "max_drawdown_pct": 5.0,
        }

    return [
        {
            "id": "S#0",
            "strategy_id": "S",
            "strategy_version": "1",
            "variant": 0,
            "params": {"a": 1},
            "train": m(train_r, n_train),
            "validation": m(val_r, n_val),
            "_trades": tr,
            "_cands": [],
        }
    ]


class FakeLab:
    plan = PLAN

    def in_part(self, tr, part):
        return tr[PLAN.mask(tr["date"].to_numpy(), part)]

    def trades(self, cands, scenario):
        return fake_records(0.2, 0.2)[0]["_trades"]


def test_freeze_rule_rejects_early_when_negative_in_train_and_validation() -> None:
    frozen, rejected = runner.freeze_rule(CFG, FakeLab(), fake_records(-0.2, -0.1))
    assert frozen == [] and rejected[0]["strategy_id"] == "S"


def test_freeze_rule_freezes_only_with_positive_train_and_validation_and_min_samples() -> None:
    frozen, _ = runner.freeze_rule(CFG, FakeLab(), fake_records(0.2, 0.2))
    assert {f["role"] for f in frozen} >= {"WHOLE_VARIANT"}
    only_train, _ = runner.freeze_rule(CFG, FakeLab(), fake_records(0.2, -0.1))
    assert all(f["role"] != "WHOLE_VARIANT" for f in only_train)
    small, _ = runner.freeze_rule(CFG, FakeLab(), fake_records(0.2, 0.2, n_train=10, n_val=5))
    assert small == []


def test_oos_refuses_without_frozen_file_and_on_hash_mismatch(tmp_path) -> None:
    with pytest.raises(SystemExit, match="no frozen"):
        runner.run_oos(CFG, tmp_path)
    payload = {"hash": "0" * 64, "set": [{"strategy_id": "S"}]}
    (tmp_path / "frozen_candidates.json").write_text(json.dumps(payload))
    with pytest.raises(SystemExit, match="hash mismatch"):
        runner.run_oos(CFG, tmp_path)
    ok = {"set": []}
    ok["hash"] = stable_hash(ok["set"])
    (tmp_path / "frozen_candidates.json").write_text(json.dumps(ok))
    with pytest.raises(SystemExit, match="Nothing frozen"):
        runner.run_oos(CFG, tmp_path)


def test_lab_smoke_generates_simulates_and_measures_on_a_small_frame() -> None:
    from tests._alpha_group_c import bars

    frame = bars()
    frame["ts"] = frame["ts"].dt.tz_convert("UTC")
    lab = runner.Lab(CFG, frame, PLAN)
    variants = runner.discover_variants()[:2]
    generated = lab.candidates_many(variants, workers=1)
    assert set(generated) == {v["id"] for v in variants}
    trades = lab.trades(generated[variants[0]["id"]], "BASE")
    metrics = lab.metrics(trades, PLAN.train)
    assert metrics["trades"] == len(lab.in_part(trades, PLAN.train))
    assert "insufficient_sample" in metrics and lab.runs == 1


def test_insufficient_sample_flag_uses_configured_threshold() -> None:
    assert CFG["sample_rules"]["min_trades_flag"] == 30
