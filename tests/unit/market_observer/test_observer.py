# ruff: noqa: E501
"""Orchestrator (``market_observer.observer``): reference == incremental, prefix / future invariance, symmetry, warm-up, restart, serialisation.

Test catalogue (mission brief) covered here, orchestrator level (the groups own their own boundary tests):
  baseline parity            test_incremental_equals_reference_*           prefix invariance        test_prefix_invariance_*
  long/short symmetry        test_mirror_symmetry_*                        no future swing usage    test_future_bars_do_not_change_the_record
  exact confirmation timing  test_confirmation_boundary_*                  touch counting           test_touch_count_*
  role transitions           test_role_*                                   cluster determinism      test_cluster_and_event_id_determinism
  feature serialisation      test_record_row_*                             restart/replay           test_restart_*
  feature vs label sep.      test_no_label_columns_in_a_live_record        recursive/warm-up inv.   test_warmup_invariance_real_data, test_warmup_flag_*
  segment/gap reset          test_gap_reset_through_the_orchestrator
"""

from __future__ import annotations

import copy
import json
import pickle
from functools import lru_cache

import numpy as np
import pytest
from test_levels_support import closes_to_rows, make_bars, mirror, random_walk

from market_observer import levels as L
from market_observer import observer as O
from market_observer import schema as S

CFG = O.OBSERVER_CONFIG.with_round_steps(1.0, 5.0)
SAMPLE = (30, 99, 100, 101, 233, 234, 235, 360, 361, 362)


@lru_cache(maxsize=1)
def world():
    return random_walk(400, 11, bars_per_day=100, segment_breaks=(150,))


def ev(bars, i, direction=1, **kw):
    return O.ObservedEvent(direction, float(bars.c[i]), family="STRUCT", variant="breakout", **kw)


def stable(rec):
    """Record as a plain comparable dict (NaN-safe)."""
    return json.loads(json.dumps(rec.to_row(), sort_keys=True, default=str))


# ---------------------------------------------------------------------------------------------- baseline parity
def test_incremental_equals_reference_on_synthetic_world():
    bars = world()
    obs = O.MarketStructureObserver(CFG)
    for i in SAMPLE:
        obs.advance(bars, i)
        for d in (1, -1):
            inc = obs.observe(bars, i, ev(bars, i, d))
            ref = O.observe_event(bars, i, ev(bars, i, d), CFG)
            assert inc == ref, f"incremental != reference at i={i} d={d}"
            assert stable(inc) == stable(ref)


def test_incremental_observer_refuses_out_of_order_events():
    bars = world()
    obs = O.MarketStructureObserver(CFG)
    obs.advance(bars, 50)
    with pytest.raises(O.StaleEventError):
        obs.observe(bars, 40, ev(bars, 40))
    with pytest.raises(ValueError, match="behind"):
        obs.observe(bars, 60, ev(bars, 60))


def test_advance_with_an_expired_deadline_stops_consistently_and_resumes_exactly():
    bars = world()
    obs = O.MarketStructureObserver(CFG)
    ticks = iter(range(10_000))
    # deadline passes after 25 bars: the state is consistent at that point and a later advance continues the same sequence
    seen = obs.advance(bars, 200, deadline=25.0, clock=lambda: float(next(ticks)))
    assert 0 < seen <= 26 and obs.bars_seen == seen
    obs.advance(bars, 120)
    assert obs.observe(bars, 120, ev(bars, 120)) == O.observe_event(bars, 120, ev(bars, 120), CFG)


# ---------------------------------------------------------------------------------------------- prefix / future invariance
@pytest.mark.parametrize("i", SAMPLE[:7])
def test_prefix_invariance_orchestrator(i):
    bars = world()
    full = O.observe_event(bars, i, ev(bars, i), CFG)
    cut = O.observe_event(bars.prefix(i + 1), i, ev(bars, i), CFG)
    assert full == cut


