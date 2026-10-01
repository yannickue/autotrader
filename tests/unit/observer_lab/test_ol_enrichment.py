# ruff: noqa: E501
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from coverage_analysis.observer_lab import enrichment as EN
from coverage_analysis.observer_lab import stats as ST
from market_observer.schema import CausalityError

LABEL = "y_fav050_before_adv050"
FEATS = ["f_levels__dist", "f_acceptance__state", "f_swings__noise"]
CFG = EN.EnrichmentConfig(B=2000, seed=1)  # p-value resolution 2/(B+1) must stay below alpha / m


def make_table(n_days=80, per_day=6, effect=0.25, seed=0, ablation=False):
    rng = np.random.default_rng(seed)
    rows = []
    t0 = 1_760_000_000_000_000_000
    for d in range(n_days):
        for k in range(per_day):
            eid = f"e{d}_{k}"
            state = "A" if rng.random() < 0.5 else "B"
            fam = "X" if k % 2 == 0 else "Y"
            dist = float(rng.normal())
            noise = float(rng.normal())
            boost = effect if (state == "A" and (not ablation or fam == "X")) else 0.0
            ts = t0 + (d * 100 + k) * 300 * 10**9
            ev = (rng.random() < 0.5 + boost)
            rows.append(dict(
                event_id=eid, is_control=False, control_of=None, decision_ts_ns=ts, m_local_day=d, m_structure_event_id=f"c{d}_{k // 2}",
                family=fam, f_levels__dist=dist, f_acceptance__state=state, f_swings__noise=noise, f_levels__confirmed_ts_ns=ts - 10**9,
                **{LABEL: float(ev)},
            ))
            rows.append(dict(
                event_id=f"c_{eid}", is_control=True, control_of=eid, decision_ts_ns=ts + 7 * 300 * 10**9, m_local_day=d,
                m_structure_event_id=None, family=fam, f_levels__dist=float(rng.normal()), f_acceptance__state="B" if rng.random() < 0.5 else "A",
                f_swings__noise=float(rng.normal()), f_levels__confirmed_ts_ns=ts, **{LABEL: float(rng.random() < 0.5)},
            ))
    return pd.DataFrame(rows)


def test_disjointness_and_causality_are_enforced():
    df = make_table(n_days=5)
    with pytest.raises(ValueError):
        EN.single_feature_enrichment(df, [*FEATS, LABEL], [LABEL], ST.HypothesisRegistry("t"), CFG)
    with pytest.raises(ValueError):
        EN.single_feature_enrichment(df, FEATS, ["f_levels__dist"], ST.HypothesisRegistry("t"), CFG)
    bad = df.copy()
    bad.loc[3, "f_levels__confirmed_ts_ns"] = bad.loc[3, "decision_ts_ns"] + 1  # a feature stamped AFTER its decision
    with pytest.raises(CausalityError):
        EN.single_feature_enrichment(bad, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG)
    nb = df.copy()
    nb[LABEL] = 0.5  # not a binary outcome
    with pytest.raises(ValueError):
        EN.single_feature_enrichment(nb, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG)


def test_planted_effect_is_found_null_is_not_and_multiplicity_is_counted():
    df = make_table()
    reg = ST.HypothesisRegistry("d1-test")
    rep = EN.single_feature_enrichment(df, FEATS, [LABEL], reg, CFG)
    by = {(r.feature, r.cell): r for r in rep.results}
    a = by[("f_acceptance__state", "=A")]
    assert a.delta == pytest.approx(0.25, abs=0.1) and a.status == ST.SIGNIFICANT_ADJUSTED and a.adjusted_p is not None
    assert a.group == "acceptance" and a.versions == {"acceptance": "mso-acceptance-1"} and a.kind == "single"
    assert a.n_clusters is not None and 70 <= a.n_blocks <= 80
    assert by[("f_acceptance__state", "=B")].status == ST.NOT_SIGNIFICANT
    # the overall event-vs-control gap (planted through 'state') shows up in every cell of an UNRELATED feature: cells must differ from base_delta
    for r in rep.results:
        if r.feature in ("f_swings__noise", "f_levels__dist"):
            assert abs(r.delta - r.base_delta) < 0.13
    assert abs(a.delta - a.base_delta) > 0.0 and a.base_delta == pytest.approx(0.125, abs=0.07)
    # 3 quantile cells x 2 numeric features + 2 categorical cells; every one registered and counted
    assert len(rep.results) == 8 == reg.n_hypotheses == rep.n_hypotheses
    assert rep.n_events_used == 480 and rep.n_excluded_warmup_events == 0
    # a second family run on the same registry grows the multiplicity (adjusted p-values get stricter, never silently reset)
    df["y_fav025_before_adv025"] = df[LABEL]
    EN.single_feature_enrichment(df, ["f_swings__noise"], ["y_fav025_before_adv025"], reg, CFG)
    assert reg.n_hypotheses == 11
    with pytest.raises(ValueError):  # re-testing an already registered hypothesis is refused (no best-of-N reruns)
        EN.single_feature_enrichment(df, ["f_swings__noise"], ["y_fav025_before_adv025"], reg, CFG)


