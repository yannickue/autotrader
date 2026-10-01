# ruff: noqa: E501
"""Level group: config/hash, counting (touch/rejection/penetration/break/reclaim, min-gap), ROLE state machine, clusters, features."""

from __future__ import annotations

import dataclasses
import itertools
import random

import pytest
from test_levels_support import DAY0, closes_to_rows, flat, make_bars

from market_observer import levels as L
from market_observer import schema as S

ROUND_ONLY = L.LevelConfig(swing_timeframes=(), struct_range_n=None, round_major_step=10.0)


def run(bars, cfg=ROUND_ONLY, upto=None):
    """All contexts i = 0..upto via the incremental registry."""
    reg = L.LevelRegistry(cfg)
    return [reg.update(bars, i) for i in range(len(bars) if upto is None else upto + 1)]


def with_warmup_bar(rows, **kw):
    """``rows`` preceded by ONE dummy bar of the previous day/segment: the registry's first (warm-up) scope. ROUND levels are created at the
    first bar of a NEW day/segment scope (an observed event), so row k of ``rows`` is bar k + 1 of the returned bars."""
    n = len(rows)
    return make_bars([rows[0], *rows], minutes=[0, *[5 * k for k in range(n)]], days=[DAY0 - 1] + [DAY0] * n, segments=[0] + [1] * n, **kw)


def run_rows(rows, cfg=ROUND_ONLY, upto=None, **kw):
    """(bars, contexts) where contexts[k] belongs to rows[k] (the warm-up bar is dropped)."""
    bars = with_warmup_bar(rows, **kw)
    return bars, run(bars, cfg, None if upto is None else upto + 1)[1:]


def dts(bars, k):
    return bars.decision_ts_ns(k + 1)


def level_at(ctx, price, source="ROUND_MAJOR"):
    found = [lv for lv in ctx.levels if lv.source == source and abs(lv.price - price) < 1e-9]
    assert len(found) == 1, f"expected exactly one {source}@{price}, got {len(found)}"
    return found[0]


# ---------------------------------------------------------------------------------------------- (l) config / definition hash
EXPECTED_STATE_FIELDS = [
    "level_id", "market", "source", "price", "zone_low", "zone_high", "created_at_ts_ns", "confirmed_at_ts_ns", "first_tradeable_at_ts_ns",
    "age_bars", "age_minutes", "touch_count", "clean_rejection_count", "penetration_count", "break_count", "reclaim_count", "last_touch_ts_ns",
    "last_break_ts_ns", "last_reclaim_ts_ns", "current_role", "previous_role", "distance_atr", "zone_width_atr", "sources_in_cluster",
]

# pinned: a silent change of config defaults, definition text or the role transition table flips this and must be a conscious version bump
PINNED_DEFINITION_HASH = "5d2edd89376e44eb"


def test_level_state_has_exactly_the_specified_fields():
    assert [f.name for f in dataclasses.fields(L.LevelState)] == EXPECTED_STATE_FIELDS


def test_config_is_frozen_with_documented_defaults():
    c = L.LevelConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.swing_n = 3  # type: ignore[misc]
    assert (c.k_tick_zone, c.k_spread_zone, c.k_atr_zone) == (2.0, 1.0, 0.10)
    assert (c.k_tick_cluster, c.k_spread_cluster, c.k_atr_cluster) == (2.0, 1.0, 0.15)
    assert (c.reject_atr, c.break_atr, c.accept_bars, c.min_touch_gap_bars) == (0.25, 0.25, 3, 3)
    assert (c.orb_minutes, c.struct_range_n, c.swing_n, c.swing_timeframes) == (15, 24, 2, ("M5", "M15"))
    assert c.round_major_step is None and c.round_minor_step is None  # never guessed: the adapter supplies the grid
    with pytest.raises(ValueError):
        L.LevelConfig(accept_bars=0)
    with pytest.raises(ValueError):
        L.LevelConfig(swing_timeframes=("H1",))


def test_definition_hash_is_pinned_and_sensitive_to_config():
    assert L.definition_hash(L.LevelConfig()) == L.LEVELS_DEFINITION_HASH
    assert L.LEVELS_DEFINITION_HASH == PINNED_DEFINITION_HASH
    assert L.definition_hash(dataclasses.replace(L.LevelConfig(), k_atr_zone=0.11)) != L.LEVELS_DEFINITION_HASH
    assert L.definition_hash(dataclasses.replace(L.LevelConfig(), accept_bars=4)) != L.LEVELS_DEFINITION_HASH
    assert L.definition_hash(L.LevelConfig(round_major_step=5.0)) != L.LEVELS_DEFINITION_HASH


