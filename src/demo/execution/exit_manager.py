# ruff: noqa: E501
"""Lane E1: wires the deterministic ``exits.ExitEngine`` into the DEMO stack (``exit_policy="staged"``).

What this is: a thin, deterministic adapter between per-position broker truth and the existing
``ExitEngine`` (NO second engine). It runs once per runner cycle from ``Mt5DemoStack.manage_exits``
(before ``on_clock``), holds the stack's submit lock, and never depends on an LLM or remote service.

Per OPEN registry row it

1. reads the broker position (truth: side, volume, entry price, live stop) and the newest executable
   quote; a missing / stale quote means NO decision this cycle - the broker stop stays the safety
   backstop (the engine's own "stale data => emergency close" fail-safe is deliberately NOT triggered
   from a feed hiccup of a market that may simply be closed);
2. builds an ``ExitPosition`` (entry fill, registry initial stop, per-position target stages,
   realized partial derived from ``original quantity - broker volume``) and an ``ExitMarketState``;
3. calls ``ExitEngine.evaluate`` and routes the result through a thin reduce-only admission
   (broker-verified own position, side, quantity <= broker volume, lot step / min lot) to
   ``ReduceJob`` (partial), ``_flatten`` (full close) and ``ModifyStopJob`` (tighten only);
4. persists stage state ONLY after the fill is verified at the broker (``stages_completed`` is also
   lower-bounded by the realized volume, so a crash between fill and persist can never re-fire a stage).

Protective-stop safety backstop: the broker-side stop placed at entry is never removed. After every
partial the adapter resizes the Nautilus SL/TP child orders to the remaining volume and this manager
re-reads the broker to confirm ``broker volume == expected remainder`` and ``broker stop present``
(otherwise the existing protection repair path flattens). A failed / rejected stop modify leaves the
previous (tighter-or-equal) stop in force - MT5 has ONE position-wide SL moved by a single atomic
SLTP request, so there is no cancel/replace window in which the position is unprotected.

Netting + tranches: MT5 holds one net position per symbol. Multiple tranches with incompatible exit
plans remain an explicit technical limitation (``R_EXIT_TRANCHE_LIMITATION``): with more than one
live tranche on a market the engine is NOT run for it (only broker stops / forced flat apply).

Reduce-only risk reservation: ``DemoRiskGate`` holds no reservation for reduce-only orders (it only
sizes ENTRIES), so ``ExitEngine.notify_terminal`` is fed a recording no-op release gate: the call is
made on every terminal outcome (fill / cancel / reject) for audit symmetry, N/A for the risk book.
"""

from __future__ import annotations

import collections
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from demo.execution import registry as reg
from demo.execution.events import ExecutionEvent
from demo.execution.parity import parse_utc
from demo.execution.strategy import JobOutcome, ModifyStopJob, ReduceJob
from exits.engine import ExitEngine
from exits.models import (
    STAGE_SOURCE_R,
    ExitMarketState,
    ExitOutcome,
    ExitPolicy,
    ExitPosition,
    PositionSide,
    StopStage,
    TakeProfitStage,
    stage_target_price,
    stop_is_unchanged_or_tighter,
)

if TYPE_CHECKING:  # pragma: no cover
    from demo.execution.live import Mt5DemoStack

_LOG = logging.getLogger(__name__)
ZERO = Decimal(0)

EXIT_POLICY_FIXED = "fixed_1_5r"  # DEFAULT: broker SL + one fixed-R broker TP, nothing else (unchanged)
EXIT_POLICY_STAGED = "staged"  # ExitEngine-managed partials / tighten-only stop moves
EXIT_POLICIES = (EXIT_POLICY_FIXED, EXIT_POLICY_STAGED)

R_EXIT_TRANCHE_LIMITATION = "EXIT_PLAN_MULTI_TRANCHE_NOT_SUPPORTED"  # TEMPORARY, explicit
R_EXIT_QUOTE_UNAVAILABLE = "EXIT_QUOTE_MISSING_OR_STALE"
R_EXIT_SIZE_BELOW_MIN_LOT = "EXIT_STAGE_SIZE_BELOW_BROKER_MIN_LOT"


class _ReleaseRecorder:
    """``RiskReleaseGate`` for ``ExitEngine.notify_terminal``: DemoRiskGate keeps no reduce-only
    reservation, so release is a recorded no-op (N/A for the DEMO risk book)."""

    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, decision_id: str) -> None:
        self.released.append(decision_id)


