# ruff: noqa: E501
"""observer-controls-3 (same Berlin clock time, other day): clock equality, partition / horizon / forward bounds, event ids, determinism, A/A disjointness,
never-an-opportunity-bar, blocking balance gate (passes on well matched data, fails on distorted data) and the step end to end (controls-2 files untouched)."""

from __future__ import annotations

import dataclasses
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from ol_backfill_support import CODE, MARKET, make_frame, make_inputs, make_mspec

from alpha.common.market_data import FORWARD_HOLDOUT_START, ForwardHoldoutError
from coverage_analysis.observer_lab import backfill as BF
from coverage_analysis.observer_lab import backfill_controls3 as B3
from coverage_analysis.observer_lab import controls as CT
from coverage_analysis.observer_lab import controls_sametime as CS
from coverage_analysis.observer_lab import splits as SP

SPAN_T0 = int(pd.Timestamp("2026-06-12", tz="UTC").value)  # crosses TRAIN/VALIDATION, OOS (07-01) and FORWARD (09-01)
PER_DAY = 288
WIDE = CS.SameTimeSpec(atr_pct_band=0.35, spread_pct_band=0.6)  # i.i.d. synthetic series: wide bands so that nearly every event finds a candidate within +-10 days


@pytest.fixture(scope="module")
def span(make_bars):
    rng = np.random.default_rng(3)
    days = 96
    n = days * PER_DAY
    minute = np.tile(5 * np.arange(PER_DAY), days)
    close = 100 + np.cumsum(rng.normal(0, 0.05, n))
    atr = np.exp(rng.normal(0, 0.5, n))
    return make_bars(close, close + 0.1, close - 0.1, close, spread=rng.uniform(0.01, 0.05, n), atr=atr, local_minute=minute,
                     local_day=np.repeat(np.arange(days), PER_DAY), t0_ns=SPAN_T0)


@pytest.fixture(scope="module")
def part(span):
    return CT.bar_partitions(span)


def _events(part, k=12, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for name in (SP.TRAIN, SP.VALIDATION, SP.OOS):
        pool = np.flatnonzero(part == name)
        pool = pool[(pool % PER_DAY > 100) & (pool % PER_DAY < 150)]
        out += rng.choice(pool, size=k, replace=False).tolist()
    return np.sort(np.array(out))


def test_controls_sit_at_the_exact_same_berlin_clock_time_on_another_trading_day_within_ten_days(span, part):
    ev = _events(part)
    cs = CS.match_controls_sametime(span, ev, seed=1, spec=WIDE)
    assert len(cs.control_idx) > 0.9 * len(ev)
    day_id, minute, _dates = CS.berlin_clock(span)
    for (pos, j), x in zip(zip(cs.event_pos, cs.control_idx, strict=True), cs.extra, strict=True):
        i = int(ev[pos])
        assert minute[j] == minute[i]  # exact Berlin clock time
        assert day_id[j] != day_id[i] and 1 <= abs(day_id[j] - day_id[i]) <= 10
        assert x["day_offset"] == day_id[j] - day_id[i]
    assert cs.report.method == CS.CONTROL_METHOD_VERSION == "observer-controls-3"


def test_trading_day_walk_skips_days_without_bars(make_bars):
    n_days = 40
    keep = np.ones(n_days, bool)
    keep[[10, 11, 12]] = False  # a three day data gap: those dates are not trading days, the walk steps over them
    sel = np.repeat(keep, PER_DAY)
    day_of = np.repeat(np.arange(n_days), PER_DAY)[sel]
    minute = np.tile(5 * np.arange(PER_DAY), n_days)[sel]
    n = int(sel.sum())
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 0.05, n))
    ts0 = int(pd.Timestamp("2026-04-01", tz="Europe/Berlin").value)  # Berlin midnight: every synthetic day is one Berlin date
    bars = make_bars(close, close + 0.1, close - 0.1, close, spread=0.02, atr=np.exp(rng.normal(0, 0.3, n)), local_minute=minute, local_day=day_of, t0_ns=ts0)
    bars = dataclasses.replace(bars, ts_ns=ts0 + (day_of.astype(np.int64) * 86400 + minute.astype(np.int64) * 60) * 10**9)
    day_id, _, dates = CS.berlin_clock(bars)
    assert len(dates) == n_days - 3 and np.all(np.diff(day_id) >= 0)
    i = int(np.flatnonzero((day_of == 9) & (minute == 5 * 130))[0])
    spec = CS.SameTimeSpec(n_controls=2, atr_pct_band=1.0, spread_pct_band=1.0)
    cs = CS.match_controls_sametime(bars, [i], spec=spec, seed=1, partition=np.full(n, SP.TRAIN, dtype=object))
    assert sorted(day_of[cs.control_idx].tolist()) == [8, 13]  # nearest trading days: the day before and the first day after the gap
    assert sorted(x["day_offset"] for x in cs.extra) == [-1, 1]


