"""Exact-fill scenarios for the research simulator (spread 2.0, slippage 0.5 at BASE)."""

from __future__ import annotations

import numpy as np
import pytest

from alpha.common.sim import (
    COST_SCENARIOS,
    ExitSpec,
    Signals,
    SimRules,
    SizingSpec,
    simulate,
)
from tests.unit.alpha.helpers import flat_bars, micro_frame

BASE = COST_SCENARIOS["BASE"]
FIXED15 = ExitSpec("fixed_r", 1.5)
START = "2025-01-15 10:00"  # winter: CET, Berlin minute 600


def sig(n: int, at: int, side: int, stop: float) -> Signals:
    s = np.zeros(n, dtype=np.int8)
    st = np.full(n, np.nan)
    s[at], st[at] = side, stop
    return Signals(side=s, stop=st)


def bars_with(overrides: dict[int, tuple[float, float, float, float]], n: int = 12):
    bars = flat_bars(n)
    for i, b in overrides.items():
        bars[i] = b
    return bars


def run(fr, sg, exit_spec=FIXED15, cost=BASE, sizing=None, rules=None):
    sizing, rules = sizing or SizingSpec(), rules or SimRules()
    trades, skips = simulate(fr, sg, exit_spec, cost, sizing, rules)
    return trades, skips


def test_long_enters_at_next_open_ask_plus_slippage_not_decision_close():
    bars = bars_with({0: (24000, 24000, 24000, 24050)})  # decision bar closes far above its open
    fr = micro_frame(START, bars)
    trades, _ = run(fr, sig(len(bars), 0, +1, 23980.0))
    t = trades.iloc[0]
    assert t.entry_idx == 1 and t.decision_idx == 0
    assert t.entry_price == pytest.approx(24000.0 + 2.0 + 0.5)  # bar-1 open + ask spread + slip
    assert t.risk_pts == pytest.approx(22.5)
    assert t.qty == pytest.approx(2.0)  # floor(50 / 22.5 to 0.25)


def test_short_enters_at_bid_open_minus_slippage():
    bars = flat_bars(12)
    fr = micro_frame(START, bars)
    t = run(fr, sig(12, 0, -1, 24020.0))[0].iloc[0]
    assert t.entry_price == pytest.approx(24000.0 - 0.5)
    assert t.risk_pts == pytest.approx(20.5)


def test_long_target_fill_and_r_multiple():
    bars = bars_with({2: (24000, 24040, 24000, 24000)})
    t = run(micro_frame(START, bars), sig(12, 0, +1, 23980.0))[0].iloc[0]
    assert t.exit_reason == "TARGET" and t.exit_idx == 2
    assert t.exit_price == pytest.approx(24002.5 + 1.5 * 22.5)
    assert t.r_multiple == pytest.approx(1.5)
    assert t.mfe_pts >= 33.75 and t.mae_pts == pytest.approx(2.5)  # bid low 24000 vs fill 24002.5


def test_long_stop_fill_with_slippage_and_r_below_minus_one():
    bars = bars_with({1: (24000, 24000, 23970, 23990)})
    t = run(micro_frame(START, bars), sig(12, 0, +1, 23980.0))[0].iloc[0]
    assert t.exit_reason == "STOP" and t.exit_idx == 1  # stop is live in the entry bar
    assert t.exit_price == pytest.approx(23980.0 - 0.5)
    assert t.r_multiple == pytest.approx(-(24002.5 - 23979.5) / 22.5)
    assert t.r_multiple < -1.0


def test_stop_wins_when_stop_and_target_touch_in_same_bar():
    bars = bars_with({1: (24000, 24100, 23970, 24000)})
    t = run(micro_frame(START, bars), sig(12, 0, +1, 23980.0))[0].iloc[0]
    assert t.exit_reason == "STOP"


