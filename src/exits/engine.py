# ruff: noqa: E501
"""Deterministic exit engine: decides when and how much of an open position
to reduce, from measurable position/market state only.

Design principle (from the sprint brief): a trade must never be cut short
just because an arbitrary fixed monetary target was reached, and a good
unrealized gain must never be allowed to disappear simply because a large
fixed take-profit hasn't been hit yet. This engine implements that by making
every threshold relative (R-multiples of the initial stop distance, basis
points, or fractions of the current position) and by treating a reached
target as a signal to take a PARTIAL profit, never an automatic full close:
the remainder of the position keeps running as a "runner", protected instead
by the break-even and trailing-stop ratchets. See `ExitPolicy` in
`src/exits/models.py` for every configurable threshold.

This module never calls a venue API (mirrors the strategy/`Signal` boundary
in the repository `CLAUDE.md`) and never depends on an LLM or remote
reasoning service -- every trigger reads already-computed, externally
supplied state (`ExitMarketState`) and applies deterministic threshold
comparisons only.

Reduce-only risk reservation wiring (docs/OPEN_QUESTIONS.md #24): once a
caller submits an `ExitDecision` to `RiskEngine.evaluate_reduce_only(
request_id=decision.request_id, ...)` and gets back an approved
`RiskDecision`, it MUST call `ExitEngine.notify_terminal(decision_id,
outcome)` on every terminal execution outcome (fill/cancel/reject) for that
reduce-only order -- exactly mirroring what `src/pipeline/paper.py` already
does for the main `evaluate()` entry path. `notify_terminal()` forwards to
the injected `RiskReleaseGate.release(decision_id)`, which is idempotent, so
double-notification is harmless. See `tests/unit/exits/test_engine.py::
test_notify_terminal_releases_reduce_only_reservation_on_every_terminal_outcome`
for a worked example against a stub gate.
"""

from dataclasses import replace
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Protocol

from exits.models import (
    ExitDecision,
    ExitEvaluation,
    ExitMarketState,
    ExitOutcome,
    ExitPolicy,
    ExitPosition,
    ExitReason,
    PositionSide,
    StopStage,
    TakeProfitStage,
    stage_target_price,
)
from risk.models import RiskSide

ZERO = Decimal("0")
TEN_THOUSAND = Decimal("10000")


class RiskReleaseGate(Protocol):
    """The one method of `RiskEngine` this module depends on.

    A `RiskEngine` instance already satisfies this Protocol structurally
    (`RiskEngine.release(self, decision_id: str) -> None`), so integration
    can pass the real engine directly; tests can pass a lightweight stub.
    """

    def release(self, decision_id: str) -> None: ...


