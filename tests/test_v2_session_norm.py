"""V2: session-conditioned (time-of-day rank) features - Train-only tables, causality, catalog."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from alpha.discovery import audit
from alpha.discovery.catalog import CATALOG, FeaturePool
from alpha.discovery.compile import ThresholdResolver, compile_genome
from alpha.discovery.genome import Clause, Genome, StopGene
from alpha.discovery.session_norm import (
    MIN_BUCKET_N,
    add_session_quantile_features,
    bucket_codes,
    fit_tables,
)
from alpha.fast.spec import FEATURE_NAMES
from alpha.fast.store import FeatureSet, FeatureStore
from alpha.session import SQ_BASE_FEATURES, SQ_FEATURE_NAMES, SessionCalendar

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "research/configs/ad1_discovery.json"
CACHE = REPO / "data/feature_store/ad1_bench_gram"
CAL = SessionCalendar()


def _fake(n_days: int = 6, seed: int = 0) -> tuple[FeatureSet, np.ndarray]:
    """One bar per 5 min over ``n_days`` local days; bar_range depends strongly on the bucket."""
    rng = np.random.default_rng(seed)
    minute = np.tile(np.arange(0, 1440, 5), n_days).astype(np.int16)
    n = len(minute)
    scale = np.select(
        [minute < 540, minute < 660, minute < 930, minute < 1200], [0.5, 3.0, 1.0, 2.0], 0.5
    )
    arrays = {name: rng.normal(0, 1, n) * scale for name in SQ_BASE_FEATURES}
    arrays["bar_range_atr"] = np.abs(arrays["bar_range_atr"])
    for name in ("brk_up_20", "brk_dn_20", "brk_up_48", "brk_dn_48"):
        arrays[name] = arrays[name] - 1.0  # mostly negative: breakouts are rare
    arrays["berlin_minute"] = minute
    arrays["ts_ns"] = np.arange(n, dtype=np.int64)
    train = np.arange(n) < n // 2
    return FeatureSet(arrays, {"cache_key": "base"}), train


def test_rank_is_bucket_conditional_and_in_unit_interval() -> None:
    f, train = _fake()
    out = add_session_quantile_features(f, train)
    codes = bucket_codes(f, CAL)
    for name in SQ_FEATURE_NAMES:
        v = out[name]
        assert np.nanmin(v) >= 0.0 and np.nanmax(v) <= 1.0
    sq = out["bar_range_atr_sq"]
    raw = f["bar_range_atr"]
    for code in range(len(CAL.bucket_names)):
        sel = train & (codes == code)
        # Train ranks are ~uniform inside EVERY bucket (the point of the conditioning) ...
        assert abs(float(np.mean(sq[sel])) - 0.5) < 0.03, code
    # ... whereas the raw value is bucket dependent: the same raw value ranks very differently
    x = float(np.quantile(raw[train & (codes == 1)], 0.5))
    low = np.flatnonzero(train & (codes == 3))[0]  # OVERNIGHT: narrow distribution
    mid = np.flatnonzero(train & (codes == 1))[0]
    probe = raw.copy()
    probe[[low, mid]] = x
    table = fit_tables(f, train, CAL, ("bar_range_atr",))
    ranks = table.rank("bar_range_atr", probe, codes)
    assert ranks[low] > 0.9 and abs(ranks[mid] - 0.5) < 0.05


def test_tables_read_train_bars_only_and_freeze_validation() -> None:
    f, train = _fake()
    base = fit_tables(f, train, CAL)
    altered = FeatureSet({k: np.array(v, copy=True) for k, v in f.items()}, f.metadata)
    for name in SQ_BASE_FEATURES:
        arr = altered[name].astype(float)
        arr[~train] = arr[~train] * 9.0 + 1000.0  # Validation / OOS / embargo values change
        arr[np.flatnonzero(~train)[::7]] = np.nan
        altered[name] = arr
    again = fit_tables(altered, train, CAL)
    assert again.fingerprint() == base.fingerprint()
    for name in SQ_BASE_FEATURES:
        for a, b in zip(base.tables[name], again.tables[name], strict=True):
            assert np.array_equal(a, b)
    # Train-partition bars are ranked identically whatever the non-Train values are
    a = add_session_quantile_features(f, train)
    b = add_session_quantile_features(altered, train)
    for name in SQ_FEATURE_NAMES:
        assert np.array_equal(a[name][train], b[name][train], equal_nan=True), name
    assert a.metadata["session_norm"]["fingerprint"] == b.metadata["session_norm"]["fingerprint"]
    # ... but the Train values DO define the table (fingerprint and cache key react)
    other = FeatureSet({k: np.array(v, copy=True) for k, v in f.items()}, f.metadata)
    other["bar_range_atr"] = other["bar_range_atr"] * 1.5
    c = add_session_quantile_features(other, train)
    assert c.metadata["session_norm"]["fingerprint"] != a.metadata["session_norm"]["fingerprint"]
    assert c.metadata["cache_key"] != a.metadata["cache_key"] != "base"
    # a different train mask (e.g. another split) gives another table
    d = add_session_quantile_features(f, np.arange(len(train)) < 200)
    assert d.metadata["cache_key"] != a.metadata["cache_key"]


def test_validation_uses_frozen_train_table_and_is_pure_per_bar() -> None:
    f, train = _fake()
    full = add_session_quantile_features(f, train)
    cut = int(len(train) * 0.8)  # prefix containing all Train bars plus part of Validation
    prefix = FeatureSet({k: np.array(v[:cut], copy=True) for k, v in f.items()}, f.metadata)
    part = add_session_quantile_features(prefix, train[:cut])
    for name in SQ_FEATURE_NAMES:
        assert np.array_equal(full[name][:cut], part[name], equal_nan=True), name
    # future values never leak backwards: changing a later bar changes nothing before it
    altered = FeatureSet({k: np.array(v, copy=True) for k, v in f.items()}, f.metadata)
    altered["bar_range_atr"][cut + 5] = 1e9
    again = add_session_quantile_features(altered, train)
    assert np.array_equal(full["bar_range_atr_sq"][: cut + 5], again["bar_range_atr_sq"][: cut + 5],
                          equal_nan=True)


def test_breakout_rank_is_zero_unless_a_fresh_breakout() -> None:
    f, train = _fake()
    out = add_session_quantile_features(f, train)
    for base in ("brk_up_20", "brk_dn_48"):
        raw, sq = f[base], out[f"{base}_sq"]
        assert (sq[raw <= 0.0] == 0.0).all()
        breakouts = raw > 0.0
        assert breakouts.any() and (sq[breakouts] > 0.0).all()
        # a clause "sq > t" with a Train quantile threshold can only select real breakouts
        t = float(np.quantile(sq[train], 0.9))
        assert not ((sq > t) & (raw <= 0.0)).any()


def test_nan_stays_nan_and_small_buckets_fall_back_to_the_pooled_table() -> None:
    f, train = _fake()
    arr = f["mom_3_atr"].copy()
    arr[10] = np.nan
    f["mom_3_atr"] = arr
    out = add_session_quantile_features(f, train)
    assert np.isnan(out["mom_3_atr_sq"][10])
    # keep < MIN_BUCKET_N finite Train values in one bucket -> pooled table for that bucket
    codes = bucket_codes(f, CAL)
    sparse = FeatureSet({k: np.array(v, copy=True) for k, v in f.items()}, f.metadata)
    keep = np.flatnonzero(train & (codes == 0))[: MIN_BUCKET_N - 1]
    mask = train & ((codes != 0) | np.isin(np.arange(len(codes)), keep))
    table = fit_tables(sparse, mask, CAL, ("mom_3_atr",))
    pooled = np.sort(np.asarray(sparse["mom_3_atr"])[mask & np.isfinite(sparse["mom_3_atr"])])
    assert np.array_equal(table.tables["mom_3_atr"][0], pooled)  # sparse bucket -> pooled
    assert len(table.tables["mom_3_atr"][1]) > MIN_BUCKET_N  # dense bucket keeps its own table
    with pytest.raises(ValueError):
        fit_tables(sparse, np.zeros(len(train), bool), CAL, ("mom_3_atr",))


# ------------------------------------------------------------------ catalog opt-in
def test_sq_catalog_entries_are_opt_in_and_mirror_in_rank_space() -> None:
    assert set(SQ_FEATURE_NAMES) <= FEATURE_NAMES
    for name in SQ_FEATURE_NAMES:
        entry = CATALOG[name]
        assert entry.session_conditioned and entry.feature == name
        assert CATALOG[name[: -len("_sq")]].session_conditioned is False
    f, train = _fake()
    plain = FeaturePool(set(f) | {"c"})
    assert not any(n in plain.names for n in SQ_FEATURE_NAMES)  # not in the FeatureSet -> absent
    pool = FeaturePool(set(add_session_quantile_features(f, train)))
    assert set(SQ_FEATURE_NAMES) <= pool.names
    assert CATALOG["mom_3_atr_sq"].mirror.kind == "reflect"
    assert CATALOG["mom_3_atr_sq"].mirror.center == 0.5
    assert CATALOG["brk_up_20_sq"].mirror.partner == "brk_dn_20_sq"


def test_sq_clause_compiles_and_short_mirrors_brk() -> None:
    f, train = _fake()
    aug = add_session_quantile_features(f, train)
    res = ThresholdResolver(aug, train)
    base = dict(trigger=(Clause("brk_up_20_sq", ">", 0.9),
                         Clause("bar_range_atr_sq", ">", 0.8)), stop=StopGene(), target_r=2.0)
    long_spec = compile_genome(Genome(direction="LONG", **base), res)
    short_spec = compile_genome(Genome(direction="SHORT", **base), res)
    assert {r.feature for r in long_spec.entry_rules} == {"brk_up_20_sq", "bar_range_atr_sq"}
    assert {r.feature for r in short_spec.entry_rules} == {"brk_dn_20_sq", "bar_range_atr_sq"}
    assert all(0.0 <= r.threshold <= 1.0 for r in long_spec.entry_rules)


# ------------------------------------------------------------------ real data: degeneracy
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


def test_sq_entries_not_always_true_on_real_data(env) -> None:
    aug = add_session_quantile_features(env["features"], env["mask"])
    pool = FeaturePool.from_features(aug)
    assert set(SQ_FEATURE_NAMES) <= pool.names
    rows = audit.degeneracy_rows(aug, env["mask"], pool)
    sq_rows = [r for r in rows if r["feature"] in SQ_FEATURE_NAMES]
    assert sq_rows
    always = {f: d for f, d in audit.flagged(sq_rows).items() if d["n_always"]}
    assert not always, audit.render_table(always)
    # the conditioning does its job: per-bucket Train rates of a high-rank clause are ~equal,
    # unlike the raw global-quantile clause
    codes = bucket_codes(aug, CAL)
    train = env["mask"]
    raw = np.asarray(aug["bar_range_atr"])
    sq = np.asarray(aug["bar_range_atr_sq"])
    thr_raw = np.nanquantile(raw[train], 0.8)
    thr_sq = np.nanquantile(sq[train], 0.8)
    spread_raw, spread_sq = [], []
    for code in range(len(CAL.bucket_names)):
        sel = train & (codes == code)
        if sel.sum() > 500:
            spread_raw.append(float(np.mean(raw[sel] > thr_raw)))
            spread_sq.append(float(np.mean(sq[sel] > thr_sq)))
    assert max(spread_sq) - min(spread_sq) <= max(spread_raw) - min(spread_raw) + 1e-9