# ---------------------------------------------------------------------------------------------- (e) counting on hand-built bars
LIFECYCLE = [
    (103, 103.2, 102.8, 103),    # 0 creates ROUND 100 (zone 99.9..100.1, price above => SUPPORT) and 110
    (103, 103.1, 102.0, 102.2),  # 1
    (102.2, 102.3, 100.05, 100.6),  # 2 touch (from outside), close 100.6 is >= 0.25 ATR above the zone => clean rejection
    (100.6, 101, 100.5, 100.8),  # 3
    (100.8, 101, 99.8, 100.5),   # 4 range enters the zone again after only 2 bars => NOT counted (min gap 3); the wick through 99.9 is not counted either
    (100.5, 100.6, 100.4, 100.5),  # 5
    (100.5, 100.6, 100.0, 100.5),  # 6 touch, gap 4 => counted, clean rejection
    (100.5, 100.55, 100.45, 100.5),  # 7
    (100.5, 100.55, 100.45, 100.5),  # 8
    (100.5, 100.5, 99.8, 99.95),  # 9 touch (gap 3) + penetration (low 99.8 < zone low 99.9); close inside the zone => episode stays open
    (99.95, 100.0, 99.4, 99.5),  # 10 close 99.5 <= 99.9 - 0.25 => BREAK (episode ends without rejection)
    (99.5, 99.55, 99.35, 99.4),  # 11 consecutive close below the zone (2)
    (99.4, 99.45, 99.25, 99.3),  # 12 third consecutive close below => ACCEPTED_BELOW
    (99.3, 99.35, 99.2, 99.3),   # 13
    (99.3, 99.95, 99.25, 99.2),  # 14 pullback touches from below and is rejected cleanly => role reversal
]


def test_touch_rejection_penetration_break_counting_and_min_gap():
    bars, ctxs = run_rows(LIFECYCLE)

    def st(i):
        return level_at(ctxs[i], 100.0)

    assert st(0).current_role == S.LevelRole.SUPPORT and st(0).touch_count == 0 and st(0).age_bars == 0
    assert (st(1).touch_count, st(1).clean_rejection_count) == (0, 0)
    assert (st(2).touch_count, st(2).clean_rejection_count, st(2).penetration_count) == (1, 1, 0)
    # bar 4 re-enters the zone within the min gap: not a counted touch, no rejection, no penetration
    assert (st(4).touch_count, st(4).clean_rejection_count, st(4).penetration_count) == (1, 1, 0)
    assert (st(6).touch_count, st(6).clean_rejection_count, st(6).penetration_count) == (2, 2, 0)
    assert (st(9).touch_count, st(9).clean_rejection_count, st(9).penetration_count, st(9).break_count) == (3, 2, 1, 0)
    assert st(9).current_role == S.LevelRole.SUPPORT  # a wick/close inside the zone is not a break
    assert (st(10).break_count, st(10).clean_rejection_count) == (1, 2)
    assert st(10).current_role == S.LevelRole.BROKEN_DOWN and st(10).previous_role == S.LevelRole.SUPPORT
    assert st(11).current_role == S.LevelRole.BROKEN_DOWN
    assert st(12).current_role == S.LevelRole.ACCEPTED_BELOW and st(12).previous_role == S.LevelRole.BROKEN_DOWN
    assert (st(14).touch_count, st(14).clean_rejection_count) == (4, 3)
    assert st(14).current_role == S.LevelRole.FLIPPED_TO_RESISTANCE and st(14).previous_role == S.LevelRole.ACCEPTED_BELOW
    # timestamps: events are stamped with the CLOSE of the bar that produced them
    assert st(14).last_touch_ts_ns == dts(bars, 14)
    assert st(14).last_break_ts_ns == dts(bars, 10)
    assert st(14).last_reclaim_ts_ns is None
    assert st(0).last_touch_ts_ns is None and st(0).last_break_ts_ns is None  # unknown stays None


def test_zone_geometry_and_signed_atr_distance():
    _, ctxs = run_rows(LIFECYCLE, atr=2.0, spread=0.5)
    s = level_at(ctxs[2], 100.0)
    hw = max(2.0 * 0.01, 1.0 * 0.5, 0.10 * 2.0)
    assert s.zone_low == pytest.approx(100.0 - hw) and s.zone_high == pytest.approx(100.0 + hw)
    assert s.zone_width_atr == pytest.approx(2 * hw / 2.0)
    assert s.distance_atr == pytest.approx((100.0 - LIFECYCLE[2][3]) / 2.0)  # signed: level below the close => negative
    assert level_at(ctxs[2], 110.0).distance_atr > 0


