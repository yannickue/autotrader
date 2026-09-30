# ruff: noqa: E501
"""Structural seal (module-source scan, like the temporal / formula modules) + forward-holdout guard + gate behaviour."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha.common.market_data import ForwardHoldoutError
from alpha.discovery.folds import berlin_dates_from_ts_ns, make_folds
from alpha.families import gate as gate_mod
from alpha.families import registry as R
from alpha.families.data import build_family_data, build_leader_features
from alpha.families.evaluate import SimContext, evaluate_grid
from tests.test_v2_families_synth import CAL, synth_data, synth_frame

ROOT = Path(__file__).resolve().parents[1]
SEARCH_SIDE = ("spec", "data", "common", "orb", "gap", "overnight", "volrev", "roundnum", "leadlag", "eod", "registry", "evaluate", "__init__")
RUNNER = ROOT / "research/runners/v2_family_probe.py"
FORBIDDEN = ("validation", "_validation", "test_mask", "fold_test_masks", "survival", "families.gate", "import gate", "families import gate",
             "splitplan.validation", "split.validation", "make_folds", "searchsplitplan")


def _src(name: str) -> str:
    return Path(importlib.import_module(f"alpha.families.{name}").__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", SEARCH_SIDE)
def test_search_side_modules_never_mention_later_partitions(name):
    src = _src(name).lower()
    for token in FORBIDDEN:
        assert token not in src, (name, token)
    assert not re.search(r"\boos\b", src), name
    assert "splitplan" not in src, name


def test_gate_is_the_single_accessor_and_imported_by_nobody_in_src():
    assert _src("gate").count("def survival_gate") == 1
    assert "test_mask" in _src("gate")  # it is the only src module that reads fold test masks
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "gate.py" and path.parent.name == "families":
            continue
        text = path.read_text(encoding="utf-8")
        assert "families.gate" not in text and "families import gate" not in text, path
    for path in (ROOT / "src/alpha/families").glob("*.py"):
        if path.name != "gate.py":
            assert "test_mask" not in path.read_text(encoding="utf-8"), path


def test_probe_never_imports_the_gate_or_touches_test_masks():
    src = RUNNER.read_text(encoding="utf-8")
    for token in ("families.gate", "families import gate", "survival_gate", "test_mask", "fold_test_masks", "light_screen", "tests_chronological"):
        assert token not in src, token
    assert "plan.train" in src and src.count("plan.mask(") == 1  # the only partition read is Train
    assert "plan.validation" not in src and "plan.oos" not in src
    assert "del frame" in src  # the full development frame is dropped right after the Train cut


def test_train_slice_contains_no_later_bar():
    from research.runners.v2_family_probe import train_slice

    frame = synth_frame(n_days=330, seed=2)
    cfg = {"folds": {"n_folds": 4, "embargo_days": 5, "purge_bars": 12, "initial_train_frac": 0.4, "min_test_days": 20}}
    tr, layout = train_slice(frame, cfg)
    ts = pd.DatetimeIndex(frame["ts"]).as_unit("ns").asi8
    folds = make_folds(berlin_dates_from_ts_ns(ts), 4, 5, 12, initial_train_frac=0.4, min_test_days=20)
    m = folds[0].train_mask
    assert len(tr) == int(m.sum()) and m[: len(tr)].all() and not m[len(tr):].any()
    assert tr["ts"].max() < frame["ts"].iloc[int(np.flatnonzero(folds[0].test_mask)[0])]  # strictly before fold-0 test
    assert layout["folds"][0]["train_bars"] == len(tr)


def test_forward_holdout_is_refused():
    frame = synth_frame(n_days=6, seed=0, start="2026-08-28")  # 28 Aug, 31 Aug, 1 Sep (forward holdout)
    with pytest.raises(ForwardHoldoutError):
        build_family_data(frame, CAL, name="X", point_size=0.01, tick_size=0.01)
    with pytest.raises(ForwardHoldoutError):
        build_leader_features(synth_frame(n_days=3, seed=0), frame, CAL, "L")
    ok = synth_frame(n_days=3, seed=0, start="2026-08-26")  # 26..28 Aug: fine
    assert len(build_family_data(ok, CAL, name="X", point_size=0.01, tick_size=0.01)) == len(ok)


@pytest.mark.skipif(not (ROOT / "data/ar1_ger40").exists(), reason="AR1 GER40 dataset not present")
def test_real_loader_never_returns_a_bar_after_the_dev_end():
    from alpha.common.market_data import DEV_END, load_dev_market_frame
    from markets.spec import load_market_spec

    fr = load_dev_market_frame(load_market_spec("GER40"), source="ar1")
    d = berlin_dates_from_ts_ns(pd.DatetimeIndex(fr["ts"]).as_unit("ns").asi8)
    assert d.max() <= np.datetime64(DEV_END)


# ----------------------------------------------------------------------------- gate behaviour
def test_gate_fits_on_train_only_and_reports_fold_test_numbers(monkeypatch):
    d = synth_data(n_days=420, seed=4, plant="gap_fade", phi=0.4, start="2024-06-03")
    dates = berlin_dates_from_ts_ns(d.ts_ns)
    folds = make_folds(dates, 4, 5, 12, initial_train_frac=0.4, min_test_days=20)
    stop = int(np.flatnonzero(folds[0].train_mask).max()) + 1
    train = d.prefix(stop)
    ctx_train, ctx_full = SimContext.default(train), SimContext.default(d)
    res, _ = evaluate_grid(train, ctx_train, R.grid_for("GAP", "GER40", 150))
    top = [r.spec for r in sorted(res, key=lambda r: -r.fitness)[:3]]
    seen = []
    real = gate_mod.fit_thresholds
    monkeypatch.setattr(gate_mod, "fit_thresholds", lambda tv, s: (seen.append(len(tv)), real(tv, s))[1])
    rows = gate_mod.survival_gate(top, full_data=d, ctx=ctx_full, folds=folds)
    assert seen == [stop] * 3  # thresholds fitted on the fold-0 Train bars only
    assert len(rows) == 3
    for row in rows:
        assert len(row.folds) == 4
        assert row.pooled_n_trades == sum(f.n_trades for f in row.folds)  # pooled = union of the TEST masks
        assert row.n_positive_folds >= 3 and row.pooled_expectancy_r > 0 and row.pooled_t > 1.5  # the planted edge survives
    # trades that enter outside every fold test (train side, embargo) are not counted
    all_test = np.zeros(len(d), dtype=bool)
    for f in folds:
        all_test |= f.test_mask
    assert not all_test[: stop].any()
