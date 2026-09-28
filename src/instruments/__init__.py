"""Canonical CFD instrument specification and symbol discovery.

Standalone from `src/adapters`, `src/risk`, `src/execution` and the
`metatrader5` package -- fully testable with plain Python fixtures.

Public API:
    `instruments.models.InstrumentSpec` -- broker-reported CFD instrument
        specification (contract size, tick size/value, volume constraints,
        currencies, margin, execution, session, provenance metadata).
    `instruments.models.MarginCalculationMode`, `TradeMode`, `ExecutionMode`,
        `FillingMode` -- enums mirroring real `MetaTrader5` module constants.
    `instruments.discovery.BrokerSymbolCandidate` -- one raw broker symbol
        record to match against a canonical instrument.
    `instruments.discovery.CanonicalInstrument` -- a canonical instrument to
        find, with its known name aliases.
    `instruments.discovery.MatchStatus`, `SymbolMatch` -- mapping result
        types distinguishing confident/ambiguous/not-found matches.
    `instruments.discovery.match_symbols(candidates, canonical_instruments,
        overrides) -> dict[str, SymbolMatch]`.
"""

from instruments.discovery import (
    BrokerSymbolCandidate,
    CanonicalInstrument,
    MatchStatus,
    SymbolMatch,
    match_symbols,
)
from instruments.models import (
    ExecutionMode,
    FillingMode,
    InstrumentSpec,
    MarginCalculationMode,
    TradeMode,
)

__all__ = [
    "BrokerSymbolCandidate",
    "CanonicalInstrument",
    "ExecutionMode",
    "FillingMode",
    "InstrumentSpec",
    "MarginCalculationMode",
    "MatchStatus",
    "SymbolMatch",
    "TradeMode",
    "match_symbols",
]
