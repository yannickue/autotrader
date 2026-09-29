from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from alpha.discovery.archetypes import ARCHETYPES, random_genome
from alpha.discovery.catalog import CATALOG, FeaturePool
from alpha.discovery.compile import (
    ThresholdResolver,
    TrialLedger,
    behavior_key,
    canonical_hash,
    canonicalize,
    compile_genome,
)
from alpha.discovery.genome import (
    Clause,
    Genome,
    GenomeError,
    StopGene,
    complexity,
    is_valid,
    validate,
)
from alpha.discovery.search import (
    LINEAGE_MAX_LEN,
    _quantile_clauses,
    crossover,
    lineage_family,
    make_lineage,
    mutate_structure,
    param_space,
    parse_lineage,
    with_params,
)
from alpha.fast.spec import evaluate_spec
from alpha.fast.store import FeatureSet, FeatureStore, _price_action_arrays

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "research/configs/ad1_discovery.json"
CACHE = REPO / "data/feature_store/ad1_bench_gram"
NEW_FEATURES = [
    "dist_pdh_atr", "dist_pdl_atr", "dist_pdc_atr", "dist_sess_open_atr", "dist_sess_high_atr",
    "dist_sess_low_atr", "dist_swing_high_atr", "dist_swing_low_atr", "brk_up_20", "brk_dn_20",
    "brk_up_48", "brk_dn_48", "from_high_24_atr", "from_low_24_atr", "range_ratio_12_48",
    "bar_range_atr", "bar_body_ratio", "bar_close_loc", "upper_wick_ratio", "lower_wick_ratio",
    "bar_dir", "mom_3_atr", "mom_6_atr", "mom_12_atr", "sweep_hi_20", "sweep_lo_20",
    "sweep_pdh", "sweep_pdl", "gap_atr",
]


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
    resolver = ThresholdResolver.from_plan(features, plan, dates)
    return {"features": features, "plan": plan, "dates": dates, "resolver": resolver,
            "pool": FeaturePool.from_features(features)}


def _genomes(pool, seed, n):
    rng = np.random.default_rng(seed)
    return [random_genome(rng, pool) for _ in range(n)]


def test_determinism_per_seed(env):
    a = [g.to_json() for g in _genomes(env["pool"], 5, 200)]
    b = [g.to_json() for g in _genomes(env["pool"], 5, 200)]
    c = [g.to_json() for g in _genomes(env["pool"], 6, 200)]
    assert a == b and a != c


def test_pool_is_catalog_intersection_and_adapts(env):
    pool = env["pool"]
    assert pool.names <= set(CATALOG)
    assert {"h1_adx14", "m5_rsi14", "context_pullback", "regime_direction"} <= pool.names
    assert all(f in env["features"] or f == "c" for e in pool.entries.values()
               for f in e.required_features())


def test_generated_genomes_validate_compile_and_evaluate(env):
    genomes = _genomes(env["pool"], 11, 600)
    lineages = set()
    for g in genomes:
        validate(g)
        spec = compile_genome(g, env["resolver"])
        cand = evaluate_spec(env["features"], spec)
        assert len(cand.decision_idx) == len(cand.stop)
        lineages.add(g.lineage)
        assert spec.metadata["genome_hash"] == canonical_hash(g)
        assert spec.metadata["complexity"] == complexity(canonicalize(g))
    assert lineages >= set(ARCHETYPES)  # every family reachable
    assert "HYBRID" in lineages


def test_every_archetype_yields_valid_compilable_genomes(env):
    rng = np.random.default_rng(3)
    for name, fn in ARCHETYPES.items():
        for _ in range(20):
            g = canonicalize(fn(rng, env["pool"]))
            assert g.lineage == name and is_valid(g)
            evaluate_spec(env["features"], compile_genome(g, env["resolver"]))


