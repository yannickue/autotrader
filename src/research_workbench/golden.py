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


def array_digest_update(h: Any, name: str, arr: np.ndarray) -> None:
    """Feed a CANONICAL encoding of ``arr`` into hash ``h``: name, dtype string, shape and the
    little-endian C-contiguous bytes (independent of the platform byte order / memory layout)."""
    a = np.ascontiguousarray(arr)
    le = a.astype(a.dtype.newbyteorder("<"), copy=False)
    h.update(f"|{name}|{le.dtype.str}|{le.shape!r}|".encode())
    h.update(le.tobytes())


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
        named = (
            ("o", m.o), ("h", m.h), ("l", m.l), ("c", m.c),
            ("spread", m.spread), ("minute", m.minute), ("day", m.day),
            ("contig_next", m.contig_next),
        )  # fmt: skip
        for name, arr in named:
            array_digest_update(h, name, arr)
        return h.hexdigest()

    def candidate_hash(self) -> str:
        c = self.candidate_arrays()
        h = hashlib.sha256()
        h.update(self.version.encode())
        named = (
            ("decision_idx", c.decision_idx), ("direction", c.direction), ("stop", c.stop),
            ("target", c.target), ("target_r", c.target_r), ("exit_kind", c.exit_kind),
        )  # fmt: skip
        for name, arr in named:
            array_digest_update(h, name, arr)
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
    "long_normal_target": ("57d955d052ad915b2df83dadfaa1338e1e33013dae2d2354855b7a833f4d96fc"),
    "short_normal_target": ("ba5a21a0958d37952cca2613327b89ad2fc4b1e64816576471f4ce9c27de867a"),
    "long_normal_stop": ("5c2588206bf7bf2c772143f10d1ba9e1ff8afcfd4c4a8652204a78bcddd587e5"),
    "short_normal_stop": ("247229ccb3d3a96896daeed65a83ae32c24de812bc57e21cd90f563424027e60"),
    "gap_through_stop": ("bd7d4ba83db421d068019ec9ffca83b8d38adf57c7871558e31ba7ac7ef859dc"),
    "target_crossed_at_fill": ("d7626115e7265688b5a65a7a7ef198a84d474899e610d3292b7258b8fee63382"),
    "session_end": ("742fcb790ed9d9d34d817b024711b4ad0b16c5a79342c3a9e9e10a66de878c23"),
    "data_gap": ("756cb5c104f13eb1d44bd6391dbec34ac13b96081ece026fba4f11e269905d7d"),
    "spread_slippage_cost_target": (
        "c583fbe61c9c25bf166f5218abb9de1805a4add6782e0a12f4029d6be4b3e30e"
    ),
    "spread_slippage_cost_stop": (
        "aeb27d1e36f266f927558e13c6f23c920256d8189e0bc196997819f552944b79"
    ),
    "long_stop_and_target_same_bar": (
        "042cff1c3d23ff60b01f09ad5da70ed4067ef1552cc8e200f8a162dd5ae47d21"
    ),
    "short_stop_and_target_same_bar": (
        "c587fc6142ddf9925c2d605fd16e066de297b738515933c60138397a2b5a98b9"
    ),
}
PINNED_CANDIDATE_HASH: dict[str, str] = {
    "long_normal_target": ("9ef8ac9df27b317922981bff426d4aa90cafeb818f5bcf61e6e7b2563bc1ec20"),
    "short_normal_target": ("b754b5bee7206daa0e08022ee00eaa3f642cae4480cc3a21706e0a008da87174"),
    "long_normal_stop": ("9ef8ac9df27b317922981bff426d4aa90cafeb818f5bcf61e6e7b2563bc1ec20"),
    "short_normal_stop": ("b754b5bee7206daa0e08022ee00eaa3f642cae4480cc3a21706e0a008da87174"),
    "gap_through_stop": ("9ef8ac9df27b317922981bff426d4aa90cafeb818f5bcf61e6e7b2563bc1ec20"),
    "target_crossed_at_fill": ("7fef748ca8ce574bba9663742fd1ea7461ea23a9ef3720b1d982d5f71da51d9b"),
    "session_end": ("9ef8ac9df27b317922981bff426d4aa90cafeb818f5bcf61e6e7b2563bc1ec20"),
    "data_gap": ("9ef8ac9df27b317922981bff426d4aa90cafeb818f5bcf61e6e7b2563bc1ec20"),
    "spread_slippage_cost_target": (
        "9ef8ac9df27b317922981bff426d4aa90cafeb818f5bcf61e6e7b2563bc1ec20"
    ),
    "spread_slippage_cost_stop": (
        "9ef8ac9df27b317922981bff426d4aa90cafeb818f5bcf61e6e7b2563bc1ec20"
    ),
    "long_stop_and_target_same_bar": (
        "9ef8ac9df27b317922981bff426d4aa90cafeb818f5bcf61e6e7b2563bc1ec20"
    ),
    "short_stop_and_target_same_bar": (
        "b754b5bee7206daa0e08022ee00eaa3f642cae4480cc3a21706e0a008da87174"
    ),
}
