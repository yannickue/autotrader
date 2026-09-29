"""Catalog degeneracy audit + floored threshold domains (research only)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from alpha.discovery import audit
from alpha.discovery.archetypes import ARCHETYPES
from alpha.discovery.catalog import CATALOG, FeaturePool
from alpha.discovery.compile import ThresholdResolver, _snap_clause, canonicalize
from alpha.discovery.genome import Clause
from alpha.fast.store import FeatureStore

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "research/configs/ad1_discovery.json"
CACHE = REPO / "data/feature_store/ad1_bench_gram"


@pytest.fixture(scope="module")
def env():
    from alpha.common.dataset import POINT, load_research_dataset
    from research.runners import ar2_fast

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    root = REPO / cfg["dataset_root"]
    if not root.exists():
        pytest.skip(f"dev dataset {root} not present (data/ar1_ger40 must be copied)")
    plan = ar2_fast._plan(cfg)
    dev = ar2_fast.dev_frame(load_research_dataset(root).frame, plan)
    features = FeatureStore.load_or_build(dev, {"point_size": POINT}, CACHE)
    dates = ar2_fast._dates(features)
    return {"features": features, "mask": plan.mask(dates, plan.train), "pool":
            FeaturePool.from_features(features)}


def test_resolver_floor_and_brk_domains(env):
    res = ThresholdResolver(env["features"], env["mask"])
    raw = res.value("brk_up_20", 0.5)
    assert raw < 0.0 <= res.value("brk_up_20", 0.5, 0.0)  # median is not a breakout; floor fixes
    assert res.value("brk_up_20", 0.99, 0.0) == res.value("brk_up_20", 0.99) > 0.0
    for name in ("brk_up_20", "brk_dn_20", "brk_up_48", "brk_dn_48"):
        e = CATALOG[name]
        assert e.floor == 0.0 and e.ops == (">",) and (e.q_lo, e.q_hi) == (0.90, 0.99)
        for q in audit.q_points(e):
            assert res.value(e.feature, q, e.floor) >= 0.0
    # grid rounding stays inside the domain (0.99 is not on the 0.05 grid)
    assert _snap_clause(Clause("brk_up_20", ">", 0.999, ())).q == 0.99


def test_no_catalog_clause_is_always_true_and_flagged_table(env, capsys):
    rows = audit.degeneracy_rows(env["features"], env["mask"], env["pool"])
    assert len(rows) > 1000
    flags = audit.flagged(rows)
    always = {f: d for f, d in flags.items() if d["n_always"]}
    assert not always, audit.render_table(always)  # nothing > 95% true under declared domains
    print(audit.render_table(flags))  # rare-only entries are reported, not failures
    # regression: the original degeneracies stay fixed
    brk = [r for r in rows if r["feature"] == "brk_up_20" and r["direction"] == "LONG"]
    assert brk and all(r["op"] == ">" and 0.005 <= r["frac"] <= 0.2 for r in brk)


def test_archetype_label_clauses_are_proper_subsets(env):
    rng = np.random.default_rng(4)
    for fn in ARCHETYPES.values():
        for _ in range(15):
            g = canonicalize(fn(rng, env["pool"]))
            for c in g.regime + g.context + g.trigger:
                if c.labels:
                    assert len(c.labels) < len(CATALOG[c.feature].labels)