def test_canonicalization_equivalence(env):
    rng = np.random.default_rng(9)
    py = random.Random(9)
    for g in _genomes(env["pool"], 21, 300):
        base = canonical_hash(g)
        assert canonical_hash(canonicalize(g)) == base  # idempotent

        def shuffled(t):
            t = list(t)
            py.shuffle(t)
            return tuple(t)

        def jitter(c):
            return c if c.q is None else replace(c, q=c.q + float(rng.uniform(-0.02, 0.02)))

        h = replace(
            g, regime=tuple(jitter(c) for c in shuffled(g.regime)),
            context=tuple(jitter(c) for c in shuffled(g.context)),
            trigger=tuple(jitter(c) for c in shuffled(g.trigger)),
            or_group=tuple(jitter(c) for c in shuffled(g.or_group)),
            stop=replace(g.stop, multiple=None if g.stop.multiple is None
                         else g.stop.multiple + float(rng.uniform(-0.04, 0.04))),
            target_r=g.target_r + float(rng.uniform(-0.1, 0.1)), lineage="OTHER",
        )
        # jitter may leave the q domain edge; canonical form clamps, so the hash must agree
        assert canonical_hash(h) == base


def test_canonicalization_drops_duplicates_contradictions_and_aliases():
    base = dict(direction="LONG", stop=StopGene(), target_r=2.0)
    a = Clause("mom_3_atr", ">", 0.6)
    tight = Clause("mom_3_atr", ">", 0.8)
    g1 = Genome(trigger=(a, tight, Clause("m5_rsi14", "<", 0.3)), **base)
    g2 = Genome(trigger=(tight, Clause("m5_rsi14", "<", 0.3)), **base)
    assert canonical_hash(g1) == canonical_hash(g2)  # same direction twice -> tighter kept
    contra = Genome(trigger=(Clause("mom_3_atr", ">", 0.8), Clause("mom_3_atr", "<", 0.4)), **base)
    assert len(canonicalize(contra).trigger) == 1  # empty band dropped
    band = Genome(trigger=(Clause("mom_3_atr", ">", 0.4), Clause("mom_3_atr", "<", 0.8)), **base)
    assert len(canonicalize(band).trigger) == 2
    ali = Genome(regime=(Clause("h1_adx14", ">", 0.5),
                         Clause("regime_trend_strength", "in", None, ("TRENDING",))),
                 trigger=(a,), **base)
    assert [c.feature for c in canonicalize(ali).regime] == ["h1_adx14"]
    rsi = Clause("m5_rsi14", "<", 0.3)
    dup_or = Genome(trigger=(a,), or_group=(rsi, rsi), **base)
    c = canonicalize(dup_or)
    assert not c.or_group and len(c.trigger) == 2


def test_complexity_limits_rejected():
    base = dict(direction="LONG", stop=StopGene(), target_r=2.0)
    t = Clause("m5_rsi14", "<", 0.3)
    r = Clause("h1_adx14", ">", 0.5)
    c = Clause("m15_adx14", ">", 0.5)
    validate(Genome(regime=(r, r), context=(c, c), trigger=(t, t), **base))  # 6 clauses ok
    bad = [
        Genome(regime=(r, r, r), trigger=(t,), **base),
        Genome(context=(c, c, c), trigger=(t,), **base),
        Genome(trigger=(t, t, t, t), **base),
        Genome(regime=(r, r), context=(c, c), trigger=(t, t, t), **base),  # 7 > MAX_TOTAL
        Genome(regime=(r, r), context=(c,), trigger=(t,), or_group=(t, t), **base),  # 6 + 0? 6 ok
        Genome(trigger=(), **base),
        Genome(trigger=(t,), or_group=(t,), **base),
        Genome(trigger=(Clause("h1_adx14", ">", 0.5),), **base),  # wrong layer
        Genome(trigger=(Clause("m5_rsi14", ">", 0.999),), **base),  # q outside domain
        Genome(trigger=(t,), time_window=(600, 610), **base),
        Genome(trigger=(t,), direction="LONG", stop=StopGene("atr_multiple", 9.0), target_r=2.0),
        Genome(trigger=(t,), direction="LONG", stop=StopGene(), target_r=9.0),
    ]
    rejected = [not is_valid(g) for g in bad]
    assert rejected == [True, True, True, True, False, True, True, True, True, True, True, True]
    with pytest.raises(GenomeError):
        validate(bad[3])
    g = Genome(regime=(r,), trigger=(t,), or_group=(t, Clause("mom_3_atr", ">", 0.6)),
               stop=StopGene("last_swing", None, None, 0.0), direction="LONG", target_r=2.0)
    assert complexity(g) == 4 + 1 + 1