def test_reclaim_and_second_break():
    rows = [
        (97, 97.2, 96.8, 97),        # 0 level 100 created below price => RESISTANCE
        (97, 100.5, 96.9, 100.5),    # 1 close 0.4 above the zone => BREAK_UP (touch counted: the range enters the zone from outside)
        (100.5, 100.8, 100.4, 100.6),  # 2
        (100.6, 100.7, 99.4, 99.5),  # 3 close back through the zone => reclaim
        (99.5, 99.6, 98.9, 99.0),    # 4
        (99.0, 100.6, 98.9, 100.5),  # 5 breaks up again
    ]
    _, ctxs = run_rows(rows)
    s = [level_at(c, 100.0) for c in ctxs]
    assert s[0].current_role == S.LevelRole.RESISTANCE
    assert s[1].current_role == S.LevelRole.BROKEN_UP and s[1].break_count == 1 and s[1].touch_count == 1
    assert s[2].current_role == S.LevelRole.BROKEN_UP
    assert s[3].current_role == S.LevelRole.RECLAIMED_FROM_ABOVE and s[3].reclaim_count == 1 and s[3].previous_role == S.LevelRole.BROKEN_UP
    assert s[4].current_role == S.LevelRole.RECLAIMED_FROM_ABOVE
    assert s[5].current_role == S.LevelRole.BROKEN_UP and s[5].break_count == 2 and s[5].reclaim_count == 1


def test_accepted_above_then_held_pullback_flips_to_support():
    rows = [
        (97, 97.2, 96.8, 97),
        (97, 100.5, 96.9, 100.5),    # break up
        (100.5, 100.8, 100.4, 100.6),
        (100.6, 100.9, 100.5, 100.7),  # third consecutive close above => accepted
        (100.7, 100.8, 100.5, 100.7),
        (100.7, 100.8, 100.05, 100.6),  # pullback into the zone, held (close >= 0.25 ATR above)
    ]
    _, ctxs = run_rows(rows)
    s = [level_at(c, 100.0) for c in ctxs]
    assert s[3].current_role == S.LevelRole.ACCEPTED_ABOVE
    assert s[5].current_role == S.LevelRole.FLIPPED_TO_SUPPORT and s[5].previous_role == S.LevelRole.ACCEPTED_ABOVE
    assert s[5].touch_count == 2 and s[5].clean_rejection_count == 1


def test_pullback_that_closes_through_the_zone_is_a_reclaim_not_a_flip():
    rows = [
        (97, 97.2, 96.8, 97),
        (97, 100.5, 96.9, 100.5),
        (100.5, 100.8, 100.4, 100.6),
        (100.6, 100.9, 100.5, 100.7),
        (100.7, 100.8, 100.5, 100.7),
        (100.7, 100.8, 99.2, 99.5),  # closes below the zone
    ]
    s = [level_at(c, 100.0) for c in run_rows(rows)[1]]
    assert s[5].current_role == S.LevelRole.RECLAIMED_FROM_ABOVE and s[5].reclaim_count == 1


def test_level_created_inside_the_zone_is_unclassified_until_the_first_close_outside():
    rows = [(100.0, 100.05, 99.95, 100.0), (100.0, 100.05, 99.95, 100.02), (100.0, 100.9, 100.0, 100.8)]
    s = [level_at(c, 100.0) for c in run_rows(rows)[1]]
    assert s[0].current_role == S.LevelRole.UNCLASSIFIED and s[1].current_role == S.LevelRole.UNCLASSIFIED
    assert s[2].current_role == S.LevelRole.SUPPORT and s[2].previous_role == S.LevelRole.UNCLASSIFIED
    assert s[2].touch_count == 0  # never entered the zone from outside


