# ruff: noqa: E501
"""Lane X: the nine exit policies stepped through the EXISTING ExitEngine on the SAME entry (synthetic bars)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from demo import structure as st
from demo.exit_policies import (
    EXIT_FORCED_FLAT,
    P_BE,
    P_EOD,
    P_FIXED,
    P_MOM,
    P_RUNNER,
    P_TIME,
    P_TP1,
    P_TP12,
    P_TRAIL,
    POLICY_IDS,
    POLICY_PARAMS,
    POLICY_SET_VERSION,
    BarSeries,
    EntryInput,
    ManagementCache,
    simulate_all,
    simulate_policy,
)
from demo.labeling import Bar, simulate_hypothetical
from demo.opportunity.operating_policy import load_operating_policy

T0 = datetime(2026, 6, 9, 8, 0, tzinfo=UTC)


def mk(rows, start=T0, spread=0.0):
    """rows: (o, h, l, c) bid bars, 5 minutes apart."""
    ts = tuple(start + timedelta(minutes=5 * i) for i in range(len(rows)))
    sp = tuple(spread if not isinstance(spread, (list, tuple)) else spread[i] for i in range(len(rows)))
    return BarSeries(ts, *(tuple(float(r[k]) for r in rows) for k in range(4)), sp)


def entry(direction=1, fill=100.0, stop=99.0, tp1=None, tp2=None, flat=None, atr=1.0, start=T0, pre=None):
    return EntryInput("e1", "GER40", direction, fill, stop, start, atr, tp1=tp1, tp2=tp2, flat_utc=flat, pre=pre)


def random_path(seed, n=60, base=100.0, vol=0.35, spread=0.0, start=T0):
    rnd = random.Random(seed)
    px, rows = base, []
    for _ in range(n):
        o = px
        hi, lo = o + rnd.random() * vol, o - rnd.random() * vol
        c = rnd.uniform(lo, hi)
        rows.append((o, hi, lo, c))
        px = c + rnd.uniform(-0.05, 0.05)
    return mk(rows, start, spread), rows


# ---- the nine policies on the SAME entry ----------------------------------------------------------------
def test_all_nine_policies_run_on_the_same_entry_and_bars():
    bars, _ = random_path(3)
    e = entry(tp1=100.6, tp2=101.2)
    res = simulate_all(e, bars)
    assert tuple(res) == POLICY_IDS and len(POLICY_IDS) == 9
    assert all(r.policy_id == pid for pid, r in res.items())
    # every policy exits (or is censored) using ONLY this entry's bars
    for r in res.values():
        assert r.applicable and r.fills
        assert all(e.entry_ts <= f.ts <= bars.ts[-1] + timedelta(minutes=5) for f in r.fills)
        assert sum(f.fraction for f in r.fills) == pytest.approx(1.0)


def test_policy_set_is_versioned_and_parameters_are_declared():
    assert POLICY_SET_VERSION == POLICY_PARAMS["version"] == "eeq-policies-1"
    for k in ("fixed_r", "be_trigger_r", "time_stop_bars", "momentum_threshold_atr", "runner_fractions", "tick_model"):
        assert k in POLICY_PARAMS


def test_structural_policies_are_not_applicable_without_levels_never_invented():
    bars, _ = random_path(4)
    none = simulate_all(entry(), bars)
    assert [none[p].applicable for p in (P_TP1, P_TP12, P_RUNNER)] == [False, False, False]
    assert none[P_TP1].na_reason == "NO_STRUCTURAL_TP1"
    assert all(none[p].applicable for p in (P_FIXED, P_TRAIL, P_BE, P_MOM, P_TIME, P_EOD))
    tp1_only = simulate_all(entry(tp1=100.5), bars)
    assert tp1_only[P_TP1].applicable and tp1_only[P_RUNNER].applicable  # TP1 + runner, no TP2 invented
    assert not tp1_only[P_TP12].applicable and tp1_only[P_TP12].na_reason == "NO_STRUCTURAL_TP2"


# ---- baseline parity with the existing labeller ---------------------------------------------------------
@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("seed", range(12))
def test_fixed_1_5r_equals_existing_hypothetical_simulation(direction, seed):
    spread = 0.04
    bars, _ = random_path(seed, n=50, spread=spread)
    long = direction == 1
    fill = 100.0 + (spread if long else 0.0)  # long fills at the ask
    stop = fill - 1.0 if long else fill + 1.0
    tgt = fill + 1.5 if long else fill - 1.5
    hyp = simulate_hypothetical(
        direction=direction, entry=fill, stop=stop, target=tgt,
        bars=[Bar(t.isoformat(), o, h, lo, c, spread) for t, o, h, lo, c in zip(bars.ts, bars.o, bars.h, bars.lo, bars.c, strict=True)],
    )
    r = simulate_policy(P_FIXED, entry(direction, fill, stop, start=bars.ts[0]), bars)
    if hyp.exit_kind == "HORIZON":
        assert r.censored  # data ended first: censored, not a rule exit
    else:
        assert r.r == pytest.approx(hyp.r, abs=1e-9), (hyp.exit_kind, r.final_reason)


# ---- tick model: stop-first, gaps, fills ----------------------------------------------------------------
def test_stop_first_inside_a_bar_touching_stop_and_target():
    bars = mk([(100.0, 101.6, 98.9, 100.0)])
    r = simulate_policy(P_FIXED, entry(), bars)
    assert r.r == pytest.approx(-1.0) and r.final_reason == "STOP"


def test_gap_through_the_stop_fills_at_the_open():
    bars = mk([(100.0, 100.2, 99.8, 100.1), (98.4, 98.6, 98.0, 98.5)])
    r = simulate_policy(P_FIXED, entry(), bars)
    assert r.final_reason == "STOP_GAP" and r.r == pytest.approx(-1.6)


def test_target_fills_at_the_level_and_short_uses_the_ask():
    bars = mk([(100.0, 100.3, 99.9, 100.2), (100.2, 101.7, 100.1, 101.6)])
    assert simulate_policy(P_FIXED, entry(), bars).r == pytest.approx(1.5)
    # short: entry 100 (bid), stop 101 (ask), target 98.5 must be reached on the ASK = low + spread
    sb = mk([(100, 100.2, 99.0, 99.5), (99.5, 99.6, 98.35, 98.6)], spread=0.1)
    r = simulate_policy(P_FIXED, entry(-1, 100.0, 101.0), sb)
    assert r.r is not None and r.final_reason == "TP1" and r.r == pytest.approx(1.5)
    tight = mk([(100, 100.2, 99.0, 99.5), (99.5, 99.6, 98.35, 98.6)], spread=0.3)  # ask never reaches 98.5
    assert simulate_policy(P_FIXED, entry(-1, 100.0, 101.0), tight).final_reason != "TP1"


def test_structural_ladders_fractions_and_fills():
    bars = mk([(100, 100.7, 99.9, 100.6), (100.6, 101.3, 100.5, 101.2), (101.2, 101.4, 100.4, 100.6)])
    e = entry(tp1=100.5, tp2=101.25)
    p1 = simulate_policy(P_TP1, e, bars)
    assert p1.r == pytest.approx(0.5) and p1.stages_hit == 1
    p12 = simulate_policy(P_TP12, e, bars)
    assert [f.fraction for f in p12.fills] == [0.5, 0.5] and p12.r == pytest.approx(0.5 * 0.5 + 0.5 * 1.25)
    runner = simulate_policy(P_RUNNER, e, bars)
    assert [round(f.fraction, 4) for f in runner.fills][:2] == [0.5, 0.25]  # TP1 50 %, TP2 25 %, runner 25 %
    assert sum(f.fraction for f in runner.fills) == pytest.approx(1.0)


def test_break_even_lock_is_cost_adjusted_and_only_after_one_r():
    # +1.0R reached -> stop to entry + exit cost (spread 0.1); then price falls back to the new stop
    bars = mk([(100, 100.4, 99.95, 100.3), (100.3, 101.05, 100.2, 101.0), (101.0, 101.1, 100.0, 100.2), (100.2, 100.3, 98.9, 99.0)], spread=0.1)
    r = simulate_policy(P_BE, entry(fill=100.1, stop=99.1), bars)
    assert r.final_reason == "BREAK_EVEN_STOP"
    assert r.stop_levels[0] == pytest.approx(99.1) and r.stop_levels[1] == pytest.approx(100.1 + 0.1)
    assert r.r == pytest.approx((100.2 - 100.1) / 1.0)  # small profit, not a loss
    plain = simulate_policy(P_FIXED, entry(fill=100.1, stop=99.1), bars)
    assert plain.r < 0  # the baseline gave it all back


def test_ratchet_applies_from_the_next_bar_not_the_same_bar():
    # bar 1 reaches +1R (BE armed at its favourable extreme) and then falls to 100.05 <= BE within the SAME bar:
    # conservative model -> the new stop is NOT in force yet, the trade survives bar 1
    bars = mk([(100, 101.1, 100.05, 100.06), (100.06, 100.3, 100.0, 100.2)])
    r = simulate_policy(P_BE, entry(), bars)
    assert r.fills[0].ts >= T0 + timedelta(minutes=5) or r.censored


def test_momentum_exit_and_time_alpha_decay_use_the_engine_rules():
    # closes fall 0.6 (= 1.2 ATR) within 3 bars -> production momentum exit at the close of that bar (bar 3 of the path)
    rows = [(100, 100.3, 99.9, 100.1), (100.1, 100.15, 99.8, 99.9), (99.9, 99.95, 99.4, 99.5), (99.5, 99.55, 99.05, 99.2)]
    pre = mk([(100, 100.2, 99.9, 100.1)] * 5, start=T0 - timedelta(minutes=25))
    bars = mk(rows)
    r = simulate_policy(P_MOM, entry(fill=100.0, stop=98.5, atr=0.5, pre=pre), bars)
    assert r.final_reason == "MOMENTUM_DETERIORATION" and r.fills[0].price == pytest.approx(99.5)
    # time stop: 24 bars flat, trade never showed 0.5R -> exits at the open of the 25th bar
    flat = mk([(100, 100.1, 99.95, 100.0)] * 40)
    t = simulate_policy(P_TIME, entry(), flat)
    assert t.final_reason == "TIME_STOP" and t.holding_s == pytest.approx(24 * 300, abs=300)
    # ... but a trade that reached 0.5R is not aged out
    worked = mk([(100, 100.6, 99.95, 100.0)] + [(100, 100.1, 99.95, 100.0)] * 39)
    w = simulate_policy(P_TIME, entry(), worked)
    assert w.final_reason != "TIME_STOP"


def test_structure_trail_ratchets_behind_confirmed_swing_and_is_stopped_by_it():
    # up-move with a higher swing LOW (bar 3 dip to 100.3, confirmed two bars later); the trail sits behind it
    # (swing low - 0.25 ATR = 100.05), then the price drops through it -> stopped there with a profit
    rows = [(100, 100.4, 99.8, 100.3), (100.6, 101.2, 100.5, 101.1), (101.1, 101.6, 100.9, 101.5), (101.5, 101.7, 100.3, 101.6),
            (101.6, 102.0, 101.0, 101.9), (101.9, 102.4, 101.4, 102.3), (102.3, 102.5, 102.0, 102.4), (102.4, 102.45, 99.9, 100.0)]
    pre = mk([(99.8, 100.1, 99.6, 100.0)] * 5, start=T0 - timedelta(minutes=25))
    r = simulate_policy(P_TRAIL, entry(fill=100.0, stop=99.0, atr=1.0, pre=pre), mk(rows))
    assert r.stop_levels[0] == 99.0 and r.stop_levels[-1] == pytest.approx(100.05)
    assert r.final_reason == "TRAILING_STOP" and r.r == pytest.approx(0.05)
    assert simulate_policy(P_EOD, entry(fill=100.0, stop=99.0, atr=1.0, pre=pre), mk(rows)).r != r.r  # the trail changed the outcome


# ---- invariants -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("seed", range(10))
def test_stop_never_loosened_by_any_policy(direction, seed):
    bars, _ = random_path(100 + seed, n=70, spread=0.03, vol=0.45)
    long = direction == 1
    fill, stop = (100.03, 99.0) if long else (100.0, 101.0)
    pre = mk([(100, 100.2, 99.8, 100.05)] * 6, start=T0 - timedelta(minutes=30), spread=0.03)
    tp1 = fill + 0.6 if long else fill - 0.6
    tp2 = fill + 1.4 if long else fill - 1.4
    res = simulate_all(entry(direction, fill, stop, tp1=tp1, tp2=tp2, pre=pre), bars)
    for pid, r in res.items():
        assert r.stop_levels[0] == pytest.approx(stop)
        for a, b in zip(r.stop_levels, r.stop_levels[1:], strict=False):
            assert (b >= a) if long else (b <= a), (pid, r.stop_levels)
        assert all((x >= stop - 1e-9) if long else (x <= stop + 1e-9) for x in r.stop_levels)


@pytest.mark.parametrize("seed", range(10))
def test_long_short_mirror_gives_identical_r_for_every_policy(seed):
    bars, rows = random_path(200 + seed, n=60, vol=0.5)
    k = 200.0
    mirror = mk([(k - o, k - lo, k - h, k - c) for o, h, lo, c in rows])
    pre_rows = [(100.0, 100.2, 99.85, 100.1), (100.1, 100.3, 99.9, 100.2), (100.2, 100.25, 99.8, 99.9), (99.9, 100.1, 99.7, 99.95), (99.95, 100.2, 99.9, 100.1), (100.1, 100.2, 99.95, 100.0)]
    pre_l = mk(pre_rows, start=T0 - timedelta(minutes=30))
    pre_s = mk([(k - o, k - lo, k - h, k - c) for o, h, lo, c in pre_rows], start=T0 - timedelta(minutes=30))
    rl = simulate_all(entry(1, 100.0, 99.0, tp1=100.7, tp2=101.3, pre=pre_l), bars)
    rs = simulate_all(entry(-1, k - 100.0, k - 99.0, tp1=k - 100.7, tp2=k - 101.3, pre=pre_s), mirror)
    for pid in POLICY_IDS:
        assert rl[pid].r == pytest.approx(rs[pid].r, abs=1e-9), pid
        assert rl[pid].final_reason == rs[pid].final_reason


# ---- causality ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", range(8))
def test_bars_after_a_policys_exit_cannot_change_its_result(seed):
    bars, rows = random_path(300 + seed, n=80, vol=0.5)
    e = entry(tp1=100.6, tp2=101.2)
    full = simulate_all(e, bars)
    for pid, r in full.items():
        if r.censored or not r.fills:
            continue
        last = r.fills[-1].ts
        keep = [i for i, t in enumerate(bars.ts) if t <= last]
        cut = BarSeries(*(tuple(col[i] for i in keep) for col in (bars.ts, bars.o, bars.h, bars.lo, bars.c, bars.spread)))
        again = simulate_policy(pid, e, cut)
        assert again.r == pytest.approx(r.r) and again.final_reason == r.final_reason, pid
        # garbage after the exit changes nothing either
        junk_rows = rows[: len(keep)] + [(1.0, 500.0, 0.5, 250.0)] * (len(rows) - len(keep))
        again2 = simulate_policy(pid, e, mk(junk_rows))
        assert again2.r == pytest.approx(r.r), pid


def test_management_cache_equals_direct_management_signals_on_every_prefix():
    import pandas as pd

    bars, _ = random_path(11, n=40, vol=0.6)
    pre = mk([(100, 100.3, 99.7, 100.1)] * 6, start=T0 - timedelta(minutes=30))
    for direction, cur_stop in ((1, 98.0), (-1, 102.0)):
        e = entry(direction, 100.0, 99.0 if direction == 1 else 101.0, atr=0.8, pre=pre)
        cache = ManagementCache(e, bars)
        for k in range(len(bars)):
            ts = list(pre.ts) + list(bars.ts[: k + 1])
            df = pd.DataFrame({
                "ts": pd.DatetimeIndex(ts),
                "open": list(pre.o) + list(bars.o[: k + 1]), "high": list(pre.h) + list(bars.h[: k + 1]),
                "low": list(pre.lo) + list(bars.lo[: k + 1]), "close": list(pre.c) + list(bars.c[: k + 1]),
            })
            direct = st.management_signals(direction, df, entered_at=T0, current_stop=cur_stop, price=100.3, atr=0.8, spread=0.0)
            cached = cache.signals(e, k, current_stop=st._dec(cur_stop), price=st._dec(100.3), spread=st._dec(0.0), as_of=bars.ts[k] + timedelta(minutes=5))
            assert cached == direct, (direction, k)


# ---- EOD forced flat / Berlin deadline ------------------------------------------------------------------
@pytest.mark.parametrize(("month", "flat_utc_hour"), [(6, 19), (1, 20)])  # 21:55 Berlin = 19:55Z summer / 20:55Z winter
def test_forced_flat_uses_the_berlin_flatten_deadline(month, flat_utc_hour):
    op = load_operating_policy()
    start = datetime(2026, month, 10, 19 if month == 6 else 20, 0, tzinfo=UTC)  # a Wednesday-ish weekday
    while start.weekday() >= 5:
        start += timedelta(days=1)
    signal = start - timedelta(minutes=30)
    # far-away market flat: the GLOBAL Berlin flatten start decides
    flat = op.effective_flat_utc("GER40", signal + timedelta(days=1), signal)
    assert (flat.hour, flat.minute) == (flat_utc_hour, 55)
    bars = mk([(100, 100.1, 99.9, 100.0)] * 12, start=start)  # 19:00Z.. (summer) flat price, no rule fires
    e = EntryInput("e", "GER40", 1, 100.0, 99.0, start, 1.0, flat_utc=flat)
    for pid in (P_EOD, P_FIXED, P_TRAIL):
        r = simulate_policy(pid, e, bars)
        assert r.final_reason == EXIT_FORCED_FLAT and not r.censored, pid
        assert r.fills[-1].ts == flat and r.fills[-1].price == pytest.approx(100.0)  # OPEN of the flat bar


def test_forced_flat_fills_a_short_at_the_ask_of_the_flat_bar_open():
    flat = T0 + timedelta(minutes=15)
    bars = mk([(100, 100.1, 99.9, 100.0)] * 5, spread=0.2)
    r = simulate_policy(P_EOD, entry(-1, 100.0, 101.0, flat=flat), bars)
    assert r.final_reason == EXIT_FORCED_FLAT and r.fills[-1].price == pytest.approx(100.2) and r.r == pytest.approx(-0.2)


def test_data_end_before_the_deadline_is_censored_not_silently_closed():
    bars = mk([(100, 100.1, 99.9, 100.0)] * 3)
    r = simulate_policy(P_EOD, entry(flat=T0 + timedelta(hours=5)), bars)
    assert r.censored and r.final_reason == "DATA_END"
