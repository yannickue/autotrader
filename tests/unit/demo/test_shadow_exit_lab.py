# ruff: noqa: E501
"""Lane W: the shadow exit lab (parallel hypothetical exit policies on the SAME entry; pure, causal, no orders)."""

from __future__ import annotations

import ast
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import demo.shadow_exit_lab as lab
from demo.exit_policies import (
    LAB_POLICY_IDS,
    P_BE_RUNNER,
    P_FM,
    P_TP1_RUNNER,
    POLICY_IDS,
    POLICY_PARAMS,
    POLICY_SET_VERSION,
    BarSeries,
    EntryInput,
    simulate_all,
    simulate_policy,
)
from demo.shadow_exit_lab import (
    BASELINE_NAME,
    LAB_NAMES,
    MINIMUM_POLICY_NAMES,
    SHADOW_LAB_VERSION,
    LabStats,
    evaluate_shadow,
    failed_move_level,
    safe_evaluate_shadow,
    summarise_groups,
    summarise_lab,
)

T0 = datetime(2026, 6, 9, 8, 0, tzinfo=UTC)
USER_MINIMUM = (
    "fixed_1_5r", "TP1_only", "TP1_plus_runner", "TP1_TP2_runner", "pure_structure_trail",
    "break_even_plus_runner", "momentum_failure", "time_decay", "failed_move_exit",
)


def mk(rows, start=T0, spread=0.0):
    ts = tuple(start + timedelta(minutes=5 * i) for i in range(len(rows)))
    return BarSeries(ts, *(tuple(float(r[k]) for r in rows) for k in range(4)), tuple(spread for _ in rows))


def entry(direction=1, fill=100.0, stop=99.0, tp1=None, tp2=None, flat=None, atr=1.0, pre=None, fml=None, eid="e1"):
    return EntryInput(eid, "GER40", direction, fill, stop, T0, atr, tp1=tp1, tp2=tp2, flat_utc=flat, pre=pre, failed_move_level=fml)


def random_path(seed, n=60, base=100.0, vol=0.35, spread=0.0):
    rnd = random.Random(seed)
    px, rows = base, []
    for _ in range(n):
        o = px
        hi, lo = o + rnd.random() * vol, o - rnd.random() * vol
        c = rnd.uniform(lo, hi)
        rows.append((o, hi, lo, c))
        px = c + rnd.uniform(-0.05, 0.05)
    return mk(rows, spread=spread), rows


PRE = mk([(99.8, 100.1, 99.6, 100.0)] * 6, start=T0 - timedelta(minutes=30))


# ---- versions / names -------------------------------------------------------------------------------------
def test_versions_and_exact_minimum_policy_names():
    assert POLICY_SET_VERSION == POLICY_PARAMS["version"] == "eeq-policies-2"
    assert SHADOW_LAB_VERSION == "swl-1" and lab.LAB_PARAMS["policy_set_version"] == POLICY_SET_VERSION
    assert MINIMUM_POLICY_NAMES == USER_MINIMUM and all(n in LAB_NAMES for n in USER_MINIMUM)
    assert set(LAB_NAMES.values()) == set(LAB_POLICY_IDS) and len(LAB_POLICY_IDS) == 12
    assert len(POLICY_IDS) == 9  # Lane X's nine are untouched (golden reproducibility)
    assert BASELINE_NAME == "fixed_1_5r"


def test_lane_x_nine_policies_unchanged_by_the_new_ones():
    bars, _ = random_path(1)
    e = entry(tp1=100.6, tp2=101.2, pre=PRE)
    nine = simulate_all(e, bars)
    twelve = simulate_all(e, bars, LAB_POLICY_IDS)
    assert tuple(nine) == POLICY_IDS
    for pid in POLICY_IDS:
        assert nine[pid].r == twelve[pid].r and nine[pid].final_reason == twelve[pid].final_reason


# ---- same entry / purity / no side effects ---------------------------------------------------------------
def test_every_policy_receives_the_identical_entry_object(monkeypatch):
    seen = []
    real = lab.simulate_policy

    def spy(pid, e, bars, mgmt=None):
        seen.append(e)
        return real(pid, e, bars, mgmt)

    monkeypatch.setattr(lab, "simulate_policy", spy)
    bars, _ = random_path(2)
    e = entry(tp1=100.6, tp2=101.2, pre=PRE, fml=99.6)
    out = evaluate_shadow(e, bars)
    assert len(seen) == len(LAB_NAMES)
    assert all(x is e for x in seen)
    assert out["entry"]["policy_entry_ids"] == ["e1"] and out["entry"]["fill"] == 100.0 and out["entry"]["stop"] == 99.0
    assert set(out["policies"]) == set(LAB_NAMES)