# ---------------------------------------------------------------------------------------------- (f) role transition table
R, E = S.LevelRole, L.RoleEvent
EXPECTED_TRANSITIONS = {
    (R.UNCLASSIFIED, E.OUT_ABOVE): R.SUPPORT,
    (R.UNCLASSIFIED, E.OUT_BELOW): R.RESISTANCE,
    (R.SUPPORT, E.BREAK_DOWN): R.BROKEN_DOWN,
    (R.FLIPPED_TO_SUPPORT, E.BREAK_DOWN): R.BROKEN_DOWN,
    (R.RECLAIMED_FROM_BELOW, E.BREAK_DOWN): R.BROKEN_DOWN,
    (R.RESISTANCE, E.BREAK_UP): R.BROKEN_UP,
    (R.FLIPPED_TO_RESISTANCE, E.BREAK_UP): R.BROKEN_UP,
    (R.RECLAIMED_FROM_ABOVE, E.BREAK_UP): R.BROKEN_UP,
    (R.BROKEN_UP, E.ACCEPT_UP): R.ACCEPTED_ABOVE,
    (R.BROKEN_UP, E.THROUGH_DOWN): R.RECLAIMED_FROM_ABOVE,
    (R.BROKEN_DOWN, E.ACCEPT_DOWN): R.ACCEPTED_BELOW,
    (R.BROKEN_DOWN, E.THROUGH_UP): R.RECLAIMED_FROM_BELOW,
    (R.ACCEPTED_ABOVE, E.THROUGH_DOWN): R.RECLAIMED_FROM_ABOVE,
    (R.ACCEPTED_ABOVE, E.HELD_ABOVE): R.FLIPPED_TO_SUPPORT,
    (R.ACCEPTED_BELOW, E.THROUGH_UP): R.RECLAIMED_FROM_BELOW,
    (R.ACCEPTED_BELOW, E.HELD_BELOW): R.FLIPPED_TO_RESISTANCE,
}


def test_role_transition_table_is_exactly_the_documented_one():
    assert dict(L.ROLE_TRANSITIONS) == EXPECTED_TRANSITIONS


@pytest.mark.parametrize(("role", "event"), list(itertools.product(list(S.LevelRole), list(L.RoleEvent))))
def test_every_legal_transition_is_taken_and_every_illegal_one_raises(role, event):
    if (role, event) in EXPECTED_TRANSITIONS:
        assert L.next_role(role, event) == EXPECTED_TRANSITIONS[(role, event)]
    else:
        with pytest.raises(L.IllegalRoleTransition):
            L.next_role(role, event)


# ---------------------------------------------------------------------------------------------- (g) clusters
def mk_state(price, source, hw=0.1, created=1, conf=2, lid=None):
    return L.LevelState(
        lid or f"{source}:{price}", "T", S.LevelSource(source), price, price - hw, price + hw, created, conf, conf, 3, 15.0, 0, 0, 0, 0, 0,
        None, None, None, S.LevelRole.SUPPORT, None, 0.5, 0.2, None,
    )


def test_cluster_is_deterministic_under_shuffled_insertion_and_counts_independent_families():
    states = [
        mk_state(100.00, "SWING_M5"), mk_state(100.05, "SWING_M15"), mk_state(100.12, "ROUND_MAJOR"),  # one cluster: 3 levels, 2 families
        mk_state(101.0, "PREV_DAY_HIGH"),                                                               # alone
        mk_state(98.0, "SWING_M5", lid="a"), mk_state(98.0, "SWING_M5", lid="b"),                       # same family twice: count 2, independent 1
    ]
    ref = L.cluster_levels(states, tol=0.05)
    rng = random.Random(5)
    for _ in range(25):
        sh = states[:]
        rng.shuffle(sh)
        assert L.cluster_levels(sh, tol=0.05) == ref
    by_count = {z.source_count: z for z in ref}
    big = by_count[3]
    assert big.independent_source_count == 2 and big.zone_low == pytest.approx(99.9) and big.zone_high == pytest.approx(100.22)
    assert big.sources == tuple(sorted(["SWING_M5", "SWING_M15", "ROUND_MAJOR"]))
    twin = by_count[2]
    assert twin.independent_source_count == 1 and twin.zone_center == pytest.approx(98.0)
    assert len(ref) == 3
    assert len({z.zone_id for z in ref}) == 3
    assert [z.zone_center for z in ref] == sorted(z.zone_center for z in ref)  # deterministic ascending order
    # a larger tolerance chains the next level in (single linkage), a tiny one splits the cluster
    assert len(L.cluster_levels(states, tol=0.0)) >= 3
    assert any(z.source_count == 4 for z in L.cluster_levels(states, tol=0.7))