def test_gap_through_stop_fills_at_open_not_at_stop():
    bars = bars_with({2: (23900, 23910, 23890, 23900)})
    t = run(micro_frame(START, bars), sig(12, 0, +1, 23980.0))[0].iloc[0]
    assert t.exit_reason == "STOP_GAP" and t.exit_idx == 2
    assert t.exit_price == pytest.approx(23900.0 - 0.5)


def test_short_stop_triggers_on_ask_high_not_bid_high():
    # bid high 24017 + spread 2.0 = ask 24019 < stop 24020 -> NOT stopped
    quiet = bars_with({1: (24000, 24017, 24000, 24000)})
    t1 = run(micro_frame(START, quiet), sig(12, 0, -1, 24020.0))[0].iloc[0]
    assert t1.exit_reason != "STOP"
    # bid high 24018 + 2.0 = ask 24020 -> stopped, filled at stop + slippage
    hit = bars_with({1: (24000, 24018, 24000, 24000)})
    t2 = run(micro_frame(START, hit), sig(12, 0, -1, 24020.0))[0].iloc[0]
    assert t2.exit_reason == "STOP" and t2.exit_price == pytest.approx(24020.5)


def test_short_target_requires_ask_low_and_pays_the_spread():
    entry = 24000.0 - 0.5
    target = entry - 1.5 * 20.5
    # bid low exactly at target: ask low = target + 2 > target -> NOT filled
    bars = bars_with({2: (24000, 24000, target, 24000)})
    t = run(micro_frame(START, bars), sig(12, 0, -1, 24020.0))[0].iloc[0]
    assert t.exit_reason != "TARGET"
    bars = bars_with({2: (24000, 24000, target - 2.0, 24000)})
    t = run(micro_frame(START, bars), sig(12, 0, -1, 24020.0))[0].iloc[0]
    assert t.exit_reason == "TARGET"


def test_session_end_exit_at_open_of_flat_bar():
    bars = flat_bars(30)
    fr = micro_frame("2025-01-15 19:50", bars)  # entry 19:55; 21:30 bar is index 20
    t = run(fr, sig(30, 0, +1, 23950.0))[0].iloc[0]
    assert t.exit_reason == "SESSION_END" and t.exit_idx == 20
    assert t.exit_price == pytest.approx(24000.0 - 0.5)


def test_day_end_backstop_never_carries_overnight():
    bars = flat_bars(6)
    fr = micro_frame("2025-01-15 21:45", bars)  # last bars of the day, then data ends
    # entry window closes at 20:00, so use an earlier start and cut the data right after entry
    fr = micro_frame("2025-01-15 19:50", flat_bars(3))
    t = run(fr, sig(3, 0, +1, 23950.0))[0].iloc[0]
    assert t.exit_reason == "DAY_END" and not t.crossed_rollover


def test_data_gap_closes_at_first_bar_after_gap():
    bars = flat_bars(10)
    fr = micro_frame(START, bars, skip=(3, 4, 5))  # bars 0,1,2,6,7,...
    t = run(fr, sig(len(fr), 0, +1, 23950.0))[0].iloc[0]
    assert t.exit_reason == "DATA_GAP" and t.exit_idx == 3  # first bar after the hole


def test_signal_before_entry_window_and_across_gap_are_skipped():
    fr = micro_frame("2025-01-15 08:50", flat_bars(6))
    trades, skips = run(fr, sig(6, 0, +1, 23950.0))  # entry bar would open 08:55 < 09:00
    assert trades.empty and skips["outside_window"] == 1
    fr = micro_frame(START, flat_bars(8), skip=(1,))
    trades, skips = run(fr, sig(len(fr), 0, +1, 23950.0))
    assert trades.empty and skips["gap_before_entry"] == 1


def test_spread_filter_uses_recorded_spread_not_stressed_spread():
    fr = micro_frame(START, flat_bars(10), spread_pts=900.0)  # 9.0 index points > 8.0 cap
    trades, skips = run(fr, sig(10, 0, +1, 23950.0))
    assert trades.empty and skips["spread_filter"] == 1