def test_resolver_uses_train_only(env):
    f, plan, dates = env["features"], env["plan"], env["dates"]
    train = plan.mask(dates, plan.train)
    assert 0 < train.sum() < len(train)
    altered = FeatureSet({k: np.array(v, copy=True) for k, v in f.items()}, f.metadata)
    for name in ("m5_rsi14", "h1_adx14", "m5_ema_slope", "compression_expansion_ratio"):
        arr = altered[name].astype(float)
        arr[~train] = arr[~train] * 7.0 + 1000.0
        altered[name] = arr
    r1 = ThresholdResolver(f, train)
    r2 = ThresholdResolver(altered, train)
    for name in ("m5_rsi14", "h1_adx14", "m5_ema_slope", "compression_expansion_ratio"):
        for q in (0.1, 0.5, 0.9):
            assert r1.value(name, q) == r2.value(name, q)
    assert r1.value("m5_rsi14", 0.9) > r1.value("m5_rsi14", 0.1)


def test_mirror_short_is_exact_mirror_of_long(env):
    res = env["resolver"]
    base = dict(
        regime=(Clause("regime_direction", "in", None, ("UP",)), Clause("h1_adx14", ">", 0.5)),
        trigger=(Clause("m5_rsi14", "<", 0.25), Clause("lvl_c_pdh", ">")),
        or_group=(Clause("lvl_c_sess_high", ">="), Clause("m5_range_position", ">", 0.7)),
        time_window=(540, 660), stop=StopGene("session_level", None, "session_low", 5.0),
        target_r=2.0,
    )
    long_spec = compile_genome(Genome(direction="LONG", **base), res)
    short_spec = compile_genome(Genome(direction="SHORT", **base), res)
    lr = {(r.feature, r.op): r for r in long_spec.entry_rules}
    sr = {(r.feature, r.op): r for r in short_spec.entry_rules}
    # self-mirror
    assert lr[("h1_adx14", ">")] == sr[("h1_adx14", ">")]
    # reflect around 50 (RSI), around 0 (slope), op flipped
    v = lr[("m5_rsi14", "<")].threshold
    assert sr[("m5_rsi14", ">")].threshold == pytest.approx(100.0 - v)
    extra = dict(context=(Clause("context_pullback", "==", None),),
                 trigger=(Clause("m5_ema_slope", ">", 0.6),), stop=StopGene(), target_r=2.0)
    le = compile_genome(Genome(direction="LONG", **extra), res)
    se = compile_genome(Genome(direction="SHORT", **extra), res)
    v = le.entry_rules[0].threshold
    assert se.entry_rules[0].op == "<" and se.entry_rules[0].threshold == pytest.approx(-v)
    assert le.context_filters == se.context_filters == ("PULLBACK",)
    # level rule: close > pdh  <->  close < pdl
    assert lr[("c", ">")].other_feature == "previous_day_high"
    assert sr[("c", "<")].other_feature == "previous_day_low"
    # or-group mirrors element-wise
    lo, so = long_spec.or_groups[0], short_spec.or_groups[0]
    assert (so[0].feature, so[0].op, so[0].other_feature) == ("c", "<=", "session_low")
    assert (so[1].feature, so[1].op) == ("m5_range_position", "<")
    assert so[1].threshold == pytest.approx(1.0 - lo[1].threshold)
    assert long_spec.regime_filters["DIRECTION"] == ("UP",)
    assert short_spec.regime_filters["DIRECTION"] == ("DOWN",)
    assert long_spec.stop.level == "session_low" and short_spec.stop.level == "session_high"
    assert [r for r in long_spec.entry_rules if r.feature == "berlin_minute"] == \
        [r for r in short_spec.entry_rules if r.feature == "berlin_minute"]


