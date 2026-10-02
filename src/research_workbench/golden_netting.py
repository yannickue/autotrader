"""Netting / non-flat golden scenarios (added after the SYN_E2E diagnosis). OFFLINE ONLY.

The base scenarios in :mod:`research_workbench.golden` use FLAT prices, which cannot tell "fills
against the decision bar close" from "fills against the PREVIOUS bar close" (the replay defect
found on SYN_E2E). These scenarios trend / gap so that every such confusion changes a number.
``expected["trades"]`` holds one hand-calculated dict per FAST trade (same keys as the single-trade
expectations in ``golden.py``).
"""

from __future__ import annotations

from research_workbench.golden import N_BARS, GoldenScenario, _build


def _trend_rows() -> dict[int, tuple[float, float, float, float]]:
    """close_k = 100 + k, open_k = close_{k-1} (open_0 = 99): never flat, never gapped."""
    rows: dict[int, tuple[float, float, float, float]] = {}
    prev = 99.0
    for k in range(N_BARS):
        c = 100.0 + k
        rows[k] = (prev, max(prev, c) + 0.2, min(prev, c) - 0.2, c)
        prev = c
    return rows


def _same_bar_rows() -> dict[int, tuple[float, float, float, float]]:
    rows = {k: (100.0, 100.5, 99.5, 100.0) for k in range(6)}
    rows[6] = (100.0, 100.5, 94.0, 97.0)  # trade A stops out at 95 inside bar 6; close 97
    rows[7] = (97.0, 97.5, 96.5, 97.5)  # trade B fills here: o[7] = c[6] = 97
    rows[8] = (97.5, 98.0, 97.0, 97.5)
    rows[9] = (97.5, 111.0, 97.0, 110.5)  # trade B target 110 inside bar 9
    for k in range(10, N_BARS):
        rows[k] = (110.5, 111.0, 110.0, 110.5)
    return rows


_TREND_ENTRY = _build(
    scenario_id="entry_on_trending_close",
    description="Trending bars (close_k = 100 + k): the fill must use the DECISION bar close, "
    "not the previous bar close.",
    overrides=_trend_rows(),
    candidates=((5, 1, 90.0, 110.0, 2.0),),
    expected={
        "n_trades": 1,
        "trades": [
            {
                "decision_idx": 5, "entry_idx": 6, "exit_idx": 10, "side": 1,
                "exit_reason": "TARGET", "fill": 106.0, "exit_price": 110.0, "risk": 16.0,
                "qty": 3.0, "net": 12.0, "cost": 3.0, "gross": 15.0, "r": 0.25, "mfe_r": 0.25,
                "mae_r": 1.2 / 16.0, "holding_bars": 5,
            }
        ],
    },
)  # fmt: skip

_SAME_BAR = _build(
    scenario_id="same_bar_exit_and_reentry",
    description="Trade A stops out INSIDE bar 6; the next candidate decides at the CLOSE of bar 6 "
    "(FAST admits decision_idx == previous exit_idx) and fills at o[7].",
    overrides=_same_bar_rows(),
    candidates=((3, 1, 95.0, 113.0, 2.0), (6, 1, 90.0, 110.0, 2.0)),
    expected={
        "n_trades": 2,
        "trades": [
            {
                "decision_idx": 3, "entry_idx": 4, "exit_idx": 6, "side": 1,
                "exit_reason": "STOP", "fill": 101.0, "exit_price": 95.0, "risk": 6.0,
                "qty": 8.25, "net": -49.5, "cost": 8.25, "gross": -41.25, "r": -1.0,
                "mfe_r": 0.0, "mae_r": 7.0 / 6.0, "holding_bars": 3,
            },
            {
                "decision_idx": 6, "entry_idx": 7, "exit_idx": 9, "side": 1,
                "exit_reason": "TARGET", "fill": 98.0, "exit_price": 110.0, "risk": 8.0,
                "qty": 6.25, "net": 75.0, "cost": 6.25, "gross": 81.25, "r": 1.5, "mfe_r": 1.5,
                "mae_r": 1.5 / 8.0, "holding_bars": 3,
            },
        ],
    },
)  # fmt: skip

_OVERLAP = _build(
    scenario_id="overlapping_candidate_dropped",
    description="The second candidate decides while trade A is still open (bar 5 < exit bar 7): "
    "FAST drops it silently (one position at a time); the replay is never asked to take it.",
    overrides={7: (100.0, 113.5, 100.0, 113.0)},
    candidates=((3, 1, 95.0, 113.0, 2.0), (5, 1, 94.0, 120.0, 2.0)),
    expected={
        "n_trades": 1,
        "trades": [
            {
                "decision_idx": 3, "entry_idx": 4, "exit_idx": 7, "side": 1,
                "exit_reason": "TARGET", "fill": 101.0, "exit_price": 113.0, "risk": 6.0,
                "qty": 8.25, "net": 99.0, "cost": 8.25, "gross": 107.25, "r": 2.0,
                "mfe_r": 2.0, "mae_r": 0.25, "holding_bars": 4,
            }
        ],
        "candidates_blocked_by_open_position": 1,
    },
)  # fmt: skip

NETTING_SCENARIOS: dict[str, GoldenScenario] = {
    s.scenario_id: s for s in (_TREND_ENTRY, _SAME_BAR, _OVERLAP)
}
