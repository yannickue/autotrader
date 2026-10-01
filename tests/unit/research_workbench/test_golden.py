"""Golden scenarios: FAST-side numbers vs HAND calculation (see src/research_workbench/golden.py).

The ``expected`` dicts in ``golden.py`` were worked out by hand from the documented FAST rules
(fill = next open + spread + slippage, qty = floor(50 EUR / risk / 0.25) * 0.25, ...); these tests
compare ``simulate_fast`` + ``normalize_fast`` against them, so a drift in either the simulator or
the normalisation is caught without relying on the simulator as its own oracle.
"""

from __future__ import annotations

import itertools
import math

import pytest

from alpha.fast.sim import simulate_fast
from research_workbench.differential import BAR_NS, bar_open_ts_ns, normalize_fast
from research_workbench.golden import (
    GOLDEN_SCENARIOS,
    GOLDEN_VERSION,
    PINNED_CANDIDATE_HASH,
    PINNED_DATA_HASH,
)

REQUIRED = {
    "long_normal_target",
    "short_normal_target",
    "long_normal_stop",
    "short_normal_stop",
    "gap_through_stop",
    "target_crossed_at_fill",
    "session_end",
    "data_gap",
    "spread_slippage_cost_target",
    "spread_slippage_cost_stop",
}
_IDS = sorted(GOLDEN_SCENARIOS)


def test_all_required_scenarios_exist() -> None:
    assert set(GOLDEN_SCENARIOS) >= REQUIRED
    assert all(s.version == GOLDEN_VERSION for s in GOLDEN_SCENARIOS.values())


@pytest.mark.parametrize("sid", _IDS)
def test_hashes_are_pinned_and_deterministic(sid: str) -> None:
    s = GOLDEN_SCENARIOS[sid]
    assert len(s.data_hash()) == 64
    assert s.data_hash() == s.data_hash() == PINNED_DATA_HASH[sid]
    assert s.candidate_hash() == PINNED_CANDIDATE_HASH[sid]


def test_data_hashes_are_unique_per_scenario() -> None:
    assert len(set(PINNED_DATA_HASH.values())) == len(PINNED_DATA_HASH)


@pytest.mark.parametrize("sid", _IDS)
def test_fast_side_matches_hand_calculation(sid: str) -> None:
    s = GOLDEN_SCENARIOS[sid]
    exp = s.expected
    market, cands, cost, window = s.inputs()
    trades = simulate_fast(market, cands, cost, window=window)
    assert len(trades) == exp["n_trades"]
    if exp["n_trades"] == 0:
        for label, count in exp["skips"].items():
            assert trades.skips[label] == count
        return
    assert int(trades.decision_idx[0]) == exp["decision_idx"]
    assert int(trades.entry_idx[0]) == exp["entry_idx"]
    assert int(trades.exit_idx[0]) == exp["exit_idx"]
    assert int(trades.side[0]) == exp["side"]
    assert str(trades.exit_reason_labels[0]) == exp["exit_reason"]
    assert trades.entry_price[0] == pytest.approx(exp["fill"], abs=1e-9)
    assert trades.exit_price[0] == pytest.approx(exp["exit_price"], abs=1e-9)
    assert trades.risk_pts[0] == pytest.approx(exp["risk"], abs=1e-9)
    assert trades.qty[0] == pytest.approx(exp["qty"], abs=1e-12)
    assert trades.net_pnl_eur[0] == pytest.approx(exp["net"], abs=1e-9)
    assert trades.cost_eur[0] == pytest.approx(exp["cost"], abs=1e-9)
    assert trades.gross_pnl_eur[0] == pytest.approx(exp["gross"], abs=1e-9)
    assert trades.r_multiple[0] == pytest.approx(exp["r"], abs=1e-9)
    assert trades.mfe_r[0] == pytest.approx(exp["mfe_r"], abs=1e-9)
    assert trades.mae_r[0] == pytest.approx(exp["mae_r"], abs=1e-9)
    assert int(trades.holding_bars[0]) == exp["holding_bars"]


@pytest.mark.parametrize("sid", _IDS)
def test_normalize_fast_fields(sid: str) -> None:
    s = GOLDEN_SCENARIOS[sid]
    exp = s.expected
    market, cands, cost, window = s.inputs()
    trades = simulate_fast(market, cands, cost, window=window)
    norm = normalize_fast(market, cands, trades)
    assert len(norm) == exp["n_trades"]
    if not norm:
        return
    t = norm[0]
    open_ts = bar_open_ts_ns(market)
    # decision at the CLOSE of bar 3 == signal timestamp == decision timestamp
    assert t.signal_ts_ns == t.decision_ts_ns == int(open_ts[exp["decision_idx"]]) + BAR_NS
    assert t.direction == exp["side"]
    assert t.entry_ref == pytest.approx(float(market.o[exp["entry_idx"]]))
    assert t.fill_price == pytest.approx(exp["fill"], abs=1e-9)
    assert t.stop == s.candidates[0][2]
    assert t.target == pytest.approx(s.candidates[0][3])
    assert t.exit_reason == exp["exit_reason"]
    assert t.exit_price == pytest.approx(exp["exit_price"], abs=1e-9)
    assert t.net_pnl == pytest.approx(exp["net"], abs=1e-9)
    assert t.gross_pnl == pytest.approx(exp["gross"], abs=1e-9)
    assert t.costs == pytest.approx(exp["cost"], abs=1e-9)
    assert t.r_multiple == pytest.approx(exp["r"], abs=1e-9)
    assert t.holding_bars == exp["holding_bars"]
    k = exp["exit_idx"]
    at_open = exp["exit_reason"] in ("SESSION_END", "DATA_GAP", "STOP_GAP")
    assert t.exit_ts_ns == int(open_ts[k]) + (0 if at_open else BAR_NS)


def test_timestamps_are_strictly_increasing_and_data_gap_is_a_real_time_gap() -> None:
    gap = GOLDEN_SCENARIOS["data_gap"].market()
    ts = bar_open_ts_ns(gap)
    assert all(b > a for a, b in itertools.pairwise(ts))
    # 5 min bars, 55 min missing after bar 5 -> bar 6 opens 60 min after bar 5
    assert (ts[6] - ts[5]) // 1_000_000_000 == 60 * 60
    assert not gap.contig_next[5]


def test_hand_numbers_are_internally_consistent() -> None:
    """gross = net + cost and r = net / (risk * qty) hold in every hand-written expectation."""
    for sid, s in GOLDEN_SCENARIOS.items():
        e = s.expected
        if e["n_trades"] == 0:
            continue
        assert math.isclose(e["gross"], e["net"] + e["cost"], abs_tol=1e-9), sid
        assert math.isclose(e["r"], e["net"] / (e["risk"] * e["qty"]), abs_tol=1e-9), sid
