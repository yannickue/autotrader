# ruff: noqa: E501
"""Import a trade that was executed OUTSIDE the runner (canary / manual / test trade) into the DemoStore.

Purpose: the account P/L must reconcile with the store although some trades were placed by hand or by a
canary script.  The import works ONLY on the supplied broker deal-history fields: it never connects to,
reads from or writes to the broker / MT5 terminal.

Rules
  * The trade is tagged ``EXECUTION_CANARY`` (default) or ``TEST_TRADE`` and is CENSORED: it is excluded
    from every alpha metric (strategy expectancy, winrate, cumulative R, learning labels, funnel) and shown
    separately in the reports, but its EUR P/L is part of the account reconciliation.
  * Idempotent: the same ``position_id`` always maps to the same ids; a repeated import is a no-op and a
    half-finished one (crash) is completed.
  * R is only defined if the initial ``stop`` is known; otherwise R fields are 0.0 and the tca record
    says ``r_undefined``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any

from demo.contracts import (
    ClockCheck,
    Decision,
    ExecutionRecord,
    MarketState,
    OpportunitySnapshot,
    OutcomeRecord,
    RiskRecord,
    TradeGeometry,
    TradeIntent,
    opportunity_id_for,
    stable_hash,
)
from demo.store import (
    CLOSED,
    FILLED,
    PLANNED,
    RISK_APPROVED,
    SENT,
    TRADE_TYPES,
    AccountMismatch,
    DemoStore,
    DemoStoreError,
    parse_utc,
)

POLICY_ID = "external-import"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExternalTrade:
    """Broker deal-history fields of one closed position (prices in price units, money in EUR, signed
    broker amounts: negative = cost)."""

    position_id: str
    market: str
    broker_symbol: str
    direction: int  # +1 long, -1 short
    volume: float
    entry_price: float
    exit_price: float
    open_time_utc: str
    close_time_utc: str
    profit_eur: float  # broker profit of the deals, before commission / swap
    commission_eur: float = 0.0  # entry + exit deals
    swap_eur: float = 0.0
    stop: float | None = None
    target: float | None = None
    magic: int | None = None
    comment: str | None = None
    exit_reason: str = "MANUAL"
    trade_type: str = "EXECUTION_CANARY"
    account_id_hash: str | None = None
    phase: str = "DISCOVERY"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExternalTrade:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def import_external_trade(store: DemoStore, trade: ExternalTrade) -> str:
    """Record ``trade`` into ``store`` as a closed, censored, non-strategy trade. Returns its intent_id."""
    if trade.trade_type not in TRADE_TYPES or trade.trade_type == "STRATEGY":
        raise ValueError("an imported external trade must be EXECUTION_CANARY or TEST_TRADE")
    if trade.direction not in (1, -1):
        raise ValueError("direction must be +1/-1")
    if not trade.volume > 0:
        raise ValueError("volume must be > 0")
    if trade.account_id_hash is not None:
        stored = store.get_meta("account_id_hash")
        if stored is not None and stored != trade.account_id_hash:
            raise AccountMismatch(f"trade belongs to account {trade.account_id_hash}, store to {stored}")
    opened, closed = parse_utc(trade.open_time_utc), parse_utc(trade.close_time_utc)
    if closed < opened:
        raise ValueError("close before open")
    d = trade.direction
    oid = opportunity_id_for(trade.market, "external-import", str(trade.position_id), opened.isoformat(), d)
    iid = "int-" + stable_hash(oid, POLICY_ID)
    stop = trade.stop if trade.stop is not None else trade.entry_price
    clock = ClockCheck(
        utc=opened.isoformat(), market_tz="n/a", local_iso=opened.isoformat(), utc_offset_min=0, local_minute=0,
        session_bucket="EXTERNAL", in_entry_window=False, minutes_to_forced_flat=0, calendar_status="external",
    )
    geo = TradeGeometry(
        intended_entry=trade.entry_price, entry_zone_lo=trade.entry_price, entry_zone_hi=trade.entry_price,
        invalidation=stop, stop=stop, target=trade.target, risk_distance=abs(trade.entry_price - stop),
        min_space_r=0.0, space_to_opposition_r=None, expected_horizon_s=int((closed - opened).total_seconds()),
        exit_kind="manual", exit_r=0.0,
    )
    snap = OpportunitySnapshot(
        opportunity_id=oid, phase=trade.phase, market=trade.market, broker_symbol=trade.broker_symbol,  # type: ignore[arg-type]
        direction=d, signal_ts_utc=opened.isoformat(), created_utc=opened.isoformat(),
        versions={"source": "external_import"}, context={}, structure={}, geometry=geo,
        market_state=MarketState(bid=trade.entry_price, ask=trade.entry_price, spread=0.0, atr=0.0,
                                 realized_vol=None, tick_activity=None, clock=clock),
        signal={"family": "EXTERNAL", "strategy_id": "external-import", "origin": "EXTERNAL_IMPORT",
                "trade_type": trade.trade_type, "position_id": str(trade.position_id), "magic": trade.magic,
                "comment": trade.comment},
    )
    store.record_snapshot(snap)
    store.record_decision(Decision(
        opportunity_id=oid, phase=trade.phase, decided_utc=opened.isoformat(), accepted=True,  # type: ignore[arg-type]
        reasons=("EXTERNAL_IMPORT",), policy_id=POLICY_ID,
    ))
    intent = TradeIntent(
        opportunity_id=oid, phase=trade.phase, intent_id=iid, market=trade.market,  # type: ignore[arg-type]
        broker_symbol=trade.broker_symbol, direction=d, entry_ref=trade.entry_price, stop=stop, target=trade.target,
        min_space_r=0.0, valid_until_utc=opened.isoformat(), forced_flat_utc=None, risk_fraction=0.0,
        context={"trade_type": trade.trade_type, "position_id": str(trade.position_id)},
    )
    store.record_intent(intent)
    # tag FIRST: from here on the trade can never leak into alpha metrics, whatever happens next
    store.record_trade_tag(
        iid, trade.trade_type, True, exit_class=trade.exit_reason, source="external_import",
        detail={"position_id": str(trade.position_id), "magic": trade.magic, "comment": trade.comment},
    )
    ts_open, ts_close = opened.isoformat(), closed.isoformat()
    store.record_risk(iid, RiskRecord(equity=0.0, risk_fraction=0.0, risk_budget=0.0, quantity=float(trade.volume),
                                      leverage=0.0, approved=True))
    for state in (RISK_APPROVED, SENT, FILLED):
        if store.get_state(iid) == PLANNED or store.get_state(iid) in (RISK_APPROVED, SENT):
            store.transition(iid, state, detail={"external_import": True}, ts=ts_open)
    store.record_execution(iid, ExecutionRecord(
        intended_entry=trade.entry_price, fill_price=trade.entry_price, quantity=float(trade.volume),
        spread_at_send=None, slippage=None, fees=trade.commission_eur, swap=trade.swap_eur,
        broker_order_id=None, broker_position_id=str(trade.position_id), protection_confirmed=trade.stop is not None,
        cost_status="verified",
    ))
    if store.get_state(iid) != CLOSED:
        store.transition(iid, CLOSED, detail={"external_import": True, "exit_reason": trade.exit_reason}, ts=ts_close)
    net = trade.profit_eur + trade.commission_eur + trade.swap_eur
    move = (trade.exit_price - trade.entry_price) * d
    risk = abs(trade.entry_price - stop)
    r_defined = trade.stop is not None and risk > 0
    if r_defined:
        gross_r = move / risk
        value = (trade.profit_eur / (move * trade.volume)) if abs(move * trade.volume) > 1e-12 else 0.0
        risk_eur = risk * trade.volume * value if value > 0 else 0.0
        net_r = gross_r + ((trade.commission_eur + trade.swap_eur) / risk_eur if risk_eur > 0 else 0.0)
    else:
        gross_r = net_r = 0.0
    outcome = OutcomeRecord(
        gross_r=gross_r, net_r=net_r, pnl_eur=net, mfe_r=max(0.0, gross_r), mae_r=max(0.0, -gross_r),
        time_to_mfe_s=None, time_to_mae_s=None, holding_s=(closed - opened).total_seconds(),
        exit_reason=trade.exit_reason, closed_utc=ts_close,
    )
    store.record_tca(iid, {
        "external_import": True, "trade_type": trade.trade_type, "r_undefined": not r_defined,
        "exit_price": trade.exit_price, "broker_profit_eur": trade.profit_eur,
        "total_commission_eur": trade.commission_eur, "total_swap_eur": trade.swap_eur,
        "computed_pnl_eur": net, "holding_seconds": outcome.holding_s, "exit_reason": trade.exit_reason,
    }, "EXIT")
    try:
        store.record_outcome(iid, outcome)
    except DemoStoreError:
        if store.get_outcome(iid) is None:
            raise
    return iid


def import_file(store: DemoStore, path: str) -> list[str]:
    """``--record-canary FILE``: a JSON object or list of objects with the ``ExternalTrade`` fields."""
    data = json.loads(open(path, encoding="utf-8").read())
    items = data if isinstance(data, list) else [data]
    return [import_external_trade(store, ExternalTrade.from_dict(x)) for x in items]

