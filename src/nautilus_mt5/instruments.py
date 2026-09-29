"""Nautilus `InstrumentProvider` for ActivTrades MT5 (GER40 first).

Every financially meaningful field comes from the broker's live `symbol_info`.
What MT5 does NOT report but Nautilus needs (margin rates, fees) is injected
through an explicit `InstrumentAssumptions` and tagged in `Cfd.info` with its
source ("ASSUMED"), so it can never be mistaken for broker truth. C4's
assumptions are NOT inherited implicitly: callers must pass them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.enums import AssetClass
from nautilus_trader.model.identifiers import InstrumentId, Symbol
from nautilus_trader.model.instruments import Cfd
from nautilus_trader.model.objects import Currency, Price, Quantity

from adapters.activtrades_mt5.models import (
    symbol_info_raw_from_mt5,
    symbol_info_to_instrument_spec,
)
from instruments.models import InstrumentSpec
from nautilus_mt5.symbols import SymbolMapping, SymbolRegistry, default_registry


class Mt5InstrumentError(RuntimeError):
    """Instrument could not be built from broker metadata (fail closed)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentAssumptions:
    """Values Nautilus requires that MT5 symbol_info does not provide."""

    margin_init: Decimal
    margin_maint: Decimal
    maker_fee: Decimal = Decimal(0)
    taker_fee: Decimal = Decimal(0)
    source: str = "ASSUMED"  # provenance tag written into Cfd.info

    def __post_init__(self) -> None:
        for name in ("margin_init", "margin_maint", "maker_fee", "taker_fee"):
            if not getattr(self, name).is_finite() or getattr(self, name) < 0:
                raise ValueError(f"{name} must be finite and non-negative")


def _decimals(value: Decimal) -> int:
    return max(0, -int(value.normalize().as_tuple().exponent))


def build_cfd(
    spec: InstrumentSpec, mapping: SymbolMapping, assumptions: InstrumentAssumptions
) -> Cfd:
    if spec.trade_contract_size != 1:
        raise Mt5InstrumentError(f"contract size {spec.trade_contract_size} unsupported (need 1)")
    if not spec.is_tradable:
        raise Mt5InstrumentError(f"{spec.broker_symbol} is not tradable per broker trade_mode")
    if spec.currency_margin != spec.currency_profit:
        raise Mt5InstrumentError("margin and profit currencies differ; not modelled")
    price_precision = spec.digits
    size_precision = _decimals(spec.volume_step)
    ts = int(spec.retrieved_at.timestamp() * 1_000_000_000)
    return Cfd(
        instrument_id=mapping.instrument_id,
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
        margin_init=assumptions.margin_init,
        margin_maint=assumptions.margin_maint,
        maker_fee=assumptions.maker_fee,
        taker_fee=assumptions.taker_fee,
        info={
            "broker_symbol": spec.broker_symbol,
            "description": spec.description,
            "source": spec.source,
            "snapshot_retrieved_at": spec.retrieved_at.isoformat(),
            "point": str(spec.point),
            "stop_level_price": str(spec.stop_level) if spec.stop_level is not None else None,
            "freeze_level_price": str(spec.freeze_level) if spec.freeze_level is not None else None,
            "filling_modes": [str(m) for m in spec.filling_modes],
            "execution_mode": str(spec.execution_mode),
            "margin_calculation_mode": str(spec.margin_calculation_mode),
            "broker_margin_initial": (
                str(spec.margin_initial) if spec.margin_initial is not None else None
            ),
            "margin_source": assumptions.source,
            "fee_source": assumptions.source,
            "quality_flags": list(spec.quality_flags),
        },
    )


class Mt5InstrumentProvider(InstrumentProvider):
    def __init__(
        self,
        client: Any,
        *,
        assumptions: InstrumentAssumptions,
        registry: SymbolRegistry | None = None,
        config: InstrumentProviderConfig | None = None,
        now: Any = None,
    ) -> None:
        super().__init__(config=config)
        self._client = client
        self._assumptions = assumptions
        self._registry = registry or default_registry()
        self._now = now or (lambda: datetime.now(UTC))
        self.specs: dict[InstrumentId, InstrumentSpec] = {}

    @property
    def registry(self) -> SymbolRegistry:
        return self._registry

    def spec(self, instrument_id: InstrumentId) -> InstrumentSpec:
        return self.specs[instrument_id]

    def _load_mapping(self, mapping: SymbolMapping) -> None:
        info = self._client.symbol_info(mapping.broker_symbol)
        if info is None:
            raise Mt5InstrumentError(f"broker has no symbol {mapping.broker_symbol!r}")
        if getattr(info, "name", None) != mapping.broker_symbol:
            raise Mt5InstrumentError(
                f"broker returned {getattr(info, 'name', None)!r} for {mapping}"
            )
        path = str(getattr(info, "path", ""))
        if not path.startswith(mapping.expected_path_prefix):
            raise Mt5InstrumentError(
                f"{mapping.broker_symbol!r} path {path!r} not under "
                f"{mapping.expected_path_prefix!r}"
            )
        spec = symbol_info_to_instrument_spec(
            symbol_info_raw_from_mt5(info),
            canonical_symbol=mapping.canonical,
            retrieved_at=self._now().astimezone(UTC),
        )
        instrument = build_cfd(spec, mapping, self._assumptions)
        self.specs[instrument.id] = spec
        self.add(instrument)

    async def load_all_async(self, filters: dict | None = None) -> None:
        for mapping in self._registry.all():
            self._load_mapping(mapping)

    async def load_ids_async(
        self, instrument_ids: list[InstrumentId], filters: dict | None = None
    ) -> None:
        for instrument_id in instrument_ids:
            self._load_mapping(self._registry.by_instrument_id(instrument_id))

    async def load_async(self, instrument_id: InstrumentId, filters: dict | None = None) -> None:
        await self.load_ids_async([instrument_id], filters)