def test_evaluation_is_pure_deterministic_and_does_not_mutate_inputs():
    bars, _ = random_path(3, spread=0.02)
    e = entry(tp1=100.6, tp2=101.2, pre=PRE, fml=99.7)
    before = (e, bars)
    a, b = evaluate_shadow(e, bars), evaluate_shadow(e, bars)
    assert a == b and (e, bars) == before


def test_lab_module_never_imports_execution_stack_or_mt5():
    banned = ("mt5", "metatrader", "demo.execution.live", "demo.execution.stack", "demo.runner", "exec_client")
    for mod in ("shadow_exit_lab.py", "exit_policies.py"):
        src = Path(lab.__file__).with_name(mod).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for n in names:
                assert not any(b in n.lower() for b in banned), (mod, n)


def test_bad_input_is_contained_and_counted_never_raised():
    stats = LabStats()
    bad = EntryInput("x", "M", 1, 100.0, 100.0, T0, 1.0)  # zero risk -> every policy NOT_APPLICABLE (no raise)
    bars, _ = random_path(4)
    ok = safe_evaluate_shadow(bad, bars, stats=stats)
    assert ok is not None and all(not r["applicable"] for r in ok["policies"].values())
    boom = safe_evaluate_shadow(entry(), None, stats=stats)  # type: ignore[arg-type]
    assert boom is None and stats.failed == 1 and stats.ok == 1 and stats.last_error and stats.mean_ms is not None


# ---- NOT_APPLICABLE ---------------------------------------------------------------------------------------
def test_not_applicable_without_structural_levels_or_failed_move_level():
    bars, _ = random_path(5)
    out = evaluate_shadow(entry(pre=PRE), bars)["policies"]
    for n in ("TP1_only", "TP1_plus_runner", "TP1_TP2_runner", "struct_tp1_tp2"):
        assert out[n]["applicable"] is False and out[n]["na_reason"].startswith("NO_STRUCTURAL")
    assert out["failed_move_exit"] == {"applicable": False, "na_reason": "NO_FAILED_MOVE_LEVEL"}
    for n in ("fixed_1_5r", "pure_structure_trail", "break_even_plus_runner", "momentum_failure", "time_decay"):
        assert out[n]["applicable"] is True
    tp1 = evaluate_shadow(entry(tp1=100.6, pre=PRE), bars)["policies"]
    assert tp1["TP1_only"]["applicable"] and tp1["TP1_plus_runner"]["applicable"]
    assert tp1["TP1_TP2_runner"]["na_reason"] == "NO_STRUCTURAL_TP2"  # the named policy needs a real TP2


def test_failed_move_level_must_lie_between_stop_and_entry():
    bars, _ = random_path(6)
    for lvl in (98.0, 101.0, 100.0, 99.0):
        r = simulate_policy(P_FM, entry(fml=lvl), bars)
        assert not r.applicable and r.na_reason == "FAILED_MOVE_LEVEL_NOT_BETWEEN_STOP_AND_ENTRY"
    assert simulate_policy(P_FM, entry(fml=99.5), bars).applicable
    assert not simulate_policy(P_FM, entry(-1, 100.0, 101.0, fml=99.5), bars).applicable


# ---- failed_move_exit semantics --------------------------------------------------------------------------
def test_failed_move_exit_long_exits_at_the_close_back_below_the_broken_edge():
    # long breakout above 100.5; entry 100.8, stop 99.5; bar 2 closes back below the edge (100.3): failed breakout
    rows = [(100.8, 101.0, 100.6, 100.9), (100.9, 101.0, 100.4, 100.7), (100.7, 100.9, 100.1, 100.3), (100.3, 100.4, 100.0, 100.2)]
    r = simulate_policy(P_FM, entry(1, 100.8, 99.5, fml=100.5), mk(rows))
    assert r.final_reason == "STRUCTURE_FAILURE" and r.fills[-1].price == pytest.approx(100.3)
    assert r.r == pytest.approx((100.3 - 100.8) / 1.3) and r.fills[-1].ts == T0 + timedelta(minutes=15)
    assert r.stop_levels == [99.5]  # the exit is not a stop move


