# ruff: noqa: E501
"""OpportunitySnapshot content: causal multi-timeframe context, structure, versions, ids, JSON safety."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from opp_helpers import bar_times, now_of

from demo.contracts import OpportunitySnapshot, opportunity_id_for
from demo.opportunity.bar_source import ReplayBarSource
from demo.opportunity.engine import OpportunityEngine
from demo.opportunity.snapshot import NOT_COMPUTED, git_commit


def _first_pairs(frames, mspecs, prod, market, start, end, n=4):
    base = ReplayBarSource(frames, {m: s.point_size for m, s in mspecs.items()})
    eng = OpportunityEngine(base, production=prod, market_specs=mspecs, commit="abc123")
    out = []
    for t in bar_times(frames[market], start, end):
        now = now_of(t)
        base.set_time(now)
        out += [(now, s, d) for s, d in eng.on_m5_close(market, now)]
        if len(out) >= n:
            break
    assert out
    return out


@pytest.fixture(scope="module")
def sample(frames, mspecs, prod):
    return _first_pairs(frames, mspecs, prod, "GER40", "2026-06-10T06:55", "2026-06-10T12:00")


def _reject_nan(s: str):
    def boom(c):
        raise ValueError(c)

    return json.loads(s, parse_constant=boom)


def test_snapshot_is_complete_json_safe_and_immutable(sample):
    now, snap, dec = sample[0]
    d = _reject_nan(snap.to_json())
    assert d["phase"] == "DISCOVERY" and d["market"] == "GER40" and d["broker_symbol"] == "Ger40"
    assert set(d) == {f.name for f in fields(OpportunitySnapshot)}
    assert not any(k in d for k in ("outcome", "pnl", "net_r", "exit_reason"))  # PRE-DECISION only
    assert snap.created_utc == now.isoformat()
    assert snap.opportunity_id == opportunity_id_for(
        "GER40", snap.signal["strategy_id"], snap.signal["spec_hash"], snap.signal_ts_utc, snap.direction
    )
    assert dec.opportunity_id == snap.opportunity_id
    with pytest.raises(FrozenInstanceError):
        snap.market = "X"  # type: ignore[misc]
    _reject_nan(dec.to_json())


def test_versions_and_signal_meta(sample, prod):
    _, snap, _ = sample[0]
    v = snap.versions
    assert set(v) == {"git_commit", "config_hash", "market_spec", "feature", "strategy_hash", "model",
                      "policy", "engine"}
    assert v["git_commit"] == "abc123" and v["strategy_hash"] == prod.strategy_hash
    assert v["policy"] == "static-demo-policy-v1" and v["model"] == "none"
    sig = snap.signal
    assert sig["family"] in ("ORB", "GAP", "OVERNIGHT", "VOLREV", "ROUND", "LEADLAG", "EOD")
    assert sig["confluence"] >= 1 and sig["independent_clusters"] >= 1
    assert sig["quality"] is None  # never faked
    assert snap.signal["forced_flat_utc"].endswith("+00:00")


def test_git_commit_falls_back_to_unknown(tmp_path: Path):
    assert git_commit(tmp_path / "does-not-exist") == "unknown"
    assert len(git_commit()) >= 7  # this repository


def test_context_is_causal_and_complete(sample, mspecs):
    for _now, snap, _ in sample:
        ctx = snap.context
        assert ctx["M1"] is None
        sig = datetime.fromisoformat(snap.signal_ts_utc)
        for tf, minutes in (("M15", 15), ("H1", 60), ("H4", 240)):
            lc = ctx[tf]["last_closed"]
            assert lc is not None, tf
            start = datetime.fromisoformat(lc["start_utc"])
            assert start + pd.Timedelta(minutes=minutes) <= sig, f"{tf} bucket closes after the decision"
            fm = ctx[tf]["forming"]
            if fm is not None:
                assert datetime.fromisoformat(fm["start_utc"]) + pd.Timedelta(minutes=minutes) > sig
        d1 = ctx["D1"]["last_closed"]
        tz = ZoneInfo(mspecs["GER40"].calendar.tz)
        assert datetime.fromisoformat(d1["start_utc"]).astimezone(tz).date() < sig.astimezone(tz).date()
        m5 = ctx["M5"]["last_closed"]
        assert datetime.fromisoformat(m5["start_utc"]) + pd.Timedelta(minutes=5) == sig
        assert ctx["M5"]["atr14"] and ctx["H1"]["atr14"] and ctx["D1"]["n_closed"] >= 10
        assert {"M5", "M15", "H1", "H4", "D1", "M1", "alignment"} <= set(ctx)


def test_structure_has_real_levels_and_explicit_nulls(sample):
    _, snap, _ = sample[-1]
    st = snap.structure
    assert st["direction"] == snap.direction
    for k in ("prev_day_high", "prev_day_low", "prev_day_close", "session_open", "overnight_high"):
        assert st[k]["price"] is not None
    assert isinstance(st["events"]["close_above_pdh"], bool)
    assert set(st["not_computed"]) == set(NOT_COMPUTED)
    assert all(v is None for v in st["not_computed"].values())
    assert st["round_numbers"]["major"]["up"] > st["round_numbers"]["major"]["down"]


def test_market_state_and_geometry_consistency(sample, mspecs):
    for _, snap, dec in sample:
        ms, g = snap.market_state, snap.geometry
        assert ms.ask >= ms.bid and ms.spread == pytest.approx(ms.ask - ms.bid)
        assert ms.atr > 0 and ms.clock.calendar_status == mspecs["GER40"].calendar.status
        assert ms.clock.market_tz == "Europe/Berlin"
        exec_px = ms.ask if snap.direction > 0 else ms.bid
        assert g.intended_entry == exec_px
        assert g.risk_distance == pytest.approx(abs(exec_px - g.stop))
        assert g.entry_zone_lo < g.entry_zone_hi and g.expected_horizon_s > 0
        if dec.accepted:
            assert snap.direction * (g.target - exec_px) > 0
            assert snap.direction * (exec_px - g.stop) > 0