def test_mirror_is_involutive_for_every_catalog_entry():
    for name, e in CATALOG.items():
        p = e.mirror.partner
        if p is not None:
            back = CATALOG[p].mirror
            assert back.partner in (name, None) or CATALOG[p].mirror.kind == "level"
            assert CATALOG[p].mirror.kind == e.mirror.kind
            assert (CATALOG[back.partner].name == name) if back.partner else True


def _negated(f):
    """Price-negated frame: o,h,l,c -> -o,-l,-h,-c and all levels swapped/negated."""
    g = dict(f)
    g.update(o=-f["o"], h=-f["l"], l=-f["h"], c=-f["c"],
             previous_day_high=-f["previous_day_low"], previous_day_low=-f["previous_day_high"],
             previous_day_close=-f["previous_day_close"], session_open=-f["session_open"],
             session_high=-f["session_low"], session_low=-f["session_high"],
             last_swing_high=-f["last_swing_low"], last_swing_low=-f["last_swing_high"])
    return _price_action_arrays(g)


def _same(x, y):
    return np.allclose(x, y, rtol=1e-9, atol=1e-9, equal_nan=True)


def test_pool_contains_new_features_and_mirror_matches_real_negated_frame(env):
    """Every price-action catalog mirror rule is exactly what a price-negated frame produces."""
    f, pool = env["features"], env["pool"]
    assert set(NEW_FEATURES) <= pool.names
    neg = _negated(f)
    checked = 0
    for name in NEW_FEATURES:
        entry = CATALOG[name]
        m = entry.mirror
        real = np.asarray(f[entry.feature], dtype=float)
        if m.kind == "self":
            expected = real
        elif m.kind == "reflect":
            expected = 2 * m.center - real
        elif m.kind == "pair":
            expected = np.asarray(f[CATALOG[m.partner].feature], dtype=float)
        elif m.kind == "pair_reflect":
            expected = 2 * m.center - np.asarray(f[CATALOG[m.partner].feature], dtype=float)
        else:
            raise AssertionError(f"unexpected mirror kind for {name}")
        # value of the feature in the negated frame == its mirrored value in the real frame
        # (pair/pair_reflect: negated-frame `name` equals reflected partner, so compare that way)
        if m.kind in ("pair", "pair_reflect"):
            assert _same(neg[name], expected), name
        else:
            assert _same(neg[name], expected), name
        checked += 1
    assert checked == len(NEW_FEATURES)


def test_catalog_kind_and_domain_match_real_arrays(env):
    f = env["features"]
    for name in NEW_FEATURES:
        entry = CATALOG[name]
        a = np.asarray(f[name], dtype=float)
        a = a[np.isfinite(a)]
        assert len(a) > 1000, name
        if entry.kind == "flag":
            assert set(np.unique(a)) <= {0.0, 1.0}, name
        elif entry.kind == "fixed":
            assert set(np.unique(a)) <= {-1.0, 0.0, 1.0}, name
        elif name == "dist_sess_high_atr":
            assert a.max() <= 0, name  # session high includes the current bar
        elif name == "dist_sess_low_atr":
            assert a.min() >= 0, name
        elif entry.kind == "signed":
            assert a.min() < 0 < a.max(), name  # both sides populated
        elif entry.mirror.kind == "reflect":
            assert a.min() < entry.mirror.center < a.max(), name
        if name in ("bar_close_loc", "bar_body_ratio", "upper_wick_ratio", "lower_wick_ratio"):
            assert a.min() >= 0 and a.max() <= 1, name
        if name in ("from_high_24_atr", "from_low_24_atr", "bar_range_atr"):
            assert a.min() >= 0, name
        if name == "range_ratio_12_48":
            assert a.min() > 0 and a.max() <= 1 + 1e-9
    for name in ("brk_up_20", "brk_up_48", "brk_dn_20", "brk_dn_48"):
        assert CATALOG[name].kind == "continuous" and CATALOG[name].mirror.kind == "pair"
        a = np.asarray(f[name], dtype=float)
        assert np.nanmin(a) < 0 < np.nanmax(a)  # >0 = fresh breakout, <0 = inside range


