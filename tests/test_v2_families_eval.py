# ruff: noqa: E501
"""Evaluation utilities: BH, day-shifted null mapping, trial ledger, baselines, and the probe runner end to end."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import numpy as np
import pytest

from alpha.families import registry as R
from alpha.families.evaluate import (
    SimContext,
    TrialKey,
    bh_adjust,
    drift_baselines,
    load_ledger,
    make_ledger,
    one_sided_p,
    shift_candidates,
)
from alpha.families.orb import ORBSpec
from tests.test_v2_families_synth import synth_data


def test_benjamini_hochberg_matches_a_hand_computation():
    p = [0.01, 0.04, 0.03, 0.005, None, 0.5]
    q = bh_adjust(p)
    # sorted p: .005 .01 .03 .04 .5 (m=5) -> raw .025 .025 .0500 .05 .5 ; step-up minima from the right
    assert q[4] is None
    assert q[3] == pytest.approx(0.025) and q[0] == pytest.approx(0.025)
    assert q[2] == pytest.approx(0.05) and q[1] == pytest.approx(0.05) and q[5] == pytest.approx(0.5)
    assert all(a is None or 0 <= a <= 1 for a in q)
    assert bh_adjust([None, None]) == [None, None]
    assert one_sided_p(0.0) == pytest.approx(0.5) and one_sided_p(1.6449) == pytest.approx(0.05, abs=1e-3) and one_sided_p(None) is None


@pytest.fixture(scope="module")
def world():
    d = synth_data(n_days=120, seed=9, plant="gap_fade", phi=0.4)
    return d, SimContext.default(d)


def test_shift_candidates_preserves_structure_and_moves_the_price_path(world):
    d, _ = world
    spec = ORBSpec("breakout", 15, 0.0, 1.0, "range", 1.0, 240)
    thr = R.fit_thresholds(d, spec)
    c = R.generate_candidates(d, spec, thr)
    assert len(c.decision_idx) > 40
    same = shift_candidates(c, d, 0)
    assert np.array_equal(same.decision_idx, c.decision_idx) and np.allclose(same.stop, c.stop) and np.allclose(same.target, c.target)
    s = shift_candidates(c, d, 7)
    assert 0 < len(s.decision_idx) <= len(c.decision_idx) and (np.diff(s.decision_idx) > 0).all()
    # same clock bar of a different day
    assert set(np.unique(d.minute[s.decision_idx])) <= set(np.unique(d.minute[c.decision_idx]))
    ndays = int(d.day.max()) + 1
    assert set(((d.day[c.decision_idx] + 7) % ndays).tolist()) >= set(d.day[s.decision_idx].tolist())  # days moved by exactly the shift
    # risk / reward DISTANCES in price units are preserved for the candidates that survive the mapping
    risk_old = np.abs(d.c[c.decision_idx] - c.stop)
    risk_new = np.abs(d.c[s.decision_idx] - s.stop)
    assert np.isin(np.round(risk_new, 6), np.round(risk_old, 6)).all()
    assert (s.direction * (d.c[s.decision_idx] - s.stop) > 0).all()
    fin = np.isfinite(s.target)
    assert fin.all() and (s.direction * (s.target - d.c[s.decision_idx]) > 0).all()
    assert d.day[s.decision_idx].min() >= 0


def test_ledger_counts_duplicates_invalid_and_roundtrips(world):
    led = make_ledger()
    a = ORBSpec("fade", 15, 0.1, 0.5, "r", 1.0, 240)
    b = ORBSpec("fade", 15, 0.1, 1.0, "r", 1.0, 240)  # canonically identical (stop_frac unused in fade)
    assert led.record(TrialKey(a, "GER40")) == "new"
    assert led.record(TrialKey(b, "GER40")) == "duplicate"
    assert led.record(TrialKey(a, "NAS100")) == "new"  # per-market identity
    assert led.total_trials == 3 and led.unique == 2 and led.duplicate_rejects == 1

    class Broken(TrialKey):  # validate raises -> counted invalid
        def validate(self) -> None:
            raise ValueError("bad")

    assert led.record(Broken(a, "X")) == "invalid" and led.invalid_rejects == 1
    again = load_ledger(led.to_json())
    assert again.total_trials == 4 and again.unique == 2
    assert again.record(TrialKey(a, "GER40")) == "duplicate" and again.record(TrialKey(a, "SPX500")) == "new"
    assert load_ledger(None).total_trials == 0


def test_drift_baselines_shape(world):
    d, ctx = world
    b = drift_baselines(d, ctx, seed=1, draws=3)
    for k in ("always_long", "always_short", "random_long_short", "same_session_random"):
        assert k in b
    assert b["always_long"]["n_trades"] > 100 and b["always_long"]["trades_per_day"] <= 6.0
    assert b["random_long_short"]["draws"] == 3 and b["same_session_random"]["decisions_per_day"] == 3
    assert b["always_long"]["expectancy_r"] < 0 and b["always_short"]["expectancy_r"] < 0  # costs, no drift on a random walk


def test_probe_runner_end_to_end_on_a_synthetic_market(tmp_path, world):
    from research.runners import v2_family_probe as probe

    d, ctx = world
    cfg = json.loads((probe.DEFAULT_CONFIG).read_text(encoding="utf-8"))
    cfg["min_train_trades"] = 20
    cfg["baselines"]["draws"] = 2

    def loader(market, _cfg):
        spec = SimpleNamespace(asset_class="index_cfd")
        return {"spec": spec, "data": d, "ctx": ctx, "layout": {"note": "synthetic"}, "n_dev_bars": len(d), "source": "synthetic",
                "leaders": [], "calendar_status": "synthetic"}

    out = tmp_path / "rep"
    s1 = probe.run(cfg, ["SYN"], ["ORB", "GAP", "LEADLAG"], out, max_specs=12, n_shifts=4, seed=1, loader=loader)
    for f in ("summary.json", "summary.md", "ledger.json", "SYN/market.json", "SYN/ORB.json", "SYN/GAP.json"):
        assert (out / f).exists(), f
    assert not (out / "SYN/LEADLAG.json").exists() and "skipped" in s1["markets"]["SYN"]["LEADLAG"]
    doc = json.loads((out / "SYN/ORB.json").read_text(encoding="utf-8"))
    assert len(doc["rows"]) == 12 and doc["summary"]["null"]["n_shifts"] == 4 and doc["summary"]["min_trades"] == 20
    assert all(math.isfinite(v) for r in doc["rows"] for v in (r["fitness"],))
    cum = s1["cumulative"]
    assert cum["this_run_trials"] == 24 and cum["cumulative_trials"] == 30310 + 24 and cum["cumulative_unique_specs"] == 29798 + 24
    assert cum["null_evaluations"] == 2 * 4 * 12
    s2 = probe.run(cfg, ["SYN"], ["ORB"], out, max_specs=12, n_shifts=2, seed=1, loader=loader, resume=True)
    assert s2["cumulative"]["carried_in_trials"] == 24 and s2["cumulative"]["cumulative_trials"] == 30310 + 24 + 12
    assert s2["cumulative"]["cumulative_unique_specs"] == 29798 + 24  # the same specs are duplicates, not new uniques
    assert "gate" not in probe.__doc__.lower().replace("later-fold", "")
