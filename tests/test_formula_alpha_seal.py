# ruff: noqa: E501
"""Structural seal: the formula search never reaches later partitions; ``gate`` is the single accessor."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import numpy as np
import pytest

from alpha.common.sim import COST_SCENARIOS
from alpha.fast.sim import simulate_fast
from alpha.formula import gate as gate_mod
from alpha.formula import signal as sg
from alpha.formula.fitness import FactorScorer
from alpha.formula.prepare import make_train_view
from alpha.formula.tree import EvalContext, T, op
from tests.test_formula_alpha_synth import planted_data, split_plan_for

SEARCH_SIDE = ("ops", "data", "tree", "fitness", "gp", "signal", "prepare", "evaluate", "__init__")
ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "research/runners/v2_formula_probe.py"


def _src(name: str) -> str:
    return Path(importlib.import_module(f"alpha.formula.{name}").__file__).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", SEARCH_SIDE)
def test_search_side_modules_never_mention_later_partitions(name):
    src = _src(name).lower()
    for token in ("validation", "_validation", "splitplan.validation", "split.validation"):
        assert token not in src, (name, token)
    assert not re.search(r"oos", src), name
    assert "formula.gate" not in src and "import gate" not in src and "validation_gate" not in src
    if name != "prepare":  # only the view builder may see the plan, and only its Train partition
        assert "splitplan" not in src and "split." not in src, name


def test_prepare_reads_only_the_train_partition():
    src = _src("prepare")
    assert "plan.train" in src and src.count("plan.mask(") == 1
    for token in ("plan.validation", "plan.oos", "embargo"):
        assert token not in src


def test_gate_is_the_single_accessor_and_is_imported_by_nobody_in_src():
    assert _src("gate").count("def validation_gate_screen") == 1
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "gate.py" and path.parent.name == "formula":
            continue
        text = path.read_text(encoding="utf-8")
        assert "formula.gate" not in text and "formula import gate" not in text, path
    if RUNNER.exists():  # the probe never selects on or even computes later-partition numbers
        rsrc = RUNNER.read_text(encoding="utf-8")
        assert "formula.gate" not in rsrc and "validation_gate_screen" not in rsrc and "light_screen" not in rsrc


def test_gate_uses_train_only_thresholds_and_embargoed_validation_trades(monkeypatch):
    fd, market, dates = planted_data(40, 2, beta=0.25)
    plan = split_plan_for(dates)
    view = make_train_view(fd, market, dates, plan)
    scorer, ctx = FactorScorer(view.data), EvalContext(view.data)
    node = op("Zscore", T("c"), params=(10,))
    score = scorer.score_array(ctx.evaluate(node), 2)
    spec = sg.SignalSpec(sign=score.sign, q=0.9)

    seen = []
    real = gate_mod.fit_thresholds

    def spy(g, minute, q, session):
        seen.append(len(g))
        return real(g, minute, q, session)

    monkeypatch.setattr(gate_mod, "fit_thresholds", spy)
    val = gate_mod.validation_gate_screen(fd, market, dates, plan, node, score, spec)
    assert seen == [view.stop - view.start]  # thresholds fitted on the Train bars only

    # independent recomputation: only trades ENTERING inside the embargoed Validation window count
    f = EvalContext(fd).evaluate(node)
    thr = real(spec.sign * f[view.start:view.stop], fd.minute[view.start:view.stop], spec.q, False)
    cands = sg.build_candidates(f, fd, spec, thr)
    trades = simulate_fast(market, cands, COST_SCENARIOS["COMBINED_ADVERSE"])
    vmask = plan.mask(dates[trades.entry_idx], plan.validation)
    assert val.n_trades == int(vmask.sum()) and val.n_trades > 0
    first_ok = np.datetime64(plan.validation.start) + np.timedelta64(plan.embargo_days, "D")
    assert (dates[trades.entry_idx][vmask] >= first_ok).all()


def test_train_view_is_train_only():
    fd, market, dates = planted_data(30, 4)
    plan = split_plan_for(dates)
    view = make_train_view(fd, market, dates, plan)
    assert view.dates.max() <= np.datetime64(plan.train.end) and view.dates.min() >= np.datetime64(plan.train.start)
    assert len(view.data) == len(view.market.o) == view.stop - view.start < len(fd)
    assert view.market.contig_next[-1] == np.False_
    assert view.data.run_start.min() == 0 and (view.data.run_start <= np.arange(len(view.data))).all()
    # non-contiguous Train (a shuffled date vector) is refused
    bad = dates.copy()
    bad[10] = dates[-1]
    with pytest.raises(ValueError):
        make_train_view(fd, market, bad, plan)
    assert re.search(r"\bTrainView\b", _src("evaluate"))
