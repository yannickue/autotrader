"""Golden scenarios for the FAST <-> NAUTILUS differential (synthetic, deterministic, OFFLINE ONLY).

Every scenario is a tiny hand-built M5-like OHLC + spread series (16 bars) plus one candidate, with
the FAST-engine result worked out BY HAND (``expected``) so the fast-side normalisation can be
tested independently of ``simulate_fast``. Nothing here is real data.

Common base bar: ``(o, h, l, c) = (100, 100.5, 99.5, 100)``, spread 1.0 price unit, bar OPEN minute
``600 + 5 k`` (same local day), ``contig_next`` all True except the last bar. The candidate decides
at the CLOSE of bar 3 and (FAST) fills at the OPEN of bar 4 (``o[4] == c[3]`` so Nautilus' decision
-close book is the same price: no gap, hence no fill-timing difference unless a scenario wants one).

Default sizing (equity 10 000, risk 0.5 % -> 50 EUR/trade, lot step 0.25, min risk 5 pts) is used.

``data_hash`` / ``candidate_hash`` are sha256 over the raw array bytes; the PINNED_* tables below
fix the expected values so any accidental edit of a scenario is caught by a test.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from alpha.common.sim import CostScenario
from alpha.fast.sim import EXIT_FIXED_R, CandidateArrays, MarketArrays, SimWindow

GOLDEN_VERSION = "golden-v1"
N_BARS = 16
BASE_BAR = (100.0, 100.5, 99.5, 100.0)

_COST_SLIP0 = CostScenario("GOLDEN_SPREAD1_SLIP0", spread_mult=1.0, slippage_pts=0.0)
# 0.01 == one GER40 tick: the only non-zero slippage Nautilus' FillModel can express.
_COST_ONE_TICK = CostScenario("GOLDEN_SPREAD2_SLIP1TICK", spread_mult=1.0, slippage_pts=0.01)


@dataclass(frozen=True)
class GoldenScenario:
    scenario_id: str
    description: str
    bars: tuple[tuple[float, float, float, float], ...]
    spread: tuple[float, ...]
    minute: tuple[int, ...]
    day: tuple[int, ...]
    contig_next: tuple[bool, ...]
    # (decision_idx, direction, stop, target, target_r)
    candidates: tuple[tuple[int, int, float, float, float], ...]
    cost: CostScenario
    window: SimWindow | None
    expected: dict[str, Any]
    version: str = GOLDEN_VERSION
    notes: dict[str, str] = field(default_factory=dict)

    def market(self) -> MarketArrays:
        a = np.asarray(self.bars, dtype=np.float64)
        return MarketArrays(
            a[:, 0],
            a[:, 1],
            a[:, 2],
            a[:, 3],
            np.asarray(self.spread, dtype=np.float64),
            np.asarray(self.minute, dtype=np.int64),
            np.asarray(self.day, dtype=np.int64),
            np.asarray(self.contig_next, dtype=np.bool_),
        )

    def candidate_arrays(self) -> CandidateArrays:
        rows = self.candidates
        return CandidateArrays(
            decision_idx=np.asarray([r[0] for r in rows], dtype=np.int64),
            direction=np.asarray([r[1] for r in rows], dtype=np.int8),
            stop=np.asarray([r[2] for r in rows], dtype=np.float64),
            target=np.asarray([r[3] for r in rows], dtype=np.float64),
            target_r=np.asarray([r[4] for r in rows], dtype=np.float64),
            exit_kind=np.full(len(rows), EXIT_FIXED_R, dtype=np.int8),
        )

    def data_hash(self) -> str:
        m = self.market()
        h = hashlib.sha256()
        h.update(self.version.encode())
        for arr in (m.o, m.h, m.l, m.c, m.spread, m.minute, m.day, m.contig_next):
            h.update(np.ascontiguousarray(arr).tobytes())
        return h.hexdigest()

    def candidate_hash(self) -> str:
        c = self.candidate_arrays()
        h = hashlib.sha256()
        h.update(self.version.encode())
        for arr in (c.decision_idx, c.direction, c.stop, c.target, c.target_r, c.exit_kind):
            h.update(np.ascontiguousarray(arr).tobytes())
        return h.hexdigest()

    def inputs(self) -> tuple[MarketArrays, CandidateArrays, CostScenario, SimWindow | None]:
        """(market, candidates, cost, window) ready for ``simulate_fast`` / ``run_differential``."""
        return self.market(), self.candidate_arrays(), self.cost, self.window


def _build(
    *,
    scenario_id: str,
    description: str,
    overrides: dict[int, tuple[float, float, float, float]] | None = None,
    candidates: tuple[tuple[int, int, float, float, float], ...],
    expected: dict[str, Any],
    cost: CostScenario = _COST_SLIP0,
    spread_value: float = 1.0,
    minute0: int = 600,
    minute_shift_from: tuple[int, int] | None = None,  # (bar index, extra minutes) -> data gap
    contig_break_after: int | None = None,
    window: SimWindow | None = None,
    notes: dict[str, str] | None = None,
) -> GoldenScenario:
    bars = [BASE_BAR] * N_BARS
    for k, row in (overrides or {}).items():
        bars[k] = row
    minute = [minute0 + 5 * k for k in range(N_BARS)]
    if minute_shift_from is not None:
        start, extra = minute_shift_from
        minute = [m + (extra if k >= start else 0) for k, m in enumerate(minute)]
    contig = [True] * N_BARS
    contig[-1] = False
    if contig_break_after is not None:
        contig[contig_break_after] = False
    return GoldenScenario(
        scenario_id=scenario_id,
        description=description,
        bars=tuple(bars),
        spread=(spread_value,) * N_BARS,
        minute=tuple(minute),
        day=(0,) * N_BARS,
        contig_next=tuple(contig),
        candidates=candidates,
        cost=cost,
        window=window,
        expected=expected,
        notes=notes or {},
    )


# --- hand-calculated expectations (FAST engine semantics, see alpha.fast.sim._simulate_kernel) ----
# fill (long)  = o[j] + spread*mult + slip ; fill (short) = o[j] - slip ; risk = |fill - stop|
# qty = floor(50 EUR / risk / 0.25) * 0.25 ; net = pnl_pts * qty
# cost = (spread + n_slip*slip) * qty
# gross = net + cost ; r = net / (risk * qty) ; mfe/mae in R.

_LONG_TARGET = _build(
    scenario_id="long_normal_target",
    description="Long, bar 7 high reaches the 113 target; stop 95 never touched.",
    overrides={7: (100.0, 113.5, 100.0, 113.0)},
    candidates=((3, 1, 95.0, 113.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 7,
        "side": 1,
        "exit_reason": "TARGET",
        "fill": 101.0,
        "exit_price": 113.0,
        "risk": 6.0,
        "qty": 8.25,
        "net": 99.0,
        "cost": 8.25,
        "gross": 107.25,
        "r": 2.0,
        "mfe_r": 2.0,
        "mae_r": 0.25,
        "holding_bars": 4,
    },
)

_SHORT_TARGET = _build(
    scenario_id="short_normal_target",
    description="Short, bar 7 ask-low (87.5 + 1.0) reaches the 89 target; stop 105 never touched.",
    overrides={7: (100.0, 100.5, 87.5, 88.0)},
    candidates=((3, -1, 105.0, 89.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 7,
        "side": -1,
        "exit_reason": "TARGET",
        "fill": 100.0,
        "exit_price": 89.0,
        "risk": 5.0,
        "qty": 10.0,
        "net": 110.0,
        "cost": 10.0,
        "gross": 120.0,
        "r": 2.2,
        "mfe_r": 2.2,
        "mae_r": 0.3,
        "holding_bars": 4,
    },
)

_LONG_STOP = _build(
    scenario_id="long_normal_stop",
    description="Long, bar 6 low 94 trades through the 95 stop (open 100 is above it: no gap).",
    overrides={6: (100.0, 100.5, 94.0, 96.0)},
    candidates=((3, 1, 95.0, 113.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 6,
        "side": 1,
        "exit_reason": "STOP",
        "fill": 101.0,
        "exit_price": 95.0,
        "risk": 6.0,
        "qty": 8.25,
        "net": -49.5,
        "cost": 8.25,
        "gross": -41.25,
        "r": -1.0,
        "mfe_r": 0.0,
        "mae_r": 7.0 / 6.0,
        "holding_bars": 3,
    },
)

_SHORT_STOP = _build(
    scenario_id="short_normal_stop",
    description="Short, bar 6 ask-high (106 + 1.0) trades through the 105 stop.",
    overrides={6: (100.0, 106.0, 99.5, 104.0)},
    candidates=((3, -1, 105.0, 89.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 6,
        "side": -1,
        "exit_reason": "STOP",
        "fill": 100.0,
        "exit_price": 105.0,
        "risk": 5.0,
        "qty": 10.0,
        "net": -50.0,
        "cost": 10.0,
        "gross": -40.0,
        "r": -1.0,
        "mfe_r": 0.0,
        "mae_r": 1.4,
        "holding_bars": 3,
    },
)

_GAP_STOP = _build(
    scenario_id="gap_through_stop",
    description="Long, bar 5 OPENS at 90, far below the 95 stop: exit at the gap open.",
    overrides={5: (90.0, 91.0, 89.0, 90.5)},
    candidates=((3, 1, 95.0, 113.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 5,
        "side": 1,
        "exit_reason": "STOP_GAP",
        "fill": 101.0,
        "exit_price": 90.0,
        "risk": 6.0,
        "qty": 8.25,
        "net": -90.75,
        "cost": 8.25,
        "gross": -82.5,
        "r": -90.75 / 49.5,
        "mfe_r": 0.0,
        "mae_r": 2.0,
        "holding_bars": 2,
    },
)

_TARGET_CROSSED = _build(
    scenario_id="target_crossed_at_fill",
    description="Long with a finite target (100.5) below the actual fill (101): candidate skipped.",
    candidates=((3, 1, 95.0, 100.5, 2.0),),
    expected={"n_trades": 0, "skips": {"target_crossed_at_fill": 1}},
)

_SESSION_END = _build(
    scenario_id="session_end",
    description="Long still open when bar 6 (minute 1290) reaches forced-flat: exit at that open.",
    minute0=1260,
    window=SimWindow(540, 1285, 1290),
    candidates=((3, 1, 95.0, 113.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 6,
        "side": 1,
        "exit_reason": "SESSION_END",
        "fill": 101.0,
        "exit_price": 100.0,
        "risk": 6.0,
        "qty": 8.25,
        "net": -8.25,
        "cost": 8.25,
        "gross": 0.0,
        "r": -8.25 / 49.5,
        "mfe_r": 0.0,
        "mae_r": 0.25,
        "holding_bars": 2,
    },
)

_DATA_GAP = _build(
    scenario_id="data_gap",
    description="Long; 55 minutes of data are missing after bar 5, bar 6 re-opens at 98.",
    overrides={k: (98.0, 98.5, 97.5, 98.0) for k in range(6, N_BARS)},
    minute_shift_from=(6, 55),
    contig_break_after=5,
    candidates=((3, 1, 95.0, 113.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 6,
        "side": 1,
        "exit_reason": "DATA_GAP",
        "fill": 101.0,
        "exit_price": 98.0,
        "risk": 6.0,
        "qty": 8.25,
        "net": -24.75,
        "cost": 8.25,
        "gross": -16.5,
        "r": -0.5,
        "mfe_r": 0.0,
        "mae_r": 0.25,
        "holding_bars": 2,
    },
)

_COST_TARGET = _build(
    scenario_id="spread_slippage_cost_target",
    description="Spread 2.0 + one-tick (0.01) slippage; long target: slippage on the entry only.",
    overrides={7: (100.0, 113.5, 100.0, 113.0)},
    spread_value=2.0,
    cost=_COST_ONE_TICK,
    candidates=((3, 1, 95.0, 113.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 7,
        "side": 1,
        "exit_reason": "TARGET",
        "fill": 102.01,
        "exit_price": 113.0,
        "risk": 7.01,
        "qty": 7.0,
        "net": 76.93,
        "cost": 14.07,
        "gross": 91.0,
        "r": 76.93 / (7.01 * 7.0),
        "mfe_r": 10.99 / 7.01,
        "mae_r": 2.51 / 7.01,
        "holding_bars": 4,
    },
)

_COST_STOP = _build(
    scenario_id="spread_slippage_cost_stop",
    description="Spread 2.0 + one-tick slippage; long stop: slippage on entry AND stop exit.",
    overrides={6: (100.0, 100.5, 94.0, 96.0)},
    spread_value=2.0,
    cost=_COST_ONE_TICK,
    candidates=((3, 1, 95.0, 113.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 6,
        "side": 1,
        "exit_reason": "STOP",
        "fill": 102.01,
        "exit_price": 94.99,
        "risk": 7.01,
        "qty": 7.0,
        "net": -49.14,
        "cost": 14.14,
        "gross": -35.0,
        "r": -7.02 / 7.01,
        "mfe_r": 0.0,
        "mae_r": 8.01 / 7.01,
        "holding_bars": 3,
    },
)

_LONG_BOTH = _build(
    scenario_id="long_stop_and_target_same_bar",
    description="Long; bar 6 touches BOTH the 113 target (high) and the 95 stop (low).",
    overrides={6: (100.0, 113.5, 94.0, 100.0)},
    candidates=((3, 1, 95.0, 113.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 6,
        "side": 1,
        "exit_reason": "STOP",
        "fill": 101.0,
        "exit_price": 95.0,
        "risk": 6.0,
        "qty": 8.25,
        "net": -49.5,
        "cost": 8.25,
        "gross": -41.25,
        "r": -1.0,
        "mfe_r": 0.0,
        "mae_r": 7.0 / 6.0,
        "holding_bars": 3,
    },
    notes={"nautilus": "ticks O,H,L,C: the long target (H) fills before the stop (L)"},
)

_SHORT_BOTH = _build(
    scenario_id="short_stop_and_target_same_bar",
    description="Short; bar 6 touches BOTH the 105 stop (ask high) and 89 target (ask low).",
    overrides={6: (100.0, 106.0, 87.5, 100.0)},
    candidates=((3, -1, 105.0, 89.0, 2.0),),
    expected={
        "n_trades": 1,
        "decision_idx": 3,
        "entry_idx": 4,
        "exit_idx": 6,
        "side": -1,
        "exit_reason": "STOP",
        "fill": 100.0,
        "exit_price": 105.0,
        "risk": 5.0,
        "qty": 10.0,
        "net": -50.0,
        "cost": 10.0,
        "gross": -40.0,
        "r": -1.0,
        "mfe_r": 0.0,
        "mae_r": 1.4,
        "holding_bars": 3,
    },
    notes={"nautilus": "ticks O,H,L,C: the short stop (ask-H) fills before the target (ask-L)"},
)

GOLDEN_SCENARIOS: dict[str, GoldenScenario] = {
    s.scenario_id: s
    for s in (
        _LONG_TARGET,
        _SHORT_TARGET,
        _LONG_STOP,
        _SHORT_STOP,
        _GAP_STOP,
        _TARGET_CROSSED,
        _SESSION_END,
        _DATA_GAP,
        _COST_TARGET,
        _COST_STOP,
        _LONG_BOTH,
        _SHORT_BOTH,
    )
}

# Pinned sha256 of each scenario's arrays (see ``GoldenScenario.data_hash`` / ``candidate_hash``).
PINNED_DATA_HASH: dict[str, str] = {
    "long_normal_target": ("a42eebf5cade0a6079ba78b75797fc593f69bdbc6f2184eb38b940e5764a0679"),
    "short_normal_target": ("3965d6bb5aabba7cfcf6f35eba0cfffadb6dc718acbbdb5aa09809d20fe4a731"),
    "long_normal_stop": ("51e0d1d75050dddb18c2bfd51c35421793ebc1224462123e90edc6076438f3c3"),
    "short_normal_stop": ("b1183012caa6a4af5ab7de32e319433f788795e0aad3c03781818526c869e56c"),
    "gap_through_stop": ("9ed759d1690447b243d186457fea017e6820aa4a30b813a5c6c47d3f675984b3"),
    "target_crossed_at_fill": ("00be33fbbed59d8d0d85989a215fd59cc3083c6f4f60d11f887fdf382c0bbe17"),
    "session_end": ("28fc6a0c8dfa96aa283c89546f4a14a4a9ed402df24b24dfe92a0988b5d8c934"),
    "data_gap": ("a3ff00b5dc919034535f7fa7f36f5c7ab2ab6c87a7b0a9948ab0907484bc548d"),
    "spread_slippage_cost_target": (
        "80626e35d2caed79c3a972a2b1a52afa0c73568f0f6bf80d38d114ef6c7320db"
    ),
    "spread_slippage_cost_stop": (
        "6c6aa643ed003a9680125be7b8b9367ffc492ca7eaa9db6dd245d8b835e06a1e"
    ),
    "long_stop_and_target_same_bar": (
        "0e128748a1d20351418fee2585d9814b14656905f23be881c177ee56696aa49b"
    ),
    "short_stop_and_target_same_bar": (
        "cdb101dc038e2ef5249ff8b76b1015e597de1eb0cd5369fb61c1da59c580fb7f"
    ),
}
PINNED_CANDIDATE_HASH: dict[str, str] = {
    "long_normal_target": ("bba6bd81862a47cc3d1396b6c8bf8fa0178542603f6fdd76247a05c8188bf6f6"),
    "short_normal_target": ("d6ed7b61baa2491cc277ff95f109dc994dc3a37abcb0f47c9f361fde33b3c927"),
    "long_normal_stop": ("bba6bd81862a47cc3d1396b6c8bf8fa0178542603f6fdd76247a05c8188bf6f6"),
    "short_normal_stop": ("d6ed7b61baa2491cc277ff95f109dc994dc3a37abcb0f47c9f361fde33b3c927"),
    "gap_through_stop": ("bba6bd81862a47cc3d1396b6c8bf8fa0178542603f6fdd76247a05c8188bf6f6"),
    "target_crossed_at_fill": ("6852636ecce17472b7e73f100c595e127876a69ec14e26b37a36fd36994f0ae8"),
    "session_end": ("bba6bd81862a47cc3d1396b6c8bf8fa0178542603f6fdd76247a05c8188bf6f6"),
    "data_gap": ("bba6bd81862a47cc3d1396b6c8bf8fa0178542603f6fdd76247a05c8188bf6f6"),
    "spread_slippage_cost_target": (
        "bba6bd81862a47cc3d1396b6c8bf8fa0178542603f6fdd76247a05c8188bf6f6"
    ),
    "spread_slippage_cost_stop": (
        "bba6bd81862a47cc3d1396b6c8bf8fa0178542603f6fdd76247a05c8188bf6f6"
    ),
    "long_stop_and_target_same_bar": (
        "bba6bd81862a47cc3d1396b6c8bf8fa0178542603f6fdd76247a05c8188bf6f6"
    ),
    "short_stop_and_target_same_bar": (
        "d6ed7b61baa2491cc277ff95f109dc994dc3a37abcb0f47c9f361fde33b3c927"
    ),
}