def test_results_are_reproducible():
    df = make_table(n_days=30)
    a = EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("x"), CFG)
    b = EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("x"), CFG)
    assert [(r.delta, r.ci_low, r.ci_high, r.adjusted_p) for r in a.results] == [(r.delta, r.ci_low, r.ci_high, r.adjusted_p) for r in b.results]


def test_small_samples_are_insufficient_evidence_but_still_counted():
    df = make_table(n_days=8, per_day=3, effect=0.4)
    reg = ST.HypothesisRegistry("small")
    rep = EN.single_feature_enrichment(df, FEATS, [LABEL], reg, CFG)
    assert all(r.status == ST.INSUFFICIENT_EVIDENCE and r.adjusted_p is None for r in rep.results)
    assert reg.n_hypotheses == len(rep.results) > 0 and reg.n_evaluated == 0


def test_censored_labels_are_excluded_from_cell_counts():
    df = make_table(n_days=60)
    df[LABEL] = df[LABEL].astype(object)
    df.loc[df.index[::7], LABEL] = None  # "neither side hit inside the horizon"
    rep = EN.single_feature_enrichment(df, ["f_acceptance__state"], [LABEL], ST.HypothesisRegistry("n"), CFG)
    full = EN.single_feature_enrichment(make_table(n_days=60), ["f_acceptance__state"], [LABEL], ST.HypothesisRegistry("n"), CFG)
    assert sum(r.n_event for r in rep.results) < sum(r.n_event for r in full.results)


def test_warmup_rows_are_excluded_and_counted_not_silently_dropped():
    df = make_table(n_days=60)
    ev_ids = df.loc[~df.is_control, "event_id"].tolist()
    bad_events = set(ev_ids[:40])
    df["warmup_ok"] = ~df.event_id.isin(bad_events)  # 40 events inside the feature warm-up (their controls are marked too? no: orphans)
    df.loc[df.is_control & df.control_of.isin(bad_events), "warmup_ok"] = True
    df.loc[df.index[df.is_control][-3:], "warmup_ok"] = False  # three controls themselves not warm
    rep = EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("w"), CFG)
    base = EN.single_feature_enrichment(df.drop(columns="warmup_ok"), FEATS, [LABEL], ST.HypothesisRegistry("w"), CFG)
    assert rep.n_excluded_warmup_events == 40 and rep.n_excluded_warmup_controls == 3
    assert rep.n_orphan_controls_dropped == 40  # controls of excluded events leave with them
    assert rep.n_events_used == base.n_events_used - 40 == 60 * 6 - 40
    assert sum(r.n_event for r in rep.results if r.feature == "f_acceptance__state") < sum(r.n_event for r in base.results if r.feature == "f_acceptance__state")
    # no warmup column => nothing excluded and the report says so
    assert base.n_excluded_warmup_events == 0 and base.warmup_column_present is False and rep.warmup_column_present is True


def test_incremental_ablation_compares_delta_with_and_without_a_group():
    df = make_table(ablation=True, effect=0.35)
    base = df.family.eq("X")  # BASE FAMILY (events and their controls share the family)
    reg = ST.HypothesisRegistry("abl")
    rep = EN.incremental_ablation(df, [LABEL], base, {"acceptance": ["f_acceptance__state"], "swings": ["f_swings__noise"]}, reg, CFG)
    assert all(r.kind == "incremental" for r in rep.results)
    assert len(rep.results) == reg.n_hypotheses == 2 + 3
    acc = {r.cell: r for r in rep.results if r.group == "acceptance"}
    a = acc["base AND =A"]
    assert a.delta > 0.1 and a.base_delta is not None and a.status == ST.SIGNIFICANT_ADJUSTED
    assert all(r.status != ST.SIGNIFICANT_ADJUSTED for r in rep.results if r.group == "swings")
    summary = EN.summarise_by_group(rep.results)
    assert summary["acceptance"][ST.SIGNIFICANT_ADJUSTED] >= 1 and summary["swings"].get(ST.SIGNIFICANT_ADJUSTED, 0) == 0