def parse_exit_plan(plan: Any) -> tuple[TakeProfitStage, ...]:
    """``{"stages": [{"r_multiple"|"target_price", "close_fraction", "stage_id", "source"}]}`` ->
    stages. An absent / empty plan is legal (TP1-less runner or policy ladder)."""
    if not plan:
        return ()
    if not isinstance(plan, dict) or not isinstance(plan.get("stages", ()), (list, tuple)):
        raise ValueError("exit_plan must be {'stages': [...]}")
    stages: list[TakeProfitStage] = []
    for raw in plan.get("stages", ()):
        if not isinstance(raw, dict):
            raise ValueError("exit_plan stage must be a mapping")
        r = raw.get("r_multiple")
        p = raw.get("target_price")
        stages.append(
            TakeProfitStage(
                close_fraction=Decimal(str(raw["close_fraction"])),
                r_multiple=None if r is None else Decimal(str(r)),
                target_price=None if p is None else Decimal(str(p)),
                stage_id=str(raw.get("stage_id", "")),
                source=str(raw.get("source", STAGE_SOURCE_R)),
            )
        )
    return tuple(stages)


def broker_target_for_staged(
    plan: Any, *, direction: int, entry_ref: Decimal
) -> Decimal | None:
    """Broker-side TP under ``staged``: ABSENT, or the FINAL stage only.

    The final stage is the one that closes the position (stage fractions sum to 1). It only becomes a
    broker TP when it is an absolute price beyond the reference entry; an R-based final stage has no
    known price before the fill (entry slippage), so the engine closes it at market instead."""
    try:
        stages = parse_exit_plan(plan)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return None
    if not stages:
        return None
    last = stages[-1]
    if last.target_price is None:
        return None
    if sum((s.close_fraction for s in stages), ZERO) != Decimal(1):
        return None  # a runner remains: no broker TP caps it
    favourable = last.target_price > entry_ref if direction == 1 else last.target_price < entry_ref
    return last.target_price if favourable else None