def test_partition_horizon_and_forward_bounds(span, part):
    ev = _events(part)
    cs = CS.match_controls_sametime(span, ev, seed=2, spec=dataclasses.replace(WIDE, n_controls=2))
    assert len(cs.control_idx) > 0
    _, _, dates = CS.berlin_clock(span)
    day_id, _, _ = CS.berlin_clock(span)
    for pos, j in zip(cs.event_pos, cs.control_idx, strict=True):
        assert part[j] == part[ev[pos]] and part[j] in (SP.TRAIN, SP.VALIDATION, SP.OOS)  # same partition; never PURGED / EMBARGO / FORWARD
        assert j + 1 + 48 < len(span) and part[min(len(span) - 1, j + 48)] != SP.FORWARD  # the whole 48-bar label horizon stays out of the forward period
        assert dates[day_id[j]] < FORWARD_HOLDOUT_START
    assert cs.control_partition == tuple(part[cs.control_idx].tolist())
    # an event inside the forward period is an error, never skipped
    fwd = np.flatnonzero(part == SP.FORWARD)
    assert len(fwd) > 0
    with pytest.raises(ForwardHoldoutError):
        CS.match_controls_sametime(span, np.array([int(fwd[len(fwd) // 2])]), seed=2)
    with pytest.raises(ValueError):
        CS.match_controls_sametime(span, ev, partition=None)


def test_controls_never_cross_into_purged_or_embargo_even_next_to_a_boundary(span, part):
    # events one day before the TRAIN/VALIDATION and VALIDATION/OOS boundary have candidate days on the other side: those bars must not be used
    for name in (SP.TRAIN, SP.VALIDATION):
        pool = np.flatnonzero(part == name)
        ev = pool[-3 * PER_DAY:][(np.arange(3 * PER_DAY) % PER_DAY > 100) & (np.arange(3 * PER_DAY) % PER_DAY < 150)][::7]
        cs = CS.match_controls_sametime(span, ev, seed=5)
        assert all(part[j] == name for j in cs.control_idx.tolist())


def test_event_position_maps_every_control_to_exactly_its_event_and_n_controls_is_respected(span, part):
    ev = _events(part)
    cs = CS.match_controls_sametime(span, ev, seed=3, spec=dataclasses.replace(WIDE, n_controls=2))
    counts = np.bincount(cs.event_pos, minlength=len(ev))
    assert counts.max() <= 2 and counts.sum() == len(cs.control_idx) and (counts == 2).sum() > 0
    assert len(set(cs.control_idx.tolist())) == len(cs.control_idx)  # without replacement
    assert cs.report.n_matched == int((counts > 0).sum()) and cs.report.n_events == len(ev)
    assert len(cs.extra) == len(cs.control_idx)


def test_determinism_and_the_seed_matters(span, part):
    ev = _events(part)
    a = CS.match_controls_sametime(span, ev, seed=11, spec=WIDE)
    b = CS.match_controls_sametime(span, ev, seed=11, spec=WIDE)
    c = CS.match_controls_sametime(span, ev, seed=12, spec=WIDE)
    assert np.array_equal(a.control_idx, b.control_idx) and np.array_equal(a.event_pos, b.event_pos) and a.extra == b.extra
    assert not np.array_equal(a.control_idx, c.control_idx)


def test_controls_b_is_disjoint_from_controls_a_and_uses_another_seed(span, part):
    ev = _events(part)
    a = CS.match_controls_sametime(span, ev, seed=4, spec=WIDE)
    b = CS.match_controls_sametime(span, ev, seed=4, spec=dataclasses.replace(WIDE, control_set="b"), avoid_idx=a.control_idx)
    assert len(b.control_idx) > 0.8 * len(ev) and not (set(a.control_idx.tolist()) & set(b.control_idx.tolist()))
    assert {x["control_set"] for x in b.extra} == {"b"} and {x["control_set"] for x in a.extra} == {"a"}
    # B without the avoid list but with its own seed would differ from A as well
    b2 = CS.match_controls_sametime(span, ev, seed=4, spec=dataclasses.replace(WIDE, control_set="b"))
    assert not np.array_equal(a.control_idx, b2.control_idx)


def test_control_bar_is_never_an_opportunity_bar_and_distance_is_a_diagnostic_only(span, part):
    ev = _events(part)
    rng = np.random.default_rng(9)
    opp = np.unique(np.concatenate([rng.choice(len(span), size=4000, replace=False), ev]))  # a dense, large set of other-family opportunities
    cs = CS.match_controls_sametime(span, ev, seed=6, exclude_idx=opp, spec=WIDE)
    assert len(cs.control_idx) > 0 and not (set(cs.control_idx.tolist()) & set(opp.tolist()))
    for j, x in zip(cs.control_idx.tolist(), cs.extra, strict=True):
        assert x["dist_to_opportunity_bars"] == int(np.min(np.abs(opp - j))) >= 1
    # no exclusion RADIUS: with a dense opportunity set some control sits within 48 bars of an opportunity (controls-2 would forbid it)
    assert min(x["dist_to_opportunity_bars"] for x in cs.extra) <= 48


def test_unmatchable_events_are_reported_not_dropped(span, part):
    ev = _events(part)
    purged = np.flatnonzero(part == SP.PURGED)
    cs = CS.match_controls_sametime(span, np.concatenate([ev, purged[:3]]), seed=1)
    assert cs.report.n_events == len(ev) + 3 and cs.report.n_events_in_excluded_partition >= 3 and len(cs.report.unmatched_event_pos) >= 3


# ---------------------------------------------------------------------------------------------- balance gate
def _pairs(n, partition, *, shift_minute=0.0, shift_atr=0.0, censor_ev=0.2, censor_c=0.2, session_c="MID", seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "partition": partition, "control_of": [f"e{k}" for k in range(n)],
        "ev_minute": rng.normal(600, 60, n), "ev_atr_pct": rng.uniform(0, 1, n), "ev_spread_pct": rng.uniform(0, 1, n), "ev_session": ["MID"] * n,
        "c_minute": rng.normal(600, 60, n) + shift_minute, "c_atr_pct": np.clip(rng.uniform(0, 1, n) + shift_atr, 0, 2), "c_spread_pct": rng.uniform(0, 1, n), "c_session": [session_c] * n,
        "ev_censored": rng.random(n) < censor_ev, "c_censored": rng.random(n) < censor_c,
    })


def test_balance_gate_passes_on_well_matched_data():
    pairs = pd.concat([_pairs(4000, p, seed=k) for k, p in enumerate(CS.GATE_PARTITIONS)], ignore_index=True)
    pairs["control_of"] = [f"e{k}" for k in range(len(pairs))]
    g = CS.balance_gate(pairs, {p: 4100 for p in CS.GATE_PARTITIONS})
    assert g["market_status"] == "analysis_eligible", g
    assert all(r["verdict"] == "PASS" for r in g["partitions"].values())


@pytest.mark.parametrize("kw,needle", [
    ({"shift_minute": 120.0}, "SMD local_minute"), ({"shift_atr": 0.5}, "SMD atr_pct"), ({"censor_ev": 0.156, "censor_c": 1.0}, "censored share"), ({"session_c": "OPEN_HOUR"}, "session share"),
])
def test_balance_gate_fails_on_distorted_data_and_marks_the_market_descriptive_only(kw, needle):
    good = [_pairs(4000, p, seed=k) for k, p in enumerate((SP.TRAIN, SP.VALIDATION))]
    bad = _pairs(4000, "FROZEN_OOS", seed=7, **kw)
    pairs = pd.concat([*good, bad], ignore_index=True)
    pairs["control_of"] = [f"e{k}" for k in range(len(pairs))]
    g = CS.balance_gate(pairs, {SP.TRAIN: 4000, SP.VALIDATION: 4000, "FROZEN_OOS": 4000})
    assert g["partitions"]["FROZEN_OOS"]["verdict"] == "FAIL" and any(needle in r for r in g["partitions"]["FROZEN_OOS"]["fail_reasons"]), g
    assert g["partitions"][SP.TRAIN]["verdict"] == "PASS" and g["market_status"] == "descriptive_only" and g["market_pass"] is False


def test_balance_gate_match_rate_and_small_n_are_blocking():
    pairs = _pairs(100, SP.TRAIN)
    g = CS.balance_gate(pairs, {SP.TRAIN: 1000})  # 100 of 1000 events matched
    assert g["partitions"][SP.TRAIN]["verdict"] == "FAIL" and "match_rate" in g["partitions"][SP.TRAIN]["fail_reasons"][0] and g["market_status"] == "descriptive_only"
    g2 = CS.balance_gate(_pairs(10, SP.TRAIN), {SP.TRAIN: 10})
    assert g2["partitions"][SP.TRAIN]["verdict"] == "INSUFFICIENT_N" and g2["market_status"] == "descriptive_only"
    g3 = CS.balance_gate(_pairs(0, SP.TRAIN), {SP.TRAIN: 500})  # no controls at all
    assert g3["partitions"][SP.TRAIN]["verdict"] == "FAIL" and g3["market_status"] == "descriptive_only"


def test_balance_gate_on_real_matching_output_of_a_stationary_series(span, part):
    ev = _events(part, k=30)
    cs = CS.match_controls_sametime(span, ev, seed=1, spec=WIDE)
    cov = CT.bar_covariates(span, rank_mode="partition", partition=part)
    pr = pd.DataFrame({
        "partition": [str(part[ev[p]]) if part[ev[p]] != SP.OOS else "FROZEN_OOS" for p in cs.event_pos], "control_of": [f"e{p}" for p in cs.event_pos],
        **{f"ev_{k}": cov[k2][ev[cs.event_pos]] for k, k2 in (("minute", "minute"), ("atr_pct", "atr_pct"), ("spread_pct", "spread_pct"), ("session", "session"))},
        **{f"c_{k}": cov[k2][cs.control_idx] for k, k2 in (("minute", "minute"), ("atr_pct", "atr_pct"), ("spread_pct", "spread_pct"), ("session", "session"))},
        "ev_censored": False, "c_censored": False,
    })
    n_by = {"TRAIN": int((part[ev] == SP.TRAIN).sum()), "VALIDATION": int((part[ev] == SP.VALIDATION).sum()), "FROZEN_OOS": int((part[ev] == SP.OOS).sum())}
    g = CS.balance_gate(pr, n_by)  # 30 events per partition: SMD noise alone can exceed 0.1, so only the structural quantities are asserted here
    for p, r in g["partitions"].items():
        assert r["match_rate"] >= 0.9 and r["session_share_diff_max"] == 0.0 and np.isfinite(list(r["smd"].values())).all(), (p, r)


# ---------------------------------------------------------------------------------------------- step end to end
def _long_world():
    frame = make_frame(n=45 * 288, t0=pd.Timestamp("2026-06-08 00:00", tz="UTC"), seed=21)
    mi = make_inputs(frame)
    ev_bars = []
    rng = np.random.default_rng(4)
    for d in range(2, 44):
        ev_bars += sorted(int(d * 288 + 100 + x) for x in rng.choice(60, size=1, replace=False))  # varying clock times inside the cash session (a fixed clock time would make every candidate an opportunity)
    return frame, mi, ev_bars


def _patched(ev_bars):
    import contextlib

    from ol_backfill_support import FakeData  # noqa: F401

    @contextlib.contextmanager
    def cm():
        old = BF.generate_candidates, BF.describe_candidate

        def gen(d, spec, thr):
            idx = np.array(ev_bars, dtype=np.int64)
            dirs = np.where(np.arange(len(idx)) % 2 == 0, 1, -1).astype(np.int8)
            stops = d.c[idx] - dirs * 2.0 * d.atr[idx]
            return SimpleNamespace(decision_idx=idx, direction=dirs, stop=stops, target=np.full(len(idx), np.nan), target_r=np.full(len(idx), np.nan), exit_kind=np.zeros(len(idx), dtype=np.int8))

        BF.generate_candidates, BF.describe_candidate = gen, (lambda d, spec, i, direction: {"break_bar_offset": 2})  # type: ignore[assignment]
        try:
            yield
        finally:
            BF.generate_candidates, BF.describe_candidate = old

    return cm()


def test_controls3_step_end_to_end_is_independent_of_controls2_and_carries_the_gate(tmp_path):
    frame, mi, ev_bars = _long_world()
    ms = make_mspec()
    with _patched(ev_bars):
        BF.run_events_step(mi, ms, tmp_path, code=CODE)
        c2 = BF.run_controls_step(frame, ms, MARKET, tmp_path, seed=1, code=CODE)
        mdir = tmp_path / MARKET
        snap = {f.name: f.stat().st_mtime_ns for f in mdir.iterdir() if f.is_file() and f.name != "backfill.log"}
        c3 = B3.run_controls3_step(frame, ms, MARKET, tmp_path, seed=1, code=CODE, spec=WIDE)
        assert c3["status_this_call"] == "BUILT" and c3["control_method_version"] == "observer-controls-3" and c3["control_set"] == "a" and c3["controls_b"]["control_set"] == "b"
        assert {f.name: f.stat().st_mtime_ns for f in mdir.iterdir() if f.is_file() and f.name in snap} == snap  # events AND controls-2 files untouched
        assert c2["control_method_version"] == "observer-controls-2" and (mdir / "controls3" / "controls_manifest.json").is_file() and (mdir / "controls3_b" / "controls_manifest.json").is_file()
        assert c3["n_controls"] > 0 and "balance_gate" in c3 and c3["market_status"] in ("analysis_eligible", "descriptive_only")
        diag = pd.read_parquet(mdir / "controls3" / B3.DIAG_FILE)
        events = pd.read_parquet(mdir / BF.EVENT_FILES["events"])
        cev = pd.read_parquet(mdir / "controls3" / BF.CONTROL_FILES["events"])
        assert set(diag["control_of"]) <= set(events["event_id"]) and (diag["control_partition"] == diag["partition"].replace({"FROZEN_OOS": "OOS"})).all()
        assert set(cev["control_of"]) == set(diag["control_of"]) and cev["is_control"].all()  # every control carries the id of its event
        bdiag = pd.read_parquet(mdir / "controls3_b" / B3.DIAG_FILE)
        assert not (set(diag["control_decision_idx"]) & set(bdiag["control_decision_idx"]))
        assert diag["day_offset"].abs().between(1, 10).all()
        # idempotent per fingerprint
        assert B3.run_controls3_step(frame, ms, MARKET, tmp_path, seed=1, code=CODE, spec=WIDE)["status_this_call"] == "SKIPPED_COMPLETE"
        # the gate recomputed from the files equals the one stored in the manifest
        again = B3.gate_for_dir(mdir, mdir / "controls3")
        assert json.dumps(again["partitions"], sort_keys=True, default=str) == json.dumps(json.loads((mdir / "controls3" / "controls_manifest.json").read_text())["balance_gate"]["partitions"], sort_keys=True, default=str)
        # loader: controls3 selectable, no label columns leak
        tbl = BF.load_event_table(mdir, with_labels=False, controls_dir="controls3")
        assert tbl["is_control"].sum() == c3["n_controls"] and not [c for c in tbl.columns if c.startswith("y_")]


def test_balance_script_runs_on_the_step_output(tmp_path):
    import importlib.util
    import sys

    frame, mi, ev_bars = _long_world()
    with _patched(ev_bars):
        BF.run_events_step(mi, make_mspec(), tmp_path, code=CODE)
        B3.run_controls3_step(frame, make_mspec(), MARKET, tmp_path, seed=2, code=CODE, spec=WIDE)
    spec = importlib.util.spec_from_file_location("observer_controls_balance", Path(__file__).resolve().parents[3] / "scripts" / "observer_controls_balance.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["observer_controls_balance"] = mod
    spec.loader.exec_module(mod)
    res = mod.run(tmp_path, [MARKET], ("a", "b"))
    assert res[MARKET]["a"]["gate_version"] == CS.GATE_VERSION and "market_status" in res[MARKET]["b"]
    assert "MARKET ->" in mod.render(res)
    assert tempfile.gettempdir()
