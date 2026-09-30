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


def entry_tolerance(
    intent: TradeIntent, *, spread: Decimal, tick_size: Decimal | None
) -> Decimal:
    """Adverse entry drift (price units) tolerated between the decision price and the fill.

    ``intent.entry_tolerance`` wins when present (finite, >= 0; anything else falls back to the
    conservative default). Default: ``max(ENTRY_TOLERANCE_SPREAD_MULTIPLE x current spread,
    ENTRY_TOLERANCE_TICK_MULTIPLE x tick)``. Zero tolerance rejected ~half of all accepted
    intents on a plain one-tick move. The drift never widens the stop; the target/stop/min_space_r
    checks still run on the ACTUAL executable price.
    """
    configured = intent.entry_tolerance
    if configured is not None:
        try:
            value = Decimal(str(configured))
        except Exception:  # malformed -> conservative default
            value = Decimal(-1)
        if value.is_finite() and value >= 0:
            return value
    tick = tick_size if tick_size is not None and tick_size > 0 else Decimal(0)
    return max(
        G.ENTRY_TOLERANCE_SPREAD_MULTIPLE * spread, G.ENTRY_TOLERANCE_TICK_MULTIPLE * tick
    )


def parity_reject(
    intent: TradeIntent,
    *,
    bid: Decimal,
    ask: Decimal,
    max_spread: Decimal,
    tick_size: Decimal | None = None,
) -> str | None:
    """First failing parity rule, in the fixed order the DEMO executor has always used."""
    executable = executable_price(intent.direction, bid, ask)
    stop = Decimal(str(intent.stop))
    target = Decimal(str(intent.target)) if intent.target is not None else None
    entry_ref = Decimal(str(intent.entry_ref))
    if ask - bid > max_spread:
        return G.R_SPREAD_CAP
    adverse_drift = (
        executable - entry_ref if intent.direction == 1 else entry_ref - executable
    )
    if adverse_drift > entry_tolerance(intent, spread=ask - bid, tick_size=tick_size):
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
