# ruff: noqa: E501
"""Real GER40 dev-frame checks: the FeatureStore columns used as terminals are causal; formulas are causal on real data."""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import pytest
from deap import gp as deap_gp

from alpha.formula import ops, tree
from alpha.formula.data import build_formula_data
from alpha.formula.fitness import forward_labels
from alpha.formula.gp import build_pset, to_node
from alpha.formula.tree import EvalContext

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "research/configs/ar2_phase2.json"
N_SHORT, N_LONG = 9000, 13001

pytestmark = pytest.mark.skipif(not (ROOT / "data/ar1_ger40").exists(), reason="AR1 GER40 dataset not present")


@pytest.fixture(scope="module")
def stores():
    from alpha.common.dataset import POINT, load_research_dataset
    from alpha.fast.store import FeatureStore
    from research.runners import ar2_fast

    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    plan = ar2_fast._plan(cfg)
    ds = load_research_dataset(ROOT / cfg["dataset_root"])
    df = ar2_fast.dev_frame(ds.frame, plan)
    short = FeatureStore.build(df.iloc[:N_SHORT].reset_index(drop=True), {"point_size": POINT})
    long = FeatureStore.build(df.iloc[:N_LONG].reset_index(drop=True), {"point_size": POINT})
    return short, long


def test_terminal_features_are_truncation_invariant(stores):
    """A column built from the first 9000 bars equals the same column built from 13001 bars, on those 9000."""
    short, long = stores
    missing = [n for n in ops.FRAME_FEATURE_TERMINALS if n not in short]
    assert not missing, missing
    bad = []
    for name in ops.FRAME_FEATURE_TERMINALS:
        a, b = np.asarray(short[name]), np.asarray(long[name])[:N_SHORT]
        if not np.array_equal(a, b, equal_nan=a.dtype.kind == "f"):
            bad.append(name)
    assert not bad, f"non-causal terminal columns: {bad}"


def _fd(store, n=None):
    from alpha.fast.store import _run_start

    day = np.asarray(store["berlin_day_id"], dtype=np.int64)
    contig = np.asarray(store["contig"], dtype=bool)
    rs = _run_start(day, contig).astype(np.int64)
    fd = build_formula_data(store["o"], store["h"], store["l"], store["c"], store["m5_atr14"], rs,
                            store["berlin_minute"], store)
    return fd if n is None else fd.prefix(n)


def test_random_formulas_on_real_frame_are_causal_and_finite(stores):
    _, long = stores
    fd = _fd(long)
    assert set(ops.FRAME_FEATURE_TERMINALS) <= set(fd.arrays) and set(ops.BAR_TERMINALS) <= set(fd.arrays)
    random.seed(11)
    pset = build_pset(list(fd.terminals))
    nodes = []
    while len(nodes) < 40:
        n = to_node(deap_gp.PrimitiveTree(deap_gp.genHalfAndHalf(pset, 1, 3)))
        if tree.size(n) <= tree.MAX_NODES:
            nodes.append(n)
    m = 7000
    full_ctx, pre_ctx = EvalContext(fd), EvalContext(fd.prefix(m))
    for n in nodes:
        a = full_ctx.evaluate(n)
        b = pre_ctx.evaluate(n)
        assert not np.isinf(a).any()
        assert np.array_equal(np.nan_to_num(a[:m], nan=-9e99), np.nan_to_num(b, nan=-9e99)), n.key


def test_real_labels_never_cross_a_day_boundary(stores):
    _, long = stores
    fd = _fd(long)
    y = forward_labels(fd, (6, 24))
    idx = np.flatnonzero(np.isfinite(y[24]))
    assert (fd.run_start[idx + 24] == fd.run_start[idx]).all()
    assert (fd.run_start[np.flatnonzero(np.isfinite(y[6])) + 6] == fd.run_start[np.flatnonzero(np.isfinite(y[6]))]).all()