def test_failed_move_exit_wick_through_the_level_without_a_close_does_nothing():
    rows = [(100.8, 101.0, 100.6, 100.9), (100.9, 101.0, 100.2, 100.6), (100.6, 100.9, 100.3, 100.7)]  # wicks below 100.5, closes above
    r = simulate_policy(P_FM, entry(1, 100.8, 99.5, fml=100.5), mk(rows))
    assert r.final_reason == "DATA_END" and r.censored


def test_failed_move_exit_short_fade_mirror_and_stop_still_wins_inside_the_bar():
    # fade short after an up-break failed: edge 100.5 (range high), entry 100.2, stop 101.2; closes back above 100.5 -> failed fade
    rows = [(100.2, 100.3, 99.9, 100.0), (100.0, 100.7, 99.95, 100.6), (100.6, 100.8, 100.4, 100.5)]
    r = simulate_policy(P_FM, entry(-1, 100.2, 101.2, fml=100.5), mk(rows))
    assert r.final_reason == "STRUCTURE_FAILURE" and r.fills[-1].price == pytest.approx(100.6)
    gap = [(100.2, 101.3, 99.9, 100.6)]  # one bar that touches the stop AND closes beyond the level: stop first
    r2 = simulate_policy(P_FM, entry(-1, 100.2, 101.2, fml=100.5), mk(gap))
    assert r2.final_reason == "STOP" and r2.r == pytest.approx(-1.0)


def test_failed_move_level_from_signal_by_variant_and_direction():
    sig = {"variant": "breakout", "structure_levels": {"range_high": 105.0, "range_low": 100.0}}
    assert failed_move_level(1, sig) == 105.0 and failed_move_level(-1, sig) == 100.0
    fade = {**sig, "variant": "fade"}
    assert failed_move_level(1, fade) == 100.0 and failed_move_level(-1, fade) == 105.0
    assert failed_move_level(1, {}) is None and failed_move_level(1, None) is None
    assert failed_move_level(1, {"variant": "fade", "structure_levels": {"range_high": 1.0}}) is None


# ---- the new staged / BE policies -----------------------------------------------------------------------
def test_tp1_plus_runner_takes_half_at_the_structural_tp1_then_runs_with_break_even():
    rows = [(100.0, 100.4, 99.9, 100.3), (100.3, 100.7, 100.2, 100.6), (100.6, 100.65, 99.9, 100.0)]
    r = simulate_policy(P_TP1_RUNNER, entry(1, 100.0, 99.0, tp1=100.6, tp2=101.5, pre=PRE), mk(rows))
    assert r.fills[0].reason == "TP1" and r.fills[0].fraction == pytest.approx(0.5)
    assert r.fills[0].price == pytest.approx(100.6) and r.stages_hit >= 1
    assert all(f.reason != "TP2" for f in r.fills)  # TP2 is NEVER used by this policy
    assert r.stop_levels[-1] >= 100.0  # break-even (cost-adjusted) after TP1


def test_break_even_plus_runner_moves_the_floor_after_one_r_from_the_next_bar_and_has_no_target():
    rows = [(100.0, 101.2, 99.9, 101.1), (101.1, 101.3, 99.95, 100.0), (100.0, 100.2, 99.9, 100.1)]
    r = simulate_policy(P_BE_RUNNER, entry(1, 100.0, 99.0, pre=PRE), mk(rows))
    assert r.stop_levels[0] == 99.0 and r.stop_levels[1] >= 100.0  # ratchet after +1R was reached
    # bar 0 reached +1.2R but its own low (99.9) is above the ORIGINAL stop: the floor applies from bar 1
    assert r.final_reason in ("BREAK_EVEN_STOP", "STOP", "TRAILING_STOP") and r.r >= -1e-9
    assert not any(f.reason.startswith("TP") for f in r.fills)


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("seed", range(10))
def test_stop_never_loosens_in_the_new_policies(direction, seed):
    bars, _ = random_path(400 + seed, n=70, spread=0.03, vol=0.45)
    long = direction == 1
    fill, stop = (100.03, 99.0) if long else (100.0, 101.0)
    e = entry(direction, fill, stop, tp1=fill + (0.6 if long else -0.6), tp2=fill + (1.4 if long else -1.4), pre=PRE, fml=(99.5 if long else 100.5))
    for pid in (P_TP1_RUNNER, P_BE_RUNNER, P_FM):
        r = simulate_policy(pid, e, bars)
        assert r.applicable
        for a, b in zip(r.stop_levels, r.stop_levels[1:], strict=False):
            assert (b >= a) if long else (b <= a), (pid, r.stop_levels)