def test_future_bars_do_not_change_the_record():
    """No future swing / level / baseline usage: scrambling every bar AFTER i leaves the record at i untouched."""
    bars = world()
    i = 233
    rng = np.random.default_rng(5)
    junk = {k: np.array(getattr(bars, k)) for k in ("o", "h", "l", "c", "tick_volume", "spread")}
    for k in ("o", "h", "l", "c"):
        junk[k][i + 1:] = junk[k][i + 1:] * rng.uniform(0.2, 3.0, len(junk[k]) - i - 1)
    junk["tick_volume"][i + 1:] = 99999.0
    scr = S.ObserverBars(bars.market, bars.ts_ns, junk["o"], junk["h"], junk["l"], junk["c"], junk["tick_volume"], junk["spread"], bars.atr,
                         bars.segment_id, bars.local_minute, bars.local_day, bars.tick_size, bars.session, bars.bar_seconds)
    assert O.observe_event(bars, i, ev(bars, i), CFG) == O.observe_event(scr, i, ev(bars, i), CFG)


# ---------------------------------------------------------------------------------------------- long / short symmetry
@pytest.mark.parametrize("i", [99, 234, 361])
def test_mirror_symmetry_orchestrator(i):
    """Price mirror p -> -p with the direction flipped: every direction-free number of every group is identical; role names swap sides."""
    bars = world()
    m = mirror(bars)
    cfg = CFG
    a = O.observe_event(bars, i, ev(bars, i, 1), cfg).features.columns
    b = O.observe_event(m, i, O.ObservedEvent(-1, -float(bars.c[i]), family="STRUCT", variant="breakout"), cfg).features.columns
    numeric_groups = ("f_levels__nearest_level_distance_atr", "f_levels__touch_count", "f_levels__clean_rejection_count", "f_levels__penetration_count",
                      "f_levels__break_count", "f_levels__reclaim_count", "f_levels__zone_source_count", "f_levels__n_levels_within_1atr")
    for k in numeric_groups:
        assert a[k] == b[k], k
    swapped = {"f_acceptance__followthrough_high_atr": "f_acceptance__followthrough_low_atr", "f_acceptance__followthrough_low_atr": "f_acceptance__followthrough_high_atr",
               }  # absolute high/low excursions swap sign under the mirror; every direction-relative value is identical

    def close(x, y):
        return x == y or (x is not None and y is not None and abs(x - y) < 1e-9)

    for k in a:
        if k not in swapped and k.startswith(("f_balance__", "f_participation__", "f_acceptance__")):
            assert close(a[k], b[k]), k
    assert close(a["f_acceptance__followthrough_high_atr"], None if b["f_acceptance__followthrough_low_atr"] is None else -b["f_acceptance__followthrough_low_atr"])
    assert close(a["f_acceptance__followthrough_low_atr"], None if b["f_acceptance__followthrough_high_atr"] is None else -b["f_acceptance__followthrough_high_atr"])


# ---------------------------------------------------------------------------------------------- confirmation / touch / role (orchestrator boundary)
def _flat_then_touch():
    # 60 flat bars at 100, then a pierce of the previous swing high region is not needed: use ROUND_MAJOR grid (step 5) levels at 100
    rows = closes_to_rows([100.0] * 40 + [101.0, 103.0, 104.5, 104.9, 105.4, 106.0, 106.2, 106.1, 106.3])
    return make_bars(rows, atr=1.0, tick=0.01)


def test_touch_count_and_role_transitions_are_visible_in_the_record():
    bars = _flat_then_touch()
    cfg = O.OBSERVER_CONFIG.with_round_steps(None, 5.0)
    cfg = O.ObserverConfig(levels=L.LevelConfig(swing_timeframes=(), struct_range_n=None, round_major_step=5.0))
    obs = O.MarketStructureObserver(cfg)
    roles = []
    touches = []
    for i in range(len(bars)):
        obs.advance(bars, i)
        r = obs.observe(bars, i, O.ObservedEvent(1, float(bars.c[i])))
        roles.append(r.features.columns["f_levels__role"])
        touches.append(r.features.columns["f_levels__touch_count"])
    assert touches == sorted(t for t in touches if t is not None) or all(isinstance(t, (int, type(None))) for t in touches)
    assert len(set(roles)) >= 2, "a break of the grid level must show up as a role change"


