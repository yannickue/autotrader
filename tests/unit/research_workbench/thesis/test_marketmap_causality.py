# ruff: noqa: E501
"""MarketMap causality / determinism / versioning / warm-up / geometry-prefix tests (offline; synthetic bars)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from market_observer.observer import ObserverConfig
from market_observer.schema import ObserverBars
from research_workbench.thesis import marketmap as MM
from research_workbench.thesis.contracts import Direction, MarketMap, MarketPhase
from tests.unit.research_workbench.thesis._obs_bars import BAR, NS, synth

N = 700
IDX = (603, 640, 699)  # >= levels warm-up (603 bars)


@pytest.fixture(scope="module")
def up_bars() -> ObserverBars:
    return synth("up", N)


@pytest.fixture(scope="module")
def seg_bars() -> ObserverBars:
    # daily segments (data break after every 40 bars): exercises segment-aware groups
    return synth("up", N, bars_per_day=40)


def _scramble_future(b: ObserverBars, i: int) -> ObserverBars:
    """Same bars through i, DIFFERENT (random) data after i: a causal map at i must not notice."""
    rng = np.random.default_rng(99)
    sl = slice(i + 1, None)
    n = len(b) - i - 1
    o, h, lo, c = b.o.copy(), b.h.copy(), b.l.copy(), b.c.copy()
    c[sl] = 100.0 + rng.normal(0, 5, n)
    o[sl] = c[sl] + 0.3
    h[sl] = c[sl] + 2.0
    lo[sl] = c[sl] - 2.0
    tv, atr = b.tick_volume.copy(), b.atr.copy()
    tv[sl] = 1.0
    atr[sl] = 9.0
    return ObserverBars(
        b.market,
        b.ts_ns,
        o,
        h,
        lo,
        c,
        tv,
        b.spread,
        atr,
        b.segment_id,
        b.local_minute,
        b.local_day,
        b.tick_size,
        b.session,
        b.bar_seconds,
    )


# ------------------------------------------------------------------------------------------------ prefix invariance (the key causality test)
@pytest.mark.parametrize("i", IDX)
def test_prefix_invariance_content_hash(up_bars: ObserverBars, i: int) -> None:
    full = MM.build_market_map(up_bars, i)
    pre = MM.build_market_map(up_bars.prefix(i + 1), i)
    assert full.content_hash() == pre.content_hash()
    assert full == pre


def test_prefix_invariance_with_segments(seg_bars: ObserverBars) -> None:
    for i in (610, 665):
        assert (
            MM.build_market_map(seg_bars, i).content_hash()
            == MM.build_market_map(seg_bars.prefix(i + 1), i).content_hash()
        )


def test_future_data_cannot_change_the_map(up_bars: ObserverBars) -> None:
    i = 650
    assert (
        MM.build_market_map(up_bars, i).content_hash()
        == MM.build_market_map(_scramble_future(up_bars, i), i).content_hash()
    )


def test_incremental_replay_equals_reference(up_bars: ObserverBars) -> None:
    rep = MM.MarketMapReplay()
    maps: dict[int, MarketMap] = {}
    for j in range(len(up_bars)):
        if j in (603, 650, 699):
            maps[j] = rep.step(up_bars, j)
        else:
            rep.advance(up_bars, j)
    for j, m in maps.items():
        assert m.content_hash() == MM.build_market_map(up_bars, j).content_hash()


def test_replay_requires_sequential_bars(up_bars: ObserverBars) -> None:
    rep = MM.MarketMapReplay()
    with pytest.raises(ValueError):
        rep.step(up_bars, 5)


# ------------------------------------------------------------------------------------------------ determinism / version sensitivity
def test_determinism(up_bars: ObserverBars) -> None:
    a, b = MM.build_market_map(up_bars, 650), MM.build_market_map(up_bars, 650)
    assert a == b and a.content_hash() == b.content_hash()


def test_hash_sensitivity_version_config_and_code_sha(
    up_bars: ObserverBars, monkeypatch: pytest.MonkeyPatch
) -> None:
    i = 650
    base = MM.build_market_map(up_bars, i)
    assert base.marketmap_version == MM.MARKETMAP_VERSION
    h0 = MM.marketmap_definition_hash()
    # version
    monkeypatch.setattr(MM, "MARKETMAP_VERSION", "marketmap-test-bumped")
    bumped = MM.build_market_map(up_bars, i)
    assert bumped.content_hash() != base.content_hash()
    assert MM.marketmap_definition_hash() != h0
    monkeypatch.undo()
    # marketmap-level config
    cfg = MM.MarketMapConfig(event_window_bars=13)
    assert MM.marketmap_definition_hash(cfg) != h0
    assert MM.build_market_map(up_bars, i, cfg).content_hash() != base.content_hash()
    # observer-group config (levels) flows into the definition hashes
    obs = ObserverConfig()
    obs2 = replace(obs, levels=replace(obs.levels, break_atr=0.30))
    cfg2 = MM.MarketMapConfig(observer=obs2)
    m2 = MM.build_market_map(up_bars, i, cfg2)
    assert m2.definition_hashes["levels"] != base.definition_hashes["levels"]
    assert m2.definition_hashes["marketmap"] != base.definition_hashes["marketmap"]
    assert m2.content_hash() != base.content_hash()
    # code sha
    assert MM.build_market_map(up_bars, i, code_sha="abc").content_hash() != base.content_hash()


def test_definition_hashes_and_provenance_cover_every_group(up_bars: ObserverBars) -> None:
    m = MM.build_market_map(up_bars, 650)
    assert set(m.definition_hashes) == {
        "levels",
        "swings",
        "acceptance",
        "balance",
        "participation",
        "marketmap",
    }
    assert m.definition_hashes["marketmap"] == MM.marketmap_definition_hash()
    assert m.observer_version == "market-structure-observer-v1"
    for field_name in (
        "session",
        "market_phase",
        "h1_context",
        "m15_structure",
        "m5_structure",
        "nearest_support",
        "nearest_resistance",
        "second_support",
        "second_resistance",
        "active_support_zone",
        "active_resistance_zone",
        "role_reversal_zones",
        "balance_state",
        "acceptance_state",
        "participation_state",
        "volatility_context",
    ):
        pv = m.provenance[field_name]
        assert pv.source and pv.version and pv.definition_hash not in ("", "-"), field_name


# ------------------------------------------------------------------------------------------------ warm-up
@pytest.mark.parametrize("i", [0, 1, 5, 20, 60, 120, 300, 478])
def test_warmup_before_swing_history_returns_none_fields_not_exceptions(
    up_bars: ObserverBars, i: int
) -> None:
    m = MM.build_market_map(up_bars, i)
    assert m.market_phase is MarketPhase.UNDEFINED
    assert (
        m.nearest_support is None
        and m.nearest_resistance is None
        and m.second_support is None
        and m.second_resistance is None
    )
    assert m.active_support_zone is None and m.active_resistance_zone is None
    assert m.role_reversal_zones == () and m.acceptance_state == {}
    assert m.m15_structure is None and m.m5_structure is None
    assert m.participation_state is None  # needs 21 previous trading days
    assert m.decision_ts_ns == int(up_bars.ts_ns[i]) + BAR * NS and m.bar_index == i


@pytest.mark.parametrize("i", [479, 500, 601])
def test_between_swing_and_level_warmup_levels_are_none_structure_is_not(
    up_bars: ObserverBars, i: int
) -> None:
    m = MM.build_market_map(up_bars, i)
    assert m.m15_structure is not None and m.m5_structure is not None
    assert m.nearest_support is None and m.nearest_resistance is None and m.acceptance_state == {}
    assert m.active_support_zone is None and m.role_reversal_zones == ()


def test_each_field_waits_only_for_its_own_group(up_bars: ObserverBars) -> None:
    early = MM.build_market_map(up_bars, 120)
    assert early.balance_state is not None  # 48 bars
    assert early.volatility_context is not None  # 96 bars
    assert early.h1_context is None  # 21 complete closed H1 buckets = 252 bars
    assert MM.build_market_map(up_bars, 300).h1_context == "UP"
    assert (
        MM.build_market_map(up_bars, 602).nearest_support is not None
    )  # level registry warm exactly at 603 bars


def test_h1_context_ignores_the_forming_bucket(up_bars: ObserverBars) -> None:
    i = next(
        k
        for k in range(400, 500)
        if (int(up_bars.ts_ns[k]) // (3600 * NS)) == (int(up_bars.ts_ns[k - 1]) // (3600 * NS))
        and (int(up_bars.ts_ns[k]) // (300 * NS)) % 12 == 5
    )
    bucket = int(up_bars.ts_ns[i]) // (3600 * NS)
    in_bucket = np.array(
        [int(t) // (3600 * NS) == bucket for t in up_bars.ts_ns[: i + 1]]
        + [False] * (len(up_bars) - i - 1)
    )
    c = up_bars.c.copy()
    c[in_bucket] += 7.0  # moves the forming H1 bucket only (ATR array unchanged)
    moved = ObserverBars(
        up_bars.market,
        up_bars.ts_ns,
        up_bars.o,
        up_bars.h,
        up_bars.l,
        c,
        up_bars.tick_volume,
        up_bars.spread,
        up_bars.atr,
        up_bars.segment_id,
        up_bars.local_minute,
        up_bars.local_day,
        up_bars.tick_size,
        up_bars.session,
        up_bars.bar_seconds,
    )
    assert MM.h1_context(moved, i) == MM.h1_context(up_bars, i)


def test_index_out_of_range_raises(up_bars: ObserverBars) -> None:
    with pytest.raises(IndexError):
        MM.build_market_map(up_bars, len(up_bars))
    with pytest.raises(IndexError):
        MM.build_market_map(up_bars, -1)


# ------------------------------------------------------------------------------------------------ decision-time convention
def test_decision_ts_is_bar_close_and_no_field_refers_to_the_future(up_bars: ObserverBars) -> None:
    i = 650
    m = MM.build_market_map(up_bars, i)
    assert (
        m.decision_ts_ns
        == up_bars.decision_ts_ns(i)
        == int(up_bars.ts_ns[i]) + up_bars.bar_seconds * NS
    )
    # every price-like fact comes from bars <= i: changing bars AFTER i never changes any field (stronger than prefix equality)
    assert MM.build_market_map(_scramble_future(up_bars, i), i) == m
    # and changing the CURRENT bar changes the decision-time view (the close is part of the decision)
    c = up_bars.c.copy()
    c[i] += 3.0
    moved = ObserverBars(
        up_bars.market,
        up_bars.ts_ns,
        up_bars.o,
        np.maximum(up_bars.h, c),
        up_bars.l,
        c,
        up_bars.tick_volume,
        up_bars.spread,
        up_bars.atr,
        up_bars.segment_id,
        up_bars.local_minute,
        up_bars.local_day,
        up_bars.tick_size,
        up_bars.session,
        up_bars.bar_seconds,
    )
    assert MM.build_market_map(moved, i) != m


def test_session_labels_follow_the_session_spec(up_bars: ObserverBars) -> None:
    seen = {MM.session_label(up_bars, i) for i in range(288)}
    assert seen == {"PRE_OPEN", "CASH", "POST_CLOSE"}
    i = next(k for k in range(288) if up_bars.local_minute[k] == 9 * 60)
    assert MM.session_label(up_bars, i) == "CASH"
    no_open = replace(up_bars, session=replace(up_bars.session, cash_open_min=None))
    assert MM.session_label(no_open, i) is None


# ------------------------------------------------------------------------------------------------ geometry (proposed entry only, prefix-only)
@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_geometry_prefix_invariance(up_bars: ObserverBars, direction: Direction) -> None:
    for i in (400, 650):
        entry = float(up_bars.c[i])
        g_full = MM.geometry_for_entry(up_bars, i, direction, entry)
        g_pre = MM.geometry_for_entry(up_bars.prefix(i + 1), i, direction, entry)
        g_scr = MM.geometry_for_entry(_scramble_future(up_bars, i), i, direction, entry)
        assert g_full is not None and g_full == g_pre == g_scr


def test_geometry_uses_only_bars_through_i_and_matches_the_decision_clock(
    up_bars: ObserverBars,
) -> None:
    i = 650
    g = MM.geometry_for_entry(up_bars, i, Direction.LONG, float(up_bars.c[i]))
    assert g is not None and g.direction == 1
    assert (
        g.as_of is not None and int(g.as_of.timestamp()) == up_bars.decision_ts_ns(i) // NS
    )  # newest used bar's close == decision ts
    frame = MM.prefix_frame(up_bars, i)
    assert (
        len(frame) <= MM.MARKETMAP_CONFIG.geometry_lookback_bars
        and frame["high"].iloc[-1] == up_bars.h[i]
    )
    assert g.stop is None or float(g.stop) < float(g.entry)


def test_geometry_short_is_directionally_consistent(up_bars: ObserverBars) -> None:
    i = 650
    g = MM.geometry_for_entry(up_bars, i, Direction.SHORT, float(up_bars.c[i]))
    assert g is not None and g.direction == -1
    if g.stop is not None:
        assert float(g.stop) > float(g.entry)
    if g.tp1 is not None:
        assert float(g.tp1.price) < float(g.entry)


def test_geometry_invalid_inputs_give_none(up_bars: ObserverBars) -> None:
    assert MM.geometry_for_entry(up_bars, 650, Direction.LONG, float("nan")) is None
    assert MM.geometry_for_entry(up_bars, len(up_bars), Direction.LONG, 100.0) is None
    assert MM.geometry_for_entry(up_bars, -1, Direction.LONG, 100.0) is None


def test_geometry_is_not_part_of_the_marketmap_contract() -> None:
    assert not any("geometry" in f for f in MarketMap.__dataclass_fields__)