@pytest.mark.parametrize("seed", range(10))
def test_long_short_mirror_for_every_lab_policy(seed):
    bars, rows = random_path(500 + seed, n=60, vol=0.5)
    k = 200.0
    mirror = mk([(k - o, k - lo, k - h, k - c) for o, h, lo, c in rows])
    pre_rows = [(100.0, 100.2, 99.85, 100.1), (100.1, 100.3, 99.9, 100.2), (100.2, 100.25, 99.8, 99.9), (99.9, 100.1, 99.7, 99.95), (99.95, 100.2, 99.9, 100.1), (100.1, 100.2, 99.95, 100.0)]
    pre_l = mk(pre_rows, start=T0 - timedelta(minutes=30))
    pre_s = mk([(k - o, k - lo, k - h, k - c) for o, h, lo, c in pre_rows], start=T0 - timedelta(minutes=30))
    a = evaluate_shadow(entry(1, 100.0, 99.0, tp1=100.7, tp2=101.3, pre=pre_l, fml=99.6), bars)
    b = evaluate_shadow(entry(-1, k - 100.0, k - 99.0, tp1=k - 100.7, tp2=k - 101.3, pre=pre_s, fml=k - 99.6), mirror)
    for n in LAB_NAMES:
        ra, rb = a["policies"][n], b["policies"][n]
        assert ra["applicable"] == rb["applicable"], n
        if ra["applicable"]:
            for key in ("r", "mfe_r", "mae_r", "giveback_r"):
                assert ra[key] == pytest.approx(rb[key], abs=1e-9), (n, key)
            assert ra["exit_reason"] == rb["exit_reason"] and ra["bars_held"] == rb["bars_held"], n


# ---- causality --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(6))
def test_truncating_future_bars_does_not_change_a_decision_at_t(seed):
    bars, _ = random_path(600 + seed, n=80, vol=0.5)
    e = entry(tp1=100.6, tp2=101.2, pre=PRE, fml=99.6)
    full = evaluate_shadow(e, bars)
    for cut in (20, 35, 50):
        part = BarSeries(*(col[:cut] for col in (bars.ts, bars.o, bars.h, bars.lo, bars.c, bars.spread)))
        sub = evaluate_shadow(e, part)
        for n, rec in full["policies"].items():
            if rec["applicable"] and not rec["censored"] and rec["bars_held"] < cut - 1:  # decided strictly inside the kept window
                assert sub["policies"][n]["r"] == pytest.approx(rec["r"]), (n, cut)
                assert sub["policies"][n]["exit_reason"] == rec["exit_reason"], (n, cut)
                assert sub["policies"][n]["mfe_r"] == pytest.approx(rec["mfe_r"]), (n, cut)


# ---- metrics -----------------------------------------------------------------------------------------------
def test_metrics_mfe_mae_capture_giveback_bars_held():
    rows = [(100.0, 100.8, 99.8, 100.6), (100.6, 101.1, 100.4, 100.9), (100.9, 101.0, 98.9, 99.0)]  # +1.1R peak, then the stop
    rec = evaluate_shadow(entry(1, 100.0, 99.0, pre=PRE), mk(rows))["policies"]["pure_structure_trail"]
    assert rec["exit_reason"] in ("STOP", "TRAILING_STOP") and rec["bars_held"] == 2
    assert rec["mfe_r"] == pytest.approx(1.1) and rec["mae_r"] >= 1.0 - 1e-9  # the stopping bar adds its adverse extreme only
    assert rec["giveback_r"] == pytest.approx(rec["mfe_r"] - rec["r"]) and rec["capture_ratio"] == 0.0
    fx = evaluate_shadow(entry(1, 100.0, 99.0, pre=PRE), mk([(100.0, 100.8, 99.8, 100.6), (100.6, 101.7, 100.4, 101.6)]))["policies"]["fixed_1_5r"]
    assert fx["r"] == pytest.approx(1.5) and fx["mfe_r"] >= 1.5 - 1e-9 and fx["capture_ratio"] == pytest.approx(1.5 / 1.7)


# ---- aggregation / report ------------------------------------------------------------------------------
def _row(i, market="GER40", family="STRUCT", variant="breakout", ev=None, direction=1, seed=None):
    bars, _ = random_path(700 + (i if seed is None else seed), n=40, vol=0.5)
    e = entry(direction, 100.0, 99.0, tp1=100.6, tp2=101.2, pre=PRE, fml=99.6, eid=f"{market}:{i}")
    return {
        "market": market, "family": family, "variant": variant, "direction": direction, "signal_ts": T0 + timedelta(hours=3 * i),
        "structure_event_id": ev, "entry_id": e.entry_id,
        "lab": evaluate_shadow(e, bars, live={"profile": "fixed_1_5r", "r": 0.1, "exit_reason": "STOP"}),
    }


