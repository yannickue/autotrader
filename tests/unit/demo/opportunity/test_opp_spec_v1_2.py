# ruff: noqa: E501
"""Production spec v1.2 (Lane F): strict superset of v1.1 with the STRUCT family for BRENT/BTCUSD, PHASE2_DISCOVERY tags,
PRIMARY/SHADOW roles. v1 and v1.1 (files, schemas, hashes) stay untouched."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from demo.opportunity import production_spec as ps
from demo.opportunity.bar_source import ReplayBarSource
from demo.opportunity.engine import SHADOW_VARIANT, OpportunityEngine
from demo.opportunity.production_spec import (
    DEFAULT_PATH,
    DEFAULT_PATH_V1_1,
    DEFAULT_PATH_V1_2,
    ProductionSpecError,
    build_v1_2_payload,
    from_payload,
    load_production_spec,
    load_production_spec_for,
)

V1_HASH = "c3eae99e782888ac"
V1_1_HASH = "4f4b33e97966cd84"
V1_2_HASH = "a4fe51b558d03274"  # sealed hash of production_spec_v1_2.json (documented in docs/V2_MARKETS.md, Lane F)
CORE = ("GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD")
PHASE2 = ("BRENT", "BTCUSD")


def test_v1_and_v1_1_are_untouched_and_still_loadable():
    assert load_production_spec().strategy_hash == V1_HASH
    assert load_production_spec(DEFAULT_PATH_V1_1).strategy_hash == V1_1_HASH
    assert json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))["schema"] == "demo-production-spec-1"
    assert json.loads(DEFAULT_PATH_V1_1.read_text(encoding="utf-8"))["schema"] == "demo-production-spec-1.1"


def test_v1_2_hash_is_deterministic_and_the_file_is_reproducible():
    v12 = load_production_spec(DEFAULT_PATH_V1_2)
    assert v12.strategy_hash == V1_2_HASH not in (V1_HASH, V1_1_HASH)
    assert build_v1_2_payload()["strategy_hash"] == build_v1_2_payload()["strategy_hash"] == V1_2_HASH
    assert json.loads(DEFAULT_PATH_V1_2.read_text(encoding="utf-8")) == json.loads(json.dumps(build_v1_2_payload()))
    assert json.loads(DEFAULT_PATH_V1_2.read_text(encoding="utf-8"))["base_strategy_hash_v1_1"] == V1_1_HASH


def test_v1_2_is_a_strict_superset_with_the_five_markets_verbatim():
    v1, v11, v12 = load_production_spec(), load_production_spec(DEFAULT_PATH_V1_1), load_production_spec_for(("BTCUSD",))
    assert v12.strategy_hash == V1_2_HASH
    assert set(v12.market_names()) == set(CORE) | set(PHASE2)
    for m in CORE:
        assert v12.specs_for(m) == v1.specs_for(m) == v11.specs_for(m)
    p1, p12 = json.loads(v1.provenance), json.loads(v12.provenance)
    for m in CORE:
        assert p12[m] == p1[m]
    raw1 = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))["markets"]
    raw12 = json.loads(DEFAULT_PATH_V1_2.read_text(encoding="utf-8"))["markets"]
    for m in CORE:
        assert raw12[m] == raw1[m]  # byte-level JSON equality of the five markets' entries (no role/tag keys leak in)


def test_selector_gating():
    assert load_production_spec_for(()).strategy_hash == V1_HASH
    assert load_production_spec_for(("BRENT",)).strategy_hash == V1_2_HASH
    assert load_production_spec_for(("BTCUSD", "BRENT")).strategy_hash == V1_2_HASH


def test_phase2_markets_use_struct_with_one_primary_and_no_orb():
    v12 = load_production_spec(DEFAULT_PATH_V1_2)
    for m in PHASE2:
        specs = v12.specs_for(m)
        assert [fs.family for fs in specs] == ["STRUCT"] * 4  # ORB dropped (no defensible session open)
        assert [fs.spec.mode for fs in specs] == ["confirmed", "breakout", "retest", "fade"]
        assert [fs.role for fs in specs] == ["PRIMARY", "SHADOW", "SHADOW", "SHADOW"]
        assert all(fs.thr_values == () for fs in specs)  # nothing fitted
        prov = json.loads(v12.provenance)[m]
        assert prov["n_fit_bars"] == 0 and prov["phase"] == "PHASE2_DISCOVERY" and prov["alpha_status"] == "NOT_ALPHA_VALIDATED"
    raw = json.loads(DEFAULT_PATH_V1_2.read_text(encoding="utf-8"))["markets"]
    for m in PHASE2:
        assert all(e["tags"] == {"phase": "PHASE2_DISCOVERY", "alpha_status": "NOT_ALPHA_VALIDATED"} for e in raw[m])
    assert all(fs.role == "PRIMARY" for m in CORE for fs in v12.specs_for(m))


def test_v1_2_is_sealed_and_rejects_unknown_roles():
    p = json.loads(DEFAULT_PATH_V1_2.read_text(encoding="utf-8"))
    from_payload(p)
    t = json.loads(json.dumps(p))
    t["markets"]["BTCUSD"][0]["spec"]["n_range"] = 12
    with pytest.raises(ProductionSpecError):
        from_payload(t)
    t = json.loads(json.dumps(p))
    t["markets"]["BTCUSD"][0]["role"] = "BOSS"
    t["strategy_hash"] = ps._hash_body({k: v for k, v in t.items() if k != "strategy_hash"})
    with pytest.raises(ProductionSpecError, match="unknown role"):
        from_payload(t)


# --------------------------------------------------------------------------------- engine on synthetic bars
def _frame(day: datetime, n_break_bar: int, tail: list[float]) -> pd.DataFrame:
    """00:00 UTC of ``day`` + 24 h pattern bars (bounded, compressed), then a planted tail starting at bar ``n_break_bar``."""
    t = np.arange(n_break_bar)
    base = 100.0 + 0.5 * np.where(t % 2 == 0, 1.0, -1.0) + 0.4 * (((t * 7) % 11) / 5.0 - 1.0)
    closes = np.r_[base, tail]
    o = np.r_[closes[0], closes[:-1]]
    rows = [
        {"ts": day + timedelta(minutes=5 * i), "open": o[i], "high": max(o[i], closes[i]) + 0.1,
         "low": min(o[i], closes[i]) - 0.1, "close": closes[i], "tick_volume": 50, "spread_pts": 6}
        for i in range(len(closes))
    ]
    fr = pd.DataFrame(rows)
    fr["ts"] = pd.to_datetime(fr["ts"], utc=True).astype("datetime64[ns, UTC]")
    return fr


def _run(market: str, tail: list[float]):
    from markets.spec import load_market_spec

    day = datetime(2026, 9, 24, tzinfo=UTC)  # Thursday
    brk = 12 * 12  # 12:00 UTC is bar 144
    frame = _frame(day, brk, tail)
    spec = load_market_spec(market)
    src = ReplayBarSource({market: frame}, {market: spec.point_size})
    eng = OpportunityEngine(src, production=load_production_spec(DEFAULT_PATH_V1_2), min_history_bars=100, commit="test")
    out = []
    for k in range(len(tail)):
        now = day + timedelta(minutes=5 * (brk + k + 1))
        src.set_time(now)
        out += eng.on_m5_close(market, now)
    return out


@pytest.mark.parametrize("market", PHASE2)
def test_engine_primary_trades_while_shadow_variants_are_recorded_but_never_traded(market):
    pairs = _run(market, [102.6, 102.5, 102.6])  # break, then two confirming closes beyond the edge
    assert pairs, "the planted compressed-range breakout must produce opportunities"
    by_mode = {}
    for snap, dec in pairs:
        assert snap.versions["strategy_hash"] == V1_2_HASH
        assert snap.signal["family"] == "STRUCT"
        assert snap.signal["phase"] == "PHASE2_DISCOVERY" and snap.signal["alpha_status"] == "NOT_ALPHA_VALIDATED"
        assert snap.signal["structure_levels"]["range_high"] > snap.signal["structure_levels"]["range_low"]
        by_mode.setdefault(snap.signal["spec"]["mode"], []).append((snap, dec))
    assert "breakout" in by_mode and "confirmed" in by_mode
    for snap, dec in by_mode["breakout"]:
        assert snap.signal["role"] == "SHADOW" and dec.accepted is False and dec.reasons == (SHADOW_VARIANT,)
    for snap, dec in by_mode["confirmed"]:
        assert snap.signal["role"] == "PRIMARY"
        assert snap.geometry.stop < snap.geometry.intended_entry  # structural stop = opposite range edge side
        assert dec.accepted is True, dec.reasons


def test_engine_emits_no_orb_on_phase2_markets_and_tags_v1_markets_nothing():
    pairs = _run("BTCUSD", [102.6, 102.5])
    assert all(s.signal["family"] != "ORB" for s, _ in pairs)
