# ruff: noqa: E501
"""Recorder-facing immutable DEMO execution events."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, slots=True)
class Accepted:
    intent_id: str
    quantity: Decimal
    equity: Decimal | None = None
    risk_fraction: Decimal | None = None
    risk_budget: Decimal | None = None
    leverage: Decimal | None = None
    # Complete, machine-readable sizing / portfolio detail of the decision (Decimal | str | bool |
    # None | nested dict): structural stop, stop distance, broker min lot, actual quantity, EUR risk,
    # equity risk %, leverage, portfolio / cluster / family risk before and after, concentration,
    # ATR, spread, expected payoff / win probability if supplied, policy id. Keys: see
    # ``DemoRiskGate`` / ``DemoPositionSizer`` (``risk_detail["decision"] == "TRADE"``).
    risk_detail: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Rejected:
    intent_id: str
    reason: str
    # Same shape as ``Accepted.risk_detail`` with ``decision == "SKIP"``, ``reject_code``,
    # ``gate_reject_class`` (SAFETY|STRUCTURAL|TEMPORARY|LEGACY_ARBITRARY), and for
    # ``size_below_min`` also ``violated_cap`` / ``cap_limit`` / ``observed_at_min_lot`` / ``message``.
    risk_detail: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    intent_id: str
    price: Decimal
    quantity: Decimal
    spread: Decimal
    slippage: Decimal
    commission: Decimal
    swap: Decimal
    broker_order_id: str
    broker_position_id: str
    # -- transaction cost analysis (all optional; price units unless stated) ------------------------
    intended_price: Decimal | None = None  # the intent's entry_ref
    reference_price: Decimal | None = None  # executable quote used for sizing (ask long / bid short)
    bid_at_send: Decimal | None = None
    ask_at_send: Decimal | None = None
    slippage_vs_intended: Decimal | None = None  # adverse-positive vs intent.entry_ref
    fill_vs_mid: Decimal | None = None  # adverse-positive: long fill - mid, short mid - fill
    fees_price_units: Decimal | None = None  # commission (abs) converted to price units per unit
    cost_price_units: Decimal | None = None  # spread + adverse slippage + fees, price units
    movement_to_cost: Decimal | None = None  # planned move (to target, else 1R) / cost_price_units
    latency_total_ms: float | None = None  # local: hand-over to the strategy -> outcome
    latency_send_to_fill_ms: float | None = None  # broker deal time - local send time (skew caveat)
    latency_send_to_ack_ms: float | None = None  # not measurable in v1 (None)
    latency_ack_to_fill_ms: float | None = None  # not measurable in v1 (None)


@dataclass(frozen=True, slots=True)
class ProtectionConfirmed:
    intent_id: str
    broker_position_id: str
    stop: Decimal
    target: Decimal | None


@dataclass(frozen=True, slots=True)
class PositionClosed:
    intent_id: str
    broker_position_id: str
    # SAFETY_FLATTEN: the stack itself flattened the position because broker-side protection could not
    # be confirmed / restored (protection failure, unprotected-position repair). MANUAL stays for
    # operator / script closes, EXTERNAL for closes by a human in the terminal.
    # Lane E2: ``EXIT_ENGINE_*`` (see ``demo.contracts.ENGINE_EXIT_REASONS``) are ExitEngine full closes
    exit_reason: str  # STOP | TARGET | SESSION_END | MANUAL | EXTERNAL | SAFETY_FLATTEN | EXIT_ENGINE_*
    exit_price: Decimal | None = None
    exit_quantity: Decimal | None = None
    closed_utc: str | None = None
    commission: Decimal | None = None  # signed broker amount, negative = cost
    swap: Decimal | None = None  # signed broker amount, negative = cost
    profit_eur: Decimal | None = None  # broker profit on closing deal(s), before costs
    net_pnl_eur: Decimal | None = None  # profit + commission + swap
    entry_price: Decimal | None = None
    holding_seconds: float | None = None
    # adverse-positive distance between the exit fill and the protective stop / target level
    exit_slippage_vs_level: Decimal | None = None


ExecutionEvent = Accepted | Rejected | Fill | ProtectionConfirmed | PositionClosed
