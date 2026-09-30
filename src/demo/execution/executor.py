"""Fail-closed, idempotent ActivTrades DEMO intent executor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from adapters.activtrades_mt5.history import ServerTimePolicy
from adapters.activtrades_mt5.models import symbol_info_raw_from_mt5, symbol_info_to_instrument_spec
from demo.contracts import TradeIntent
from demo.execution.events import (
    Accepted,
    ExecutionEvent,
    Fill,
    PositionClosed,
    ProtectionConfirmed,
    Rejected,
)
from demo.execution.market_config import DemoMarketSpec, load_demo_market_specs
from demo.execution.parity import parity_reject
from demo.execution.ports import Recorder
from nautilus_mt5.constants import ORDER_CHECK_OK, SendOutcome, classify_send_retcode
from nautilus_mt5.translate import close_request, market_entry_request
from risk.models import ReconciliationState


@dataclass(frozen=True, slots=True)
class RiskApproval:
    quantity: Decimal
    risk_budget: Decimal


class DemoRiskBridge(Protocol):
    """Adapter around ``NautilusRiskBridge``; it is the sole sizing authority."""

    def size(
        self, *, intent: TradeIntent, bid: Decimal, ask: Decimal, now: datetime
    ) -> RiskApproval | str: ...


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


class DemoExecutor:
    def __init__(
        self,
        *,
        broker: Any,
        risk_bridge: DemoRiskBridge,
        recorder: Recorder,
        reconciliation: Callable[[], ReconciliationState],
        now: Callable[[], datetime] | None = None,
        market_specs: dict[str, DemoMarketSpec] | None = None,
        dry_run: bool = False,
        max_quote_age_seconds: float = 30.0,
    ) -> None:
        self._broker = broker
        self._risk_bridge = risk_bridge
        self._recorder = recorder
        self._reconciliation = reconciliation
        self._now = now or (lambda: datetime.now(UTC))
        self._specs = market_specs or load_demo_market_specs()
        self._dry_run = dry_run
        self._max_quote_age_seconds = max_quote_age_seconds
        self._server_time = ServerTimePolicy()
        self._results: dict[str, list[ExecutionEvent]] = {}
        self._open: dict[str, tuple[TradeIntent, int]] = {}
        self._halted = False

    def set_reconciliation(self, value: Callable[[], ReconciliationState]) -> None:
        self._reconciliation = value

    def _reject(self, intent: TradeIntent, reason: str) -> list[ExecutionEvent]:
        event = Rejected(intent_id=intent.intent_id, reason=reason)
        try:
            self._recorder.record(event)
        except Exception:
            self._halted = True
        return [event]

    def _preflight(self, intent: TradeIntent, now: datetime) -> str | None:
        if self._halted:
            return "halted"
        terminal = self._broker.terminal_info()
        if terminal is None or not getattr(terminal, "connected", False):
            return "broker_disconnect"
        account = self._broker.account_info()
        if account is None:
            return "unknown_account"
        if int(getattr(account, "trade_mode", -1)) != 0:
            return "non_demo_account"
        if int(getattr(account, "leverage", 10**9)) > 30:
            return "broker_leverage_above_ceiling"
        if self._reconciliation() is not ReconciliationState.RECONCILED:
            return "not_reconciled"
        if now > _utc(intent.valid_until_utc):
            return "stale_signal"
        if intent.market not in self._specs:
            return "unknown_market"
        spec = self._specs[intent.market]
        if intent.broker_symbol != spec.broker_symbol:
            return "symbol_mismatch"
        if self._broker.positions_get(symbol=intent.broker_symbol):
            return "position_exists"
        return None

    def submit(self, intent: TradeIntent) -> list[ExecutionEvent]:
        if intent.intent_id in self._results:
            return self._results[intent.intent_id]
        now = self._now().astimezone(UTC)
        reason = self._preflight(intent, now)
        if reason:
            result = self._reject(intent, reason)
            self._results[intent.intent_id] = result
            return result

        tick = self._broker.symbol_info_tick(intent.broker_symbol)
        if tick is None:
            result = self._reject(intent, "stale_feed")
            self._results[intent.intent_id] = result
            return result
        bid, ask = Decimal(str(tick.bid)), Decimal(str(tick.ask))
        tick_epoch = float(getattr(tick, "time_msc", tick.time * 1000)) / 1000
        quote_time = self._server_time.server_epoch_to_utc(tick_epoch)
        quote_age = (now - quote_time).total_seconds()
        if quote_age < -2:
            self._halted = True
            result = self._reject(intent, "clock_anomaly")
            self._results[intent.intent_id] = result
            return result
        if quote_age > self._max_quote_age_seconds:
            result = self._reject(intent, "stale_feed")
            self._results[intent.intent_id] = result
            return result
        spec = self._specs[intent.market]
        spread = ask - bid
        stop, target = Decimal(str(intent.stop)), (
            Decimal(str(intent.target)) if intent.target is not None else None
        )
        entry_ref = Decimal(str(intent.entry_ref))
        parity_reason = parity_reject(intent, bid=bid, ask=ask, max_spread=spec.max_spread)
        if parity_reason:
            result = self._reject(intent, parity_reason)
            self._results[intent.intent_id] = result
            return result

        approval = self._risk_bridge.size(intent=intent, bid=bid, ask=ask, now=now)
        if isinstance(approval, str):
            result = self._reject(intent, approval)
            self._results[intent.intent_id] = result
            return result
        if approval.quantity < spec.volume_min:
            result = self._reject(intent, "size_below_min")
            self._results[intent.intent_id] = result
            return result
        accepted = Accepted(intent_id=intent.intent_id, quantity=approval.quantity)
        try:
            self._recorder.record(accepted)
        except Exception:
            self._halted = True
            result = self._reject(intent, "recorder_failure")
            self._results[intent.intent_id] = result
            return result
        if self._dry_run:
            result = [accepted]
            self._results[intent.intent_id] = result
            return result

        raw_info = self._broker.symbol_info(intent.broker_symbol)
        instrument = symbol_info_to_instrument_spec(
            symbol_info_raw_from_mt5(raw_info), canonical_symbol=intent.market, retrieved_at=now
        )
        request = market_entry_request(
            instrument,
            symbol_filling_mask=int(raw_info.filling_mode),
            is_buy=intent.direction == 1,
            quantity=approval.quantity,
            quote=type("Quote", (), {"bid": bid, "ask": ask})(),
            magic=740_003,
            token=intent.intent_id[:31],
            deviation_points=20,
            stop_loss=stop,
            take_profit=target,
        )
        checked = self._broker.order_check(request)
        if checked is None or int(checked.retcode) != ORDER_CHECK_OK:
            result = self._reject(intent, "broker_reject")
            self._results[intent.intent_id] = result
            return result
        sent = self._broker.order_send(request)
        if sent is None:
            self._halted = True
            result = self._reject(intent, "order_outcome_unknown")
            self._results[intent.intent_id] = result
            return result
        outcome = classify_send_retcode(int(sent.retcode))
        accepted_outcomes = {
            SendOutcome.ACCEPTED_FILLED,
            SendOutcome.ACCEPTED_PARTIAL,
            SendOutcome.ACCEPTED_PLACED,
        }
        if outcome not in accepted_outcomes:
            if outcome is SendOutcome.IN_DOUBT:
                self._halted = True
            reason = (
                "broker_reject"
                if outcome is SendOutcome.REJECTED
                else "order_outcome_unknown"
            )
            result = self._reject(intent, reason)
            self._results[intent.intent_id] = result
            return result
        positions = self._broker.positions_get(symbol=intent.broker_symbol) or ()
        position = positions[0] if positions else None
        if position is None or Decimal(str(position.sl or 0)) != stop:
            self._halted = True
            if position is not None:
                self._flatten(intent, position)
            result = [
                accepted,
                Rejected(intent_id=intent.intent_id, reason="protection_unconfirmed"),
            ]
            self._results[intent.intent_id] = result
            return result
        fill = Fill(
            intent_id=intent.intent_id,
            price=Decimal(str(sent.price)),
            quantity=Decimal(str(sent.volume)),
            spread=spread,
            slippage=Decimal(str(sent.price)) - entry_ref,
            commission=Decimal(0),
            swap=Decimal(str(position.swap)),
            broker_order_id=str(sent.order),
            broker_position_id=str(position.ticket),
        )
        protection = ProtectionConfirmed(
            intent_id=intent.intent_id,
            broker_position_id=str(position.ticket),
            stop=stop,
            target=target,
        )
        result = [accepted, fill, protection]
        self._open[intent.intent_id] = (intent, int(position.ticket))
        try:
            for event in (fill, protection):
                self._recorder.record(event)
        except Exception:
            self._halted = True
        self._results[intent.intent_id] = result
        return result

    def _flatten(self, intent: TradeIntent, position: Any) -> bool:
        raw_info = self._broker.symbol_info(intent.broker_symbol)
        tick = self._broker.symbol_info_tick(intent.broker_symbol)
        if raw_info is None or tick is None:
            return False
        now = self._now().astimezone(UTC)
        instrument = symbol_info_to_instrument_spec(
            symbol_info_raw_from_mt5(raw_info), canonical_symbol=intent.market, retrieved_at=now
        )
        bid, ask = Decimal(str(tick.bid)), Decimal(str(tick.ask))
        request = close_request(
            instrument,
            symbol_filling_mask=int(raw_info.filling_mode),
            position_ticket=int(position.ticket),
            position_is_long=int(position.type) == 0,
            quantity=Decimal(str(position.volume)),
            quote=type("Quote", (), {"bid": bid, "ask": ask})(),
            magic=740_003,
            token=(intent.intent_id + "-flat")[:31],
            deviation_points=20,
        )
        checked = self._broker.order_check(request)
        if checked is None or int(checked.retcode) != ORDER_CHECK_OK:
            return False
        sent = self._broker.order_send(request)
        if sent is None:
            return False
        return classify_send_retcode(int(sent.retcode)) in {
            SendOutcome.ACCEPTED_FILLED,
            SendOutcome.ACCEPTED_PARTIAL,
        }

    def on_clock(self, now: datetime | None = None) -> list[PositionClosed]:
        """Flatten positions whose immutable intent deadline has arrived."""
        current = (now or self._now()).astimezone(UTC)
        events: list[PositionClosed] = []
        for intent_id, (intent, ticket) in list(self._open.items()):
            if intent.forced_flat_utc is None or current < _utc(intent.forced_flat_utc):
                continue
            positions = self._broker.positions_get(ticket=ticket) or ()
            if not positions:
                self._open.pop(intent_id, None)
                continue
            if not self._flatten(intent, positions[0]):
                self._halted = True
                continue
            event = PositionClosed(
                intent_id=intent_id,
                broker_position_id=str(ticket),
                exit_reason="SESSION_END",
            )
            self._recorder.record(event)
            events.append(event)
            self._open.pop(intent_id, None)
        return events
