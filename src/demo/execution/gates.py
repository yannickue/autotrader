# ruff: noqa: E501
"""Gate catalogue of the DEMO execution stack: every reject / fail-closed code, classified.

Purpose: keep SAFETY separate from quality and from arbitrary legacy thresholds, so that the audit
and the rejection funnel can show WHY an otherwise interesting opportunity was blocked and whether
that block is a real safety rule, a structural fact, a temporary v1 limitation or a legacy
threshold that should be reviewed. Pure data, no I/O.

Classes
* ``SAFETY``     hard rules that protect the account: DEMO-only, identity, reconciliation, stale
                 feed/signal, mandatory broker protection, leverage/margin, catastrophic
                 portfolio caps, duplicate/execution safety, kill switch.
* ``STRUCTURAL`` facts about the venue or the intent geometry (broker stop level, volume step,
                 entry already crossed, ...). Not risk opinions.
* ``TEMPORARY``  a limitation of THIS implementation (v1), not a risk rule; it names the condition
                 that lifts it and is counted separately in the funnel (``TEMPORARY_LIMITATION``).
* ``QUALITY``    signal-quality inputs (confidence, confluence, family score, quality components,
                 expected payoff, win probability): logged and ranked ONLY - ``hard`` is False and
                 they never reject in the executor.
* ``LEGACY_ARBITRARY`` thresholds inherited from earlier lanes / the simulator that are not
                 derived from safety; kept for parity, flagged for review.

Netting. "One net position per symbol" (``NETTING_ONE_NET_POSITION_PER_SYMBOL``) describes ONLY
how the RETAIL_NETTING broker represents positions. It is NOT a one-opportunity-per-symbol rule:
internally several intents (tranches) may contribute to the same net position as long as the
portfolio limits hold. v1 cannot protect several tranches with different structural stops with
ONE position-level SL/TP, so add-on exposure is a documented TEMPORARY limitation.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum


class GateClass(StrEnum):
    SAFETY = "SAFETY"
    STRUCTURAL = "STRUCTURAL"
    TEMPORARY = "TEMPORARY"
    QUALITY = "QUALITY"
    LEGACY_ARBITRARY = "LEGACY_ARBITRARY"


@dataclass(frozen=True, slots=True)
class Gate:
    code: str
    gate_class: GateClass
    hard: bool  # True: a violation blocks/aborts; False: informational only
    why: str
    emits: str = "reject"  # reject (Rejected event) | fatal (StackFailClosed) | log_only | none
    lifting_condition: str | None = None  # TEMPORARY gates: what removes the limitation


# ---- reject / fatal reason codes (single source of truth; modules import these names) ----------------

R_HALTED = "halted"
R_UNKNOWN_MARKET = "unknown_market"
R_SYMBOL_MISMATCH = "symbol_mismatch"
R_INVALID_DIRECTION = "invalid_direction"
R_STALE_SIGNAL = "stale_signal"
R_PAST_FORCED_FLAT = "past_forced_flat"
R_DUPLICATE_INTENT = "duplicate_intent"
R_STALE_FEED = "stale_feed"
R_INVALID_QUOTE = "invalid_quote"
R_FX_STALE = "fx_rate_stale"
R_FX_UNAVAILABLE = "fx_rate_unavailable"
R_UNSUPPORTED_PROFIT_CCY = "unsupported_profit_currency"
R_FOREIGN_POSITION = "foreign_position_at_broker"
R_BROKER_CALL_FAILED = "broker_call_failed"
R_SPREAD_CAP = "spread_cap"
R_ENTRY_OVERSHOOT = "entry_overshoot"
R_INVALIDATION_CROSSED = "structural_invalidation_crossed"
R_TARGET_CROSSED = "target_crossed_at_fill"
R_MIN_SPACE_R = "min_space_r"
R_ADDON = "ADDON_EXPOSURE_NOT_SUPPORTED_V1"
R_OPPOSITE = "OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1"
R_ADDON_SHARED = "ADDON_SHARED_STOP_POSSIBLE_NOT_YET_IMPLEMENTED"
R_SIZE_BELOW_MIN = "size_below_min"
R_DAILY_LOSS = "daily_loss_limit"
R_DRAWDOWN = "drawdown_limit"
R_CONSECUTIVE_LOSSES = "consecutive_loss_limit"
R_EQUITY = "equity_non_positive"
R_INVALID_INPUT = "invalid_input"
R_INVALID_STOP = "invalid_stop"
R_INVALID_MARKET_FACTS = "invalid_market_facts"
R_UNKNOWN_CLUSTER = "unknown_cluster"
R_RISK_FRACTION_INVALID = "risk_fraction_invalid"
R_MARGIN_LIQUIDATION = "margin_stop_too_close_to_liquidation"
R_MARGIN_BEYOND = "margin_stop_beyond_liquidation"
R_EXPOSURE_LIMIT = "exposure_limit"
R_DATA_STALE = "data_stale"
R_DATA_NOT_LIVE = "data_not_live"
R_SIGNAL_STALE = "signal_stale"
R_SPREAD_TOO_WIDE = "spread_too_wide"
R_ENTRY_DEVIATION = "entry_price_deviation"
R_LIQUIDITY = "liquidity_insufficient"
R_INSTRUMENT_MISMATCH = "instrument_mismatch"
R_RISK_ERROR = "risk_error"
R_PROTECTION_UNCONFIRMED = "protection_unconfirmed"
R_OUTCOME_UNKNOWN = "order_outcome_unknown"
R_BROKER_REJECT = "broker_reject"
R_STOP_LEVEL = "stop_inside_broker_stop_level"
R_INVALID_VOLUME = "invalid_volume"
R_REQUEST_REJECTED = "request_rejected"
R_NOT_RECONCILED = "not_reconciled"
R_RUNTIME_NOT_READY = "runtime_not_ready"
R_UNPROTECTED_POSITION = "unprotected_position"
R_NON_DEMO = "non_demo_account"
R_IDENTITY = "account_identity_changed"
R_BROKER_DISCONNECT = "broker_disconnect"
R_NO_BROKER_RECORD = "no_broker_record"
R_EXECUTION_DENIED = "execution_denied"
R_NO_STOP = "no_stop"
R_QUANTITY_PRECISION = "quantity_precision"
R_CLOCK_SKEW = "clock_skew"

# entry-drift tolerance default (parity.entry_tolerance): max(2 x current spread, 2 x tick)
ENTRY_TOLERANCE_SPREAD_MULTIPLE = 2
# Spread/cost gate (user decision 2026-09-30): the PRIMARY gate is relative cost - the spread may take at
# most SPREAD_MAX_FRACTION_OF_RISK of 1R. The per-market absolute bound (historical p99 bar spread) is
# only a SAFETY cap against feed glitches / news gaps at SPREAD_EXTREME_MULTIPLE x that bound. Live
# spreads at the bar-close instant run ~2x the recorded bar-spread median, so the absolute p99 alone
# would suppress most NAS100 setups.
SPREAD_MAX_FRACTION_OF_RISK = 0.20
SPREAD_EXTREME_MULTIPLE = 4
ENTRY_TOLERANCE_TICK_MULTIPLE = 2

_G = Gate
S, T, Q, L, X = (
    GateClass.SAFETY,
    GateClass.STRUCTURAL,
    GateClass.QUALITY,
    GateClass.LEGACY_ARBITRARY,
    GateClass.TEMPORARY,
)

_ENTRIES: tuple[Gate, ...] = (
    # -- SAFETY (hard) -------------------------------------------------------------------------------
    _G(R_HALTED, S, True, "kill switch / halt latch (protection failure, unknown outcome, external activity, drawdown latch)"),
    _G(R_UNKNOWN_MARKET, S, True, "intent for a market outside the configured DEMO universe"),
    _G(R_SYMBOL_MISMATCH, S, True, "broker symbol of the intent differs from the configured mapping"),
    _G(R_INVALID_DIRECTION, S, True, "direction must be +1 / -1"),
    _G(R_STALE_SIGNAL, S, True, "stale signal: intent validity window elapsed (stale data)"),
    _G(R_PAST_FORCED_FLAT, S, True, "time stop: the intent's forced-flat time has already passed"),
    _G(R_DUPLICATE_INTENT, S, True, "duplicate / execution safety: exactly one order per intent_id"),
    _G(R_STALE_FEED, S, True, "stale feed: executable quote older than the freshness bound"),
    _G(R_INVALID_QUOTE, S, True, "non-positive or crossed quote"),
    _G(R_FX_STALE, S, True, "account-currency conversion needs a fresh EURUSD quote"),
    _G(R_FX_UNAVAILABLE, S, True, "account-currency conversion rate unavailable"),
    _G(R_UNSUPPORTED_PROFIT_CCY, S, True, "profit currency other than EUR/USD cannot be converted safely"),
    _G(R_FOREIGN_POSITION, S, True, "a position we did not open exists at the broker: account state not ours to size against"),
    _G(R_BROKER_CALL_FAILED, S, True, "broker read failed while the terminal is still attached"),
    _G(R_SPREAD_CAP, S, True, "execution-cost protection at send time: PRIMARY = spread > 20% of 1R (relative cost); SAFETY cap = spread > 4x the PROVISIONAL per-market M5 p99 bound (feed glitch / news gap)"),
    _G(R_INVALIDATION_CROSSED, S, True, "structural stop already crossed by the executable price: a protective stop cannot be placed"),
    _G(R_SIZE_BELOW_MIN, S, True, "the broker MINIMUM lot would violate a configured hard safety cap (violated_cap + numbers in risk_detail)"),
    _G(R_DAILY_LOSS, S, True, "daily loss halt (fraction of start-of-day equity, UTC day)"),
    _G(R_DRAWDOWN, S, True, "maximum drawdown halt (fraction of peak equity)"),
    _G(R_CONSECUTIVE_LOSSES, S, True, "consecutive-loss halt"),
    _G(R_EQUITY, S, True, "equity not positive"),
    _G(R_INVALID_INPUT, S, True, "non-finite / malformed risk input"),
    _G(R_INVALID_STOP, S, True, "stop on the wrong side of the executable price or zero distance"),
    _G(R_INVALID_MARKET_FACTS, S, True, "market facts (contract size / fx) invalid"),
    _G(R_UNKNOWN_CLUSTER, S, True, "market without a correlation cluster cannot be portfolio-limited"),
    _G(R_RISK_FRACTION_INVALID, S, True, "target risk fraction must be positive"),
    _G(R_MARGIN_LIQUIDATION, S, True, "structural stop too close to the (buffered) liquidation estimate"),
    _G(R_MARGIN_BEYOND, S, True, "structural stop beyond the liquidation estimate"),
    _G(R_EXPOSURE_LIMIT, S, True, "portfolio leverage / gross / net exposure capacity exhausted"),
    _G(R_DATA_STALE, S, True, "market data older than the policy bound"),
    _G(R_DATA_NOT_LIVE, S, True, "market data not LIVE quality"),
    _G(R_SIGNAL_STALE, S, True, "signal older than the policy bound"),
    _G(R_SPREAD_TOO_WIDE, S, True, "spread (bps) above the instrument bound"),
    _G(R_ENTRY_DEVIATION, S, True, "sizing reference deviates from the executable price beyond tolerance"),
    _G(R_LIQUIDITY, S, True, "no liquidity capacity"),
    _G(R_INSTRUMENT_MISMATCH, S, True, "instrument mismatch between request, snapshot and limits"),
    _G(R_RISK_ERROR, S, True, "internal risk-evaluation error: fail closed"),
    _G(R_PROTECTION_UNCONFIRMED, S, True, "mandatory broker-side protection could not be confirmed: reduce-only flatten + halt"),
    _G(R_OUTCOME_UNKNOWN, S, True, "order outcome unknown: never retried, halts new exposure until reconciled"),
    _G(R_BROKER_REJECT, S, True, "the broker refused the order (order_check / order_send)"),
    _G(R_NOT_RECONCILED, S, True, "venue reconciliation not RECONCILED"),
    _G(R_RUNTIME_NOT_READY, S, True, "adapter runtime not READY"),
    _G(R_UNPROTECTED_POSITION, S, True, "an unprotected position exists at the broker"),
    _G(R_NON_DEMO, S, True, "account is not DEMO"),
    _G(R_IDENTITY, S, True, "attached account changed"),
    _G(R_BROKER_DISCONNECT, S, True, "broker disconnected"),
    _G(R_NO_BROKER_RECORD, S, True, "send never reached the broker (resolved by reconciliation)"),
    _G(R_EXECUTION_DENIED, S, True, "execution layer denied the order (see suffix)"),
    _G(R_NO_STOP, S, True, "a broker-side stop is mandatory for every entry"),
    _G(R_CLOCK_SKEW, S, True, "server quote ahead of the local clock by more than the tolerance but below the fatal threshold: this market is rejected temporarily (logged metric), the stack keeps running"),
    _G(R_QUANTITY_PRECISION, T, True, "quantity not representable on the instrument step"),
    # -- STRUCTURAL ----------------------------------------------------------------------------------------
    _G(R_ENTRY_OVERSHOOT, T, True, "intent geometry: the executable price drifted beyond the intended entry on the adverse side by MORE than the tolerance (TradeIntent.entry_tolerance, else max(2 x spread, 2 x tick))"),
    _G(R_TARGET_CROSSED, T, True, "intent geometry: the target is already crossed at the executable price"),
    _G(R_STOP_LEVEL, T, True, "broker constraint: stop inside the broker minimum stop distance"),
    _G(R_INVALID_VOLUME, T, True, "broker constraint: volume outside min/max/step"),
    _G(R_REQUEST_REJECTED, T, True, "broker request constraint (price/filling/comment); see suffix"),
    _G("NETTING_ONE_NET_POSITION_PER_SYMBOL", T, False, "BROKER representation only (RETAIL_NETTING): one net position per symbol. NOT a one-opportunity-per-symbol rule; the internal tranche ledger is keyed by intent_id", emits="none"),
    # -- TEMPORARY (v1 limitations, counted separately) ----------------------------------------------------------
    _G(R_ADDON, X, True, "TEMPORARY_STRUCTURAL_LIMITATION: a second same-direction tranche on a symbol with an open net position cannot be broker-protected independently (ONE position-level SL/TP protects the whole net position)", lifting_condition="a proven mechanism keeps EVERY tranche's structural invalidation protected within the hard caps: recompute the position SL as tranches change, or reduce-only pending stop orders per tranche"),
    _G(R_ADDON_SHARED, X, True, "TEMPORARY_STRUCTURAL_LIMITATION: a same-direction add-on that COULD share the existing net position's broker stop and target is permitted in principle (see tranches.classify_addon) but the execution path for it is not implemented yet", lifting_condition="adapter add-on support: explicit shared SL/TP on the combined net position, verified at the broker after the fill, combined size checked against the position/aggregate/cluster/leverage/margin caps, tranche ledger updated, Nautilus protective-order bookkeeping for the enlarged position"),
    _G(R_OPPOSITE, X, True, "TEMPORARY_STRUCTURAL_LIMITATION: an opposite-side signal while a net position is open would reduce/reverse it; explicit reduction/reversal handling is out of scope in v1", lifting_condition="explicit reduce/reverse handling with tranche accounting and re-protection of the remaining net position"),
    # -- LEGACY / ARBITRARY ------------------------------------------------------------------------------------
    _G(R_MIN_SPACE_R, L, True, "minimum reward space to target in R on the executable price; kept for simulator parity, NOT derived from safety - review before FROZEN phase"),
    # -- QUALITY (never reject in the executor) --------------------------------------------------------------------
    _G("confidence", Q, False, "signal confidence: logged / ranked only", emits="log_only"),
    _G("confluence", Q, False, "confluence count: logged / ranked only", emits="log_only"),
    _G("family_score", Q, False, "family score: logged / ranked only", emits="log_only"),
    _G("quality_components", Q, False, "quality components: logged / ranked only", emits="log_only"),
    _G("expected_payoff_r", Q, False, "expected payoff (R): logged only", emits="log_only"),
    _G("win_probability", Q, False, "predicted win probability / uncertainty (uncalibrated, shadow): logged only, never scales risk", emits="log_only"),
    _G("atr", Q, False, "volatility (ATR): logged and used for stop/ATR ratio only", emits="log_only"),
    # -- FATAL (StackFailClosed) ---------------------------------------------------------------------------------------
    _G("unknown_account", S, True, "account unreadable", emits="fatal"),
    _G("account_identity_mismatch", S, True, "terminal attached to another account than expected", emits="fatal"),
    _G("unexpected_server", S, True, "attached server differs from the expected DEMO server", emits="fatal"),
    _G("account_server_unknown", S, True, "server name unreadable", emits="fatal"),
    _G("unsupported_account_currency", S, True, "account currency not EUR", emits="fatal"),
    _G("broker_leverage_above_ceiling", S, True, "account leverage above the hard 30x ceiling (the observed leverage is part of the message; the ceiling is never raised)", emits="fatal"),
    _G("clock_anomaly", S, True, "server quote SUSTAINEDLY (last N observations) ahead of the local clock by more than clock_fatal_skew_s: local clock cannot be trusted", emits="fatal"),
    _G("server_time_offset_mismatch", S, True, "start self-check: the newest tick, converted with the inferred server timezone, is a whole 1-2 hours away from the local UTC clock (DST change / wrong zone): quote freshness cannot be trusted", emits="fatal"),
    _G("ambiguous_server_time", S, True, "newest tick inside an ambiguous DST hour", emits="fatal"),
    _G("unprotected_exposure", S, True, "own exposure without a broker stop", emits="fatal"),
    _G("terminal_lock_lost", S, True, "single-owner terminal lock lost", emits="fatal"),
    _G("mt5_lane_timeout", S, True, "MT5 IPC call exceeded its bound; terminal state unknown", emits="fatal"),
    _G("mt5_rates_schema", S, True, "bar schema differs from the observed layout", emits="fatal"),
    _G("account_login_enabled", S, True, "attach-only stack refuses MT5_ALLOW_ACCOUNT_LOGIN", emits="fatal"),
    _G("flatten_failed", S, True, "reduce-only flatten failed repeatedly", emits="fatal"),
    _G("connect_failed", S, True, "attach / lock / netting / instrument load failed", emits="fatal"),
    _G("start_failed", S, True, "start-up failed", emits="fatal"),
    _G("spec_mismatch", S, True, "broker contract/tick size differs from the checked-in market config", emits="fatal"),
    _G("stack_not_running", S, True, "stack not started or already stopped", emits="fatal"),
    _G("stack_not_started", S, True, "stack not started", emits="fatal"),
)

GATE_CATALOG: Mapping[str, Gate] = {g.code: g for g in _ENTRIES}

# hard caps of the sizing layer: ``risk_detail["violated_cap"]`` names one of these
CAP_GATES: tuple[Gate, ...] = (
    _G("max_position_stop_risk_fraction", S, True, "per-trade stop risk cap (fraction of equity)", emits="none"),
    _G("max_aggregate_open_stop_risk_fraction", S, True, "aggregate open stop-risk cap (fraction of equity)", emits="none"),
    _G("max_cluster_stop_risk_fraction", S, True, "correlated-cluster stop-risk cap (INDEX/METAL/FX)", emits="none"),
    _G("max_family_share_of_open_risk", S, True, "per-family concentration cap (share of the aggregate risk budget)", emits="none"),
    _G("max_leverage", S, True, "position leverage cap (hard 30x ceiling, never a target)", emits="none"),
    _G("max_portfolio_leverage", S, True, "gross portfolio leverage cap (hard 30x ceiling)", emits="none"),
    _G("liquidation_safe_leverage", S, True, "structural stop must stay clear of the estimated (buffered) liquidation price at the portfolio leverage after the fill", emits="none"),
    _G("max_margin_fraction_of_free_margin", S, True, "order margin must fit the broker's free margin", emits="none"),
    _G("instrument_max_leverage", S, True, "observed broker margin leverage of the instrument", emits="none"),
    _G("instrument_volume_max", T, True, "broker maximum volume", emits="none"),
)
CATALOG_WITH_CAPS: Mapping[str, Gate] = {**GATE_CATALOG, **{g.code: g for g in CAP_GATES}}


def base_code(reason: str) -> str:
    """``execution_denied:ORDER_CHECK_10016`` -> ``execution_denied``."""
    return reason.split(":", 1)[0]


def gate_for(reason: str) -> Gate | None:
    return CATALOG_WITH_CAPS.get(base_code(reason))


TEMPORARY_LIMITATION = "TEMPORARY_LIMITATION"


def funnel(
    reasons: Iterable[str], *, otherwise_valid: Iterable[str] = ()
) -> dict[str, dict[str, object]]:
    """Rejection funnel by gate class. TEMPORARY limitations form their OWN category
    (``TEMPORARY_LIMITATION``) with the count of otherwise-valid opportunities they blocked."""
    counts = Counter(reasons)
    valid = Counter(otherwise_valid)
    out: dict[str, dict[str, object]] = {}
    for reason, n in counts.items():
        gate = gate_for(reason)
        key = (
            TEMPORARY_LIMITATION
            if gate is not None and gate.gate_class is GateClass.TEMPORARY
            else (gate.gate_class.value if gate is not None else "UNCLASSIFIED")
        )
        bucket = out.setdefault(key, {"total": 0, "codes": {}})
        bucket["total"] = int(bucket["total"]) + n  # type: ignore[call-overload]
        bucket["codes"][reason] = n  # type: ignore[index]
    if TEMPORARY_LIMITATION in out:
        out[TEMPORARY_LIMITATION]["otherwise_valid_blocked"] = sum(valid.values())
        out[TEMPORARY_LIMITATION]["otherwise_valid_by_code"] = dict(valid)
    return out
