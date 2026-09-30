"""V2: directional context flags, directional catalog mirror, archetypes (research only)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpha.context import DIRECTIONAL_LABELS, GEOMETRY_COLUMNS, directional_context
from alpha.discovery import audit
from alpha.discovery.archetypes import ARCHETYPES
from alpha.discovery.catalog import CATALOG, FeaturePool
from alpha.discovery.compile import ThresholdResolver, canonicalize, compile_genome
from alpha.discovery.genome import Clause, Genome, StopGene
from alpha.fast.spec import FEATURE_NAMES
from alpha.fast.store import DIRECTIONAL_CONTEXT_NAMES, FeatureConfig, FeatureStore
from tests.test_alpha_fast_price_action import _synthetic_multi_day

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "research/configs/ad1_discovery.json"
CACHE = REPO / "data/feature_store/ad1_bench_gram"
PAIRS = (
    ("pullback_up", "pullback_down"),
    ("breakout_setup_up", "breakout_setup_down"),
    ("retest_up", "retest_down"),
    ("momentum_continuation_up", "momentum_continuation_down"),
    ("reversal_up", "reversal_down"),
    ("failed_breakout_up", "failed_breakout_down"),
    ("range_extreme_bottom", "range_extreme_top"),
)
AGNOSTIC_REMOVED = (
    "pullback", "range_extreme", "breakout_setup", "retest", "failed_breakout",
    "momentum_continuation", "reversal_context",
)


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    return a.dtype == b.dtype and np.array_equal(a, b, equal_nan=True)


def _trending(drift: float, days: int = 30) -> pd.DataFrame:
    frame = _synthetic_multi_day("2024-01-08", days, seed=3)
    shift = np.cumsum(np.full(len(frame), drift))
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column] + shift
    return frame


# ------------------------------------------------------------------ unit: assembly rules
def _geo(n: int = 4, **true: list[int]) -> pd.DataFrame:
    frame = pd.DataFrame({"defined": np.ones(n, bool)})
    for column in GEOMETRY_COLUMNS:
        frame[column] = False
    for name, rows in true.items():
        frame.loc[rows, f"geo_{name}"] = True
    return frame


def test_h1_gate_and_direction_rules_hand_made() -> None:
    geo = _geo(
        4,
        pullback_up=[0, 1, 2, 3],
        pullback_down=[0, 1, 2, 3],
        momentum_up=[0, 1],
        momentum_down=[2, 3],
        reversal_up=[0, 1, 2],
        reversal_down=[0, 1, 2],
        prior_down=[1],
        breakout_up=[0],
        failed_up=[3],
        extreme_top=[2],
        extreme_bottom=[1],
    )
    h1_up = np.array([True, False, False, False])
    h1_down = np.array([False, True, False, False])
    flags = directional_context(geo, h1_up, h1_down)
    assert set(flags) == {label.lower() for label in DIRECTIONAL_LABELS}
    # pullback / momentum need the H1 trend of the same direction
    assert flags["pullback_up"].tolist() == [True, False, False, False]
    assert flags["pullback_down"].tolist() == [False, True, False, False]
    assert flags["momentum_continuation_up"].tolist() == [True, False, False, False]
    assert flags["momentum_continuation_down"].tolist() == [False, False, False, False]
    # reversal_up = bullish geometry AND prior downtrend evidence (H1 DOWN or M15 prior slope down)
    assert flags["reversal_up"].tolist() == [False, True, False, False]
    assert flags["reversal_down"].tolist() == [True, False, False, False]  # H1 UP -> against trend
    # ungated M15 structure flags
    assert flags["breakout_setup_up"].tolist() == [True, False, False, False]
    assert flags["failed_breakout_up"].tolist() == [False, False, False, True]
    assert flags["range_extreme_top"].tolist() == [False, False, True, False]
    assert flags["range_extreme_bottom"].tolist() == [False, True, False, False]


def test_undefined_context_is_never_directional() -> None:
    geo = _geo(2, pullback_up=[0, 1], breakout_up=[0, 1], extreme_top=[0, 1])
    geo.loc[1, "defined"] = False
    flags = directional_context(geo, np.ones(2, bool), np.zeros(2, bool))
    assert all(not value[1] for value in flags.values())


# ------------------------------------------------------------------ integration on the store
@pytest.mark.parametrize("drift", [-0.03, 0.03])
def test_flags_follow_the_trend_and_gates_hold(drift: float) -> None:
    f = FeatureStore.build(_trending(drift), FeatureConfig())
    up, down = f["regime_direction"] == 3, f["regime_direction"] == 1
    assert not (f["context_pullback_up"] & ~up).any()
    assert not (f["context_pullback_down"] & ~down).any()
    assert not (f["context_momentum_continuation_up"] & ~up).any()
    assert not (f["context_momentum_continuation_down"] & ~down).any()
    assert not (f["context_pullback_up"] & f["context_pullback_down"]).any()
    both = f["context_momentum_continuation_up"] & f["context_momentum_continuation_down"]
    assert not both.any()
    with_trend, against = (
        ("context_pullback_up", "context_pullback_down") if drift > 0
        else ("context_pullback_down", "context_pullback_up")
    )
    # in an up (down) trend the direction-agnostic flag hides which side; the V2 flags separate it
    assert f[with_trend].sum() > 5 * f[against].sum()
    assert f["context_pullback"].sum() > 0  # V1 array untouched and still present


def test_truncation_invariance_of_directional_flags_across_boundaries_and_dst() -> None:
    frame = _synthetic_multi_day("2024-03-25", 14)
    full = FeatureStore.build(frame, FeatureConfig())
    boundary = int(np.flatnonzero(np.diff(full["berlin_day_id"]) != 0)[2]) + 1
    dst_bar = int(np.searchsorted(pd.DatetimeIndex(frame["ts"]),
                                  pd.Timestamp("2024-04-01", tz="UTC")))
    for cut in (400, boundary - 1, boundary, boundary + 1, dst_bar - 1, dst_bar + 25,
                len(frame) - 3):
        short = FeatureStore.build(frame.iloc[:cut].copy().reset_index(drop=True), FeatureConfig())
        for name in DIRECTIONAL_CONTEXT_NAMES:
            assert _same(full[name][:cut], short[name]), (name, cut)


# ------------------------------------------------------------------ catalog / compile / archetypes
def test_registered_names_and_removed_agnostic_catalog_entries() -> None:
    assert set(DIRECTIONAL_CONTEXT_NAMES) <= FEATURE_NAMES
    assert len(DIRECTIONAL_CONTEXT_NAMES) == 14
    for old in AGNOSTIC_REMOVED:
        assert f"context_{old}" not in CATALOG  # symmetric 'self' mirror replaced
    for long_name, short_name in PAIRS:
        entry = CATALOG[f"context_{long_name}"]
        assert entry.mirror.kind == "pair"
        assert entry.mirror.partner_feature == f"context_{short_name}"
        assert f"context_{short_name}" not in CATALOG  # only the LONG frame is sampled
    for still in ("trend_continuation", "consolidation", "compression"):
        assert CATALOG[f"context_{still}"].mirror.kind == "self"


@pytest.mark.parametrize("long_name,short_name", PAIRS)
def test_long_uses_up_and_short_mirrors_to_down(long_name: str, short_name: str) -> None:
    res = ThresholdResolver({"m5_ema_slope": np.linspace(-1, 1, 50)}, np.ones(50, bool))
    base = dict(context=(Clause(f"context_{long_name}", "==", None),),
                trigger=(Clause("bar_dir", ">", None),), stop=StopGene(), target_r=2.0)
    long_spec = compile_genome(Genome(direction="LONG", **base), res)
    short_spec = compile_genome(Genome(direction="SHORT", **base), res)
    assert long_spec.context_filters == (long_name.upper(),)
    assert short_spec.context_filters == (short_name.upper(),)
    assert f"context_{short_name}" in FEATURE_NAMES


def test_pool_requires_the_partner_raw_feature() -> None:
    names = set(FEATURE_NAMES)
    assert FeaturePool(names).has("context_pullback_up")
    assert not FeaturePool(names - {"context_pullback_down"}).has("context_pullback_up")


def test_archetypes_use_only_directional_flags() -> None:
    pool = FeaturePool(set(FEATURE_NAMES))
    rng = np.random.default_rng(0)
    seen: set[str] = set()
    for fn in ARCHETYPES.values():
        for _ in range(30):
            g = canonicalize(fn(rng, pool))
            seen |= {c.feature for c in g.context if c.feature.startswith("context_")}
    assert seen and seen <= set(CATALOG)
    assert {"context_pullback_up", "context_retest_up", "context_range_extreme_bottom",
            "context_reversal_up", "context_failed_breakout_up"} <= seen


# ------------------------------------------------------------------ degeneracy on real data
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
    return {"features": features, "mask": plan.mask(dates, plan.train)}


def test_directional_flags_are_neither_always_nor_never_true(env) -> None:
    f, mask = env["features"], env["mask"]
    for name in DIRECTIONAL_CONTEXT_NAMES:
        frac = float(np.mean(np.asarray(f[name])[mask]))
        assert 0.005 < frac < 0.6, (name, frac)


def test_v2_catalog_entries_stay_non_degenerate(env) -> None:
    """Degeneracy audit over the V2 catalog entries (directional flags, cash levels)."""
    pool = FeaturePool.from_features(env["features"])
    v2 = {n for n in pool.names
          if n.endswith("_cash_atr") or n.startswith(("dist_ovn", "lvl_c_p", "context_"))
          or n.endswith("_up") or n.endswith("_bottom")}
    assert {"context_pullback_up", "dist_pdh_cash_atr", "gap_cash_atr"} <= v2
    rows = [r for r in audit.degeneracy_rows(env["features"], env["mask"], pool)
            if r["feature"] in v2]
    assert rows
    always = {f: d for f, d in audit.flagged(rows).items() if d["n_always"]}
    assert not always, audit.render_table(always)
