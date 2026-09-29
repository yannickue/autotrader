"""LEGACY_RUNTIME / SHADOW_ORACLE: stateful wrapper around the pure risk policy.

The validated admission and sizing logic now lives in the PURE modules
`risk.policy` (gates -> `ProposedOrderIntent`) and `risk.sizing`
(`PositionSizer`). What remains here is exactly the runtime state the pure
layer must not own: the halt latch, the per-signal decision cache, and the
approved-but-unfilled reservation ledger, plus conversion of an intent into a
`RiskDecision`. Under the Nautilus convergence plan (docs/ARCHITECTURE_AUDIT_
2026-09-29.md, C6-C8) this class is retained as a parity oracle and retired
from the production path; do not add features to it.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from data.models import MarketSnapshot
from risk.models import (
    MAX_SYSTEM_LEVERAGE,
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    RiskDecision,
    RiskPolicy,
    RiskReason,
    RiskSide,
    RuntimeRiskState,
)
from risk.policy import (
    PendingExposure,
    PolicyRejection,
    ProposedOrderIntent,
    RiskHook,
    RiskHookContext,
    RiskPolicyEvaluator,
    readiness_rejection,
)
from risk.sizing import RiskRejection

__all__ = ["RiskEngine", "RiskHook", "RiskHookContext", "RiskRejection"]

ZERO = Decimal("0")


class RiskEngine:
    """Evaluate exposure-changing requests against an immutable policy.

    Fails closed: any unexpected exception during evaluation is converted into a
    rejected decision (reason_code RISK_ERROR) and latches the engine into HALT
    until :meth:`reset_halt` is called explicitly.
    """

    def __init__(self, policy: RiskPolicy, hooks: Sequence[RiskHook] | None = None) -> None:
        self._policy = policy
        self._evaluator = RiskPolicyEvaluator(policy, hooks=tuple(hooks) if hooks else ())
        self._halted = False
        self._halt_reason: str | None = None
        self._decisions: dict[str, RiskDecision] = {}
        # decision_id -> (side, abs_notional, signed_notional)
        self._reservations: dict[str, tuple[RiskSide, Decimal, Decimal]] = {}
        # decision_id -> (instrument, quantity); tracks reduce-only requests
        # that have been approved but not yet released (filled/canceled), so
        # concurrent reduce-only requests against the same position can't
        # jointly reduce past zero and flip it (RISK_CONTRACT.md: reduce-only
        # can never increase absolute or directional exposure).
        self._reduce_only_reservations: dict[str, tuple[str, Decimal]] = {}

    # -- halt management -------------------------------------------------

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str | None:
        """Mirrors `PaperExecutionEngine.halt_reason`'s public naming so a
        caller (e.g. the persistence integration slice) can read/persist
        either engine's halt reason uniformly without reaching into a
        private attribute."""
        return self._halt_reason

    def halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason

    def reset_halt(self, operator_note: str) -> None:
        if not operator_note:
            raise ValueError("operator_note is required to reset a halt")
        self._halted = False
        self._halt_reason = None

    # -- reservation ledger -----------------------------------------------

    @property
    def _reserved_gross(self) -> Decimal:
        return sum((abs_notional for _, abs_notional, _ in self._reservations.values()), ZERO)

    @property
    def _reserved_net(self) -> Decimal:
        return sum((signed for _, _, signed in self._reservations.values()), ZERO)

    def release(self, decision_id: str) -> None:
        """Idempotent release of a reservation (fill-applied or cancel)."""
        self._reservations.pop(decision_id, None)
        self._reduce_only_reservations.pop(decision_id, None)

    def _reserved_reduce_only_quantity(self, instrument: str) -> Decimal:
        return sum(
            (qty for inst, qty in self._reduce_only_reservations.values() if inst == instrument),
            ZERO,
        )

    # -- checkpointing ---------------------------------------------------

    def export_state(self) -> dict[str, Any]:
        """Serialize the risk-capacity/halt state a restart must restore.

        Deliberately does NOT include `_decisions` (the full per-signal
        decision audit cache): a replayed signal after restart just
        re-evaluates fresh against current market/account state (it is not
        idempotency-critical the way reservations are), and execution's own
        request-id dedup (`PaperExecutionEngine._requests`) still catches a
        duplicate submit independently. Matches
        `PaperExecutionEngine.export_checkpoint()`'s naming convention: plain
        JSON-compatible types only (`str` for every `Decimal`, `.value` for
        every enum), sorted keys for deterministic output.
        """
        return {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "reservations": {
                decision_id: {
                    "side": side.value,
                    "abs_notional": str(abs_notional),
                    "signed_notional": str(signed_notional),
                }
                for decision_id, (side, abs_notional, signed_notional) in sorted(
                    self._reservations.items()
                )
            },
            "reduce_only_reservations": {
                decision_id: {"instrument": instrument, "quantity": str(quantity)}
                for decision_id, (instrument, quantity) in sorted(
                    self._reduce_only_reservations.items()
                )
            },
        }

    def import_state(self, state: Mapping[str, Any]) -> None:
        """Restore state previously produced by `export_state()`.

        Feeds back into real risk-capacity math immediately: an imported
        reservation is included in `_reserved_gross`/`_reserved_net` (and
        therefore in `evaluate()`'s exposure-limit checks) exactly like one
        created by a live `evaluate()` call, since both are read from the
        same `_reservations` dict.
        """
        self._halted = bool(state["halted"])
        self._halt_reason = state["halt_reason"]
        self._reservations = {
            decision_id: (
                RiskSide(payload["side"]),
                Decimal(payload["abs_notional"]),
                Decimal(payload["signed_notional"]),
            )
            for decision_id, payload in state.get("reservations", {}).items()
        }
        self._reduce_only_reservations = {
            decision_id: (payload["instrument"], Decimal(payload["quantity"]))
            for decision_id, payload in state.get("reduce_only_reservations", {}).items()
        }

    # -- public API ---------------------------------------------------------

    def evaluate(
        self,
        *,
        request: PositionSizingRequest,
        snapshot: MarketSnapshot,
        account: AccountRiskState,
        runtime: RuntimeRiskState,
        instrument: InstrumentRiskLimits,
        now: datetime,
    ) -> RiskDecision:
        decision_id = f"risk:{request.signal_id}"
        cached = self._decisions.get(decision_id)
        if cached is not None:
            if cached.approved:
                not_ready = self._readiness_rejection(runtime)
                if not_ready is not None:
                    # Fail-closed: the engine's readiness has degraded since
                    # this decision was approved (halted, kill-switched,
                    # DEGRADED/RECONCILING mode, or risk_ready=False). Do not
                    # silently re-hand out a stale approval — the stored
                    # decision stays in the audit trail unchanged, but a
                    # caller re-querying it now gets a fresh rejection.
                    return self._reject(
                        decision_id=decision_id,
                        request=request,
                        reason_code=not_ready,
                        reason="engine readiness degraded since this decision was approved; "
                        "cached approval withheld",
                    )
            return cached

        try:
            decision = self._evaluate_inner(
                decision_id=decision_id,
                request=request,
                snapshot=snapshot,
                account=account,
                runtime=runtime,
                instrument=instrument,
                now=now,
            )
        except RiskRejection as rejection:
            decision = self._reject(
                decision_id=decision_id,
                request=request,
                reason_code=rejection.reason_code,
                reason=rejection.reason,
            )
        except Exception as exc:
            self.halt(f"RISK_ERROR: {exc}")
            decision = self._reject(
                decision_id=decision_id,
                request=request,
                reason_code=RiskReason.RISK_ERROR,
                reason=f"unexpected error: {exc}",
            )

        self._decisions[decision_id] = decision
        return decision

    def evaluate_reduce_only(
        self,
        *,
        request_id: str,
        instrument: str,
        side: RiskSide,
        quantity: Decimal,
        account: AccountRiskState,
        runtime: RuntimeRiskState,
        now: datetime,
    ) -> RiskDecision:
        del runtime  # reduce-only is allowed even while halted/limited
        decision_id = f"reduce-only:{request_id}"
        cached = self._decisions.get(decision_id)
        if cached is not None:
            return cached

        try:
            decision = self._evaluate_reduce_only_inner(
                decision_id=decision_id,
                request_id=request_id,
                instrument=instrument,
                side=side,
                quantity=quantity,
                account=account,
                now=now,
            )
        except RiskRejection as rejection:
            decision = self._reduce_only_reject(
                decision_id=decision_id,
                request_id=request_id,
                instrument=instrument,
                side=side,
                now=now,
                reason_code=rejection.reason_code,
                reason=rejection.reason,
            )
        except Exception as exc:
            self.halt(f"RISK_ERROR: {exc}")
            decision = self._reduce_only_reject(
                decision_id=decision_id,
                request_id=request_id,
                instrument=instrument,
                side=side,
                now=now,
                reason_code=RiskReason.RISK_ERROR,
                reason=f"unexpected error: {exc}",
            )

        self._decisions[decision_id] = decision
        return decision

    # -- reduce-only ---------------------------------------------------------

    def _evaluate_reduce_only_inner(
        self,
        *,
        decision_id: str,
        request_id: str,
        instrument: str,
        side: RiskSide,
        quantity: Decimal,
        account: AccountRiskState,
        now: datetime,
    ) -> RiskDecision:
        result = self._evaluator.evaluate_reduce_only(
            instrument=instrument,
            side=side,
            quantity=quantity,
            account=account,
            now=now,
            reserved_reduce_only_quantity=self._reserved_reduce_only_quantity(instrument),
        )
        if isinstance(result, PolicyRejection):
            raise RiskRejection(result.reason_code, result.reason)
        del request_id
        self._reduce_only_reservations[decision_id] = (instrument, quantity)
        return RiskDecision(
            decision_id=decision_id,
            signal_id=decision_id,
            instrument=instrument,
            timestamp=now,
            approved=True,
            reason="reduce-only approved",
            reason_code=RiskReason.APPROVED,
            quantity=result.quantity,
            notional=result.notional,
            leverage=result.leverage,
            max_leverage=result.max_leverage,
            risk_budget=result.risk_budget,
            stop_price=None,
            metadata=result.metadata,
        )

    def _reduce_only_reject(
        self,
        *,
        decision_id: str,
        request_id: str,
        instrument: str,
        side: RiskSide,
        now: datetime,
        reason_code: RiskReason | str,
        reason: str,
    ) -> RiskDecision:
        del request_id
        return RiskDecision(
            decision_id=decision_id,
            signal_id=decision_id,
            instrument=instrument,
            timestamp=now,
            approved=False,
            reason=reason,
            reason_code=reason_code,
            quantity=ZERO,
            notional=ZERO,
            leverage=ZERO,
            max_leverage=MAX_SYSTEM_LEVERAGE,
            risk_budget=ZERO,
            stop_price=None,
            metadata={"reduce_only": True, "side": side.value},
        )

    # -- main evaluation ------------------------------------------------------

    def _reject(
        self,
        *,
        decision_id: str,
        request: PositionSizingRequest,
        reason_code: RiskReason | str,
        reason: str,
    ) -> RiskDecision:
        return RiskDecision(
            decision_id=decision_id,
            signal_id=request.signal_id,
            instrument=request.instrument,
            timestamp=request.timestamp,
            approved=False,
            reason=reason,
            reason_code=reason_code,
            quantity=ZERO,
            notional=ZERO,
            leverage=ZERO,
            max_leverage=MAX_SYSTEM_LEVERAGE,
            risk_budget=ZERO,
            stop_price=None,
            metadata={"side": request.side.value},
        )

    def _readiness_rejection(self, runtime: RuntimeRiskState) -> RiskReason | None:
        """Shared by the fresh-evaluation path (inside the pure policy) and
        the cache-hit path in `evaluate()` so a cached APPROVED decision is
        re-checked against every readiness predicate a fresh call would use."""
        return readiness_rejection(runtime, latched_halt=self._halted)

    def _evaluate_inner(
        self,
        *,
        decision_id: str,
        request: PositionSizingRequest,
        snapshot: MarketSnapshot,
        account: AccountRiskState,
        runtime: RuntimeRiskState,
        instrument: InstrumentRiskLimits,
        now: datetime,
    ) -> RiskDecision:
        result = self._evaluator.evaluate_entry(
            request=request,
            snapshot=snapshot,
            account=account,
            runtime=runtime,
            instrument=instrument,
            now=now,
            pending=PendingExposure(gross=self._reserved_gross, net=self._reserved_net),
            latched_halt=self._halted,
        )
        if isinstance(result, PolicyRejection):
            raise RiskRejection(result.reason_code, result.reason)
        return self._approve(decision_id=decision_id, request=request, intent=result)

    def _approve(
        self, *, decision_id: str, request: PositionSizingRequest, intent: ProposedOrderIntent
    ) -> RiskDecision:
        # The reservation write is the legacy engine's own state; the pure
        # policy that produced `intent` never touches it.
        signed_notional = intent.notional if intent.side == RiskSide.BUY else -intent.notional
        self._reservations[decision_id] = (intent.side, intent.notional, signed_notional)
        return RiskDecision(
            decision_id=decision_id,
            signal_id=request.signal_id,
            instrument=intent.instrument,
            timestamp=request.timestamp,
            approved=True,
            reason="approved",
            reason_code=RiskReason.APPROVED,
            quantity=intent.quantity,
            notional=intent.notional,
            leverage=intent.leverage,
            max_leverage=intent.max_leverage,
            risk_budget=intent.risk_budget,
            stop_price=intent.stop_price,
            metadata=intent.metadata,
        )
