"""Demo-trader data contracts (ActivTrades DEMO discovery phase).

Pure, immutable, JSON-serialisable records shared by the opportunity generator, the demo runner,
the recorder and the learning stack. No venue, no MT5, no ML imports here.

Hard rules encoded by the types:
  * `OpportunitySnapshot` holds PRE-DECISION information only (no outcome fields exist on it).
  * `Decision` is a separate insert-once record keyed by `opportunity_id`.
  * Outcomes / counterfactual labels are separate records written AFTER the horizon.
  * Every record carries `phase` (DISCOVERY | FROZEN) so discovery-demo data can never be mistaken
    for a clean frozen-forward test.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

SCHEMA_VERSION = "demo-contracts-1"
Phase = Literal["DISCOVERY", "FROZEN"]
PHASES: tuple[str, ...] = ("DISCOVERY", "FROZEN")
MAX_BROKER_LEVERAGE = 30  # hard cap, never a target

# Lane E2: exit reasons of full closes decided by the ExitEngine (exit_policy="staged"). They are
# STRATEGY exits (the strategy's own chart/time/deterioration rules decided them), so they are
# UNCENSORED in the store - unlike MANUAL / EXTERNAL / SAFETY_FLATTEN. An engine
# EMERGENCY_RISK_EXIT is a safety action, reported as SAFETY_FLATTEN (censored), never as these.
ENGINE_EXIT_REASONS: frozenset[str] = frozenset(
    {
        "EXIT_ENGINE_TP1", "EXIT_ENGINE_TP2", "EXIT_ENGINE_TP3", "EXIT_ENGINE_TP4",
        "EXIT_ENGINE_TRAIL", "EXIT_ENGINE_BREAK_EVEN", "EXIT_ENGINE_STOP", "EXIT_ENGINE_STRUCTURE",
        "EXIT_ENGINE_MOMENTUM", "EXIT_ENGINE_LIQUIDITY", "EXIT_ENGINE_TIME_STOP", "EXIT_ENGINE_EOD",
        "EXIT_ENGINE_GIVEBACK",
    }
)


def stable_hash(*parts: Any, n: int = 16) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:n]


def opportunity_id_for(market: str, strategy_id: str, spec_hash: str, signal_ts_utc: str,
                       direction: int) -> str:
    """Deterministic id: the same causal opportunity always maps to the same id (dedupe)."""
    return "opp-" + stable_hash(market, strategy_id, spec_hash, signal_ts_utc, direction)


def _plain(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str, sort_keys=True))


class _Record:
    def to_dict(self) -> dict[str, Any]:
        return _plain(asdict(self))  # type: ignore[call-overload]

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass(frozen=True, slots=True, kw_only=True)
class ClockCheck(_Record):
    """UTC -> market tz -> DST -> local trading minute -> session -> SimWindow (addendum item 3)."""

    utc: str
    market_tz: str
    local_iso: str
    utc_offset_min: int
    local_minute: int
    session_bucket: str
    in_entry_window: bool
    minutes_to_forced_flat: int
    calendar_status: str  # "verified_current_constants" | "provisional"


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeGeometry(_Record):
    intended_entry: float
    entry_zone_lo: float
    entry_zone_hi: float
    invalidation: float
    stop: float
    target: float | None
    risk_distance: float  # |entry - stop| in price units
    min_space_r: float
    space_to_opposition_r: float | None
    expected_horizon_s: int
    exit_kind: str  # "fixed_r" | "trail"
    exit_r: float


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketState(_Record):
    bid: float
    ask: float
    spread: float  # price units
    atr: float
    realized_vol: float | None
    tick_activity: float | None  # ticks or tick-volume per minute if available
    clock: ClockCheck


@dataclass(frozen=True, slots=True, kw_only=True)
class OpportunitySnapshot(_Record):
    opportunity_id: str
    phase: Phase
    market: str
    broker_symbol: str
    direction: int  # +1 long, -1 short
    signal_ts_utc: str  # bar CLOSE of the deciding bar
    created_utc: str
    versions: dict[str, str]  # git_commit, config_hash, market_spec, feature, strategy_hash, model
    context: dict[str, Any]  # per timeframe D1/H4/H1/M15/M5/M1 causal state
    structure: dict[str, Any]  # zones, trend, breakout, retest, sweep, BOS, CHOCH, pattern, events
    geometry: TradeGeometry
    market_state: MarketState
    signal: dict[str, Any]  # family, strategy_id, confluence, independent_clusters, quality
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ValueError(f"bad phase {self.phase!r}")
        if self.direction not in (1, -1):
            raise ValueError("direction must be +1/-1")


@dataclass(frozen=True, slots=True, kw_only=True)
class Decision(_Record):
    opportunity_id: str
    phase: Phase
    decided_utc: str
    accepted: bool
    reasons: tuple[str, ...]  # exact machine-readable reason codes (accept AND reject)
    policy_id: str  # e.g. "static-demo-policy-v1" (champion)
    shadow: dict[str, Any] = field(default_factory=dict)  # challenger predictions (pre-trade only)


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeIntent(_Record):
    """What the runner hands to Risk -> Execution for an ACCEPTED opportunity."""

    opportunity_id: str
    phase: Phase
    intent_id: str  # idempotency key; client order id derives from it
    market: str
    broker_symbol: str
    direction: int
    entry_ref: float
    stop: float
    target: float | None
    min_space_r: float
    valid_until_utc: str  # stale-signal expiry
    forced_flat_utc: str | None
    risk_fraction: float  # of equity, <= policy cap
    entry_tolerance: float | None = None  # adverse drift tolerated (price units)
    context: dict[str, Any] | None = None  # family/confidence/atr etc.: logged only, never sizing


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskRecord(_Record):
    equity: float
    risk_fraction: float
    risk_budget: float
    quantity: float
    leverage: float
    approved: bool
    reject_reason: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionRecord(_Record):
    intended_entry: float
    fill_price: float | None
    quantity: float
    spread_at_send: float | None
    slippage: float | None
    fees: float | None
    swap: float | None
    broker_order_id: str | None
    broker_position_id: str | None
    protection_confirmed: bool
    parity_checks: dict[str, bool] = field(default_factory=dict)  # target_crossed_at_fill etc.
    cost_status: str = "provisional"


@dataclass(frozen=True, slots=True, kw_only=True)
class OutcomeRecord(_Record):
    gross_r: float
    net_r: float
    pnl_eur: float
    mfe_r: float
    mae_r: float
    time_to_mfe_s: float | None
    time_to_mae_s: float | None
    holding_s: float
    exit_reason: str  # STOP | TARGET | SESSION_END | MANUAL | EXTERNAL | REJECTED_BY_BROKER
    partial_fills: int = 0
    closed_utc: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class CounterfactualLabel(_Record):
    """POST-OUTCOME label for a REJECTED opportunity (hypothetical, never traded)."""

    opportunity_id: str
    phase: Phase
    horizon_end_utc: str
    hypothetical_mfe_r: float
    hypothetical_mae_r: float
    hypothetical_r: float
    target_before_stop: bool | None
    labelled_utc: str
    # Lane X (additive, optional): entry vs exit quality fields; legacy labels: None
    entry_exit: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TradeLearningRecord(_Record):
    """Denormalised join of everything for one real DEMO trade (built after close)."""

    opportunity_id: str
    phase: Phase
    snapshot: OpportunitySnapshot
    decision: Decision
    risk: RiskRecord
    execution: ExecutionRecord
    outcome: OutcomeRecord