class ExitEngine:
    """Evaluate one open position per tick and decide whether to reduce it.

    Stateless across positions: all mutable-looking state (stop ratchet,
    high-water mark, pending-close guard) lives on the `ExitPosition` the
    caller passes in and is threaded forward via `ExitEvaluation.updated_*`,
    so the engine itself is trivially safe to share across many positions
    and to unit test without hidden state.

    The only piece of real state `ExitEngine` owns is the injected
    `RiskReleaseGate`, used exclusively by `notify_terminal()`.
    """

    def __init__(self, *, policy: ExitPolicy, risk_gate: RiskReleaseGate) -> None:
        self._policy = policy
        self._risk_gate = risk_gate

    # -- reduce-only reservation release (docs/OPEN_QUESTIONS.md #24) -------

    def notify_terminal(self, decision_id: str, outcome: ExitOutcome) -> None:
        """Release the reduce-only risk reservation for a terminal outcome.

        Call this exactly once per terminal execution event (fill, cancel,
        or reject) for a reduce-only order this engine requested, mirroring
        `src/pipeline/paper.py`'s handling of the main `evaluate()` path. The
        underlying `RiskEngine.release()` is idempotent, so an extra call
        (e.g. a retried notification) is a safe no-op rather than a double
        release.
        """
        del outcome  # accepted for auditing/logging symmetry; release is unconditional
        self._risk_gate.release(decision_id)

    # -- evaluation -----------------------------------------------------------

    def evaluate(
        self, *, position: ExitPosition, market: ExitMarketState, now: datetime
    ) -> ExitEvaluation:
        """Decide whether `position` should be reduced given `market` at `now`.

        Fails closed on untrustworthy input: a stale, mismatched, or
        otherwise invalid market snapshot forces an immediate full
        `EMERGENCY_RISK_EXIT` rather than silently leaving the position
        unmanaged, unless a close is already pending or the position is
        already flat (see "no double close" below).
        """
        no_op = self._no_op_if_already_closing(position)
        if no_op is not None:
            return no_op

        try:
            self._validate_market(position=position, market=market, now=now)
        except ValueError as exc:
            return self._forced_emergency_close(
                position=position, now=now, detail=f"invalid or untrustworthy market data: {exc}"
            )

        return self._evaluate_inner(position=position, market=market, now=now)

    def _no_op_if_already_closing(self, position: ExitPosition) -> ExitEvaluation | None:
        # Already flat, or a close request is already outstanding for this
        # position: never emit a second, overlapping reduce-only request.
        if position.quantity <= 0 or position.pending_close_request_id is not None:
            return ExitEvaluation(
                decision=None,
                updated_stop_price=position.current_stop_price,
                updated_stop_stage=position.stop_stage,
                updated_high_water_mark=position.effective_high_water_mark,
                break_even_activated=False,
            )
        return None

    def _validate_market(
        self, *, position: ExitPosition, market: ExitMarketState, now: datetime
    ) -> None:
        if now.tzinfo is None or now.utcoffset() is None or now.utcoffset().total_seconds() != 0:
            raise ValueError("now must be UTC-aware")
        if market.instrument != position.instrument:
            raise ValueError("market.instrument does not match position.instrument")
        if market.timestamp > now:
            raise ValueError("market.timestamp is in the future")
        if (now - market.timestamp) > self._policy.max_market_data_age:
            raise ValueError("market data is stale")

    def _forced_emergency_close(
        self, *, position: ExitPosition, now: datetime, detail: str
    ) -> ExitEvaluation:
        decision = self._build_decision(
            position=position,
            quantity=position.quantity,
            is_partial=False,
            reason=ExitReason.EMERGENCY_RISK_EXIT,
            reason_detail=detail,
            now=now,
            metadata={"forced": True},
        )
        return ExitEvaluation(
            decision=decision,
            updated_stop_price=position.current_stop_price,
            updated_stop_stage=position.stop_stage,
            updated_high_water_mark=position.effective_high_water_mark,
            break_even_activated=False,
        )

    def _evaluate_inner(
        self, *, position: ExitPosition, market: ExitMarketState, now: datetime
    ) -> ExitEvaluation:
        policy = self._policy
        is_long = position.side == PositionSide.LONG

        high_water_mark = self._ratchet_high_water_mark(position, market.price, is_long)
        stop_price, stop_stage, break_even_activated = self._ratchet_stop(
            position=position, market=market, high_water_mark=high_water_mark, is_long=is_long
        )

        def evaluation(decision: ExitDecision | None) -> ExitEvaluation:
            return ExitEvaluation(
                decision=decision,
                updated_stop_price=stop_price,
                updated_stop_stage=stop_stage,
                updated_high_water_mark=high_water_mark,
                break_even_activated=break_even_activated,
            )

        # A. Emergency/risk exit -- highest priority, bypasses every other rule.
        if market.risk_halt:
            return evaluation(
                self._build_decision(
                    position=position,
                    quantity=position.quantity,
                    is_partial=False,
                    reason=ExitReason.EMERGENCY_RISK_EXIT,
                    reason_detail="external risk HALT",
                    now=now,
                    metadata={},
                )
            )

        # B. Original / break-even / trailing stop hit.
        stop_hit = market.price <= stop_price if is_long else market.price >= stop_price
        if stop_hit:
            reason = (
                ExitReason.TRAILING_STOP
                if stop_stage == StopStage.TRAILING
                else ExitReason.INVALIDATION_STOP
            )
            return evaluation(
                self._build_decision(
                    position=position,
                    quantity=position.quantity,
                    is_partial=False,
                    reason=reason,
                    reason_detail=(
                        f"price {market.price} crossed {stop_stage.value} stop {stop_price}"
                    ),
                    now=now,
                    metadata={"stop_stage": stop_stage.value, "stop_price": str(stop_price)},
                )
            )

        # C. Signal reversal.
        if market.signal_reversal:
            return evaluation(
                self._build_decision(
                    position=position,
                    quantity=position.quantity,
                    is_partial=False,
                    reason=ExitReason.SIGNAL_REVERSAL,
                    reason_detail="opposite-direction signal fired",
                    now=now,
                    metadata={},
                )
            )

        # C2. Structural failure (Lane E2): a closed bar broke the newest confirmed post-entry swing.
        if policy.structure_failure_exit and market.structure_failure:
            return evaluation(
                self._build_decision(
                    position=position,
                    quantity=position.quantity,
                    is_partial=False,
                    reason=ExitReason.STRUCTURE_FAILURE,
                    reason_detail="closed bar broke the latest confirmed post-entry swing",
                    now=now,
                    metadata={},
                )
            )

        # D. Momentum deterioration.
        threshold = policy.momentum_deterioration_threshold
        if (
            threshold is not None
            and market.momentum_score is not None
            and market.momentum_score <= threshold
        ):
            return evaluation(
                self._build_decision(
                    position=position,
                    quantity=position.quantity,
                    is_partial=False,
                    reason=ExitReason.MOMENTUM_DETERIORATION,
                    reason_detail=f"momentum_score {market.momentum_score} <= {threshold}",
                    now=now,
                    metadata={"momentum_score": str(market.momentum_score)},
                )
            )

        # D2. Late-session loser (Lane E2): a losing trade with adverse momentum near the mandatory flat
        # exits early through the same reduce-only path instead of being held hoping for a reversal.
        late_threshold = policy.late_loser_momentum_threshold
        if (
            late_threshold is not None
            and self._in_late_window(market)
            and market.momentum_score is not None
            and market.momentum_score <= late_threshold
            and (market.price < position.entry_price if is_long else market.price > position.entry_price)
        ):
            return evaluation(
                self._build_decision(
                    position=position,
                    quantity=position.quantity,
                    is_partial=False,
                    reason=ExitReason.LATE_SESSION_DETERIORATION,
                    reason_detail=(
                        f"late-session loser, momentum_score {market.momentum_score} <= {late_threshold}"
                    ),
                    now=now,
                    metadata={"momentum_score": str(market.momentum_score)},
                )
            )

        # D3. MFE giveback (Lane E2).
        if (
            policy.max_giveback_fraction is not None
            and market.mfe_r is not None
            and market.giveback_r is not None
            and market.mfe_r >= policy.giveback_min_mfe_r
            and market.mfe_r > 0
            and market.giveback_r / market.mfe_r >= policy.max_giveback_fraction
        ):
            return evaluation(
                self._build_decision(
                    position=position,
                    quantity=position.quantity,
                    is_partial=False,
                    reason=ExitReason.MFE_GIVEBACK,
                    reason_detail=(
                        f"gave back {market.giveback_r}R of a {market.mfe_r}R excursion "
                        f"(>= {policy.max_giveback_fraction})"
                    ),
                    now=now,
                    metadata={"mfe_r": str(market.mfe_r), "giveback_r": str(market.giveback_r)},
                )
            )

        # E. Spread/liquidity deterioration.
        liquidity_reason = self._liquidity_deterioration_reason(market)
        if liquidity_reason is not None:
            return evaluation(
                self._build_decision(
                    position=position,
                    quantity=position.quantity,
                    is_partial=False,
                    reason=ExitReason.LIQUIDITY_DETERIORATION,
                    reason_detail=liquidity_reason,
                    now=now,
                    metadata={},
                )
            )

        # F. Time stop.
        if policy.max_holding_duration is not None:
            held_for = now - position.opened_at
            # time-alpha decay (Lane E2): a trade that showed enough favourable excursion is not aged out
            worked = (
                policy.time_stop_min_mfe_r is not None
                and market.mfe_r is not None
                and market.mfe_r >= policy.time_stop_min_mfe_r
            )
            if held_for >= policy.max_holding_duration and not worked:
                return evaluation(
                    self._build_decision(
                        position=position,
                        quantity=position.quantity,
                        is_partial=False,
                        reason=ExitReason.TIME_STOP,
                        reason_detail=f"held {held_for} >= max {policy.max_holding_duration}",
                        now=now,
                        metadata={},
                    )
                )

        # G. Target reached -> partial profit taking (supports "runners").
        target_decision = self._target_decision(position=position, market=market, now=now)
        if target_decision is not None:
            return evaluation(target_decision)

        return evaluation(None)

    def _in_late_window(self, market: ExitMarketState) -> bool:
        window = self._policy.late_window
        left = market.time_to_forced_flat
        return window is not None and left is not None and left <= window

    def _ratchet_high_water_mark(
        self, position: ExitPosition, price: Decimal, is_long: bool
    ) -> Decimal:
        current = position.effective_high_water_mark
        return max(current, price) if is_long else min(current, price)

    def _ratchet_stop(
        self,
        *,
        position: ExitPosition,
        market: ExitMarketState,
        high_water_mark: Decimal,
        is_long: bool,
    ) -> tuple[Decimal, StopStage, bool]:
        policy = self._policy
        stop_price = position.current_stop_price
        stop_stage = position.stop_stage
        break_even_activated = False

        initial_risk = position.initial_risk
        if initial_risk <= 0:
            return stop_price, stop_stage, False

        favorable_move = (
            (market.price - position.entry_price)
            if is_long
            else (position.entry_price - market.price)
        )
        r_multiple = favorable_move / initial_risk

        cost = market.expected_exit_cost if market.expected_exit_cost is not None else ZERO
        in_late_window = self._in_late_window(market)
        # Break-even: only from ORIGINAL, only forward, only once. Triggers: the configured R, the first
        # target stage taken (Lane E2) or, late in the session, any profit that covers the exit cost.
        trigger = r_multiple >= policy.breakeven_trigger_r_multiple
        if policy.breakeven_after_first_stage and position.stages_completed >= 1:
            trigger = True
        if in_late_window and favorable_move > cost:
            trigger = True
        breakeven_ready = stop_stage == StopStage.ORIGINAL and trigger
        if breakeven_ready:
            # cost-adjusted: entry +/- (configured buffer + expected cost of exiting), so a stop-out at
            # "break-even" does not lose money to spread/fees
            buffer = position.entry_price * policy.breakeven_buffer_bps / TEN_THOUSAND + cost
            breakeven_price = (
                position.entry_price + buffer if is_long else position.entry_price - buffer
            )
            improves = breakeven_price > stop_price if is_long else breakeven_price < stop_price
            # never place the stop at/through the current price (that would be an immediate stop-out)
            safe = breakeven_price < market.price if is_long else breakeven_price > market.price
            if improves and safe:
                stop_price = breakeven_price
                stop_stage = StopStage.BREAK_EVEN
                break_even_activated = True

        # Trailing: activates by R-multiple, requires volatility, only ratchets forward.
        if r_multiple >= policy.trailing_activation_r_multiple and market.volatility is not None:
            distance = (
                market.price * market.volatility * policy.trailing_distance_volatility_multiplier
            )
            candidate = high_water_mark - distance if is_long else high_water_mark + distance
            improves = candidate > stop_price if is_long else candidate < stop_price
            if improves and candidate > 0:
                stop_price = candidate
                stop_stage = StopStage.TRAILING

        # Structure trailing (Lane E2): behind the newest CONFIRMED post-entry swing, forward only, never
        # through the price. The candidate is computed from closed bars by the caller.
        structure = market.structure_trail_price
        if policy.structure_trailing and structure is not None:
            improves = structure > stop_price if is_long else structure < stop_price
            safe = structure < market.price if is_long else structure > market.price
            if improves and safe:
                stop_price = structure
                stop_stage = StopStage.TRAILING

        return stop_price, stop_stage, break_even_activated

    def _liquidity_deterioration_reason(self, market: ExitMarketState) -> str | None:
        policy = self._policy
        mid = (market.bid + market.ask) / 2
        spread_bps = (market.ask - market.bid) / mid * TEN_THOUSAND if mid > 0 else None
        if (
            policy.max_spread_bps is not None
            and spread_bps is not None
            and spread_bps > policy.max_spread_bps
        ):
            return f"spread {spread_bps}bps > max {policy.max_spread_bps}bps"
        if policy.min_liquidity_notional is not None:
            liquidity = market.available_liquidity_notional
            if liquidity is None or liquidity < policy.min_liquidity_notional:
                min_liquidity = policy.min_liquidity_notional
                return f"available_liquidity_notional {liquidity} < min {min_liquidity}"
        return None

    def _target_price(self, position: ExitPosition) -> tuple[Decimal, str] | None:
        if position.fixed_target_price is not None:
            return position.fixed_target_price, "fixed"
        if self._policy.target_r_multiple is not None:
            initial_risk = position.initial_risk
            offset = initial_risk * self._policy.target_r_multiple
            if position.side == PositionSide.LONG:
                return position.entry_price + offset, "volatility"
            return position.entry_price - offset, "volatility"
        return None

    def _target_decision(
        self, *, position: ExitPosition, market: ExitMarketState, now: datetime
    ) -> ExitDecision | None:
        # PRECEDENCE (see `ExitPolicy.take_profit_stages` docstring): a
        # non-empty stage ladder is used EXCLUSIVELY; the single-stage
        # target_r_multiple/fixed_target_price/partial_take_profit_fraction
        # fields are only consulted when no ladder is configured, preserving
        # the pre-multi-stage behavior exactly (existing tests/callers).
        if position.target_stages or self._policy.take_profit_stages:
            return self._staged_target_decision(position=position, market=market, now=now)
        return self._single_stage_target_decision(position=position, market=market, now=now)

    def _single_stage_target_decision(
        self, *, position: ExitPosition, market: ExitMarketState, now: datetime
    ) -> ExitDecision | None:
        # A target is only taken once per position (realized_partial_quantity
        # tracks whether it already fired), so a strongly continuing move
        # after the partial is never cut short a second time.
        if position.realized_partial_quantity > 0:
            return None
        target = self._target_price(position)
        if target is None:
            return None
        target_price, target_source = target
        is_long = position.side == PositionSide.LONG
        target_hit = market.price >= target_price if is_long else market.price <= target_price
        if not target_hit:
            return None

        policy = self._policy
        raw_partial = position.quantity * policy.partial_take_profit_fraction
        partial_quantity = self._round_down_to_step(raw_partial, policy.quantity_step)
        remaining_after_partial = position.quantity - partial_quantity

        if partial_quantity <= 0 or remaining_after_partial < policy.min_remaining_quantity:
            quantity = position.quantity
            is_partial = False
        else:
            quantity = partial_quantity
            is_partial = True

        return self._build_decision(
            position=position,
            quantity=quantity,
            is_partial=is_partial,
            reason=ExitReason.TAKE_PROFIT,
            reason_detail=f"price {market.price} reached {target_source} target {target_price}",
            now=now,
            metadata={"target_source": target_source, "target_price": str(target_price)},
        )

    def _staged_target_decision(
        self, *, position: ExitPosition, market: ExitMarketState, now: datetime
    ) -> ExitDecision | None:
        # A per-position ladder (Lane E: R- and/or structural price stages) replaces the policy one.
        stages: tuple[TakeProfitStage, ...] = (
            position.target_stages or self._policy.take_profit_stages
        )
        # Only the NEXT unfired stage is ever checked, and at most one stage
        # decision is emitted per evaluate() call -- even if price has
        # gapped past multiple stage triggers in a single tick, the
        # remaining stage(s) are picked up on subsequent ticks once
        # `stages_completed` has advanced from a real fill. This is a
        # deliberate design choice (see PART 1 test coverage), not an
        # oversight: it keeps exactly one reduce-only request in flight at a
        # time, mirroring the engine's existing "one decision per tick"
        # pattern used everywhere else in `_evaluate_inner`.
        if position.stages_completed >= len(stages):
            return None

        stage = stages[position.stages_completed]
        initial_risk = position.initial_risk
        if initial_risk <= 0:
            return None

        is_long = position.side == PositionSide.LONG
        target_price = stage_target_price(
            stage, side=position.side, entry_price=position.entry_price, initial_risk=initial_risk
        )
        target_hit = market.price >= target_price if is_long else market.price <= target_price
        if not target_hit:
            return None

        policy = self._policy
        # close_fraction is a fraction of the ORIGINAL position quantity
        # (see `TakeProfitStage` docstring), not of whatever remains open
        # now -- reconstruct it from the currently-open quantity plus
        # whatever has already been realized via earlier partial closes.
        original_quantity = position.quantity + position.realized_partial_quantity
        raw_stage_quantity = original_quantity * stage.close_fraction
        stage_quantity = self._round_down_to_step(raw_stage_quantity, policy.quantity_step)
        # Defensive clamp: rounding/Decimal drift across stages must never
        # let a stage try to close more than is actually still open.
        stage_quantity = min(stage_quantity, position.quantity)
        remaining_after_stage = position.quantity - stage_quantity

        is_last_configured_stage = position.stages_completed == len(stages) - 1
        # The dust-avoidance "just take everything" fallback the single-stage
        # path uses only ever applies on this ladder's LAST stage -- an
        # earlier stage rounding to (near) zero is a policy misconfiguration,
        # not a reason to prematurely close the whole runner while later
        # stages are still meant to fire.
        #
        # Lane E refinement: a last stage that rounds to ZERO lots only closes everything when the
        # ladder is COMPLETE (fractions sum to 1, i.e. this stage is the intended final close).
        # For TP1+runner ladders (sum < 1) an undersized stage is skipped: rounding never closes
        # the runner. A last stage that leaves only a dust remainder still closes it fully.
        ladder_complete = sum((s.close_fraction for s in stages), ZERO) >= Decimal("1")
        dust_remainder = (
            stage_quantity > 0 and remaining_after_stage < policy.min_remaining_quantity
        )
        undersized_final = stage_quantity <= 0 and ladder_complete
        if is_last_configured_stage and (dust_remainder or undersized_final):
            quantity = position.quantity
            is_partial = False
        elif stage_quantity <= 0:
            return None
        else:
            quantity = stage_quantity
            is_partial = quantity < position.quantity

        return self._build_decision(
            position=position,
            quantity=quantity,
            is_partial=is_partial,
            reason=ExitReason.TAKE_PROFIT,
            reason_detail=(
                f"price {market.price} reached stage {position.stages_completed} "
                f"target {target_price} ({stage.stage_id or stage.source})"
            ),
            now=now,
            metadata={
                "target_source": "staged",
                "target_price": str(target_price),
                "stage_index": position.stages_completed,
                "stage_r_multiple": None if stage.r_multiple is None else str(stage.r_multiple),
                "stage_close_fraction": str(stage.close_fraction),
                "stage_id": stage.stage_id or f"stage{position.stages_completed}",
                "stage_source": stage.source,
                "stage_is_last": is_last_configured_stage,
            },
        )

    @staticmethod
    def _round_down_to_step(raw_quantity: Decimal, quantity_step: Decimal) -> Decimal:
        return (raw_quantity / quantity_step).to_integral_value(rounding=ROUND_DOWN) * quantity_step

    def _build_decision(
        self,
        *,
        position: ExitPosition,
        quantity: Decimal,
        is_partial: bool,
        reason: ExitReason,
        reason_detail: str,
        now: datetime,
        metadata: dict[str, object],
    ) -> ExitDecision:
        close_side = RiskSide.SELL if position.side == PositionSide.LONG else RiskSide.BUY
        request_id = f"exit:{position.position_id}:{reason.value.lower()}:{now.isoformat()}"
        return ExitDecision(
            request_id=request_id,
            position_id=position.position_id,
            instrument=position.instrument,
            close_side=close_side,
            quantity=quantity,
            is_partial=is_partial,
            reason=reason,
            reason_detail=reason_detail,
            timestamp=now,
            metadata=dict(metadata),
        )


def apply_evaluation(position: ExitPosition, evaluation: ExitEvaluation) -> ExitPosition:
    """Fold an `ExitEvaluation`'s ratcheted state back into a new `ExitPosition`.

    Convenience for callers driving repeated `evaluate()` ticks; also used by
    this module's own tests. Does not touch `quantity` or
    `realized_partial_quantity` -- those change only once execution reports a
    real fill, which is outside this module's scope (`src/execution` owns
    fills).
    """
    return replace(
        position,
        current_stop_price=evaluation.updated_stop_price,
        stop_stage=evaluation.updated_stop_stage,
        high_water_mark=evaluation.updated_high_water_mark,
    )
