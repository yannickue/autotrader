"""Public Binance USD-M instrument and market-data normalization."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.request import Request, urlopen

from data.models import DataQuality, MarketSnapshot

BINANCE_USDM_BASE_URL = "https://fapi.binance.com"
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"

JsonObject = Mapping[str, Any]
JsonReader = Callable[[str], object]


def _as_decimal(value: object, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"{field} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _require_positive(value: Decimal, *, field: str) -> None:
    if value <= 0:
        raise ValueError(f"{field} must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class InstrumentRules:
    """Order increments and minimums published by Binance for one perpetual."""

    instrument: str
    venue_symbol: str
    base_asset: str
    quote_asset: str
    margin_asset: str
    status: str
    price_tick: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    market_quantity_step: Decimal

    def __post_init__(self) -> None:
        for field_name in (
            "instrument",
            "venue_symbol",
            "base_asset",
            "quote_asset",
            "margin_asset",
            "status",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} cannot be empty")
        for field_name in (
            "price_tick",
            "quantity_step",
            "min_quantity",
            "min_notional",
            "market_quantity_step",
        ):
            value = getattr(self, field_name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{field_name} must be finite and positive")

    def floor_price(self, price: Decimal) -> Decimal:
        """Floor a price to the venue tick without converting through float."""
        return self._floor_to_increment(price, self.price_tick, field="price")

    def floor_quantity(self, quantity: Decimal, *, market: bool = False) -> Decimal:
        """Floor a quantity to the applicable limit or market lot increment."""
        increment = self.market_quantity_step if market else self.quantity_step
        return self._floor_to_increment(quantity, increment, field="quantity")

    def meets_minimums(self, *, price: Decimal, quantity: Decimal) -> bool:
        """Return whether raw order values satisfy quantity and notional minimums."""
        if not price.is_finite() or not quantity.is_finite():
            return False
        return quantity >= self.min_quantity and price * quantity >= self.min_notional

    @staticmethod
    def _floor_to_increment(value: Decimal, increment: Decimal, *, field: str) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError(f"{field} must be finite and non-negative")
        return ((value // increment) * increment).quantize(increment)


def _filters_by_type(symbol: JsonObject) -> dict[str, JsonObject]:
    raw_filters = symbol.get("filters")
    if not isinstance(raw_filters, list):
        raise ValueError("symbol filters must be a list")
    result: dict[str, JsonObject] = {}
    for raw_filter in raw_filters:
        if not isinstance(raw_filter, Mapping):
            raise ValueError("each symbol filter must be an object")
        filter_type = raw_filter.get("filterType")
        if isinstance(filter_type, str):
            result[filter_type] = raw_filter
    return result


def _parse_instrument(symbol: JsonObject) -> InstrumentRules:
    filters = _filters_by_type(symbol)
    try:
        price_filter = filters["PRICE_FILTER"]
        lot_filter = filters["LOT_SIZE"]
    except KeyError as exc:
        raise ValueError(f"missing required Binance filter: {exc.args[0]}") from exc

    notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL")
    if notional_filter is None:
        raise ValueError("missing required Binance filter: MIN_NOTIONAL or NOTIONAL")
    market_lot_filter = filters.get("MARKET_LOT_SIZE", lot_filter)

    venue_symbol = str(symbol.get("symbol", ""))
    price_tick = _as_decimal(price_filter.get("tickSize"), field="tickSize")
    quantity_step = _as_decimal(lot_filter.get("stepSize"), field="stepSize")
    min_quantity = _as_decimal(lot_filter.get("minQty"), field="minQty")
    min_notional_raw = notional_filter.get("notional", notional_filter.get("minNotional"))
    min_notional = _as_decimal(min_notional_raw, field="minNotional")
    market_quantity_step = _as_decimal(
        market_lot_filter.get("stepSize"), field="market stepSize"
    )
    for field, value in (
        ("tickSize", price_tick),
        ("stepSize", quantity_step),
        ("minQty", min_quantity),
        ("minNotional", min_notional),
        ("market stepSize", market_quantity_step),
    ):
        _require_positive(value, field=field)

    return InstrumentRules(
        instrument=f"{venue_symbol}-PERP",
        venue_symbol=venue_symbol,
        base_asset=str(symbol.get("baseAsset", "")),
        quote_asset=str(symbol.get("quoteAsset", "")),
        margin_asset=str(symbol.get("marginAsset", "")),
        status=str(symbol.get("status", "")),
        price_tick=price_tick,
        quantity_step=quantity_step,
        min_quantity=min_quantity,
        min_notional=min_notional,
        market_quantity_step=market_quantity_step,
    )


def parse_exchange_info(payload: object) -> tuple[InstrumentRules, ...]:
    """Parse Binance exchangeInfo while excluding dated futures contracts."""
    if not isinstance(payload, Mapping):
        raise ValueError("exchange info payload must be an object")
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, list):
        raise ValueError("exchange info symbols must be a list")

    instruments = []
    for raw_symbol in raw_symbols:
        if not isinstance(raw_symbol, Mapping):
            raise ValueError("each exchange info symbol must be an object")
        if raw_symbol.get("contractType") != "PERPETUAL":
            continue
        instruments.append(_parse_instrument(raw_symbol))
    return tuple(sorted(instruments, key=lambda item: item.venue_symbol))


def _read_public_json(url: str) -> object:
    request = Request(url, headers={"User-Agent": "autotrader-infrastructure/0.1"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


@dataclass(frozen=True, slots=True)
class BinanceUsdMDiscovery:
    """Read public Binance USD-M instrument metadata without credentials."""

    read_json: JsonReader = _read_public_json
    base_url: str = BINANCE_USDM_BASE_URL

    def discover_instruments(self) -> tuple[InstrumentRules, ...]:
        return parse_exchange_info(self.read_json(f"{self.base_url}{EXCHANGE_INFO_PATH}"))


@dataclass(frozen=True, slots=True)
class SnapshotPolicy:
    """Event-age thresholds used to classify normalized market snapshots."""

    live_after: timedelta = timedelta(seconds=1)
    stale_after: timedelta = timedelta(seconds=5)
    max_future_skew: timedelta = timedelta(seconds=1)

    def __post_init__(self) -> None:
        if self.live_after < timedelta(0):
            raise ValueError("live_after cannot be negative")
        if self.stale_after <= self.live_after:
            raise ValueError("stale_after must exceed live_after")
        if self.max_future_skew < timedelta(0):
            raise ValueError("max_future_skew cannot be negative")

    def quality(self, *, event_time: datetime, receipt_time: datetime) -> DataQuality:
        if event_time.tzinfo is None or receipt_time.tzinfo is None:
            raise ValueError("event_time and receipt_time must be timezone-aware")
        if event_time.utcoffset() != timedelta(0) or receipt_time.utcoffset() != timedelta(0):
            raise ValueError("event_time and receipt_time must be UTC")
        age = receipt_time - event_time
        if age < -self.max_future_skew:
            return DataQuality.INVALID
        if age <= self.live_after:
            return DataQuality.LIVE
        if age <= self.stale_after:
            return DataQuality.DELAYED
        return DataQuality.STALE


class BinanceUsdMSnapshotFactory:
    """Normalize Binance book ticker and 24-hour ticker payloads."""

    def __init__(self, policy: SnapshotPolicy | None = None) -> None:
        self._policy = policy or SnapshotPolicy()

    def create(
        self,
        *,
        instrument: InstrumentRules,
        book_ticker: JsonObject,
        ticker: JsonObject,
        receipt_time: datetime,
        volatility: Decimal | None,
        volatility_window: int | None = None,
        volatility_method: str = "sqrt_sum_log_returns",
    ) -> MarketSnapshot:
        if book_ticker.get("s") != instrument.venue_symbol:
            raise ValueError("book ticker symbol does not match instrument")
        if ticker.get("s") != instrument.venue_symbol:
            raise ValueError("ticker symbol does not match instrument")

        try:
            event_milliseconds = int(book_ticker["E"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("book ticker event timestamp must be epoch milliseconds") from exc
        event_time = datetime.fromtimestamp(event_milliseconds / 1000, tz=UTC)
        bid = _as_decimal(book_ticker.get("b"), field="bid")
        ask = _as_decimal(book_ticker.get("a"), field="ask")
        last = _as_decimal(ticker.get("c"), field="last")
        volume = _as_decimal(ticker.get("v"), field="volume")
        quote_volume = _as_decimal(ticker.get("q"), field="quote volume")

        for field, value in (("bid", bid), ("ask", ask), ("last", last)):
            _require_positive(value, field=field)
        for field, value in (("volume", volume), ("quote volume", quote_volume)):
            if value < 0:
                raise ValueError(f"{field} cannot be negative")
        if volatility is not None and (not volatility.is_finite() or volatility < 0):
            raise ValueError("volatility must be finite and non-negative")
        if volatility is not None and (volatility_window is None or volatility_window < 2):
            raise ValueError("volatility_window must be at least 2 when volatility is provided")
        if volatility is not None and not volatility_method.strip():
            raise ValueError("volatility_method cannot be empty when volatility is provided")

        spread = ask - bid
        if spread < 0:
            raise ValueError("bid cannot exceed ask")
        midpoint = (bid + ask) / Decimal(2)
        spread_bps = spread / midpoint * Decimal(10_000)
        quality = self._policy.quality(event_time=event_time, receipt_time=receipt_time)

        return MarketSnapshot(
            instrument=instrument.instrument,
            timestamp=event_time,
            bid=bid,
            ask=ask,
            last=last,
            volume=volume,
            volatility=volatility,
            liquidity=quote_volume,
            source="binance-usdm",
            quality=quality,
            metadata={
                "schema_version": 1,
                "venue_symbol": instrument.venue_symbol,
                "sequence": book_ticker.get("u"),
                "receipt_timestamp": receipt_time.isoformat(),
                "spread": str(spread),
                "spread_bps": str(spread_bps),
                "volume_semantics": "rolling_24h_base",
                "liquidity_semantics": "rolling_24h_quote_volume",
                "volatility_method": volatility_method if volatility is not None else None,
                "volatility_window": volatility_window if volatility is not None else None,
            },
        )
