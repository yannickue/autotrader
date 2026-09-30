"""Lot-level feasibility model (min lot, lot step, leverage cap) from real market specs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LotSizing:
    contract_eur_per_pt: float  # EUR per 1.0 price unit per lot
    notional_eur_per_lot: float  # EUR notional of one lot (leverage base)
    min_lot: float
    lot_step: float
    leverage_cap: float = 10.0  # 10x research cap; 30x is a permitted ceiling, never a target

    def __post_init__(self) -> None:
        if not 0.0 < self.leverage_cap <= 30.0:
            raise ValueError("leverage_cap must be in (0, 30]")
        vals = (self.contract_eur_per_pt, self.notional_eur_per_lot, self.min_lot, self.lot_step)
        if min(vals) <= 0:
            raise ValueError("sizing parameters must be positive")


def lot_sizing_for(canonical: str = "GER40", *, leverage_cap: float = 10.0) -> LotSizing:
    """Real min-lot/step/contract of a market via ``alpha.common.market_costs`` (lazy import)."""
    from alpha.common.market_costs import OBSERVED_LOT_NOTIONAL_EUR, sizing_for
    from markets.spec import load_market_spec

    sz = sizing_for(load_market_spec(canonical), 500.0)
    return LotSizing(
        contract_eur_per_pt=sz.contract_size,
        notional_eur_per_lot=OBSERVED_LOT_NOTIONAL_EUR[canonical],
        min_lot=sz.min_lot,
        lot_step=sz.lot_step,
        leverage_cap=leverage_cap,
    )