def test_brk_and_dist_definitions_on_real_arrays(env):
    f = env["features"]
    c, h, low, atr = f["c"], f["h"], f["l"], f["m5_atr14"]
    day = f["berlin_day_id"]
    checked = 0
    for i in range(60, len(c), 997):
        if not np.isfinite(f["brk_up_20"][i]) or day[i - 20] != day[i]:
            continue
        assert f["brk_up_20"][i] == pytest.approx((c[i] - h[i - 20:i].max()) / atr[i])
        assert f["brk_dn_20"][i] == pytest.approx((low[i - 20:i].min() - c[i]) / atr[i])
        checked += 1
    assert checked > 10
    assert _same(f["dist_pdh_atr"], (c - f["previous_day_high"]) / atr)
    assert _same(f["dist_pdl_atr"], (c - f["previous_day_low"]) / atr)


def test_short_compile_of_pair_clauses_uses_partner_features(env):
    res = env["resolver"]
    base = dict(trigger=(Clause("brk_up_20", ">", 0.9), Clause("from_high_24_atr", ">", 0.5),
                         Clause("dist_pdh_atr", ">", 0.6)), stop=StopGene(), target_r=2.0)
    ls = compile_genome(Genome(direction="LONG", **base), res)
    ss = compile_genome(Genome(direction="SHORT", **base), res)
    lr = {r.feature: r for r in ls.entry_rules}
    sr = {r.feature: r for r in ss.entry_rules}
    assert sr["brk_dn_20"].op == ">" and sr["brk_dn_20"].threshold == lr["brk_up_20"].threshold
    fl, fh = sr["from_low_24_atr"], lr["from_high_24_atr"]
    assert fl.op == ">" and fl.threshold == fh.threshold
    dl, dh = sr["dist_pdl_atr"], lr["dist_pdh_atr"]
    assert dl.op == "<" and dl.threshold == pytest.approx(-dh.threshold)
    flags = Genome(direction="SHORT", trigger=(Clause("sweep_lo_20", "=="),), stop=StopGene(),
                   target_r=2.0)
    assert compile_genome(flags, res).entry_rules[0].feature == "sweep_hi_20"


def test_every_archetype_has_workable_candidate_counts_on_real_data(env):
    rng = np.random.default_rng(101)
    for name, fn in ARCHETYPES.items():
        specs = [compile_genome(fn(rng, env["pool"]), env["resolver"]) for _ in range(40)]
        counts = [len(evaluate_spec(env["features"], sp).decision_idx) for sp in specs]
        assert np.median(counts) >= 30, (name, np.median(counts))


def test_trial_ledger_counts_and_json_roundtrip(env):
    ledger = TrialLedger()
    gs = _genomes(env["pool"], 8, 300)
    outcomes = [ledger.record(g, "structural") for g in gs]
    assert ledger.record(gs[0], "param") == "duplicate"
    bad = replace(gs[0], trigger=())
    assert ledger.record(bad, "param") == "invalid"
    assert ledger.total_trials == 302 and ledger.structural_trials == 300
    assert ledger.param_trials == 2
    assert ledger.invalid_rejects == 1
    assert ledger.duplicate_rejects == outcomes.count("duplicate") + 1
    assert ledger.unique == outcomes.count("new")
    again = TrialLedger.from_json(ledger.to_json())
    assert again == ledger
    assert json.loads(ledger.to_json())["unique"] == ledger.unique


