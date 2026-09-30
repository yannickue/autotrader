import json

import pytest

from demo.contracts import (
    ClockCheck,
    Decision,
    MarketState,
    OpportunitySnapshot,
    TradeGeometry,
    opportunity_id_for,
)


def _snap(phase="DISCOVERY", direction=1):
    clock = ClockCheck(utc="2026-10-01T08:00:00+00:00", market_tz="Europe/Berlin",
                       local_iso="2026-10-01T10:00:00+02:00", utc_offset_min=120, local_minute=600,
                       session_bucket="open", in_entry_window=True, minutes_to_forced_flat=400,
                       calendar_status="provisional")
    geo = TradeGeometry(intended_entry=100.0, entry_zone_lo=99.9, entry_zone_hi=100.1,
                        invalidation=98.0, stop=98.5, target=103.0, risk_distance=1.5,
                        min_space_r=1.0, space_to_opposition_r=3.0, expected_horizon_s=3600,
                        exit_kind="fixed_r", exit_r=2.0)
    ms = MarketState(bid=99.95, ask=100.05, spread=0.1, atr=2.0, realized_vol=None,
                     tick_activity=None, clock=clock)
    oid = opportunity_id_for("GER40", "orb", "h1", clock.utc, direction)
    return OpportunitySnapshot(opportunity_id=oid, phase=phase, market="GER40",
                               broker_symbol="Ger40", direction=direction,
                               signal_ts_utc=clock.utc, created_utc=clock.utc, versions={},
                               context={}, structure={}, geometry=geo, market_state=ms, signal={})


def test_id_deterministic_and_direction_sensitive():
    assert _snap().opportunity_id == _snap().opportunity_id
    assert _snap().opportunity_id != _snap(direction=-1).opportunity_id


def test_snapshot_json_roundtrip_and_no_outcome_fields():
    d = json.loads(_snap().to_json())
    assert d["phase"] == "DISCOVERY"
    assert not {"net_r", "outcome", "pnl_eur", "mfe_r"} & set(d)


def test_bad_phase_and_direction_rejected():
    with pytest.raises(ValueError):
        _snap(phase="LIVE")
    with pytest.raises(ValueError):
        _snap(direction=0)


def test_decision_carries_reasons():
    dec = Decision(opportunity_id="x", phase="FROZEN", decided_utc="t", accepted=False,
                   reasons=("SPREAD_TOO_WIDE",), policy_id="p")
    assert dec.to_dict()["reasons"] == ["SPREAD_TOO_WIDE"]