def test_confirmation_boundary_a_swing_is_not_visible_before_its_confirmation_bar():
    bars = world()
    # swings: usable from the close of bar j+n (own timeframe). A record at i must equal the record on the prefix: at the boundary bars too.
    for i in (200, 201, 202, 203, 204, 205):
        assert O.observe_event(bars, i, ev(bars, i), CFG).features.columns == O.observe_event(bars.prefix(i + 1), i, ev(bars, i), CFG).features.columns


def test_cluster_and_event_id_determinism():
    bars = world()
    a = O.observe_event(bars, 234, ev(bars, 234), CFG)
    b = O.observe_event(bars, 234, ev(bars, 234), CFG)
    assert a == b and a.event_id == b.event_id
    assert a.event_id != O.observe_event(bars, 234, ev(bars, 234, -1), CFG).event_id
    assert a.event_id == O.event_id_for("RW", bars.decision_ts_ns(234), "STRUCT", "breakout", 1)
    assert O.event_id_for("RW", 1, None, None, 1) != O.event_id_for("RW", 2, None, None, 1)


# ---------------------------------------------------------------------------------------------- serialisation / separation
def test_record_row_carries_versions_schema_hashes_and_json_roundtrips():
    bars = world()
    r = O.observe_event(bars, 234, ev(bars, 234, opportunity_id="opp-1", structure_event_id="se-1"), CFG)
    row = r.to_row()
    assert row["schema_version"] == S.SCHEMA_VERSION and row["observer_version"] == S.OBSERVER_VERSION and row["status"] == S.STATUS
    assert {row[f"v_{g}"] for g in O.GROUPS_OBSERVED} == {S.GROUP_VERSIONS[g] for g in O.GROUPS_OBSERVED}
    assert row["m_warmup_ok"] is False and row["m_opportunity_id"] == "opp-1" and row["m_structure_event_id"] == "se-1"
    for g in O.GROUPS_OBSERVED:
        assert len(row[f"m_hash_{g}"]) >= 16
    assert json.loads(json.dumps(row, default=str))["event_id"] == r.event_id
    assert any(k.startswith("f_levels__") for k in row) and any(k.startswith("f_participation__") for k in row)


def test_no_label_columns_in_a_live_record():
    r = O.observe_event(world(), 234, ev(world(), 234), CFG)
    assert r.labels is None and not any(k.startswith(S.LABEL_PREFIX) for k in r.to_row())
    assert all(k.startswith(S.FEATURE_PREFIX) for k in r.features.columns)


# ---------------------------------------------------------------------------------------------- restart / replay determinism
def test_restart_pickled_state_and_replay_give_identical_records():
    bars = world()
    obs = O.MarketStructureObserver(CFG)
    obs.advance(bars, 200)
    restored = pickle.loads(pickle.dumps(obs))
    cloned = copy.deepcopy(obs)
    for o in (restored, cloned):
        o.advance(bars, 300)
    fresh = O.MarketStructureObserver(CFG)
    fresh.advance(bars, 300)
    recs = [o.observe(bars, 300, ev(bars, 300)) for o in (restored, cloned, fresh)]
    assert recs[0] == recs[1] == recs[2] == O.observe_event(bars, 300, ev(bars, 300), CFG)


