"""Pure simulator-parity checks on the ACTUAL executable price (shared by DEMO execution paths).

The opportunity generator decides on closed-bar information; by the time an intent reaches the
broker the market has moved. These checks re-validate the intent against the executable quote
(ask for a long, bid for a short) exactly like the simulator's fill model does, and return a
machine reason code (or ``None`` if the intent is still valid). No I/O, no clock, no state.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from demo.contracts import TradeIntent
from demo.execution import gates as G


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def executable_price(direction: int, bid: Decimal, ask: Decimal) -> Decimal:
    return ask if direction == 1 else bid


def parity_reject(
    intent: TradeIntent, *, bid: Decimal, ask: Decimal, max_spread: Decimal
) -> str | None:
    """First failing parity rule, in the fixed order the DEMO executor has always used."""
    executable = executable_price(intent.direction, bid, ask)
    stop = Decimal(str(intent.stop))
    target = Decimal(str(intent.target)) if intent.target is not None else None
    entry_ref = Decimal(str(intent.entry_ref))
    if ask - bid > max_spread:
        return G.R_SPREAD_CAP
    if (intent.direction == 1 and executable > entry_ref) or (
        intent.direction == -1 and executable < entry_ref
    ):
        return G.R_ENTRY_OVERSHOOT
    if (intent.direction == 1 and executable <= stop) or (
        intent.direction == -1 and executable >= stop
    ):
        return G.R_INVALIDATION_CROSSED
    if target is not None:
        if (intent.direction == 1 and executable >= target) or (
            intent.direction == -1 and executable <= target
        ):
            return G.R_TARGET_CROSSED
        risk_distance = abs(executable - stop)
        reward_distance = abs(target - executable)
        minimum = Decimal(str(intent.min_space_r))
        if risk_distance <= 0 or reward_distance / risk_distance < minimum:
            return G.R_MIN_SPACE_R
    return None