def test_duplicate_rate_over_2000_random_genomes_reported(env, capsys):
    ledger = TrialLedger()
    keys = set()
    for g in _genomes(env["pool"], 2024, 2000):
        ledger.record(g)
        keys.add(behavior_key(compile_genome(g, env["resolver"])))
    rate = ledger.duplicate_rejects / ledger.total_trials
    behav = 1 - len(keys) / 2000
    with capsys.disabled():
        print(f"\n[dup] 2000 random genomes: canonical dup rate {rate:.3%} "
              f"(unique {ledger.unique}); behavior-level dup rate {behav:.3%}")
    assert ledger.invalid_rejects == 0
    assert rate < 0.5  # sanity bound; the measured value is reported, not tuned


def test_param_space_and_with_params_roundtrip(env):
    for g in _genomes(env["pool"], 13, 200):
        space = param_space(g)
        names = [s[0] for s in space]
        assert len(names) == len(set(names)) and "target_r" in names
        same = with_params(g, {n: _current(g, n) for n in names})
        assert canonical_hash(same) == canonical_hash(g)
        assert with_params(g, [_current(g, n) for n in names]) == same
        lows = with_params(g, {n: lo for n, lo, hi, k in space})
        highs = with_params(g, {n: hi for n, lo, hi, k in space})
        validate(lows)
        validate(highs)
        assert with_params(g, {n: 1e9 for n, *_ in space}) is not None  # clamped, still valid
        if g.time_window is not None:
            assert {"tw_start", "tw_end"} <= set(names)


def _current(g, name):
    for pname, _group, _i, c in _quantile_clauses(g):
        if pname == name:
            return c.q
    return {"stop_mult": g.stop.multiple, "stop_offset": g.stop.offset, "target_r": g.target_r,
            "tw_start": (g.time_window or (0, 0))[0], "tw_end": (g.time_window or (0, 0))[1]}[name]


def test_mutate_and_crossover_return_valid_genomes_with_lineage(env):
    rng = np.random.default_rng(17)
    pool = env["pool"]
    genomes = _genomes(pool, 31, 60)
    changed = 0
    for g in genomes:
        m = mutate_structure(g, rng, pool)
        validate(m)
        if canonical_hash(m) != canonical_hash(g):
            changed += 1
            assert ">MUT" in m.lineage and lineage_family(m.lineage) == lineage_family(g.lineage)
        c1, c2 = crossover(g, genomes[int(rng.integers(len(genomes)))], rng)
        validate(c1)
        validate(c2)
        evaluate_spec(env["features"], compile_genome(c1, env["resolver"]))
    assert changed >= 50
    a, b = genomes[0], genomes[1]
    x, _ = crossover(a, b, np.random.default_rng(2))
    assert ">X1" in x.lineage and lineage_family(x.lineage) == lineage_family(a.lineage)


def test_lineage_stays_bounded_and_keeps_family(env):
    rng = np.random.default_rng(5)
    pool = env["pool"]
    g = _genomes(pool, 7, 4)[0]
    fam = lineage_family(g.lineage)
    a, b = g, _genomes(pool, 8, 4)[0]
    for _ in range(300):
        a = mutate_structure(a, rng, pool)
        a, b = crossover(a, b, rng)
        assert len(a.lineage) <= LINEAGE_MAX_LEN and len(b.lineage) <= LINEAGE_MAX_LEN
    assert lineage_family(a.lineage) == fam
    assert parse_lineage("MUT:X:MUT:A|B|C") == ("A", 2, 1)  # legacy strings still parse
    assert len(make_lineage("F" * 200, 5000, 5000)) <= LINEAGE_MAX_LEN
