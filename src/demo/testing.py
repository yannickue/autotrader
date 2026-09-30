# ruff: noqa: E501
"""Deterministic in-memory test doubles for the DEMO runner (zero MT5, zero network).

* ``FakeClock``       injectable clock.
* ``FakeBarSource``   the LiveBarSource surface (``m5_frame`` with ts/open/high/low/close/tick_volume/
                      spread_pts, ``latest_quote``, ``last_closed_bar_close_utc``) with synthetic,
                      overridable M5 bars generated from the clock.
* ``FakeStack``       ``StackPort`` implementation: exactly-once submit per intent_id, scripted fills,
                      broker-side closes, shadow-mode hard guard (``AssertionError`` on any submit).
* ``ScriptedEngine``  stands in for ``OpportunityEngine`` (returns queued ``(snapshot, decision)`` pairs).
* ``make_pair``       snapshot + decision + intent factory.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from demo.contracts import (
    ClockCheck,
    Decision,
    MarketState,
    OpportunitySnapshot,
    TradeGeometry,
    TradeIntent,
    opportunity_id_for,
    stable_hash,
)
from demo.execution import gates as G
from demo.execution.events import (
    Accepted,
    ExecutionEvent,
    Fill,
    PositionClosed,
    ProtectionConfirmed,
    Rejected,
)
from demo.execution.stack_port import AccountSnapshot, StackFailClosed
from demo.opportunity.bar_source import Quote

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)  # a Thursday
M5 = timedelta(minutes=5)


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now


def floor5(dt: datetime) -> datetime:
    return dt.replace(minute=dt.minute - dt.minute % 5, second=0, microsecond=0)


class FakeBarSource:
    """Flat synthetic bid bars (o=c=base, h=base+0.5, l=base-0.5) ending at the last CLOSED bar."""

    def __init__(self, clock: FakeClock, markets: Sequence[str], *, base: float = 100.0, bars: int = 60) -> None:
        self.clock, self.base, self.n = clock, base, bars
        self.markets = tuple(markets)
        self.frozen: dict[str, datetime] = {}  # market -> "now" the feed froze at (stale simulation)
        self.overrides: dict[tuple[str, datetime], tuple[float, float, float, float]] = {}
        self.frame_calls = 0
        self.fail: set[str] = set()

    def _last_open(self, market: str) -> datetime:
        ref = self.frozen.get(market, self.clock())
        return floor5(ref) - M5  # newest fully closed bar's OPEN

    def m5_frame(self, market: str, n: int | None = None) -> pd.DataFrame:
        self.frame_calls += 1
        if market in self.fail:
            raise RuntimeError("feed failure")
        last = self._last_open(market)
        ts = [last - M5 * k for k in range(self.n - 1, -1, -1)]
        rows = []
        for t in ts:
            o, h, low, c = self.overrides.get(
                (market, t), (self.base, self.base + 0.5, self.base - 0.5, self.base)
            )
            rows.append({"ts": t, "open": o, "high": h, "low": low, "close": c, "tick_volume": 10, "spread_pts": 2})
        fr = pd.DataFrame(rows)
        fr["ts"] = pd.to_datetime(fr["ts"], utc=True).astype("datetime64[ns, UTC]")
        return fr.iloc[-n:].reset_index(drop=True) if n else fr

    def latest_quote(self, market: str) -> Quote | None:
        return Quote(ts_utc=self.frozen.get(market, self.clock()), bid=self.base, ask=self.base + 0.1)

    def tick_activity(self, market: str) -> float | None:
        return None

    def last_closed_bar_close_utc(self, market: str) -> datetime | None:
        if market in self.fail:
            raise RuntimeError("feed failure")
        return self._last_open(market) + M5


@dataclass
class FakeStack:
    clock: FakeClock
    markets: Sequence[str] = ("GER40",)
    shadow: bool = False
    fill_price_offset: float = 0.05
    mode: str = "full"  # full | reject | fail_closed | unknown_error | accept_only
    account: AccountSnapshot = field(default_factory=lambda: FakeStack.default_account())
    bar_source: FakeBarSource = field(init=False)
    submits: list[TradeIntent] = field(default_factory=list)
    halts: list[str] = field(default_factory=list)
    positions: dict[str, TradeIntent] = field(default_factory=dict)
    pending: list[ExecutionEvent] = field(default_factory=list)
    started: bool = False
    stopped: bool = False
    clock_calls: int = 0
    known: set[str] = field(default_factory=set)
    contexts: dict[str, dict[str, Any]] = field(default_factory=dict)
    reject_reasons: list[str] = field(default_factory=list)
    script: dict[str, list[ExecutionEvent]] = field(default_factory=dict)  # intent_id -> events to return

    def __post_init__(self) -> None:
        self.bar_source = FakeBarSource(self.clock, self.markets)

    @staticmethod
    def default_account(**kw: Any) -> AccountSnapshot:
        base = dict(
            is_demo=True, account_id_hash="h", equity=10000.0, balance=10000.0, profit=0.0,
            reconciliation="RECONCILED", connected=True, open_positions=0, open_orders=0,
            all_positions_protected=True, kill_switch=False,
        )
        base.update(kw)
        return AccountSnapshot(**base)  # type: ignore[arg-type]

    # ---- test controls ------------------------------------------------------------------
    def set_account(self, **kw: Any) -> None:
        self.account = replace(self.account, **kw)

    def close_position(
        self, intent_id: str, *, reason: str = "TARGET", exit_price: float = 103.0,
        commission: float = -1.0, swap: float = 0.0, profit_eur: float | None = None,
    ) -> None:
        it = self.positions.pop(intent_id)
        qty = Decimal("1")
        self.pending.append(PositionClosed(
            intent_id=intent_id, broker_position_id="pos-" + intent_id, exit_reason=reason,  # type: ignore[arg-type]
            exit_price=Decimal(str(exit_price)), exit_quantity=qty,
            closed_utc=(self.clock() + timedelta(minutes=30)).isoformat(),
            commission=Decimal(str(commission)), swap=Decimal(str(swap)),
            profit_eur=None if profit_eur is None else Decimal(str(profit_eur)),
        ))
        del it

    # ---- StackPort ----------------------------------------------------------------------
    def start(self) -> AccountSnapshot:
        self.started = True
        return self.account

    def account_snapshot(self) -> AccountSnapshot:
        prot = self.account.all_positions_protected
        return replace(self.account, open_positions=len(self.positions), all_positions_protected=prot,
                       server_time_utc=self.account.server_time_utc)

    def has_position(self, market: str) -> bool:
        return any(i.market == market for i in self.positions.values())

    def submit(self, intent: TradeIntent, context: Any = None) -> list[ExecutionEvent]:
        if self.shadow:
            raise AssertionError("submit() called in shadow mode")
        if intent.intent_id in self.known:  # exactly-once per intent_id
            return []
        self.known.add(intent.intent_id)
        self.submits.append(intent)
        self.contexts[intent.intent_id] = dict(context or {})
        iid = intent.intent_id
        if iid in self.script:
            evs = self.script[iid]
            for e in evs:
                if isinstance(e, Rejected):
                    self.reject_reasons.append(e.reason)
                if isinstance(e, Fill):
                    self.positions[iid] = intent
            return list(evs)
        if self.mode == "fail_closed":
            raise StackFailClosed("scripted fail closed")
        if self.mode == "unknown_error":
            raise RuntimeError("scripted unknown error")
        if self.mode == "reject":
            self.reject_reasons.append("risk: scripted rejection")
            return [Rejected(iid, "risk: scripted rejection")]
        acc = Accepted(iid, Decimal("1"), Decimal("10000"), Decimal("0.01"), Decimal("100"), Decimal("2"))
        if self.mode == "accept_only":
            return [acc]
        px = Decimal(str(intent.entry_ref + self.fill_price_offset * intent.direction))
        self.positions[iid] = intent
        return [
            acc,
            Fill(iid, px, Decimal("1"), Decimal("0.1"), Decimal(str(self.fill_price_offset)),
                 Decimal("-1.0"), Decimal("0"), "ord-" + iid, "pos-" + iid),
            ProtectionConfirmed(iid, "pos-" + iid, Decimal(str(intent.stop)),
                                None if intent.target is None else Decimal(str(intent.target))),
        ]

    def poll_events(self) -> list[ExecutionEvent]:
        out, self.pending = self.pending, []
        return out

    def on_clock(self, now: datetime) -> list[ExecutionEvent]:
        self.clock_calls += 1
        return []

    def open_intents(self) -> Sequence[str]:
        return list(self.positions)

    def rejection_funnel(self) -> dict[str, dict[str, object]]:
        return G.funnel(self.reject_reasons)

    def halt_new_exposure(self, reason: str) -> None:
        self.halts.append(reason)

    def stop(self) -> None:
        self.stopped = True


class ScriptedEngine:
    """Returns pre-queued pairs once per market (``on_m5_close``); records the calls."""

    def __init__(self) -> None:
        self.queue: dict[str, list[list[tuple[OpportunitySnapshot, Decision, TradeIntent | None]]]] = {}
        self.calls: list[tuple[str, datetime]] = []
        self._intents: dict[str, TradeIntent] = {}
        self.last_intents: list[TradeIntent] = []

    def push(self, market: str, *triples: tuple[OpportunitySnapshot, Decision, TradeIntent | None]) -> None:
        self.queue.setdefault(market, []).append(list(triples))

    def on_m5_close(self, market: str, now: datetime) -> list[tuple[OpportunitySnapshot, Decision]]:
        self.calls.append((market, now))
        batch = self.queue.get(market, [])
        if not batch:
            return []
        triples = batch.pop(0)
        self.last_intents = []
        for s, _d, i in triples:
            if i is not None:
                self._intents[s.opportunity_id] = i
                self.last_intents.append(i)
        return [(s, d) for s, d, _ in triples]

    def intents_for(self, pairs: list[tuple[OpportunitySnapshot, Decision]]) -> list[TradeIntent]:
        return [self._intents[s.opportunity_id] for s, d in pairs if d.accepted and s.opportunity_id in self._intents]


def make_pair(
    *, market: str = "GER40", signal_ts: datetime = T0, direction: int = 1, accepted: bool = True,
    phase: str = "DISCOVERY", entry: float = 100.0, risk: float = 1.5, target_r: float = 2.0,
    horizon_s: int = 3600, valid_s: int = 300, tag: str = "a",
) -> tuple[OpportunitySnapshot, Decision, TradeIntent | None]:
    utc = signal_ts.isoformat()
    clock = ClockCheck(
        utc=utc, market_tz="Europe/Berlin", local_iso=utc, utc_offset_min=120, local_minute=660,
        session_bucket="open", in_entry_window=True, minutes_to_forced_flat=300, calendar_status="provisional",
    )
    stop = entry - direction * risk
    target = entry + direction * target_r * risk
    geo = TradeGeometry(
        intended_entry=entry, entry_zone_lo=entry - 0.1, entry_zone_hi=entry + 0.1, invalidation=stop,
        stop=stop, target=target, risk_distance=risk, min_space_r=1.0, space_to_opposition_r=3.0,
        expected_horizon_s=horizon_s, exit_kind="fixed_r", exit_r=target_r,
    )
    ms = MarketState(bid=entry, ask=entry + 0.1, spread=0.1, atr=2.0, realized_vol=None, tick_activity=None, clock=clock)
    oid = opportunity_id_for(market, "orb-" + tag, "h1", utc, direction)
    snap = OpportunitySnapshot(
        opportunity_id=oid, phase=phase, market=market, broker_symbol=market, direction=direction,  # type: ignore[arg-type]
        signal_ts_utc=utc, created_utc=utc, versions={"git_commit": "abc"}, context={"H1": {"t": 1}},
        structure={"zones": [1]}, geometry=geo, market_state=ms,
        signal={"family": "orb", "strategy_id": "orb-" + tag, "confluence": 2, "quality": 0.5},
    )
    dec = Decision(
        opportunity_id=oid, phase=phase, decided_utc=(signal_ts + timedelta(seconds=5)).isoformat(),  # type: ignore[arg-type]
        accepted=accepted, reasons=("ok",) if accepted else ("min_space",), policy_id="static-demo-policy-v1",
    )
    intent = None
    if accepted:
        intent = TradeIntent(
            opportunity_id=oid, phase=phase, intent_id="int-" + stable_hash(oid), market=market,  # type: ignore[arg-type]
            broker_symbol=market, direction=direction, entry_ref=entry, stop=stop, target=target,
            min_space_r=1.0, valid_until_utc=(signal_ts + timedelta(seconds=valid_s)).isoformat(),
            forced_flat_utc=(signal_ts + timedelta(hours=5)).isoformat(), risk_fraction=0.01,
        )
    return snap, dec, intent
