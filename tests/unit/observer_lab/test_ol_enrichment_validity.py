# ruff: noqa: E501
"""Research-lab validity: forward-holdout guards are CALLED by the entry points, cells frozen on TRAIN, the two single-feature contrasts,
p-value resolution / power, persistent registry and predeclared small families."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest
from test_ol_enrichment import FEATS, LABEL, make_table

from alpha.common.market_data import ForwardHoldoutError
from coverage_analysis.observer_lab import enrichment as EN
from coverage_analysis.observer_lab import stats as ST

CFG = EN.EnrichmentConfig(B=2000, seed=1)
T_VALID = int(pd.Timestamp("2026-06-05", tz="UTC").value)  # inside the dev period (VALIDATION days)
T_OOS = int(pd.Timestamp("2026-07-15", tz="UTC").value)
T_FWD = int(pd.Timestamp("2026-09-10", tz="UTC").value)


def shifted(df, partition, t0_ns, **over):
    out = df.copy()
    out["decision_ts_ns"] = out["decision_ts_ns"] - out["decision_ts_ns"].min() + t0_ns
    out["partition"] = partition
    for k, v in over.items():
        out[k] = v
    return out


# ---------------------------------------------------------------------------------------------- guards are called by the entry points
def test_partition_column_and_purpose_are_required():
    df = make_table(n_days=5)
    with pytest.raises(ValueError, match="partition"):
        EN.single_feature_enrichment(df.drop(columns="partition"), FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose="fit")
    with pytest.raises(ValueError, match="purpose"):
        EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG)
    with pytest.raises(ValueError, match="purpose"):
        EN.incremental_ablation(df, [LABEL], df.is_control == False, {"a": ["f_acceptance__state"]}, ST.HypothesisRegistry("t"), CFG, purpose="select")  # noqa: E712
    with pytest.raises(ValueError, match="label"):
        EN.single_feature_enrichment(df.assign(partition=None), FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose="fit")


@pytest.mark.parametrize("partition", ["OOS", "VALIDATION"])
def test_fit_refuses_non_train_rows(partition):
    df = make_table(n_days=5)
    df.loc[df.index[:10], "partition"] = partition
    with pytest.raises(ValueError, match="not allowed for fit"):
        EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose="fit")


def test_forward_rows_are_only_usable_with_forward_monitor():
    fwd = shifted(make_table(n_days=30), "FORWARD", T_FWD)
    for purpose in ("fit", "validate", "oos_test"):
        with pytest.raises(ForwardHoldoutError):
            EN.single_feature_enrichment(fwd, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose=purpose)
    defs = EN.single_feature_enrichment(make_table(n_days=30), FEATS, [LABEL], ST.HypothesisRegistry("f"), CFG, purpose="fit").cell_defs
    rep = EN.single_feature_enrichment(fwd, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose="forward_monitor", cell_defs=defs)
    assert rep.purpose == "forward_monitor" and rep.n_events_used == 30 * 6
    # a TRAIN-labelled table cannot be passed off as forward data
    with pytest.raises(ValueError, match="not allowed"):
        EN.single_feature_enrichment(make_table(n_days=30), FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose="forward_monitor", cell_defs=defs)
    early = shifted(make_table(n_days=30), "FORWARD", T_OOS)
    with pytest.raises(ForwardHoldoutError, match="must start at"):
        EN.single_feature_enrichment(early, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose="forward_monitor", cell_defs=defs)


def test_timestamps_after_the_dev_end_are_refused_even_with_a_dev_partition_label():
    """guard_dev_only runs on event AND control timestamps: a mislabelled / late CONTROL is caught too."""
    df = make_table(n_days=30)
    late = df.copy()
    ctl = np.flatnonzero(late.is_control.to_numpy())[5]
    late.loc[ctl, "decision_ts_ns"] = T_FWD * 1  # one control stamped in the forward period
    with pytest.raises(ForwardHoldoutError):
        EN.single_feature_enrichment(late, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose="fit")
    ev_late = df.copy()
    ev_late.loc[0, "decision_ts_ns"] = T_FWD
    with pytest.raises(ForwardHoldoutError):
        EN.single_feature_enrichment(ev_late, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose="fit")


def test_a_control_in_another_partition_than_its_event_is_an_error():
    df = make_table(n_days=30)
    ctl = np.flatnonzero(df.is_control.to_numpy())[3]
    df.loc[ctl, "partition"] = "VALIDATION"
    with pytest.raises(ValueError, match="different partition"):
        EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("t"), CFG, purpose="fit")


def test_purged_and_embargo_rows_are_dropped_and_counted_with_their_controls():
    df = make_table(n_days=60)
    ev_ids = df.loc[~df.is_control, "event_id"].tolist()
    purged = set(ev_ids[:10])
    df.loc[df.event_id.isin(purged), "partition"] = "PURGED"
    df.loc[df.index[df.is_control][-4:], "partition"] = "EMBARGO"  # controls lying in an embargo zone (not controls of the purged events)
    rep = EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("p"), CFG, purpose="fit")
    assert rep.n_excluded_partition_events == 10 and rep.n_excluded_partition_controls == 4
    assert rep.n_events_used == 60 * 6 - 10
    assert rep.n_orphan_controls_dropped == 10  # the purged events' controls leave with them
    with pytest.raises(ValueError, match="refer to an event"):
        bad = df.copy()
        bad.loc[bad.index[bad.is_control][0], "control_of"] = "does-not-exist"
        EN.single_feature_enrichment(bad, FEATS, [LABEL], ST.HypothesisRegistry("p"), CFG, purpose="fit")


# ---------------------------------------------------------------------------------------------- cells frozen on TRAIN
def test_validation_and_oos_use_the_frozen_train_cells_never_refit():
    train = make_table(n_days=80, seed=0)
    rep = EN.single_feature_enrichment(train, ["f_levels__dist"], [LABEL], ST.HypothesisRegistry("fit"), CFG, purpose="fit")
    d = rep.cell_defs["f_levels__dist"]
    assert d.kind == "quantile" and len(d.edges) == 2
    edges_train = np.nanquantile(train.loc[~train.is_control, "f_levels__dist"], [1 / 3, 2 / 3])
    assert np.allclose(d.edges, edges_train)
    val = shifted(make_table(n_days=60, seed=5), "VALIDATION", T_VALID)
    val["f_levels__dist"] = val["f_levels__dist"] + 5.0  # a regime shift: with edges refitted on validation the cells would stay balanced
    with pytest.raises(ValueError, match="frozen cell_defs"):
        EN.single_feature_enrichment(val, ["f_levels__dist"], [LABEL], ST.HypothesisRegistry("v0"), CFG, purpose="validate")
    vrep = EN.single_feature_enrichment(val, ["f_levels__dist"], [LABEL], ST.HypothesisRegistry("v"), CFG, purpose="validate", cell_defs=rep.cell_defs)
    assert vrep.cell_defs["f_levels__dist"].edges == d.edges  # unchanged
    by = {r.cell: r for r in vrep.results}
    assert by["Q3/3"].n_event > 10 * max(by["Q1/3"].n_event, 1)  # all shifted rows fall in the top TRAIN cell
    assert all(r.purpose == "validate" and r.partition == "VALIDATION" for r in vrep.results)
    with pytest.raises(ValueError, match="no definition"):
        EN.single_feature_enrichment(val, FEATS, [LABEL], ST.HypothesisRegistry("v2"), CFG, purpose="validate", cell_defs=rep.cell_defs)


# ---------------------------------------------------------------------------------------------- the two single-feature contrasts
def _informative_feature_table(seed=0, n_days=90, per_day=8):
    """y depends on the feature identically in events AND controls (p=0.7 in cell A, 0.3 in B); events are NOT different from controls given the feature."""
    rng = np.random.default_rng(seed)
    rows = []
    t0 = 1_760_000_000_000_000_000
    for d in range(n_days):
        for k in range(per_day):
            eid = f"e{d}_{k}"
            ts = t0 + (d * 100 + k) * 300 * 10**9
            for is_ctl in (False, True):
                st = "A" if rng.random() < 0.5 else "B"
                y = float(rng.random() < (0.7 if st == "A" else 0.3))
                rows.append(dict(
                    event_id=f"c_{eid}" if is_ctl else eid, is_control=is_ctl, control_of=eid if is_ctl else None, decision_ts_ns=ts + (7 * 300 * 10**9 if is_ctl else 0),
                    m_local_day=d, m_structure_event_id=None if is_ctl else f"c{d}_{k // 2}", partition="TRAIN", f_acceptance__state=st, **{LABEL: y},
                ))
    return pd.DataFrame(rows)


def test_same_cell_contrast_is_the_primary_and_differs_from_lift_within_cell():
    df = _informative_feature_table()
    cfg = EN.EnrichmentConfig(B=2000, seed=3)
    same = EN.single_feature_enrichment(df, ["f_acceptance__state"], [LABEL], ST.HypothesisRegistry("s"), cfg, purpose="fit")
    lift = EN.lift_within_cell(df, ["f_acceptance__state"], [LABEL], ST.HypothesisRegistry("l"), cfg, purpose="fit")
    s, lf = {r.cell: r for r in same.results}, {r.cell: r for r in lift.results}
    assert all(r.contrast == EN.CONTRAST_SAME_CELL for r in same.results) and all(r.contrast == EN.CONTRAST_LIFT for r in lift.results)
    # the feature is informative for y in events and controls alike, events are no different: the same-cell contrast is ~0 everywhere ...
    assert abs(s["=A"].delta) < 0.1 and abs(s["=B"].delta) < 0.1
    assert s["=A"].status != ST.SIGNIFICANT_ADJUSTED and s["=B"].status != ST.SIGNIFICANT_ADJUSTED
    # ... whereas the old test (events in the cell vs controls matched to those events, control feature ignored) shows a large spurious "lift"
    assert lf["=A"].delta > 0.1 and lf["=B"].delta < -0.1
    assert lf["=A"].status == ST.SIGNIFICANT_ADJUSTED and lf["=B"].status == ST.SIGNIFICANT_ADJUSTED
    assert s["=A"].n_control != lf["=A"].n_control or s["=A"].p_control != lf["=A"].p_control


# ---------------------------------------------------------------------------------------------- resolution / power
def test_choose_B_follows_the_20_m_over_alpha_rule_and_reports_caps():
    c = ST.choose_B(12, 0.05)
    assert c.B == 4800 and not c.capped and not c.resolution_limited and c.p_floor == pytest.approx(2 / 4801)
    assert ST.choose_B(1, 0.05).B == 2000  # b_min floor
    big = ST.choose_B(3000, 0.05)  # 83 x 4 x labels
    assert big.required == 1_200_000 and big.B == ST.DEFAULT_B_MAX and big.capped and big.resolution_limited and "resolution_limited" in (big.warning or "")
    assert ST.choose_B(10, 0.05, requested=500).B == 500  # explicit B honoured (and judged)
    assert ST.is_power_limited(3000, 1000) and ST.is_power_limited(100, 1000) and not ST.is_power_limited(10, 1000)
    assert ST.final_status("OK", 1.0, -0.1, 0.1, power_limited=True) == ST.POWER_LIMITED
    assert ST.final_status("OK", 0.001, 0.05, 0.2, power_limited=True) == ST.SIGNIFICANT_ADJUSTED  # a real finding is never hidden
    assert ST.normal_p(0.2, 0.05) < 1e-4 and np.isnan(ST.normal_p(0.2, 0.0))


def test_a_family_that_cannot_reach_significance_is_power_limited_not_null():
    df = make_table()  # planted +0.25 effect in state A
    tiny_b = EN.EnrichmentConfig(B=60, seed=1)  # smallest attainable p = 2/61 = 0.033; with m >= 2 Holm can never pass 0.05
    rep = EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("pl"), tiny_b, purpose="fit")
    ok = [r for r in rep.results if r.status != ST.INSUFFICIENT_EVIDENCE]
    assert ok and all(r.power_limited for r in ok)
    assert all(r.status in (ST.POWER_LIMITED, ST.INSUFFICIENT_EVIDENCE) for r in ok)  # never a quiet NOT_SIGNIFICANT
    assert rep.power_limited_families and any("power_limited" in w for w in rep.warnings)
    assert all(r.p_floor == pytest.approx(2 / 61) for r in ok)
    # the normal-approximation p has no such floor and is reported next to the bootstrap p
    a = next(r for r in rep.results if r.feature == "f_acceptance__state" and r.cell == "=A")
    assert a.p_norm is not None and a.p_norm < 1e-3
    pn = EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("pn"), EN.EnrichmentConfig(B=60, seed=1, p_method="normal"), purpose="fit")
    an = next(r for r in pn.results if r.feature == "f_acceptance__state" and r.cell == "=A")
    assert not an.power_limited or an.status == ST.SIGNIFICANT_ADJUSTED


def test_automatic_B_scales_with_the_family_and_is_reported():
    df = make_table(n_days=40)
    rep = EN.single_feature_enrichment(df, FEATS, [LABEL], ST.HypothesisRegistry("auto"), EN.EnrichmentConfig(seed=1), purpose="fit")
    assert rep.B >= 2000 and all(r.B == rep.B and r.n_hypotheses_ever == rep.n_hypotheses_ever for r in rep.results)
    assert all(r.m_family is not None and r.family for r in rep.results)


# ---------------------------------------------------------------------------------------------- persistent registry
def test_registry_persists_across_runs_and_refuses_re_registration(tmp_path):
    path = tmp_path / "reg" / "lab.json"
    r1 = ST.HypothesisRegistry("lab", path)
    r1.declare_family("fam-1", definition="predeclared 2 contrasts")
    r1.register_many(["a", "b"], "fam-1")
    r1.record("a", 0.01)
    r1.flush()
    assert json.loads(path.read_text())["n_hypotheses_ever"] == 2
    r2 = ST.HypothesisRegistry("lab", path)  # a NEW run
    assert r2.n_hypotheses == 2 and r2.family_of("a") == "fam-1" and r2.n_evaluated == 1
    with pytest.raises(ST.HypothesisReuseError):
        r2.register("a")
    with pytest.raises(ST.HypothesisReuseError):
        r2.register_many(["c", "b"])  # all-or-nothing: 'c' must NOT be registered either
    assert r2.n_hypotheses == 2
    with pytest.raises(ST.HypothesisReuseError):
        r2.declare_family("fam-1")
    r2.register("c", "fam-2")
    assert ST.HypothesisRegistry("lab", path).n_hypotheses == 3
    assert r2.family_info("fam-1")["n_results_recorded_at_declaration"] == 0
    with pytest.raises(ValueError, match="belongs to"):
        ST.HypothesisRegistry("other", path)
    # the recorded p-values survive the reload and still enter the correction
    assert ST.HypothesisRegistry("lab", path).adjust("holm")["a"] == pytest.approx(0.02)  # family fam-1 has m=2


def test_enrichment_runs_share_one_persistent_registry_and_report_the_total(tmp_path):
    path = tmp_path / "lab.json"
    df = make_table(n_days=40)
    rep1 = EN.single_feature_enrichment(df, ["f_acceptance__state"], [LABEL], ST.HypothesisRegistry("lab", path), CFG, purpose="fit")
    reg2 = ST.HypothesisRegistry("lab", path)
    rep2 = EN.single_feature_enrichment(df, ["f_swings__noise"], [LABEL], reg2, CFG, purpose="fit")
    assert rep1.n_hypotheses_ever == 2 and rep2.n_hypotheses_ever == 5 and all(r.n_hypotheses_ever == 5 for r in rep2.results)
    with pytest.raises(ST.HypothesisReuseError):  # the same test again in a later run: no best-of-N
        EN.single_feature_enrichment(df, ["f_acceptance__state"], [LABEL], ST.HypothesisRegistry("lab", path), CFG, purpose="fit")


# ---------------------------------------------------------------------------------------------- predeclared small family reaches significance
def test_predeclared_small_family_reaches_significance_where_the_giant_family_cannot(tmp_path):
    df = make_table(ablation=True, effect=0.35)
    base = df.family.eq("X")
    reg = ST.HypothesisRegistry("pre", tmp_path / "pre.json")
    # a huge unrelated exploration already sits in the registry (3000 hypotheses, p ~ 0.5): the registry-wide Holm threshold is ~alpha/3000
    others = [f"explore|{i}" for i in range(3000)]
    reg.register_many(others, "exploration")
    for h in others:
        reg.record(h, 0.5)
    groups = {"acceptance": ["f_acceptance__state"], "swings": ["f_swings__noise"]}
    declared = [EN.PredeclaredContrast("acceptance", "f_acceptance__state", "=A"), EN.PredeclaredContrast("swings", "f_swings__noise", "Q3/3")]
    fit = EN.incremental_ablation(df, [LABEL], base, groups, ST.HypothesisRegistry("scratch"), CFG, purpose="fit")  # cells are fitted on TRAIN only
    rep = EN.incremental_ablation(df, [LABEL], base, groups, reg, CFG, purpose="fit", cell_defs=fit.cell_defs, contrasts=declared)
    assert len(rep.results) == 2 and reg.n_hypotheses == 3002 == rep.n_hypotheses_ever
    acc = next(r for r in rep.results if r.group == "acceptance")
    swi = next(r for r in rep.results if r.group == "swings")
    assert acc.m_family == 1 and acc.family == "incr|fit|y_fav050_before_adv050|acceptance"
    assert acc.status == ST.SIGNIFICANT_ADJUSTED and acc.adjusted_p is not None and acc.adjusted_p < 0.05  # planted effect, predeclared family
    assert swi.status == ST.NOT_SIGNIFICANT and not swi.power_limited  # the null stays a (properly powered) null
    # the very same hypothesis judged against everything ever registered could never pass: that is what the old design did to every cell
    assert acc.adjusted_p_registry is not None and acc.adjusted_p_registry > 0.05
    # the declaration carries its provenance
    info = reg.family_info(acc.family)
    assert info["n_results_recorded_at_declaration"] == 3000 and json.loads(str(info["definition"]))
    with pytest.raises(ValueError, match="predeclared contrast"):
        EN.incremental_ablation(df, [LABEL], base, groups, ST.HypothesisRegistry("e"), CFG, purpose="fit", cell_defs=fit.cell_defs,
                                contrasts=[EN.PredeclaredContrast("acceptance", "f_acceptance__state", "=Z")])


def test_registry_wide_scope_is_available_and_stricter():
    df = make_table(ablation=True, effect=0.35)
    base = df.family.eq("X")
    cfg = EN.EnrichmentConfig(B=2000, seed=1, adjust_scope="registry")
    reg = ST.HypothesisRegistry("scope")
    others = [f"x{i}" for i in range(500)]
    reg.register_many(others, "other")
    for h in others:
        reg.record(h, 0.5)
    rep = EN.incremental_ablation(df, [LABEL], base, {"acceptance": ["f_acceptance__state"]}, reg, cfg, purpose="fit")
    assert all(r.adjusted_p == r.adjusted_p_registry for r in rep.results)
    assert all(r.status != ST.SIGNIFICANT_ADJUSTED for r in rep.results)  # 502 hypotheses at B=2000: cannot pass, flagged
    assert any(r.power_limited for r in rep.results) or all(r.status == ST.INSUFFICIENT_EVIDENCE for r in rep.results)
