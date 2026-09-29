"""Map the REAL MT5 `symbol_info` snapshot of GER40 into a Nautilus `Cfd`.

Every financially meaningful field comes from the broker snapshot
(`tests/fixtures/ger40/symbol_info.json`, captured live from the ActivTrades
demo). Fields Nautilus needs that the broker did NOT provide are listed in
`INSTRUMENT_ASSUMPTIONS` -- explicit, never silently invented:

- margin_init / margin_maint: broker reported 0.0 ("not provided"); 5% assumed
  (ESMA retail index leverage 1:20). Higher margin = more conservative.
- maker/taker fee: 0. ActivTrades index CFDs are spread-priced; no commission
  observed in symbol_info. UNVERIFIED for the live account.
- Swap/financing: NOT modelled (swap_long -5.445 / swap_short -0.555 observed;
  the proof strategy is intraday-flat).
- contract size must be 1.0 (Nautilus `Cfd` has no multiplier); other values
  are rejected fail-closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import Cfd
from nautilus_trader.model.objects import Currency, Price, Quantity

from adapters.activtrades_mt5.models import (
    symbol_info_raw_from_mt5,
    symbol_info_to_instrument_spec,
)
from instruments.models import InstrumentSpec

VENUE = Venue("ACTIVTRADES")
INSTRUMENT_ID = InstrumentId(Symbol("GER40"), VENUE)

INSTRUMENT_ASSUMPTIONS: dict[str, str] = {
    "margin_init": "0.05 (broker margin_initial=0.0 not provided; ESMA 1:20 index CFD assumed)",
    "margin_maint": "0.05 (conservatively equal to initial)",
    "fees": "0 commission (spread-priced CFD; unverified for live account)",
    "swap": "not modelled (intraday-flat proof strategy)",
    "contract_size": "1.0 required (broker-reported); Nautilus Cfd has no multiplier",
    "asset_class": "INDEX (broker path 'Cash Indices\\Ger40')",
    "min_max_notional_price": "not provided by broker -> unset",
    "trade_calc_mode": "4 (CFD leverage) observed; margin modelled with StandardMarginModel",
}

MARGIN_INIT = Decimal("0.05")
MARGIN_MAINT = Decimal("0.05")


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolInfoSnapshot:
    retrieved_at: datetime
    account_kind: str
    spec: InstrumentSpec


def load_symbol_info_snapshot(path: Path | str, *, canonical: str = "GER40") -> SymbolInfoSnapshot:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = symbol_info_raw_from_mt5(SimpleNamespace(**payload["symbol_info"]))
    retrieved_at = datetime.fromisoformat(payload["retrieved_at"]).astimezone(UTC)
    spec = symbol_info_to_instrument_spec(
        raw, canonical_symbol=canonical, retrieved_at=retrieved_at
    )
    return SymbolInfoSnapshot(
        retrieved_at=retrieved_at, account_kind=str(payload["account_kind"]), spec=spec
    )


def _decimals(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -int(exponent))


def spec_to_nautilus_cfd(
    spec: InstrumentSpec, *, instrument_id: InstrumentId = INSTRUMENT_ID
) -> Cfd:
    """Build the Nautilus `Cfd` from the broker-derived `InstrumentSpec`."""
    if spec.trade_contract_size != 1:
        raise ValueError(
            f"contract size {spec.trade_contract_size} unsupported (Nautilus Cfd has no multiplier)"
        )
    if not spec.is_tradable:
        raise ValueError("instrument is not tradable per broker trade_mode")
    if spec.currency_margin != spec.currency_profit:
        raise ValueError("margin and profit currencies differ; not modelled")
    price_precision = spec.digits
    size_precision = _decimals(spec.volume_step)
    ts = 0
    return Cfd(
        instrument_id=instrument_id,
        raw_symbol=Symbol(spec.broker_symbol),
        asset_class=AssetClass.INDEX,
        quote_currency=Currency.from_str(spec.currency_profit),
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=Price(float(spec.trade_tick_size), price_precision),
        size_increment=Quantity(float(spec.volume_step), size_precision),
        ts_event=ts,
        ts_init=ts,
        min_quantity=Quantity(float(spec.volume_min), size_precision),
        max_quantity=Quantity(float(spec.volume_max), size_precision),
        margin_init=MARGIN_INIT,
        margin_maint=MARGIN_MAINT,
        maker_fee=Decimal(0),
        taker_fee=Decimal(0),
        info={
            "broker_symbol": spec.broker_symbol,
            "description": spec.description,
            "source": spec.source,
            "snapshot_retrieved_at": spec.retrieved_at.isoformat(),
            "point": str(spec.point),
            "contract_size": str(spec.trade_contract_size),
            "stop_level_price": str(spec.stop_level),
            "margin_calculation_mode": str(spec.margin_calculation_mode),
            "quality_flags": list(spec.quality_flags),
            "assumptions": INSTRUMENT_ASSUMPTIONS,
        },
    )