def test_independent_families_documented():
    fam = L.SOURCE_FAMILY
    assert fam[S.LevelSource.SWING_M5] == fam[S.LevelSource.SWING_M15]
    assert fam[S.LevelSource.ROUND_MAJOR] == fam[S.LevelSource.ROUND_MINOR]
    assert fam[S.LevelSource.PREV_DAY_HIGH] == fam[S.LevelSource.PREV_DAY_CLOSE]
    assert len({fam[S.LevelSource.SWING_M5], fam[S.LevelSource.ROUND_MAJOR], fam[S.LevelSource.PREV_DAY_HIGH]}) == 3
    assert set(S.LevelSource) - {S.LevelSource.VWAP_PROXY} <= set(fam)  # VWAP_PROXY is not in V1


# ---------------------------------------------------------------------------------------------- features / reference level
def test_level_features_contract_and_values():
    _, all_ctx = run_rows(LIFECYCLE)
    ctx = all_ctx[14]
    ev = LIFECYCLE[14][3]
    res = L.level_features(ctx, 1, ev)
    assert res.group == "levels" and res.version == S.GROUP_VERSIONS["levels"]
    assert set(res.values) == {
        "nearest_level_distance_atr", "nearest_level_source", "nearest_level_age_bars", "touch_count", "clean_rejection_count", "penetration_count",
        "first_touch", "break_count", "reclaim_count", "role", "previous_role", "zone_source_count", "zone_independent_source_count",
        "zone_width_atr", "n_levels_within_1atr", "last_touch_ts_ns", "last_break_ts_ns", "last_reclaim_ts_ns",
    }
    v = res.values
    assert v["nearest_level_source"] == "ROUND_MAJOR" and v["touch_count"] == 4 and v["role"] == "FLIPPED_TO_RESISTANCE"
    assert v["nearest_level_distance_atr"] == pytest.approx(1 * (100.0 - ev) / 1.0)  # signed in trade direction: +1 => level above is "ahead"
    assert L.level_features(ctx, -1, ev).values["nearest_level_distance_atr"] == pytest.approx(-(100.0 - ev))
    assert v["first_touch"] is False and v["n_levels_within_1atr"] == 1
    assert v["previous_role"] == "ACCEPTED_BELOW" and v["zone_source_count"] == 1 and v["zone_independent_source_count"] == 1
    first = L.level_features(all_ctx[2], 1, LIFECYCLE[2][3])  # touched exactly on this bar for the first time
    assert first.values["touch_count"] == 1 and first.values["first_touch"] is True
    never = L.level_features(all_ctx[1], 1, LIFECYCLE[1][3])
    assert never.values["touch_count"] == 0 and never.values["first_touch"] is True and never.values["last_touch_ts_ns"] is None


def test_features_without_levels_or_atr_are_none_not_zero():
    bars = make_bars(flat(100.0, 3), atr=float("nan"))
    ctx = L.build_level_context(bars, 2, L.LevelConfig(swing_timeframes=(), struct_range_n=None))
    v = L.level_features(ctx, 1, 100.0).values
    assert v["nearest_level_source"] is None and v["nearest_level_distance_atr"] is None and v["touch_count"] is None
    assert v["n_levels_within_1atr"] is None  # ATR unknown => the count is unknown
    assert L.reference_level(ctx, 1, 100.0) is None


def test_reference_level_is_the_nearest_zone_behind_the_event_price():
    bars = with_warmup_bar(closes_to_rows([103, 103, 102.6, 102.2, 102.0]))
    cfg = L.LevelConfig(swing_timeframes=(), struct_range_n=None, round_major_step=5.0, round_minor_step=1.0)
    ctx = L.build_level_context(bars, 5, cfg)
    ref_long = L.reference_level(ctx, 1, 102.0)  # long: the nearest zone at/below the price => the minor level 102 containing it
    assert isinstance(ref_long, S.LevelRef) and ref_long.zone_low <= 102.0 <= ref_long.zone_high
    ref_short = L.reference_level(ctx, -1, 102.0)  # short: nearest zone at/above the price (also the one containing it)
    assert ref_short is not None and ref_short.zone_high >= 102.0
    ref_long2 = L.reference_level(ctx, 1, 102.5)
    assert ref_long2 is not None and ref_long2.price == pytest.approx(102.0)
    ref_short2 = L.reference_level(ctx, -1, 102.5)
    assert ref_short2 is not None and ref_short2.price == pytest.approx(103.0)
    assert ref_long2.confirmed_at_ts_ns <= bars.decision_ts_ns(5) and ref_long2.created_at_ts_ns <= ref_long2.confirmed_at_ts_ns
    assert all(isinstance(s, str) for s in ref_long2.sources)
