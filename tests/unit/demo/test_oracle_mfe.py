# ruff: noqa: E501
"""Lane X2: ORACLE available-MFE diagnostic - synthetic-bar tests (offline, hindsight, never in the live path)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coverage_analysis import oracle_mfe as om
from coverage_analysis.oracle_mfe import (
    LEVELS_R,
    ORACLE_VERSION,
    aggregate_oracle,
    available_mfe,
    capture_fields,
    null_reach_probability,
    opportunity_bucket,
    oracle_meta,
    render_oracle_markdown,
    simulate_null_reach,
    to_oracle_json,
)
from demo.entry_exit_quality import Step, walk_entry_path

T0 = datetime(2026, 5, 4, 8, 0, tzinfo=UTC)


def _steps(bars):
    """bars: (hi, lo) exit-side prices, 5-minute spacing from T0."""
    return [Step(T0 + timedelta(minutes=5 * k), None, hi, lo, None) for k, (hi, lo) in enumerate(bars)]


# ---- the two canonical examples -------------------------------------------------------------------
def test_small_available_mfe_then_stop_means_little_opportunity():
    st = _steps([(100.08, 99.6), (99.5, 98.9), (99.0, 98.0)])
    o = available_mfe(direction=1, entry=100.0, stop=99.0, steps=st, entry_ts=T0, horizon_ts=T0 + timedelta(hours=1))
    assert o.available_mfe_r == pytest.approx(0.08)
    cap = capture_fields(o.available_mfe_r, -1.0)
    assert cap["giveback_from_available_mfe"] == pytest.approx(1.08)
    assert opportunity_bucket(o.available_mfe_r) == "LITTLE_OPPORTUNITY"


def test_large_available_mfe_then_stop_is_a_capture_problem():
    st = _steps([(102.4, 99.9), (100.0, 98.5)])
    o = available_mfe(direction=1, entry=100.0, stop=99.0, steps=st, entry_ts=T0, horizon_ts=T0 + timedelta(hours=1))
    assert o.available_mfe_r == pytest.approx(2.4)
    assert o.time_to_available_mfe_s == 0.0
    cap = capture_fields(o.available_mfe_r, -1.0)
    assert cap["realized_capture_r"] == -1.0
    assert cap["capture_ratio"] == pytest.approx(-1.0 / 2.4)
    assert cap["giveback_from_available_mfe"] == pytest.approx(3.4)
    assert opportunity_bucket(o.available_mfe_r) == "LARGE_OPPORTUNITY"


def test_capture_ratio_undefined_without_opportunity():
    assert capture_fields(0.05, -1.0)["capture_ratio"] is None
    assert capture_fields(0.05, -1.0)["giveback_from_available_mfe"] == pytest.approx(1.05)


# ---- horizon, independence of the stop, mirror, normalisation ------------------------------------
def test_horizon_clips_at_thesis_horizon_exclusive():
    st = _steps([(100.5, 99.8), (100.7, 99.9), (105.0, 99.9), (110.0, 99.9)])
    flat = T0 + timedelta(minutes=10)  # third bar opens AT the horizon: not usable
    o = available_mfe(direction=1, entry=100.0, stop=99.0, steps=st, entry_ts=T0, horizon_ts=flat)
    assert o.available_mfe_r == pytest.approx(0.7)
    assert o.bars == 2
    assert o.horizon_complete is True


def test_incomplete_horizon_is_flagged():
    st = _steps([(100.5, 99.8), (100.7, 99.9)])
    o = available_mfe(direction=1, entry=100.0, stop=99.0, steps=st, entry_ts=T0, horizon_ts=T0 + timedelta(hours=5))
    assert o.horizon_complete is False


def test_independent_of_the_stop_out():
    st = _steps([(100.2, 99.5), (100.0, 98.5), (101.5, 100.5)])  # stop (99) hit in bar 1, rally in bar 2
    kw = dict(direction=1, entry=100.0, stop=99.0, entry_ts=T0)
    o = available_mfe(steps=st, horizon_ts=T0 + timedelta(hours=1), **kw)
    assert o.available_mfe_r == pytest.approx(1.5)
    assert o.stop_touched is True and o.time_to_stop_s == 300.0
    stop_bound = walk_entry_path(steps=st, apply_stop=True, **kw)
    assert stop_bound.mfe_r == pytest.approx(0.2) and o.available_mfe_r > stop_bound.mfe_r


def test_short_mirrors_long():
    # long: entry 100 stop 99; short mirror about 200: entry 100 stop 101, favourable = LOWER prices
    long_o = available_mfe(direction=1, entry=100.0, stop=99.0, steps=_steps([(102.0, 99.5), (100.0, 98.0)]), entry_ts=T0, horizon_ts=T0 + timedelta(hours=1))
    short_o = available_mfe(direction=-1, entry=100.0, stop=101.0, steps=_steps([(100.5, 98.0), (102.0, 100.0)]), entry_ts=T0, horizon_ts=T0 + timedelta(hours=1))
    assert long_o.available_mfe_r == pytest.approx(short_o.available_mfe_r) == pytest.approx(2.0)
    assert short_o.stop_touched is True


def test_r_normalisation_uses_the_initial_risk():
    st = _steps([(101.0, 99.9)])
    a = available_mfe(direction=1, entry=100.0, stop=99.0, steps=st, entry_ts=T0, horizon_ts=T0 + timedelta(hours=1))
    b = available_mfe(direction=1, entry=100.0, stop=98.0, steps=st, entry_ts=T0, horizon_ts=T0 + timedelta(hours=1))
    assert a.available_mfe_r == pytest.approx(1.0) and b.available_mfe_r == pytest.approx(0.5)


def test_invalid_geometry_rejected():
    with pytest.raises(ValueError):
        available_mfe(direction=1, entry=100.0, stop=101.0, steps=[], entry_ts=T0, horizon_ts=T0)
    with pytest.raises(ValueError):
        available_mfe(direction=2, entry=100.0, stop=99.0, steps=[], entry_ts=T0, horizon_ts=T0)


def test_available_mfe_never_below_zero_and_empty_path():
    o = available_mfe(direction=1, entry=100.0, stop=99.0, steps=_steps([(99.9, 99.2)]), entry_ts=T0, horizon_ts=T0 + timedelta(hours=1))
    assert o.available_mfe_r == 0.0
    assert available_mfe(direction=1, entry=100.0, stop=99.0, steps=[], entry_ts=T0, horizon_ts=T0 + timedelta(hours=1)).bars == 0


# ---- null reference -------------------------------------------------------------------------------
def test_null_probability_monotonicity_and_bounds():
    ps = [null_reach_probability(x, risk=1.0, sigma_bar=0.1, n_bars=100) for x in LEVELS_R]
    assert all(0.0 <= p <= 1.0 for p in ps)
    assert ps == sorted(ps, reverse=True) and len(set(ps)) == len(ps)
    short = null_reach_probability(1.0, risk=1.0, sigma_bar=0.1, n_bars=10)
    long = null_reach_probability(1.0, risk=1.0, sigma_bar=0.1, n_bars=200)
    assert long > short  # longer horizon -> more reach
    assert null_reach_probability(1.0, risk=2.0, sigma_bar=0.1, n_bars=100) < null_reach_probability(1.0, risk=1.0, sigma_bar=0.1, n_bars=100)
    assert null_reach_probability(0.0, risk=1.0, sigma_bar=0.1, n_bars=100) == 1.0
    assert null_reach_probability(1.0, risk=1.0, sigma_bar=0.1, n_bars=0) == 0.0


def test_null_probability_known_value_and_simulation_agrees():
    # a = level*risk = 1.0, sigma*sqrt(n) = 1.0 -> P(max >= 1) = 2*(1-Phi(1)) = 0.3173
    assert null_reach_probability(1.0, risk=1.0, sigma_bar=0.1, n_bars=100) == pytest.approx(0.3173, abs=1e-3)
    sim = simulate_null_reach(LEVELS_R, risk=1.0, sigma_bar=0.05, n_bars=400, n_paths=4000, seed=7)
    for x in LEVELS_R:
        ana = null_reach_probability(x, risk=1.0, sigma_bar=0.05, n_bars=400)
        assert sim[x] == pytest.approx(ana, abs=0.05)  # discrete monitoring sits slightly below the continuous reference
        assert sim[x] <= ana + 0.02


def test_null_simulation_is_deterministic_per_seed():
    a = simulate_null_reach(LEVELS_R, risk=1.0, sigma_bar=0.05, n_bars=50, n_paths=500, seed=3)
    b = simulate_null_reach(LEVELS_R, risk=1.0, sigma_bar=0.05, n_bars=50, n_paths=500, seed=3)
    c = simulate_null_reach(LEVELS_R, risk=1.0, sigma_bar=0.05, n_bars=50, n_paths=500, seed=4)
    assert a == b and a != c


# ---- aggregation / outputs ------------------------------------------------------------------------
def _row(i, avail, base_r, direction=1, market="GER40", variant="m1"):
    return {
        "entry_id": f"e{i}", "market": market, "family": "STRUCT", "variant": variant, "direction": direction,
        "signal_ts": T0 + timedelta(hours=2 * i), "structure_event_id": None,
        "available_mfe_r": avail, "time_to_available_mfe_s": 300.0, "horizon_complete": True, "horizon_bars": 50,
        "stop_touched_before_horizon_end": True, "null_p": {f"{x:g}": 0.5 / (1 + x) for x in LEVELS_R},
        "realised": {"P1_FIXED_1_5R": base_r, "P3_STRUCT_TP1_TP2": None},
    }


def test_aggregate_distribution_null_and_contrast():
    rows = [_row(0, 0.08, -1.0), _row(1, 2.4, -1.0), _row(2, 1.0, 1.5), _row(3, 3.5, 1.5)]
    res = aggregate_oracle(rows)
    cell = res["families"]["STRUCT:m1"]["all"]
    assert cell["n"] == 4 and cell["small_n"] is True
    assert cell["available_mfe_mean"] == pytest.approx((0.08 + 2.4 + 1.0 + 3.5) / 4)
    assert cell["available_mfe_median"] == pytest.approx((1.0 + 2.4) / 2)
    assert cell["p_available_ge"]["1"] == pytest.approx(0.75)
    assert cell["p_available_ge"]["0.25"] == pytest.approx(0.75)
    assert cell["null_p_available_ge"]["1"] == pytest.approx(0.25)
    assert cell["enrichment_vs_null"]["1"] == pytest.approx(0.5)
    ct = cell["contrast"]["P1_FIXED_1_5R"]
    assert ct["LITTLE_OPPORTUNITY"]["loss"] == 1 and ct["LARGE_OPPORTUNITY"]["loss"] == 1 and ct["LARGE_OPPORTUNITY"]["win"] == 1
    assert cell["policies"]["P1_FIXED_1_5R"]["n"] == 4
    assert "P3_STRUCT_TP1_TP2" not in cell["policies"] or cell["policies"]["P3_STRUCT_TP1_TP2"]["n"] == 0
    assert set(res["families"]["STRUCT:m1"]["by_direction"]) == {"long", "short"}


def test_labels_and_versions_in_every_output():
    meta = oracle_meta(["note"], ["caveat"])
    assert meta["oracle_version"] == ORACLE_VERSION and ORACLE_VERSION == "oracle-mfe-1"
    assert meta["label"] == "ORACLE_RETROSPECTIVE" and meta["live_use"] == "NEVER_IN_LIVE_DECISION_PATH"
    res = {"meta": meta, "markets": {"GER40": {"result": aggregate_oracle([_row(i, 0.5 + i, 1.0) for i in range(3)]), "markdown": "## GER40"}}}
    md = render_oracle_markdown(res)
    js = to_oracle_json(res)
    for text in (md, js):
        assert "ORACLE_RETROSPECTIVE" in text and "NEVER_IN_LIVE_DECISION_PATH" in text and ORACLE_VERSION in text
    assert "null" in md.lower()
    assert "ORACLE_RETROSPECTIVE" in (om.__doc__ or "") and "NEVER_IN_LIVE_DECISION_PATH" in (om.__doc__ or "")


def test_predeclared_constants():
    assert LEVELS_R == (0.25, 0.5, 1.0, 1.5, 2.0, 3.0)
    assert om.SMALL_AVAILABLE_R < om.LARGE_AVAILABLE_R
    assert isinstance(om.NULL_SEED, int)


# ---- isolation: the oracle is never imported by live code ------------------------------------------
def test_oracle_is_never_imported_by_live_code():
    root = Path(__file__).resolve().parents[3]
    src = root / "src"
    pat = re.compile(r"^\s*(?:from|import)\s+(?:coverage_analysis(?:\.oracle_mfe)?\b|.*\boracle_mfe\b)", re.M)
    live = [src / "demo", src / "exits", src / "nautilus_mt5"]
    for d in live:
        assert d.is_dir(), d
        for py in d.rglob("*.py"):
            assert not pat.search(py.read_text(encoding="utf-8")), f"{py} imports the oracle"
    for sub in ("demo/execution", "demo/opportunity"):
        assert (src / sub).is_dir()
    assert not pat.search((root / "scripts" / "demo_trader.py").read_text(encoding="utf-8"))
    for py in src.rglob("*.py"):
        if py.relative_to(src).parts[:1] == ("coverage_analysis",):
            continue
        assert "oracle_mfe" not in py.read_text(encoding="utf-8"), f"{py} references the oracle"
    own = (src / "coverage_analysis" / "oracle_mfe.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:from|import)\s+(execution|risk|portfolio|strategies|pipeline|persistence|exits|nautilus_mt5|demo\.execution|demo\.opportunity)\b", own, re.M)
