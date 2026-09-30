# ruff: noqa: E501
"""Production spec v1.1 (Lane M2): a strict superset of the frozen v1 that adds the Phase-2 markets' fit-free families.

v1 (file, schema, hash, per-market specs) must stay bit-identical and stay the default."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from demo.opportunity import production_spec as ps
from demo.opportunity.bar_source import ReplayBarSource
from demo.opportunity.engine import OpportunityEngine
from demo.opportunity.production_spec import (
    DEFAULT_PATH,
    DEFAULT_PATH_V1_1,
    ProductionSpecError,
    build_v1_1_payload,
    from_payload,
    load_production_spec,
    load_production_spec_for,
)

V1_HASH = "c3eae99e782888ac"  # sealed hash of production_spec_v1.json (documented in docs/V2_MARKETS.md, Lane M2)
V1_1_HASH = "4f4b33e97966cd84"
CORE = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD")
PHASE2 = ("BRENT", "BTCUSD")


def test_v1_is_untouched_and_remains_the_default():
    v1 = load_production_spec()
    assert v1.strategy_hash == V1_HASH
    assert set(v1.market_names()) == set(CORE)
    assert load_production_spec_for(()).strategy_hash == V1_HASH
    assert json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))["schema"] == "demo-production-spec-1"


def test_v1_1_is_a_strict_superset_with_a_new_documented_hash():
    v1, v11 = load_production_spec(), load_production_spec(DEFAULT_PATH_V1_1)
    assert v11.strategy_hash == V1_1_HASH != v1.strategy_hash
    assert set(v11.market_names()) == set(CORE) | set(PHASE2)
    for m in CORE:  # verbatim: same specs, same thresholds, same order
        assert v11.specs_for(m) == v1.specs_for(m)
    # Lane F: the selector now picks the v1.2 superset when a Phase-2 market is enabled; v1.1 stays loadable by path
    assert load_production_spec_for(("BTCUSD",)).strategy_hash != V1_1_HASH
    assert json.loads(DEFAULT_PATH_V1_1.read_text(encoding="utf-8"))["base_strategy_hash_v1"] == V1_HASH


def test_v1_1_file_is_reproducible_from_v1():
    assert json.loads(DEFAULT_PATH_V1_1.read_text(encoding="utf-8")) == json.loads(json.dumps(build_v1_1_payload()))


def test_phase2_markets_get_only_the_fit_free_orb_family_and_no_invented_parameters():
    v11 = load_production_spec(DEFAULT_PATH_V1_1)
    for m in PHASE2:
        specs = v11.specs_for(m)
        assert [fs.family for fs in specs] == ["ORB", "ORB"]
        assert {fs.spec.mode for fs in specs} == {"breakout", "fade"}
        assert all(fs.thr_values == () for fs in specs)  # nothing fitted: no history exists for these markets
        prov = json.loads(v11.provenance)[m]
        assert prov["n_fit_bars"] == 0 and prov["fit_free_families_only"] is True
    # the SAME class-default ORB specs the five core markets use (nothing tuned on the new markets)
    core_orb = [fs.spec for fs in v11.specs_for("GER40") if fs.family == "ORB"]
    for m in PHASE2:
        assert [fs.spec for fs in v11.specs_for(m)] == core_orb


def test_v1_1_is_sealed_and_v1_schema_rejects_phase2_markets():
    p = json.loads(DEFAULT_PATH_V1_1.read_text(encoding="utf-8"))
    from_payload(p)
    tampered = json.loads(json.dumps(p))
    tampered["markets"]["BRENT"][0]["spec"]["window_min"] = 120
    with pytest.raises(ProductionSpecError):
        from_payload(tampered)
    as_v1 = json.loads(json.dumps(p))
    as_v1["schema"] = "demo-production-spec-1"
    as_v1["strategy_hash"] = ps._hash_body({k: v for k, v in as_v1.items() if k != "strategy_hash"})
    with pytest.raises(ProductionSpecError, match="unknown market"):
        from_payload(as_v1)


# --------------------------------------------------------------------------------- engine on synthetic bars
def _frame(day_last: datetime, break_at: str, *, days: int = 11) -> pd.DataFrame:
    rows = []
    first = day_last.replace(hour=0, minute=0) - timedelta(days=days)
    t = first
    while t < day_last + timedelta(days=1):
        if t.weekday() < 5:
            o = c = 98.0
            h, low = 98.1, 97.9
            if t.date() == day_last.date() and t.strftime("%H:%M") == break_at:
                h, c = 99.1, 99.0
            rows.append({"ts": t, "open": o, "high": h, "low": low, "close": c, "tick_volume": 50, "spread_pts": 6})
        t += timedelta(minutes=5)
    fr = pd.DataFrame(rows)
    fr["ts"] = pd.to_datetime(fr["ts"], utc=True).astype("datetime64[ns, UTC]")
    return fr


def _engine(market: str, frame: pd.DataFrame) -> tuple[OpportunityEngine, ReplayBarSource]:
    from markets.spec import load_market_spec

    spec = load_market_spec(market)  # default resolution reaches the Phase-2 config root
    src = ReplayBarSource({market: frame}, {market: spec.point_size})
    eng = OpportunityEngine(
        src, production=load_production_spec(DEFAULT_PATH_V1_1), min_history_bars=100, commit="test"
    )
    return eng, src


@pytest.mark.parametrize(
    "market,break_at,now",
    [
        ("BRENT", "07:15", datetime(2026, 9, 25, 7, 20, tzinfo=UTC)),  # London 08:00 (BST) cash open + 15 min range
        ("BTCUSD", "08:15", datetime(2026, 9, 25, 8, 20, tzinfo=UTC)),  # provisional UTC reference open 08:00
    ],
)
def test_orb_breakout_is_found_on_the_new_markets_through_the_generic_engine(market, break_at, now):
    day = datetime(2026, 9, 25, tzinfo=UTC)
    eng, src = _engine(market, _frame(day, break_at))
    src.set_time(now)
    pairs = eng.on_m5_close(market, now)
    assert pairs, "an opening-range breakout on the new market must produce an opportunity"
    snap, dec = pairs[0]
    assert snap.market == market and snap.signal["family"] == "ORB" and snap.direction == 1
    assert snap.versions["strategy_hash"] == V1_1_HASH
    assert snap.geometry.stop < snap.geometry.intended_entry  # structural stop = the other side of the range
    assert snap.market_state.clock.calendar_status == "provisional"
    assert dec.opportunity_id == snap.opportunity_id


def test_breakout_outside_the_orb_decision_window_yields_nothing():
    day = datetime(2026, 9, 25, tzinfo=UTC)
    eng, src = _engine("BTCUSD", _frame(day, "13:05"))  # 5 h after the reference open: beyond window_min=240
    now = datetime(2026, 9, 25, 13, 10, tzinfo=UTC)
    src.set_time(now)
    assert eng.on_m5_close("BTCUSD", now) == []
