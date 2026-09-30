# ruff: noqa: E501
"""Static demo policy ``static-demo-policy-v1``: technical-validity gates only.

The policy does NOT judge whether an opportunity is "good"; it only rejects opportunities that could
not be executed the way the simulator (``alpha.fast.sim._simulate_kernel``) would execute them. All
price checks are evaluated on the EXECUTABLE side (ask for a long, bid for a short) = the actual-fill
proxy, mirroring the simulator:

* fill        long: ask, short: bid (sim: ``o[j] + spread`` / ``o[j]``)
* fill_risk   ``d * (fill - stop)``; ``<= 0`` means the stop is already crossed at the fill (sim ``entry_gap``)
* target      finite structural target: must lie strictly beyond the fill by more than
              ``1e-9 * max(1, |fill|)`` and the implied R (beyond / fill_risk) must be finite and > 0
              (sim skip ``target_crossed_at_fill``); NaN target = R target ``fill + d * r * fill_risk``
* min space   implied R of a finite target must be ``>= min_space_r - 1e-12`` at the fill
              (sim skip ``space_below_min_at_fill``)
* spread      ``max(spread of the deciding bar, current quote spread) <= MarketSpec.max_entry_spread_price``
              (sim: ``max(raw_spread[i], raw_spread[j])``)
* window      the ENTRY bar (opens at the signal timestamp) must have its LOCAL minute in the spec's
              effective ``[entry_start, entry_end)`` on the same local day as the deciding bar
              (sim skips ``outside_window`` / ``gap_before_entry``)
* flat_min    the clock exit is the spec's effective ``exit_min`` (goes to ``TradeIntent.forced_flat_utc``)

No one-position rule: the policy never rejects because a position exists on the instrument. Broker
netting (one net position per symbol) is only the broker's representation; same-symbol add-on /
opposite-side handling belongs to the execution stack (temporary ``ADDON_*`` codes). QUALITY inputs
(confluence, family score, quality) are logged in the snapshot and NEVER reject. Every reject code is
classified in ``GATE_CLASSIFICATION`` (used by the rejection funnel).

Not in this policy (Risk / execution lanes, they need equity and lot rules): sim ``risk_out_of_range``
and ``size_below_min``.

Reason codes are exact strings (``REASONS``); an accepted decision has exactly ``("ACCEPTED",)``, a
rejected one carries EVERY applicable gate in the fixed ``REASONS`` order (useful for counterfactuals).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from alpha.families.spec import EffectiveWindow
from alpha.fast.sim import EXIT_TRAIL
from demo.contracts import (
    ClockCheck,
    Decision,
    OpportunitySnapshot,
    Phase,
    TradeGeometry,
    TradeIntent,
    stable_hash,
)
from demo.opportunity.bar_source import Quote
from demo.opportunity.clock import (
    forced_flat_utc,
    local_of,
    make_clock_check,
    to_utc,
)
from demo.opportunity.operating_policy import OperatingPolicy
from markets.spec import MarketSpec

POLICY_ID = "static-demo-policy-v1"

STALE_SIGNAL = "STALE_SIGNAL"
OUTSIDE_ENTRY_WINDOW = "OUTSIDE_ENTRY_WINDOW"
SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
# PRIMARY cost gate = spread <= 20% of 1R; absolute per-market p99 bound only as a 4x SAFETY cap
# (user decision 2026-09-30; mirrors demo.execution.gates, which this layer must not import).
SPREAD_MAX_FRACTION_OF_RISK = 0.20
SPREAD_EXTREME_MULTIPLE = 4
ENTRY_OVERSHOT = "ENTRY_OVERSHOT"
TARGET_ALREADY_CROSSED = "TARGET_ALREADY_CROSSED"
SPACE_BELOW_MIN_R = "SPACE_BELOW_MIN_R"
NO_STRUCTURAL_STOP = "NO_STRUCTURAL_STOP"
DUPLICATE_OPPORTUNITY = "DUPLICATE_OPPORTUNITY"
MARKET_CLOSED = "MARKET_CLOSED"
CLOCK_ANOMALY = "CLOCK_ANOMALY"
ACCEPTED = "ACCEPTED"

# fixed emission order of reject reasons
REASONS: tuple[str, ...] = (
    CLOCK_ANOMALY, MARKET_CLOSED, STALE_SIGNAL, DUPLICATE_OPPORTUNITY, OUTSIDE_ENTRY_WINDOW,
    NO_STRUCTURAL_STOP, SPREAD_TOO_WIDE, ENTRY_OVERSHOT, TARGET_ALREADY_CROSSED, SPACE_BELOW_MIN_R,
)
ALL_CODES: tuple[str, ...] = (*REASONS, ACCEPTED)


@dataclass(frozen=True, slots=True)
class PolicyGate:
    code: str
    gate_class: str  # SAFETY | STRUCTURAL | QUALITY | LEGACY_ARBITRARY | TEMPORARY
    hard: bool
    why: str


_PG = PolicyGate
GATE_CLASSIFICATION: dict[str, PolicyGate] = {
    g.code: g
    for g in (
        _PG(CLOCK_ANOMALY, "SAFETY", True, "signal close off the M5 grid / in the future / bar span wrong: clock or data cannot be trusted"),
        _PG(MARKET_CLOSED, "SAFETY", True, "no valid executable quote or weekend: nothing to execute against"),
        _PG(STALE_SIGNAL, "SAFETY", True, "stale signal or stale quote: the opportunity is no longer executable as decided"),
        _PG(DUPLICATE_OPPORTUNITY, "SAFETY", True, "duplicate safety: the same causal opportunity is decided exactly once"),
        _PG(OUTSIDE_ENTRY_WINDOW, "STRUCTURAL", True, "the entry bar lies outside the frozen spec's entry window (simulator: outside_window / gap_before_entry)"),
        _PG(NO_STRUCTURAL_STOP, "STRUCTURAL", True, "no finite structural stop on the correct side of the close (or trailing exit): a mandatory broker stop cannot be placed"),
        _PG(SPREAD_TOO_WIDE, "SAFETY", True, "execution-cost protection: PRIMARY spread > 20% of 1R; SAFETY cap spread > 4x the per-market p99 bound"),
        _PG(ENTRY_OVERSHOT, "STRUCTURAL", True, "entry already crossed: executable price drifted beyond entry_tolerance_atr adverse to the decision close, or the structural stop is already crossed at the fill (simulator: entry_gap)"),
        _PG(TARGET_ALREADY_CROSSED, "STRUCTURAL", True, "the finite structural target is already crossed at the executable price (simulator: target_crossed_at_fill)"),
        _PG(SPACE_BELOW_MIN_R, "LEGACY_ARBITRARY", True, "candidate-carried minimum reward space (simulator: space_below_min_at_fill); policy default is 0.0 so it only fires for specs carrying their own min_space_r; review before FROZEN"),
    )
}
# Lane U2: terminal MEASUREMENT-ONLY reject codes (never an intent, never the stack). Deliberately NOT part of
# ``GATE_CLASSIFICATION`` / ``REASONS`` (those describe the tradable policy); the funnel / labeller read ``SHADOW_SCAN_GATES``.
OUT_OF_WINDOW_SHADOW = "OUT_OF_WINDOW_SHADOW"
SHADOW_UNIVERSE = "SHADOW_UNIVERSE"
SHADOW_SCAN_GATES: dict[str, PolicyGate] = {
    OUT_OF_WINDOW_SHADOW: _PG(OUT_OF_WINDOW_SHADOW, "WINDOW", True, "active market, broker tradable, but the frozen family's entry window is closed: what the family WOULD have signalled (measurement only, no widening of any window)"),
    SHADOW_UNIVERSE: _PG(SHADOW_UNIVERSE, "SHADOW_UNIVERSE", True, "market is shadow-only (not in the trading registry): observed, evaluated and labelled, never tradable"),
}
QUALITY_INPUTS: tuple[str, ...] = ("confidence", "confluence", "family_score", "quality", "independent_clusters")
# Logged / ranked only; there is deliberately NO policy reject code for any of them.

M5_SECONDS = 300
PositionHook = Callable[[str], bool]


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyConfig:
    stale_after_s: float = 120.0  # now - signal close beyond this => STALE_SIGNAL
    quote_max_age_s: float = 60.0  # quote older than this (vs now) => STALE_SIGNAL
    clock_skew_s: float = 5.0  # signal close in the future by more than this => CLOCK_ANOMALY
    entry_tolerance_atr: float = 0.5  # adverse drift of the executable price vs decision close (STRUCTURAL ENTRY_OVERSHOT)
    # Default 0.0 = exact simulator behaviour: only a target that is already crossed (implied R <= 0)
    # rejects. A ``min_space_r`` carried by the candidate (frozen spec) still applies (SPACE_BELOW_MIN_R,
    # LEGACY_ARBITRARY). Do NOT raise this default to tune opportunity counts.
    min_space_r: float = 0.0
    risk_fraction: float = 0.01
    valid_for_s: int = M5_SECONDS  # TradeIntent expiry = signal_ts + one M5 bar

    def config_hash(self) -> str:
        return hashlib.sha256(repr(sorted(asdict(self).items())).encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """One causal signal of one frozen spec at the just-closed bar (pre-gate)."""

    market: str
    broker_symbol: str
    family: str
    strategy_id: str
    spec_hash: str
    direction: int
    signal_ts: datetime  # CLOSE of the deciding bar == OPEN of the entry bar (UTC)
    bar_open_ts: datetime  # OPEN of the deciding bar (UTC)
    close: float  # bid close of the deciding bar
    atr: float
    bar_spread: float  # PRICE units, recorded spread of the deciding bar
    stop: float
    target: float  # finite structural target price, NaN = R target
    target_r: float
    exit_kind: int
    min_space_r: float  # NaN = not set by the generator
    window: EffectiveWindow


@dataclass(frozen=True, slots=True, kw_only=True)
class Assessment:
    geometry: TradeGeometry
    reasons: tuple[str, ...]
    clock: ClockCheck
    exec_price: float
    bid: float
    ask: float
    implied_r: float | None

    @property
    def accepted(self) -> bool:
        return self.reasons == (ACCEPTED,)


def _fin(v: float) -> bool:
    return math.isfinite(v)


class StaticDemoPolicy:
    policy_id = POLICY_ID

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()

    def assess(
        self,
        cand: Candidate,
        quote: Quote | None,
        now: datetime,
        mspec: MarketSpec,
        *,
        is_duplicate: bool = False,
        position_open: bool = False,  # API compatibility ONLY: ignored; same-symbol handling is the stack's job
    ) -> Assessment:
        del position_open
        cfg = self.config
        now = to_utc(now)
        sig = to_utc(cand.signal_ts)
        d = cand.direction
        reasons: set[str] = set()

        # ---- quote / executable side ------------------------------------------------------
        have_quote = quote is not None and quote.valid
        if have_quote:
            assert quote is not None
            bid, ask = quote.bid, quote.ask
        else:
            bid, ask = cand.close, cand.close + cand.bar_spread
        exec_price = ask if d > 0 else bid

        # ---- clock -----------------------------------------------------------------------
        clock = make_clock_check(mspec, sig, cand.window)
        loc_sig = local_of(mspec, sig)
        loc_bar = local_of(mspec, cand.bar_open_ts)
        if (
            int(sig.timestamp()) % M5_SECONDS != 0
            or (sig - now).total_seconds() > cfg.clock_skew_s
            or sig - to_utc(cand.bar_open_ts) != timedelta(seconds=M5_SECONDS)
        ):
            reasons.add(CLOCK_ANOMALY)
        if (
            not have_quote or loc_sig.weekday() >= 5
        ):
            reasons.add(MARKET_CLOSED)
        if (now - sig).total_seconds() > cfg.stale_after_s:
            reasons.add(STALE_SIGNAL)
        if have_quote and quote is not None and (now - to_utc(quote.ts_utc)).total_seconds() > (
            cfg.quote_max_age_s
        ):
            reasons.add(STALE_SIGNAL)
        if is_duplicate:
            reasons.add(DUPLICATE_OPPORTUNITY)
        if not (
            loc_sig.date() == loc_bar.date()
            and cand.window.entry_start_min <= clock.local_minute < cand.window.entry_end_min
        ):
            reasons.add(OUTSIDE_ENTRY_WINDOW)

        # ---- geometry at the executable price --------------------------------------------
        stop_ok = _fin(cand.stop) and d * (cand.close - cand.stop) > 0.0 and cand.exit_kind != EXIT_TRAIL
        if not stop_ok:
            reasons.add(NO_STRUCTURAL_STOP)
        risk = d * (exec_price - cand.stop) if _fin(cand.stop) else float("nan")
        spread_now = (ask - bid) if have_quote else 0.0
        spread_seen = max(cand.bar_spread, spread_now)
        if spread_seen > mspec.max_entry_spread_price * SPREAD_EXTREME_MULTIPLE or (
            risk > 0.0 and spread_seen / risk > SPREAD_MAX_FRACTION_OF_RISK
        ):
            reasons.add(SPREAD_TOO_WIDE)

        atr = cand.atr if _fin(cand.atr) and cand.atr > 0 else float("nan")
        tol = cfg.entry_tolerance_atr * atr if _fin(atr) else 0.0
        if stop_ok and (not risk > 0.0 or d * (exec_price - cand.close) > tol):
            reasons.add(ENTRY_OVERSHOT)

        finite_target = _fin(cand.target)
        implied_r: float | None = None
        target_out: float | None
        req_space = 0.0
        if finite_target:
            target_out = float(cand.target)
            req_space = cand.min_space_r if _fin(cand.min_space_r) else cfg.min_space_r
            if _fin(risk) and risk > 0.0:
                beyond = d * (cand.target - exec_price)
                eps = 1e-9 * max(1.0, abs(exec_price))
                r = beyond / risk
                if not (beyond > eps and math.isfinite(r) and r > 0.0):
                    reasons.add(TARGET_ALREADY_CROSSED)
                else:
                    implied_r = float(r)
                    if r < req_space - 1e-12:
                        reasons.add(SPACE_BELOW_MIN_R)
        elif _fin(risk) and risk > 0.0 and _fin(cand.target_r) and cand.target_r > 0:
            target_out = exec_price + d * cand.target_r * risk
            implied_r = float(cand.target_r)
        else:
            target_out = None

        ordered = tuple(r for r in REASONS if r in reasons) or (ACCEPTED,)

        horizon = max(0, (cand.window.exit_min - clock.local_minute) * 60)
        risk_dist = float(abs(exec_price - cand.stop)) if _fin(cand.stop) else 0.0
        geometry = TradeGeometry(
            intended_entry=float(exec_price),
            entry_zone_lo=float(cand.close - tol),
            entry_zone_hi=float(cand.close + tol),
            invalidation=float(cand.stop) if _fin(cand.stop) else float(cand.close),
            stop=float(cand.stop) if _fin(cand.stop) else float(cand.close),
            target=target_out,
            risk_distance=risk_dist,
            min_space_r=float(req_space),
            space_to_opposition_r=None,
            expected_horizon_s=int(horizon),
            exit_kind="fixed_r",
            exit_r=float(implied_r if implied_r is not None else 0.0),
        )
        return Assessment(
            geometry=geometry, reasons=ordered, clock=clock, exec_price=float(exec_price),
            bid=float(bid), ask=float(ask), implied_r=implied_r,
        )

    # ---- records -----------------------------------------------------------------------------
    def decision(
        self, opportunity_id: str, phase: Phase, decided_utc: datetime, assessment: Assessment,
        shadow: dict | None = None,
    ) -> Decision:
        return Decision(
            opportunity_id=opportunity_id,
            phase=phase,
            decided_utc=to_utc(decided_utc).isoformat(),
            accepted=assessment.accepted,
            reasons=assessment.reasons,
            policy_id=POLICY_ID,
            shadow=shadow or {},
        )

    def intent_for(
        self, snapshot: OpportunitySnapshot, decision: Decision, mspec: MarketSpec,
        window: EffectiveWindow, operating: OperatingPolicy | None = None,
    ) -> TradeIntent | None:
        """TradeIntent of an ACCEPTED decision (None for a rejected one). Pure function of the records."""
        if not decision.accepted or decision.opportunity_id != snapshot.opportunity_id:
            return None
        sig = datetime.fromisoformat(snapshot.signal_ts_utc)
        g = snapshot.geometry
        return TradeIntent(
            opportunity_id=snapshot.opportunity_id,
            phase=snapshot.phase,
            intent_id="int-" + stable_hash(snapshot.opportunity_id, POLICY_ID),
            market=snapshot.market,
            broker_symbol=snapshot.broker_symbol,
            direction=snapshot.direction,
            entry_ref=g.intended_entry,
            stop=g.stop,
            target=g.target,
            min_space_r=g.min_space_r,
            valid_until_utc=(sig + timedelta(seconds=self.config.valid_for_s)).isoformat(),
            forced_flat_utc=forced_flat_utc(mspec, sig, window.exit_min, operating).isoformat(),
            risk_fraction=self.config.risk_fraction,
        )
