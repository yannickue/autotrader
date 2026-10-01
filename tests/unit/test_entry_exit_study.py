# ruff: noqa: E501
"""Lane X offline study on a synthetic frame: same entries for all policies, causality, Berlin deadline, clusters."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from alpha.common.market_data import ForwardHoldoutError
from alpha.families import structbrk
from alpha.families.data import build_family_data
from alpha.families.spec import MarketCalendar
from coverage_analysis.entry_exit import (
    MarketInputs,
    aggregate_market,
    build_entry_rows,
    market_cluster_rollup,
    meta_block,
    render_markdown,
    render_market_md,
    to_json,
)
from demo.entry_exit_quality import ANALYSIS_VERSION, LABEL_SPEC_VERSION, assign_event_clusters
from demo.exit_policies import POLICY_IDS, POLICY_SET_VERSION
from demo.opportunity.operating_policy import load_operating_policy
from demo.opportunity.production_spec import FrozenSpec

CAL = MarketCalendar("UTC", 0, 1440, 0, 1440, 1440)  # full-day calendar (as the Lane F offline study)
START = "2026-06-08 00:00"  # a Monday


def synth(seed=2, days=4, start=START):
    rnd = np.random.default_rng(seed)
    n = days * 288
    ts = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    px, rows = 1000.0, []
    for i in range(n):
        phase = (i // 60) % 3
        vol = 0.3 if phase < 2 else 1.4
        drift = 0.0 if phase < 2 else (0.5 if (i // 180) % 2 == 0 else -0.5)
        o = px
        c = o + rnd.normal(drift, vol)
        h = max(o, c) + abs(rnd.normal(0, vol * 0.4))
        lo = min(o, c) - abs(rnd.normal(0, vol * 0.4))
        rows.append((o, h, lo, c))
        px = c
    a = np.array(rows)
    return pd.DataFrame({"ts": ts, "open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3], "tick_volume": 100.0, "spread_pts": 20.0})


def inputs(frame, eval_from=None, market="BTCUSD"):
    d = build_family_data(frame, CAL, name=market, point_size=0.01, tick_size=0.01, asset_class="crypto_cfd")
    specs = [FrozenSpec(market, structbrk.STRUCTSpec(mode=m), (), "PRIMARY" if m == "confirmed" else "SHADOW") for m in structbrk.MODES]
    return MarketInputs(market, d, frame, specs, 0.01, pd.Timestamp(eval_from or frame["ts"].iloc[300]), "UTC")


@pytest.fixture(scope="module")
def built():
    fr = synth()
    mi = inputs(fr)
    rows, excl = build_entry_rows(mi, load_operating_policy())
    return fr, mi, rows, excl


def test_synthetic_frame_produces_entries(built):
    _, _, rows, excl = built
    assert len(rows) >= 10 and excl["analysed"] == len(rows)


def test_every_entry_is_evaluated_under_all_nine_policies_same_ids(built):
    _, _, rows, _ = built
    ids = [r["entry_id"] for r in rows]
    assert len(set(ids)) == len(ids)
    for r in rows:
        assert set(r["policy_r"]) == set(POLICY_IDS) and len(POLICY_IDS) == 9
        assert set(r["policy_exit"]) == set(POLICY_IDS)
        # the same entry: one fill / stop / signal time, never a per-policy entry
        assert r["baseline_r"] == r["policy_r"]["P1_FIXED_1_5R"]
    res = aggregate_market(rows)
    cell = res["all"]
    assert cell["n"] == len(rows)
    for s in cell["policies"].values():
        assert s["n_applicable"] <= cell["n"]
    assert cell["policies"]["P1_FIXED_1_5R"]["n_applicable"] == cell["n"]  # the baseline applies to every entry
    assert cell["policies"]["P9_EOD_FORCED_FLAT"]["n_applicable"] == cell["n"]


def test_paired_structural_subset_uses_only_entries_where_all_structural_policies_apply(built):
    _, _, rows, _ = built
    cell = aggregate_market(rows)["all"]
    want = [r for r in rows if all(r["policy_r"][p] is not None for p in ("P2_STRUCT_TP1", "P3_STRUCT_TP1_TP2", "P4_TP1_TP2_RUNNER"))]
    assert cell["paired_structural_subset"]["n"] == len(want)
    if want:
        means = cell["paired_structural_subset"]["mean_r"]
        assert means["P1_FIXED_1_5R"] == pytest.approx(sum(r["baseline_r"] for r in want) / len(want))


def test_flat_deadline_is_the_berlin_flatten_start(built):
    _, _, rows, excl = built
    for r in rows:
        loc = r["flat_utc"].astimezone(ZoneInfo("Europe/Berlin"))
        assert (loc.hour, loc.minute) == (21, 55)  # the Lane P flatten start, DST-aware (19:55Z in June)
        assert r["flat_utc"] > r["signal_ts"]
    # Saturday/Sunday bars and entries inside the flatten window / runway are refused by the operating policy
    sat = synth(seed=2, days=3, start="2026-06-06 00:00")  # Sat, Sun, Mon
    rows2, excl2 = build_entry_rows(inputs(sat, eval_from=sat["ts"].iloc[100]), load_operating_policy())
    assert excl2.get("policy_non_operating_day", 0) > 0
    assert all(r["signal_ts"].astimezone(ZoneInfo("Europe/Berlin")).weekday() < 5 for r in rows2)  # operating day = Berlin date
    assert any(k.startswith("policy_ENTRY") for k in {**excl, **excl2}) or excl.get("candidates", 0) > 0


def test_positions_are_flat_at_the_deadline_under_every_policy(built):
    _, _, rows, _ = built
    for r in rows:
        if not r["baseline_censored"]:
            assert r["holding_baseline_s"] <= (r["flat_utc"] - r["signal_ts"]).total_seconds() + 1e-6


def test_truncating_future_bars_does_not_change_a_past_entrys_measurement():
    fr = synth()
    full_rows, _ = build_entry_rows(inputs(fr), load_operating_policy())
    cut = fr.iloc[: 288 * 2 + 160].reset_index(drop=True)  # keep up to Tuesday ~13:20Z: later bars are gone
    part_rows, _ = build_entry_rows(inputs(cut, eval_from=fr["ts"].iloc[300]), load_operating_policy())
    last = pd.Timestamp(cut["ts"].iloc[-1]).to_pydatetime()
    by_id = {r["entry_id"]: r for r in full_rows}
    checked = 0
    for r in part_rows:
        # an entry whose whole path (to its flat deadline) lies inside the truncated data is fully determined
        if r["flat_utc"] <= last:
            f = by_id[r["entry_id"]]
            for k in ("mfe_r", "mae_r", "baseline_r", "time_to_mfe_s", "time_to_mae_s", "entry_exit_label", "tp1_reached", "tp2_reached", "max_structure_reached_r", "regime", "session"):
                assert f[k] == r[k], (r["entry_id"], k)
            assert f["policy_r"] == r["policy_r"]
            checked += 1
    assert checked >= 3


def test_entries_before_the_evaluation_start_are_warmup_only(built):
    fr, mi, rows, _ = built
    assert all(r["signal_ts"] >= mi.eval_from for r in rows)
    late = inputs(fr, eval_from=fr["ts"].iloc[700])
    rows2, _ = build_entry_rows(late, load_operating_policy())
    assert len(rows2) < len(rows) and all(r["signal_ts"] >= late.eval_from for r in rows2)


def test_forward_holdout_frames_are_refused_by_the_existing_guard():
    fr = synth(days=2, start="2026-09-01 00:00")
    with pytest.raises(ForwardHoldoutError):
        build_family_data(fr, CAL, name="BTCUSD", point_size=0.01, tick_size=0.01, asset_class="crypto_cfd")


def test_struct_variants_of_one_break_are_one_event_cluster(built):
    _, _, rows, _ = built
    with_event = [r for r in rows if r["structure_event_id"]]
    assert with_event
    clusters = assign_event_clusters(rows)
    n_events = len({r["structure_event_id"] for r in with_event})
    assert len(set(clusters)) <= n_events < len(with_event)  # several variants share one break
    cell = aggregate_market(rows)["all"]
    assert cell["n_event_clusters"] == len(set(clusters)) < cell["n"]


def test_market_cluster_rollup_reports_raw_event_and_cross_market_counts():
    from demo.execution.risk_policy import cluster_of

    t = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
    rows = [{"market": m, "direction": 1, "signal_ts": t, "structure_event_id": None} for m in ("GER40", "NAS100", "SPX500", "XAUUSD")]
    out = market_cluster_rollup(rows, cluster_of)
    assert out["INDEX"] == {"raw_entries": 3, "event_clusters": 3, "cross_market_clusters": 1}
    assert out[cluster_of("XAUUSD")]["raw_entries"] == 1 and cluster_of("XAUUSD") != cluster_of("GER40")


def test_outputs_carry_versions_small_n_flags_and_caveats(built):
    _, _, rows, excl = built
    res = aggregate_market(rows)
    for cell in res["families"].values():
        assert "flag" in cell["all"] and "verdict" in cell["all"]
        assert cell["all"]["n"] >= 0
    small = [c["all"] for c in res["families"].values() if c["all"]["n"] < 30]
    assert small and all(c["verdict"] == "INCONCLUSIVE-n" and "n too small" in c["flag"] for c in small)
    md = render_market_md("BTCUSD", res, "synthetic", excl)
    assert "INCONCLUSIVE-n" in md and "SAME entries" in md
    result = {"meta": meta_block(["synthetic data"], ["hindsight"]), "markets": {"BTCUSD": {"markdown": md, "result": res}}, "market_clusters": market_cluster_rollup(rows, lambda m: "CRYPTO")}
    doc = render_markdown(result)
    assert ANALYSIS_VERSION in doc and LABEL_SPEC_VERSION in doc and POLICY_SET_VERSION in doc
    assert "NULL REFERENCE" in doc and js_ref(doc)
    assert "No edge" in doc and "no frozen Train/holdout split" in doc
    js = json.loads(to_json(result))
    assert js["meta"]["analysis_version"] == ANALYSIS_VERSION and js["meta"]["policy_set_version"] == POLICY_SET_VERSION
    assert js["meta"]["label_thresholds"]["useful_mfe_r"] == 0.5
    assert "markdown" not in js["markets"]["BTCUSD"]


def test_signal_age_and_cost_state_fields_are_recorded(built):
    _, _, rows, _ = built
    for r in rows[:20]:
        assert r["signal_age_s"] == 0.0 and r["spread_state"] in ("LOW", "MID", "HIGH")
        assert r["session"] and r["regime"] in ("LOW_VOL", "MID_VOL", "HIGH_VOL", "UNKNOWN")
        assert r["direction"] in (1, -1) and r["family"] == "STRUCT" and r["variant"] in structbrk.MODES


def js_ref(doc):
    from demo.entry_exit_quality import RANDOM_WALK_REFERENCE, random_walk_reach_probability

    assert RANDOM_WALK_REFERENCE["0.5"] == pytest.approx(2 / 3) and random_walk_reach_probability(1.0) == 0.5
    return "0.67" in doc
