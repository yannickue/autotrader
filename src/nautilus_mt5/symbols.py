"""Canonical instrument <-> broker symbol registry (GER40 first; extensible)."""

from __future__ import annotations

from dataclasses import dataclass

from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue

VENUE = Venue("ACTIVTRADES")


@dataclass(frozen=True, slots=True, kw_only=True)
class SymbolMapping:
    canonical: str  # e.g. "GER40" (Nautilus symbol)
    broker_symbol: str  # e.g. "Ger40" (MT5 name, case-sensitive)
    expected_path_prefix: str  # broker category guard, e.g. "Cash Indices"
    research_only: bool = False  # True: history/research data only, never traded

    @property
    def instrument_id(self) -> InstrumentId:
        return InstrumentId(Symbol(self.canonical), VENUE)


class SymbolRegistry:
    """Add instruments by registering a mapping -- no code changes elsewhere."""

    def __init__(self, mappings: list[SymbolMapping] | None = None) -> None:
        self._by_canonical: dict[str, SymbolMapping] = {}
        self._by_broker: dict[str, SymbolMapping] = {}
        for mapping in mappings or []:
            self.register(mapping)

    def register(self, mapping: SymbolMapping) -> None:
        if mapping.canonical in self._by_canonical or mapping.broker_symbol in self._by_broker:
            raise ValueError(f"duplicate symbol mapping: {mapping}")
        self._by_canonical[mapping.canonical] = mapping
        self._by_broker[mapping.broker_symbol] = mapping

    def by_instrument_id(self, instrument_id: InstrumentId) -> SymbolMapping:
        try:
            return self._by_canonical[instrument_id.symbol.value]
        except KeyError:
            raise KeyError(f"unregistered instrument {instrument_id}") from None

    def by_broker_symbol(self, broker_symbol: str) -> SymbolMapping | None:
        return self._by_broker.get(broker_symbol)

    def all(self) -> list[SymbolMapping]:
        return list(self._by_canonical.values())


# Only broker-verified symbols are registered. Names below were OBSERVED via symbols_get on the
# ActivTrades DEMO terminal (2026-09-30, research/reports/v2_markets/symbol_snapshot.json) and
# unique-matched by path + description -- never guessed.
GER40 = SymbolMapping(canonical="GER40", broker_symbol="Ger40", expected_path_prefix="Cash Indices")

# V2 research-only markets: DATA ONLY. Not part of `default_registry()` (the live/paper registry),
# so no execution or data client picks them up; use `research_registry()` for research tooling.
NAS100 = SymbolMapping(
    canonical="NAS100",
    broker_symbol="UsaTec",  # "US Tech 100 Cash Index"
    expected_path_prefix="Cash Indices",
    research_only=True,
)
SPX500 = SymbolMapping(
    canonical="SPX500",
    broker_symbol="Usa500",  # "SP 500 Cash Index"
    expected_path_prefix="Cash Indices",
    research_only=True,
)
XAUUSD = SymbolMapping(
    canonical="XAUUSD",
    broker_symbol="GOLD",  # "Gold" spot
    expected_path_prefix="Metals",
    research_only=True,
)
EURUSD = SymbolMapping(
    canonical="EURUSD",
    broker_symbol="EURUSD",  # "Euro vs US Dollar"
    expected_path_prefix="Forex",
    research_only=True,
)
RESEARCH_ONLY_MAPPINGS = (NAS100, SPX500, XAUUSD, EURUSD)


def default_registry() -> SymbolRegistry:
    return SymbolRegistry([GER40])


def research_registry() -> SymbolRegistry:
    """GER40 + the V2 research-only markets (never handed to execution/data clients)."""
    return SymbolRegistry([GER40, *RESEARCH_ONLY_MAPPINGS])
