"""Read-only queries over the Nautilus cache (no state of our own).

With `OmsType.NETTING` (ActivTrades' account is retail netting, MT5
`margin_mode=0`) Nautilus reuses one position id per instrument and moves each
completed lifecycle into a position SNAPSHOT; `cache.positions_closed()` alone
therefore only shows the latest lifecycle. Every completed trade is
snapshots + currently-closed positions, de-duplicated by lifecycle.
"""

from __future__ import annotations

from typing import Any

from nautilus_trader.model.identifiers import InstrumentId


def closed_position_lifecycles(cache: Any, instrument_id: InstrumentId) -> list[Any]:
    seen: set[tuple[Any, int, int]] = set()
    out: list[Any] = []
    candidates = [
        *cache.position_snapshots(),
        *cache.positions_closed(instrument_id=instrument_id),
    ]
    for position in candidates:
        if position.instrument_id != instrument_id or position.ts_closed == 0:
            continue
        key = (position.id, position.ts_opened, position.ts_closed)
        if key in seen:
            continue
        seen.add(key)
        out.append(position)
    out.sort(key=lambda p: p.ts_closed)
    return out