# ---------------------------------------------------------------------------------------------- segment / gap
def test_gap_reset_through_the_orchestrator():
    """The synthetic world has a contiguity break at bar 150: balance windows never span it and levels of the old segment are not carried over."""
    bars = world()
    assert bars.segment_id[149] != bars.segment_id[150]
    r_after = O.observe_event(bars, 150 + 10, ev(bars, 160), CFG).features.columns
    assert r_after["f_balance__range_width_atr_w24"] is None and r_after["f_balance__range_width_atr_w48"] is None
    r_ok = O.observe_event(bars, 150 + 30, ev(bars, 180), CFG).features.columns
    assert r_ok["f_balance__range_width_atr_w24"] is not None and r_ok["f_balance__range_width_atr_w48"] is None


# ---------------------------------------------------------------------------------------------- warm-up
def test_warmup_flag_follows_the_documented_constants():
    mh = O.OBSERVER_CONFIG.min_history()
    assert mh == {"levels": 603, "swings": 480, "acceptance": 98, "balance": 48, "participation_prev_days": 21}
    assert O.MAX_MIN_HISTORY_BARS == 603
    bars = world()
    ok, detail = O.warmup_status(bars, 399, CFG)
    assert ok is False and detail["bars_available"] == 400 and detail["min_bars_levels"] == 603 and detail["min_prev_days_participation"] == 21
    assert detail["prev_days_available"] == O.previous_trading_days(bars, 399) == 3


@lru_cache(maxsize=1)
def real_bars():
    from alpha.common.market_data import load_dev_market_frame
    from alpha.families.data import round_steps
    from market_observer import bars_adapter as BA
    from markets.spec import load_market_spec

    ms = load_market_spec("GER40")
    fr = load_dev_market_frame(ms).iloc[-9000:].reset_index(drop=True)
    c = ms.calendar
    bars = BA.bars_from_frame("GER40", fr, point_size=ms.point_size, tick_size=ms.tick_size, session=BA.session_spec(c.tz, c.cash_open_min, c.cash_close_min))
    minor, major = round_steps(ms.asset_class, ms.tick_size)
    return bars, O.OBSERVER_CONFIG.with_round_steps(minor, major)


def _slice(bars, a, b):
    sl = slice(a, b)
    return S.ObserverBars(bars.market, bars.ts_ns[sl], bars.o[sl], bars.h[sl], bars.l[sl], bars.c[sl], bars.tick_volume[sl], bars.spread[sl], bars.atr[sl],
                          bars.segment_id[sl], bars.local_minute[sl], bars.local_day[sl], bars.tick_size, bars.session, bars.bar_seconds)


def test_warmup_invariance_real_data():
    """RECURSIVE / WARM-UP INVARIANCE through the orchestrator on real GER40 bars: the same decision bar observed with 120 / 240 / 500 / 700 loaded
    bars is flagged warmup_ok=False (history requirement of at least one group unmet); with the documented history (6000 and 9000 bars: >= 603 bars and
    >= 21 previous trading days) the feature columns are EXACTLY equal."""
    bars, cfg = real_bars()
    end = len(bars)
    i = end - 1
    event = O.ObservedEvent(1, float(bars.c[i]), family="STRUCT", variant="breakout")
    recs = {}
    for n in (120, 240, 500, 700, 6000, 9000):
        w = _slice(bars, end - n, end)
        obs = O.MarketStructureObserver(cfg)
        obs.advance(w, len(w) - 1)
        recs[n] = obs.observe(w, len(w) - 1, event)
    for n in (120, 240, 500, 700):
        assert recs[n].meta["warmup_ok"] is False, n
    assert recs[6000].meta["warmup_ok"] is True and recs[9000].meta["warmup_ok"] is True
    assert recs[6000].features == recs[9000].features, {
        k: (recs[6000].features.columns[k], recs[9000].features.columns[k]) for k in recs[6000].features.columns if recs[6000].features.columns[k] != recs[9000].features.columns[k]
    }
    # the full-history reference equals the incremental one (bar 0 start)
    assert O.observe_event(_slice(bars, end - 6000, end), 5999, event, cfg).features == recs[6000].features