def test_leverage_cap_reduces_quantity():
    fr = micro_frame(START, flat_bars(12))
    trades, _ = run(fr, sig(12, 0, +1, 23996.0))  # 6.5 pt risk -> 7.5 lots uncapped = 18x
    t = trades.iloc[0]
    assert t.leverage_capped and t.leverage <= 10.0 + 1e-9 and t.qty == pytest.approx(4.0)


def test_size_below_minimum_is_skipped_not_forced():
    fr = micro_frame(START, flat_bars(12))
    trades, skips = run(fr, sig(12, 0, +1, 23805.0))  # ~197 pt risk -> 0.25 lots ok
    assert len(trades) == 1
    trades, skips = run(fr, sig(12, 0, +1, 23610.0), sizing=SizingSpec(max_risk_pts=1000.0))
    assert trades.empty and skips["size_below_min"] == 1  # 392 pt risk -> 0.12 lots < 0.25


def test_one_position_at_a_time_and_daily_cap():
    n = 40
    s = np.zeros(n, dtype=np.int8)
    st = np.full(n, np.nan)
    for i in range(0, 30, 3):
        s[i], st[i] = 1, 23980.0
    fr = micro_frame(START, flat_bars(n))
    trades, _ = run(fr, Signals(s, st), rules=SimRules(max_trades_per_day=2))
    assert len(trades) <= 2
    # never overlapping
    assert (trades["entry_idx"].to_numpy()[1:] >= trades["exit_idx"].to_numpy()[:-1]).all()


def test_costs_are_monotone_gross_ge_base_ge_stress_on_identical_trades():
    # stop-out at the same absolute stop: price PnL must worsen monotonically with costs
    bars = bars_with({1: (24000, 24000, 23970, 23990)})
    fr = micro_frame(START, bars)
    pnl = {}
    for name in ("GROSS_REFERENCE", "BASE", "SPREAD_STRESS", "SLIPPAGE_STRESS", "COMBINED_ADVERSE"):
        tr, _ = run(fr, sig(12, 0, +1, 23980.0), cost=COST_SCENARIOS[name])
        assert len(tr) == 1
        pnl[name] = tr.iloc[0].pnl_pts
    assert pnl["GROSS_REFERENCE"] > pnl["BASE"] > pnl["SPREAD_STRESS"]
    assert pnl["BASE"] > pnl["SLIPPAGE_STRESS"]
    assert pnl["COMBINED_ADVERSE"] < min(pnl["SPREAD_STRESS"], pnl["SLIPPAGE_STRESS"])


def test_trailing_exit_ratchets_only_from_completed_bars():
    # long: trail distance 1R = 22.5. bar2 rallies to 24060 (close 24060); bar3 falls to 24030
    bars = bars_with({2: (24000, 24060, 24000, 24060), 3: (24060, 24060, 24030, 24030)}, n=12)
    fr = micro_frame(START, bars)
    tr, _ = run(fr, sig(12, 0, +1, 23980.0), exit_spec=ExitSpec("trail", 1.0))
    t = tr.iloc[0]
    # after bar 2 the stop is 24060 - 22.5 = 24037.5; bar 3 low 24030 hits it (STOP), not before
    assert t.exit_reason == "STOP" and t.exit_idx == 3
    assert t.exit_price == pytest.approx(24037.5 - 0.5)


def test_commission_is_charged_per_lot():
    from dataclasses import replace

    cost = replace(BASE, commission_eur_per_lot=3.0)
    bars = bars_with({2: (24000, 24040, 24000, 24000)})
    a = run(micro_frame(START, bars), sig(12, 0, +1, 23980.0))[0].iloc[0]
    b = run(micro_frame(START, bars), sig(12, 0, +1, 23980.0), cost=cost)[0].iloc[0]
    assert a.pnl_eur - b.pnl_eur == pytest.approx(3.0 * a.qty)
