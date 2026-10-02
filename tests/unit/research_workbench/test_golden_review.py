"""Review round 1: canonical hashing and an INDEPENDENT sizing oracle for the golden scenarios."""

from __future__ import annotations

import hashlib
from decimal import ROUND_FLOOR, Decimal

import numpy as np
import pytest

from alpha.common.sim import DEFAULT_SIZING, SizingSpec
from alpha.fast.sim import simulate_fast
from research_workbench.golden import GOLDEN_SCENARIOS, array_digest_update

_SIZED = [s for s in sorted(GOLDEN_SCENARIOS) if GOLDEN_SCENARIOS[s].expected["n_trades"]]


def _digest(name: str, arr: np.ndarray) -> str:
    h = hashlib.sha256()
    array_digest_update(h, name, arr)
    return h.hexdigest()


def test_canonical_hash_is_sensitive_to_dtype_shape_and_name_but_not_byte_order() -> None:
    base = np.arange(6, dtype=np.float64)
    assert _digest("a", base) == _digest("a", base.copy())
    assert _digest("a", base) == _digest("a", base.astype(">f8"))  # byte-order independent
    assert _digest("a", base) != _digest("a", base.astype(np.float32))  # dtype sensitive
    assert _digest("a", base.reshape(2, 3)) != _digest("a", base.reshape(3, 2))  # shape sensitive
    assert _digest("a", base.reshape(2, 3)) != _digest("a", base)  # rank sensitive
    assert _digest("a", base) != _digest("b", base)  # name sensitive
    fortran = np.asfortranarray(base.reshape(2, 3))
    assert _digest("a", fortran) == _digest("a", base.reshape(2, 3))  # layout independent


def oracle_qty(sizing: SizingSpec, fill: float, stop: float) -> float:
    """Independent sizing oracle: Decimal, written from the SizingSpec definition (not sim.py)."""
    d = Decimal
    risk = abs(d(str(fill)) - d(str(stop)))
    step, cs, eq = d(str(sizing.lot_step)), d(str(sizing.contract_size)), d(str(sizing.equity_eur))
    by_risk = (eq * d(str(sizing.risk_fraction)) / (risk * cs) / step).to_integral_value(
        ROUND_FLOOR
    )
    by_cap = (d(str(sizing.max_leverage)) * eq / (d(str(fill)) * cs) / step).to_integral_value(
        ROUND_FLOOR
    )
    qty = min(by_risk, by_cap) * step
    return float(qty) if qty >= d(str(sizing.min_lot)) else 0.0


@pytest.mark.parametrize("sid", _SIZED)
def test_fast_quantity_matches_independent_sizing_oracle(sid: str) -> None:
    s = GOLDEN_SCENARIOS[sid]
    market, cands, cost, window = s.inputs()
    trades = simulate_fast(market, cands, cost, window=window)
    oracle = oracle_qty(DEFAULT_SIZING, float(trades.entry_price[0]), s.candidates[0][2])
    assert oracle == s.expected["qty"]  # hand-calculated number == oracle
    assert float(trades.qty[0]) == oracle  # simulate_fast sizing == oracle; the replay is not used


def test_sizing_oracle_leverage_cap_and_minimum_lot() -> None:
    tight = SizingSpec(equity_eur=10_000.0, risk_fraction=0.5, max_leverage=1.0)
    assert oracle_qty(tight, 100.0, 95.0) == 100.0  # risk-sized 1000 lots, capped at 1x notional
    assert oracle_qty(SizingSpec(risk_fraction=0.00001), 100.0, 95.0) == 0.0  # below min lot
    market, cands, cost, window = GOLDEN_SCENARIOS["long_normal_target"].inputs()
    capped = simulate_fast(market, cands, cost, sizing=tight, window=window)
    assert float(capped.qty[0]) == oracle_qty(tight, float(capped.entry_price[0]), 95.0)