class StagedExitManager:
    def __init__(self, stack: Mt5DemoStack, policy: ExitPolicy) -> None:
        self._stack = stack
        self._policy = policy
        self._gate = _ReleaseRecorder()
        self._engines: dict[str, ExitEngine] = {}
        self._pending: dict[str, str] = {}  # intent_id -> request_id of an in-doubt reduce
        self._noted: set[tuple[str, str]] = set()
        self._stop_failures: dict[str, int] = collections.defaultdict(int)
        self.log: collections.deque[dict[str, Any]] = collections.deque(maxlen=1000)
        self.counters: collections.Counter[str] = collections.Counter()

    # ------------------------------------------------------------------------------ public

    @property
    def released(self) -> list[str]:
        return self._gate.released

    def run(self, now: datetime) -> list[ExecutionEvent]:
        """One cycle. Must be called with the stack's submit lock held."""
        stack = self._stack
        assert stack._registry is not None
        events: list[ExecutionEvent] = []
        rows = stack._registry.with_status(reg.OPEN)
        per_market = collections.Counter(r.market for r in rows)
        for row in rows:
            if per_market[row.market] > 1:
                self._note_once(row.intent_id, R_EXIT_TRANCHE_LIMITATION, now)
                continue
            try:
                events.extend(self._manage_row(row, now))
            except Exception as exc:
                if type(exc).__name__ == "StackFailClosed":
                    raise
                self.counters["row_errors"] += 1
                self._log("row_error", row, error=f"{type(exc).__name__}:{exc}"[:200])
        return events

    # ------------------------------------------------------------------------------ helpers

    def _engine_for(self, market: str) -> ExitEngine:
        engine = self._engines.get(market)
        if engine is None:
            spec = self._stack._markets[market].spec
            policy = replace(
                self._policy,
                quantity_step=spec.volume_step,
                min_remaining_quantity=max(self._policy.min_remaining_quantity, spec.volume_min),
            )
            engine = ExitEngine(policy=policy, risk_gate=self._gate)
            self._engines[market] = engine
        return engine

    def _terminal(self, market: str, request_id: str, outcome: ExitOutcome) -> None:
        """Release-equivalent on every terminal outcome (fill / cancel / reject)."""
        self._engine_for(market).notify_terminal(request_id, outcome)

    def _log(self, kind: str, row: reg.IntentRow, **fields: Any) -> None:
        entry = {"kind": kind, "intent_id": row.intent_id, "market": row.market, **fields}
        self.log.append(entry)
        _LOG.info("exit_manager %s", entry)

    def _note_once(self, intent_id: str, code: str, now: datetime) -> None:
        if (intent_id, code) in self._noted:
            return
        self._noted.add((intent_id, code))
        self.log.append({"kind": "limitation", "intent_id": intent_id, "code": code, "at": now.isoformat()})

    @staticmethod
    def _ctx(row: reg.IntentRow) -> dict[str, Any]:
        try:
            value = json.loads(row.context) if row.context else {}
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _save_state(self, row: reg.IntentRow, ctx: dict[str, Any], state: dict[str, Any]) -> None:
        assert self._stack._registry is not None
        ctx["exit_state"] = state
        self._stack._registry.update(row.intent_id, context=json.dumps(ctx, default=str))

    @staticmethod
    def _stage_quantities(
        stages: tuple[TakeProfitStage, ...], original: Decimal, step: Decimal
    ) -> list[Decimal]:
        return [((original * s.close_fraction) / step).to_integral_value(rounding=ROUND_DOWN) * step for s in stages]

    # ------------------------------------------------------------------------------ one position

    def _manage_row(self, row: reg.IntentRow, now: datetime) -> list[ExecutionEvent]:
        stack = self._stack
        info = stack._markets[row.market]
        ctx = self._ctx(row)
        state: dict[str, Any] = dict(ctx.get("exit_state") or {})

        positions = [
            p
            for p in stack._on_lane(stack._lane_symbol_positions, info.broker_symbol, strict=True)
            if int(p.magic) == stack._cfg.magic
        ]
        if len(positions) != 1:
            return []  # closure / anomaly handling belongs to poll_events
        position = positions[0]
        if row.position_ticket is not None and int(row.position_ticket) != int(position.ticket):
            return []
        if (1 if int(position.type) == 0 else -1) != row.direction:
            self._log("skip_side_mismatch", row)
            return []

        quote = self._quote(row, now)
        if quote is None:
            return []
        bid, ask, quote_ts = quote

        side = PositionSide.LONG if row.direction == 1 else PositionSide.SHORT
        entry = Decimal(str(position.price_open))
        volume = Decimal(str(position.volume))
        try:
            original = Decimal(str(ctx["quantity"]))
        except (KeyError, ValueError, ArithmeticError):
            self._log("skip_no_original_quantity", row)
            return []
        realized = max(ZERO, original - volume)
        try:
            plan_stages = parse_exit_plan(ctx.get("exit_plan"))
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            self._log("skip_bad_exit_plan", row, error=str(exc)[:160])
            return []
        ladder = plan_stages or self._policy.take_profit_stages
        stages_completed = self._stages_done(state, ladder, original, realized, info.spec.volume_step)
        broker_stop = Decimal(str(position.sl or 0))
        initial_stop = Decimal(row.stop)
        current_stop = broker_stop if broker_stop > 0 else initial_stop
        price = bid if side is PositionSide.LONG else ask
        atr = ctx.get("atr")
        vol = (Decimal(str(atr)) / price) if atr not in (None, "") and price > 0 else None
        hwm = state.get("high_water_mark")
        try:
            exit_position = ExitPosition(
                position_id=row.intent_id,
                instrument=row.market,
                side=side,
                entry_price=entry,
                quantity=volume,
                initial_stop_price=initial_stop,
                current_stop_price=current_stop,
                stop_stage=StopStage(state.get("stop_stage", StopStage.ORIGINAL.value)),
                high_water_mark=None if hwm in (None, "") else Decimal(str(hwm)),
                opened_at=parse_utc(row.created_utc),
                realized_partial_quantity=realized,
                stages_completed=stages_completed,
                pending_close_request_id=self._pending.get(row.intent_id),
                target_stages=plan_stages,
            )
            risk = exit_position.initial_risk
            favourable = (price - entry) if side is PositionSide.LONG else (entry - price)
            hw = exit_position.effective_high_water_mark
            mfe_r = ((hw - entry) if side is PositionSide.LONG else (entry - hw)) / risk
            market_state = ExitMarketState(
                instrument=row.market, timestamp=quote_ts, price=price, bid=bid, ask=ask,
                volatility=vol,
                mfe_r=max(mfe_r, favourable / risk),
                giveback_r=max(ZERO, mfe_r - favourable / risk),
                holding_seconds=Decimal(str((now - exit_position.opened_at).total_seconds())),
            )
        except ValueError as exc:
            self._log("skip_invalid_position", row, error=str(exc)[:200])
            return []

        evaluation = self._engine_for(row.market).evaluate(
            position=exit_position, market=market_state, now=now
        )
        events: list[ExecutionEvent] = []

        # 1. tighten the protective stop first (protection-first), then reduce.
        new_stop = evaluation.updated_stop_price
        if evaluation.updated_high_water_mark != exit_position.effective_high_water_mark:
            state["high_water_mark"] = str(evaluation.updated_high_water_mark)
            self._save_state(row, ctx, state)
        if new_stop != current_stop:
            events.extend(
                self._tighten_stop(
                    row, ctx, state, info, position, side, current_stop, new_stop,
                    evaluation.updated_stop_stage, risk,
                )
            )
        decision = evaluation.decision
        if decision is None:
            return events

        if decision.is_partial:
            events.extend(
                self._reduce_partial(row, ctx, state, info, side, entry, initial_stop, risk, original, volume, decision, stages_completed, now)
            )
        else:
            events.extend(self._close_fully(row, info, decision))
        return events

    # ------------------------------------------------------------------------------ inputs

    def _quote(self, row: reg.IntentRow, now: datetime) -> tuple[Decimal, Decimal, datetime] | None:
        stack = self._stack
        try:
            quote = stack.bar_source.latest_quote(row.market)
        except Exception as exc:
            if type(exc).__name__ == "StackFailClosed":
                raise
            quote = None
        if quote is None or not quote.valid:
            self.counters["quote_missing"] += 1
            self._log("skip_quote", row, code=R_EXIT_QUOTE_UNAVAILABLE)
            return None
        age = (now - quote.ts_utc).total_seconds()
        bound = min(stack._cfg.max_quote_age_s, self._policy.max_market_data_age.total_seconds())
        if age > bound or age < -stack._cfg.clock_skew_s:
            self.counters["quote_stale"] += 1
            self._log("skip_quote", row, code=R_EXIT_QUOTE_UNAVAILABLE, age_s=age)
            return None
        ts = min(quote.ts_utc, now)
        return Decimal(str(quote.bid)), Decimal(str(quote.ask)), ts

    def _stages_done(
        self, state: dict[str, Any], ladder: tuple[TakeProfitStage, ...], original: Decimal,
        realized: Decimal, step: Decimal,
    ) -> int:
        persisted = int(state.get("stages_completed", 0))
        cumulative, derived = ZERO, 0
        for qty in self._stage_quantities(ladder, original, step):
            cumulative += qty
            if qty > 0 and realized >= cumulative:
                derived += 1
            else:
                break
        return min(len(ladder), max(persisted, derived))

    # ------------------------------------------------------------------------------ stop move

    def _tighten_stop(
        self, row: reg.IntentRow, ctx: dict[str, Any], state: dict[str, Any], info: Any,
        position: Any, side: PositionSide, current: Decimal, proposed: Decimal,
        stage: StopStage, risk: Decimal,
    ) -> list[ExecutionEvent]:
        stack = self._stack
        tick = info.spec.tick_size
        rounding = ROUND_CEILING if side is PositionSide.LONG else ROUND_FLOOR
        new_stop = (proposed / tick).to_integral_value(rounding=rounding) * tick
        if not stop_is_unchanged_or_tighter(side, old=current, new=new_stop) or new_stop == current:
            return []
        improvement = abs(new_stop - current)
        if improvement < max(tick, risk * stack._cfg.staged_stop_min_step_r):
            return []  # throttle: not worth a broker modify
        job = ModifyStopJob(instrument_id=info.instrument_id, new_stop=new_stop, tag=f"exit-stop:{row.intent_id}")
        assert stack._strategy is not None
        stack._strategy.enqueue(job)
        try:
            outcome: JobOutcome = job.future.result(timeout=stack._cfg.exposure_timeout_s)
        except Exception:
            outcome = JobOutcome("timeout", "no_outcome_within_bound")
        if outcome.status == "denied" and outcome.reason.startswith("stop_orders_0"):
            # Restart adoption: the position is adopted without a local Nautilus stop child. Use the
            # adapter's tighten-only, broker-verified protection path (single atomic SLTP request).
            try:
                denial = stack._on_lane(stack._lane_protect, int(position.ticket), new_stop, retry_reads=False)
            except Exception as exc:
                if type(exc).__name__ == "StackFailClosed":
                    raise
                denial = f"protect_unavailable:{type(exc).__name__}"
            outcome = JobOutcome("modified" if denial is None else "modify_rejected", denial or "emergency_protect")
        # Broker truth decides, whatever the strategy reported (an uncertain state is re-queried).
        seen = stack._on_lane(stack._lane_symbol_positions, info.broker_symbol, strict=True)
        seen_stop = Decimal(str(seen[0].sl or 0)) if len(seen) == 1 else None
        events: list[ExecutionEvent] = []
        if seen_stop is not None and abs(seen_stop - new_stop) < tick:
            state["stop_stage"] = stage.value
            state["stop_moves"] = int(state.get("stop_moves", 0)) + 1
            self._save_state(row, ctx, state)
            self._stop_failures.pop(row.intent_id, None)
            self.counters["stop_moves"] += 1
            self._log("stop_moved", row, old=str(current), new=str(new_stop), stage=stage.value)
            return events
        self._stop_failures[row.intent_id] += 1
        self.counters["stop_move_failed"] += 1
        self._log("stop_move_failed", row, status=outcome.status, reason=outcome.reason[:120], broker_stop=None if seen_stop is None else str(seen_stop))
        if seen_stop is not None and seen_stop == 0 and len(seen) == 1:
            # the stop vanished during the modification: protection-first repair (restore or flatten)
            events.extend(stack._repair_protection(row, seen[0]))
        return events

    # ------------------------------------------------------------------------------ reductions

    def _reduce_partial(
        self, row: reg.IntentRow, ctx: dict[str, Any], state: dict[str, Any], info: Any,
        side: PositionSide, entry: Decimal, initial_stop: Decimal, risk: Decimal,
        original: Decimal, volume: Decimal, decision: Any, stages_completed: int, now: datetime,
    ) -> list[ExecutionEvent]:
        stack = self._stack
        spec = info.spec
        quantity = decision.quantity
        step = spec.volume_step
        if quantity < spec.volume_min or (quantity / step) != (quantity / step).to_integral_value():
            self.counters["size_below_min"] += 1
            self._log("skip_reduce", row, code=R_EXIT_SIZE_BELOW_MIN_LOT, quantity=str(quantity))
            return []
        # thin reduce-only admission on FRESH broker truth
        fresh = [
            p for p in stack._on_lane(stack._lane_symbol_positions, info.broker_symbol, strict=True)
            if int(p.magic) == stack._cfg.magic
        ]
        if len(fresh) != 1 or Decimal(str(fresh[0].volume)) != volume or quantity >= volume:
            self._log("skip_reduce", row, code="ADMISSION_BROKER_TRUTH_CHANGED", quantity=str(quantity))
            return []
        assert stack._strategy is not None
        job = ReduceJob(instrument_id=info.instrument_id, quantity=quantity, tag=f"exit:{decision.request_id}")
        self._pending[row.intent_id] = decision.request_id
        stack._strategy.enqueue(job)
        try:
            outcome: JobOutcome = job.future.result(timeout=stack._cfg.flatten_wait_s)
        except Exception:
            outcome = JobOutcome("timeout", "no_outcome_within_bound")
        after = stack._on_lane(stack._lane_symbol_positions, info.broker_symbol, strict=True)
        now_volume = Decimal(str(after[0].volume)) if len(after) == 1 else ZERO
        expected = volume - quantity
        events: list[ExecutionEvent] = []
        if outcome.status in ("denied", "rejected", "failed", "flat") and now_volume == volume:
            self._pending.pop(row.intent_id, None)
            self._terminal(row.market, decision.request_id, ExitOutcome.REJECTED)
            self.counters["reduce_refused"] += 1
            self._log("reduce_refused", row, status=outcome.status, reason=outcome.reason[:160], quantity=str(quantity))
            return events
        if now_volume == volume:  # timeout / unknown: nothing visible yet
            stack._halt("order_outcome_unknown")
            self.counters["reduce_unknown"] += 1
            self._log("reduce_unknown", row, status=outcome.status, quantity=str(quantity))
            return events  # _pending stays: the engine does not double-submit; truth re-read next cycle
        self._pending.pop(row.intent_id, None)
        self._terminal(row.market, decision.request_id, ExitOutcome.FILLED)
        if now_volume != expected:
            stack._halt("order_outcome_unknown")
            self._log("reduce_quantity_mismatch", row, expected=str(expected), broker=str(now_volume))
        reduced = volume - now_volume
        if reduced > 0 and now_volume > 0:
            if Decimal(str(after[0].sl or 0)) == 0:
                # remaining exposure without a broker stop: never leave it (existing protection path)
                events.extend(stack._repair_protection(row, after[0]))
            self._record_partial(
                row, ctx, state, side, entry, initial_stop, risk, original, volume, reduced, now_volume,
                outcome, decision, stages_completed, Decimal(str(after[0].sl or 0)),
            )
        elif now_volume == 0:
            closed = stack._on_lane(stack._lane_build_closed, stack._registry.get(row.intent_id) or row, strict=True)
            if closed is not None:
                events.append(closed)
        return events

    def _record_partial(
        self, row: reg.IntentRow, ctx: dict[str, Any], state: dict[str, Any], side: PositionSide,
        entry: Decimal, initial_stop: Decimal, risk: Decimal, original: Decimal, before: Decimal,
        reduced: Decimal, remaining: Decimal, outcome: JobOutcome, decision: Any,
        stages_completed: int, stop_now: Decimal,
    ) -> None:
        sign = Decimal(1) if side is PositionSide.LONG else Decimal(-1)
        fills = outcome.fills
        fill_qty = sum((q for q, _ in fills), ZERO)
        avg = (sum((q * p for q, p in fills), ZERO) / fill_qty) if fill_qty > 0 else None
        unit_r = None if avg is None else (avg - entry) * sign / risk
        realized_r = None if unit_r is None else unit_r * reduced / original
        remaining_risk_r = (remaining / original) * ((entry - stop_now) * sign / risk) if stop_now > 0 else None
        partial = {
            "intent_id": row.intent_id,
            "tranche_id": row.intent_id,  # one tranche per netted position (see module docstring)
            "strategy_family": ctx.get("family"),
            "stage_index": stages_completed,
            "stage_id": decision.metadata.get("stage_id"),
            "stage_source": decision.metadata.get("stage_source"),
            "reason": decision.reason.value,
            "quantity_before": str(before),
            "quantity_reduced": str(reduced),
            "quantity_remaining": str(remaining),
            "fill_price": None if avg is None else str(avg),
            "realized_r_of_reduced_unit": None if unit_r is None else str(unit_r),
            "realized_r_of_position": None if realized_r is None else str(realized_r),
            "remaining_risk_r": None if remaining_risk_r is None else str(remaining_risk_r),
            "at": datetime.now(UTC).isoformat(),
        }
        state["stages_completed"] = stages_completed + 1
        state["partials"] = [*state.get("partials", []), partial]
        self._save_state(row, ctx, state)
        self.counters["partials"] += 1
        self._log("partial_exit", row, **partial)

    def _close_fully(self, row: reg.IntentRow, info: Any, decision: Any) -> list[ExecutionEvent]:
        stack = self._stack
        assert stack._registry is not None
        stack._registry.update(row.intent_id, detail=f"exit_engine:{decision.reason.value}"[:200])
        ok = stack._flatten(info, tag=f"exit:{decision.request_id}", hint=None)
        self._terminal(row.market, decision.request_id, ExitOutcome.FILLED if ok else ExitOutcome.REJECTED)
        self._log("full_close", row, reason=decision.reason.value, flat=ok)
        if not ok:
            return []
        fresh = stack._registry.get(row.intent_id) or row
        closed = stack._on_lane(stack._lane_build_closed, fresh, strict=True)
        return [closed] if closed is not None else []


__all__ = [
    "EXIT_POLICIES",
    "EXIT_POLICY_FIXED",
    "EXIT_POLICY_STAGED",
    "R_EXIT_QUOTE_UNAVAILABLE",
    "R_EXIT_SIZE_BELOW_MIN_LOT",
    "R_EXIT_TRANCHE_LIMITATION",
    "ExitOutcome",
    "StagedExitManager",
    "broker_target_for_staged",
    "parse_exit_plan",
    "stage_target_price",
]
