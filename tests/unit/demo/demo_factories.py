"""Shared record factories for Lane D tests (plain module; not a test file)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from demo.contracts import (
    ClockCheck,
    CounterfactualLabel,
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

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def make_snapshot(
    *,
    i: int = 0,
    market: str = "GER40",
    direction: int = 1,
    phase: str = "DISCOVERY",
    signal_ts: datetime | None = None,
    entry: float = 100.0,
    risk: float = 1.5,
    target_r: float | None = 2.0,
    horizon_s: int = 3600,
    spread: float = 0.1,
    signal: dict | None = None,
    local_minute: int = 600,
) -> OpportunitySnapshot:
    ts = signal_ts or (T0 + timedelta(hours=i))
    clock = ClockCheck(
        utc=iso(ts),
        market_tz="Europe/Berlin",
        local_iso=iso(ts),
        utc_offset_min=120,
        local_minute=local_minute,
        session_bucket="open",
        in_entry_window=True,
        minutes_to_forced_flat=400,
        calendar_status="provisional",
    )
    stop = entry - direction * risk
    target = None if target_r is None else entry + direction * target_r * risk
    geo = TradeGeometry(
        intended_entry=entry,
        entry_zone_lo=entry - 0.1,
        entry_zone_hi=entry + 0.1,
        invalidation=stop,
        stop=stop,
        target=target,
        risk_distance=risk,
        min_space_r=1.0,
        space_to_opposition_r=3.0,
        expected_horizon_s=horizon_s,
        exit_kind="fixed_r",
        exit_r=target_r or 0.0,
    )
    ms = MarketState(
        bid=entry - spread / 2,
        ask=entry + spread / 2,
        spread=spread,
        atr=2.0,
        realized_vol=None,
        tick_activity=None,
        clock=clock,
    )
    oid = opportunity_id_for(market, "orb", "h1", clock.utc, direction)
    return OpportunitySnapshot(
        opportunity_id=oid,
        phase=phase,
        market=market,
        broker_symbol=market,
        direction=direction,
        signal_ts_utc=clock.utc,
        created_utc=clock.utc,
        versions={"git_commit": "abc"},
        context={"H1": {"t": 1}},
        structure={"zones": [1, 2]},
        geometry=geo,
        market_state=ms,
        signal=signal or {"family": "orb", "strategy_id": "orb", "confluence": 2, "quality": 0.5},
    )


def make_decision(
    snap: OpportunitySnapshot,
    accepted: bool = True,
    *,
    delta_s: int = 5,
    shadow: dict | None = None,
) -> Decision:
    ts = datetime.fromisoformat(snap.created_utc) + timedelta(seconds=delta_s)
    return Decision(
        opportunity_id=snap.opportunity_id,
        phase=snap.phase,
        decided_utc=iso(ts),
        accepted=accepted,
        reasons=("ok",) if accepted else ("min_space",),
        policy_id="static-demo-policy-v1",
        shadow=shadow or {},
    )


def make_intent(snap: OpportunitySnapshot) -> TradeIntent:
    g = snap.geometry
    return TradeIntent(
        opportunity_id=snap.opportunity_id,
        phase=snap.phase,
        intent_id="int-" + stable_hash(snap.opportunity_id),
        market=snap.market,
        broker_symbol=snap.broker_symbol,
        direction=snap.direction,
        entry_ref=g.intended_entry,
        stop=g.stop,
        target=g.target,
        min_space_r=g.min_space_r,
        valid_until_utc=iso(datetime.fromisoformat(snap.created_utc) + timedelta(minutes=5)),
        forced_flat_utc=None,
        risk_fraction=0.01,
    )


def make_risk(approved: bool = True) -> RiskRecord:
    return RiskRecord(
        equity=10000.0,
        risk_fraction=0.01,
        risk_budget=100.0,
        quantity=1.0,
        leverage=2.0,
        approved=approved,
    )


def make_exec(
    fill: float | None = 100.0,
    fees: float | None = -1.0,
    swap: float | None = 0.0,
    slippage: float | None = 0.05,
) -> ExecutionRecord:
    return ExecutionRecord(
        intended_entry=100.0,
        fill_price=fill,
        quantity=1.0,
        spread_at_send=0.1,
        slippage=slippage,
        fees=fees,
        swap=swap,
        broker_order_id="b1",
        broker_position_id="p1",
        protection_confirmed=True,
    )


def make_outcome(
    net_r: float = 1.0,
    *,
    gross_r: float | None = None,
    closed: datetime | None = None,
    pnl_eur: float | None = None,
    mfe: float = 1.5,
    mae: float = 0.5,
) -> OutcomeRecord:
    return OutcomeRecord(
        gross_r=net_r if gross_r is None else gross_r,
        net_r=net_r,
        pnl_eur=net_r * 100 if pnl_eur is None else pnl_eur,
        mfe_r=mfe,
        mae_r=mae,
        time_to_mfe_s=600.0,
        time_to_mae_s=120.0,
        holding_s=1800.0,
        exit_reason="TARGET",
        closed_utc=iso(closed or (T0 + timedelta(hours=1))),
    )


def make_label(snap: OpportunitySnapshot, r: float = -1.0) -> CounterfactualLabel:
    return CounterfactualLabel(
        opportunity_id=snap.opportunity_id,
        phase=snap.phase,
        horizon_end_utc=iso(T0 + timedelta(hours=2)),
        hypothetical_mfe_r=0.5,
        hypothetical_mae_r=1.0,
        hypothetical_r=r,
        target_before_stop=False,
        labelled_utc=iso(T0 + timedelta(hours=3)),
    )


def drive_full_trade(
    store, snap, *, net_r: float = 1.0, closed: datetime | None = None, upto: str = "CLOSED"
):
    """Persist snapshot+decision+intent and walk the lifecycle up to `upto`."""
    store.record_snapshot(snap)
    store.record_decision(make_decision(snap, True))
    it = make_intent(snap)
    store.record_intent(it)
    order = ["RISK_APPROVED", "SENT", "FILLED", "PROTECTED", "CLOSED"]
    for st in order:
        if st == "RISK_APPROVED":
            store.record_risk(it.intent_id, make_risk())
        if st == "FILLED":
            store.record_execution(it.intent_id, make_exec())
        store.transition(it.intent_id, st)
        if st == upto:
            break
    if upto == "CLOSED":
        store.record_outcome(it.intent_id, make_outcome(net_r, closed=closed))
    return it