def test_summary_cell_flags_small_n_checks_same_entry_and_clusters_variants():
    rows = [_row(i, variant=v, ev=f"EV{i // 4}", seed=i // 4) for i, v in zip(range(8), ("breakout", "confirmed", "retest", "fade") * 2, strict=True)]
    s = summarise_lab(rows)
    assert s["n"] == 8 and s["n_event_clusters"] == 2 and s["small_n"] is True and s["same_entry_assertion"] is True
    assert set(s["policies"]) == set(LAB_NAMES)
    assert s["paired_vs_fixed_1_5r"]["TP1_only"]["n_event_clusters"] <= 2 and s["paired_vs_fixed_1_5r"]["TP1_only"]["small_n"] is True
    assert BASELINE_NAME not in s["paired_vs_fixed_1_5r"] and s["live"]["n"] == 8
    g = summarise_groups(rows)
    assert g["statement"].startswith("hypotheses only; forward evidence decides promotion") and "GER40|STRUCT|breakout" in g["groups"]
    assert "fixed_1_5r" in g["by_live_profile"]
    rows[0]["entry_id"] = "somebody-else"  # a mismatching entry id is detected
    assert summarise_lab(rows)["same_entry_assertion"] is False


def test_summary_paired_difference_uses_only_entries_where_both_apply():
    rows = [_row(i) for i in range(6)]
    rows[0]["lab"]["policies"]["TP1_only"] = {"applicable": False, "na_reason": "NO_STRUCTURAL_TP1"}
    s = summarise_lab(rows)
    assert s["paired_vs_fixed_1_5r"]["TP1_only"]["n_pairs"] == 5
    assert s["policies"]["TP1_only"]["share_not_applicable"] == pytest.approx(1 / 6)


def test_policy_parameters_are_pinned_no_tuning_between_runs():
    """Any change here is a POLICY_SET_VERSION bump, never a silent re-tune on outcomes."""
    p = POLICY_PARAMS
    assert (p["fixed_r"], p["be_trigger_r"], p["momentum_threshold_atr"], p["time_stop_bars"], p["time_stop_min_mfe_r"]) == ("1.5", "1.0", "-1.0", 24, "0.5")
    assert p["tp12_fractions"] == ["0.5", "0.5"] and p["runner_fractions"] == ["0.5", "0.25"] and p["swing_n"] == 2
    assert POLICY_SET_VERSION == "eeq-policies-2" and SHADOW_LAB_VERSION == "swl-1"
    bars, _ = random_path(9)
    e = entry(tp1=100.6, tp2=101.2, pre=PRE, fml=99.6)
    first = evaluate_shadow(e, bars)
    assert evaluate_shadow(e, bars) == first and lab.LAB_PARAMS["policy_params"] is POLICY_PARAMS


def test_offline_study_aggregation_and_rendering_are_deterministic():
    from coverage_analysis.shadow_exit_lab_study import (
        aggregate_market,
        market_seed,
        meta_block,
        pooled,
        render_markdown,
    )

    def rows(n, fam):
        out = []
        for i in range(n):
            r = _row(i, family=fam, variant="breakout")
            out.append({**r, "shadow_lab": r["lab"], "shadow_lab_ms": 25.0, "shadow_lab_bars": 40})
        return out

    res = {"real": rows(6, "STRUCT"), "control": rows(6, "RANDOM_CONTROL"), "excl": {}, "excl_control": {}, "seed": market_seed("GER40")}
    assert market_seed("GER40") == market_seed("GER40") != market_seed("NAS100")
    agg = aggregate_market(res)
    assert agg["n_real"] == 6 and agg["lab_cost"]["mean_ms"] == 25.0 and agg["real"]["overall"]["small_n"] is True
    cm, cmc = pooled(res["real"], res["control"])
    result = {"meta": meta_block([], ["c1"]), "markets": {"GER40": agg}, "cross_market": cm, "cross_market_control": cmc}
    md = render_markdown(result)
    assert render_markdown(result) == md
    assert "no policy promoted from this table" in md and "random-entry control" in md and "Forward hook cost and default" in md
    for n in USER_MINIMUM:
        assert n in md
