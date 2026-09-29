"""Engine-agnostic contract-test harness.

The tests in this package pin SAFETY BEHAVIOR (reason codes, sizing arithmetic,
fail-closed rules), not the structure of any one engine. Each contract talks to
a thin adapter, so a future implementation only has to provide the adapter:

* ``EntryAdmission``   -- new-exposure and reduce-only admission
                          (``admit`` / ``admit_reduce_only``).
* ``RuntimeUnderTest`` -- the lifecycle (entry, exits, replayed fills).

Implementations registered today:

* ``pure_policy``   -- the pure ``risk.policy.RiskPolicyEvaluator``.
* ``legacy_engine`` -- the legacy stateful ``risk.engine.RiskEngine`` (shadow
                       oracle). Every risk contract runs against BOTH, so any
                       divergence between them fails a contract test.
* ``legacy_paper_pipeline`` -- ``PaperTradingPipeline`` + ``ExitEngine`` +
                       ``PaperExecutionEngine`` (the legacy runtime).

TODO(nautilus): add a third ``EntryAdmission`` implementation (and a
``RuntimeUnderTest`` implementation) backed by the Nautilus-based runtime and
append it to ``ADMISSION_IMPLEMENTATIONS`` / ``RUNTIME_IMPLEMENTATIONS``. Do not
import Nautilus anywhere else in this package; only the adapters may know it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

import pytest

from data.models import DataQuality, MarketSnapshot
from exits.models import ExitReason
from pipeline.paper import PipelineStage
from risk.engine import RiskEngine
from risk.models import (
    MAX_SYSTEM_LEVERAGE,
    AccountRiskState,
    InstrumentRiskLimits,
    PositionSizingRequest,
    ReconciliationState,
    RiskPolicy,
    RiskReason,
    RiskSide,
    RuntimeMode,
    RuntimeRiskState,
)
from risk.policy import (
    PendingExposure,
    PolicyRejection,
    RiskPolicyEvaluator,
)
from signals.models import Direction
from tests.integration import e2e_scenarios as scenarios

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
INSTRUMENT = "BTCUSDT-PERP"
ZERO = Decimal("0")

# -- fixture builders (same defaults as tests/unit/risk/test_engine.py) -----------


def make_policy(**changes: object) -> RiskPolicy:
    values: dict[str, object] = {
        "policy_id": "risk-v1",
        "risk_fraction": Decimal("0.01"),
        "max_leverage": Decimal("10"),
        "max_gross_notional": Decimal("10000"),
        "max_net_notional": Decimal("5000"),
        "max_daily_loss": Decimal("100"),
        "max_drawdown": Decimal("200"),
        "max_consecutive_losses": 3,
        "liquidity_fraction": Decimal("0.10"),
        "max_data_age": timedelta(seconds=5),
        "max_signal_age": timedelta(seconds=5),
    }
    values.update(changes)
    return RiskPolicy(**values)  # type: ignore[arg-type]


def make_limits(**changes: object) -> InstrumentRiskLimits:
    values: dict[str, object] = {
        "instrument": INSTRUMENT,
        "max_leverage": Decimal("8"),
        "quantity_step": Decimal("0.1"),
        "min_quantity": Decimal("0.1"),
        "min_notional": Decimal("10"),
        "max_notional": Decimal("4000"),
        "max_spread_bps": Decimal("250"),
        "maintenance_margin_rate": Decimal("0.005"),
    }
    values.update(changes)
    return InstrumentRiskLimits(**values)  # type: ignore[arg-type]


def make_account(**changes: object) -> AccountRiskState:
    values: dict[str, object] = {
        "state_version": "account-1",
        "known": True,
        "reconciliation": ReconciliationState.RECONCILED,
        "pnl_window_start": NOW.replace(hour=0, minute=0),
        "equity": Decimal("1000"),
        "peak_equity": Decimal("1000"),
        "realized_pnl_today": Decimal("0"),
        "unrealized_pnl": Decimal("0"),
        "gross_notional": Decimal("0"),
        "net_notional": Decimal("0"),
        "instrument_notionals": {},
        "positions": {},
        "leverage_cap": Decimal("5"),
        "consecutive_losses": 0,
    }
    values.update(changes)
    return AccountRiskState(**values)  # type: ignore[arg-type]


def make_runtime(**changes: object) -> RuntimeRiskState:
    values: dict[str, object] = {
        "state_version": "runtime-1",
        "mode": RuntimeMode.READY,
        "risk_ready": True,
        "kill_switch": False,
    }
    values.update(changes)
    return RuntimeRiskState(**values)  # type: ignore[arg-type]


def make_snapshot(**changes: object) -> MarketSnapshot:
    values: dict[str, object] = {
        "instrument": INSTRUMENT,
        "timestamp": NOW,
        "bid": Decimal("99"),
        "ask": Decimal("101"),
        "last": Decimal("100"),
        "volume": Decimal("1000"),
        "volatility": Decimal("0.02"),
        "liquidity": Decimal("1"),
        "source": "venue-a",
        "quality": DataQuality.LIVE,
    }
    values.update(changes)
    return MarketSnapshot(**values)  # type: ignore[arg-type]


def make_request(**changes: object) -> PositionSizingRequest:
    values: dict[str, object] = {
        "signal_id": "signal-1",
        "instrument": INSTRUMENT,
        "timestamp": NOW,
        "side": RiskSide.BUY,
        "entry_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "confidence": Decimal("0.2"),
        "available_liquidity_notional": Decimal("10000"),
        "metadata": {"signal_version": "v1"},
    }
    values.update(changes)
    return PositionSizingRequest(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True, kw_only=True)
class Scenario:
    """Everything one new-exposure admission decision depends on."""

    policy: RiskPolicy = field(default_factory=make_policy)
    limits: InstrumentRiskLimits = field(default_factory=make_limits)
    account: AccountRiskState = field(default_factory=make_account)
    runtime: RuntimeRiskState = field(default_factory=make_runtime)
    snapshot: MarketSnapshot = field(default_factory=make_snapshot)
    request: PositionSizingRequest = field(default_factory=make_request)
    now: datetime = NOW
    # Exposure already committed by approved-but-unfilled requests (contract 14).
    pending_gross: Decimal = ZERO
    pending_net: Decimal = ZERO
    # The caller's halt latch (contract 16).
    latched_halt: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class AdmissionOutcome:
    approved: bool
    reason_code: str
    quantity: Decimal
    notional: Decimal
    leverage: Decimal
    max_leverage: Decimal
    reference_price: Decimal | None = None
    risk_budget: Decimal | None = None
    binding_constraint: str | None = None
    reduce_risk: bool | None = None
    reduce_only: bool = False


class EntryAdmission(Protocol):
    """What every risk implementation must offer to be contract-tested."""

    name: str

    def admit(self, scenario: Scenario) -> AdmissionOutcome: ...

    def admit_reduce_only(
        self,
        *,
        account: AccountRiskState,
        side: RiskSide,
        quantity: Decimal,
        runtime: RuntimeRiskState | None = None,
        now: datetime = NOW,
        reserved_quantity: Decimal = ZERO,
        latched_halt: bool = False,
        instrument: str = INSTRUMENT,
    ) -> AdmissionOutcome: ...


def _rejected(reason_code: object, *, reduce_only: bool = False) -> AdmissionOutcome:
    return AdmissionOutcome(
        approved=False,
        reason_code=str(reason_code),
        quantity=ZERO,
        notional=ZERO,
        leverage=ZERO,
        max_leverage=MAX_SYSTEM_LEVERAGE,
        reduce_only=reduce_only,
    )


class PurePolicyAdmission:
    """(a) The pure `RiskPolicyEvaluator`."""

    name = "pure_policy"

    def admit(self, scenario: Scenario) -> AdmissionOutcome:
        evaluator = RiskPolicyEvaluator(scenario.policy)
        result = evaluator.evaluate_entry(
            request=scenario.request,
            snapshot=scenario.snapshot,
            account=scenario.account,
            runtime=scenario.runtime,
            instrument=scenario.limits,
            now=scenario.now,
            pending=PendingExposure(gross=scenario.pending_gross, net=scenario.pending_net),
            latched_halt=scenario.latched_halt,
        )
        if isinstance(result, PolicyRejection):
            return _rejected(result.reason_code)
        return AdmissionOutcome(
            approved=True,
            reason_code=str(RiskReason.APPROVED),
            quantity=result.quantity,
            notional=result.notional,
            leverage=result.leverage,
            max_leverage=result.max_leverage,
            reference_price=result.reference_price,
            risk_budget=result.risk_budget,
            binding_constraint=result.metadata["binding_constraint"],
            reduce_risk=result.metadata["reduce_risk"],
        )

    def admit_reduce_only(
        self,
        *,
        account: AccountRiskState,
        side: RiskSide,
        quantity: Decimal,
        runtime: RuntimeRiskState | None = None,
        now: datetime = NOW,
        reserved_quantity: Decimal = ZERO,
        latched_halt: bool = False,
        instrument: str = INSTRUMENT,
    ) -> AdmissionOutcome:
        del runtime, latched_halt  # reduce-only ignores halt/kill switch by contract
        evaluator = RiskPolicyEvaluator(make_policy())
        result = evaluator.evaluate_reduce_only(
            instrument=instrument,
            side=side,
            quantity=quantity,
            account=account,
            now=now,
            reserved_reduce_only_quantity=reserved_quantity,
        )
        if isinstance(result, PolicyRejection):
            return _rejected(result.reason_code, reduce_only=True)
        return AdmissionOutcome(
            approved=True,
            reason_code=str(RiskReason.APPROVED),
            quantity=result.quantity,
            notional=result.notional,
            leverage=result.leverage,
            max_leverage=result.max_leverage,
            reduce_only=result.reduce_only,
        )


class LegacyEngineAdmission:
    """(b) The legacy stateful `RiskEngine` (shadow oracle).

    A FRESH engine is built for every call so the per-signal decision cache and
    reservation ledger of one call can never leak into the next. Pre-existing
    state a scenario needs (pending exposure, reduce-only reservations, the halt
    latch) is injected through the engine's public `import_state()`.
    """

    name = "legacy_engine"

    def admit(self, scenario: Scenario) -> AdmissionOutcome:
        engine = RiskEngine(scenario.policy)
        reservations: dict[str, Any] = {}
        if scenario.pending_gross != 0 or scenario.pending_net != 0:
            reservations["pending-1"] = {
                "side": (RiskSide.BUY if scenario.pending_net >= 0 else RiskSide.SELL).value,
                "abs_notional": str(scenario.pending_gross),
                "signed_notional": str(scenario.pending_net),
            }
        engine.import_state(
            {
                "halted": scenario.latched_halt,
                "halt_reason": "contract-test latch" if scenario.latched_halt else None,
                "reservations": reservations,
            }
        )
        decision = engine.evaluate(
            request=scenario.request,
            snapshot=scenario.snapshot,
            account=scenario.account,
            runtime=scenario.runtime,
            instrument=scenario.limits,
            now=scenario.now,
        )
        if not decision.approved:
            return _rejected(decision.reason_code)
        return AdmissionOutcome(
            approved=True,
            reason_code=str(decision.reason_code),
            quantity=decision.quantity,
            notional=decision.notional,
            leverage=decision.leverage,
            max_leverage=decision.max_leverage,
            reference_price=Decimal(decision.metadata["risk_reference_price"]),
            risk_budget=decision.risk_budget,
            binding_constraint=decision.metadata["binding_constraint"],
            reduce_risk=decision.metadata["reduce_risk"],
        )

    def admit_reduce_only(
        self,
        *,
        account: AccountRiskState,
        side: RiskSide,
        quantity: Decimal,
        runtime: RuntimeRiskState | None = None,
        now: datetime = NOW,
        reserved_quantity: Decimal = ZERO,
        latched_halt: bool = False,
        instrument: str = INSTRUMENT,
    ) -> AdmissionOutcome:
        engine = RiskEngine(make_policy())
        reduce_only_reservations: dict[str, Any] = {}
        if reserved_quantity != 0:
            reduce_only_reservations["reduce-only:earlier"] = {
                "instrument": instrument,
                "quantity": str(reserved_quantity),
            }
        engine.import_state(
            {
                "halted": latched_halt,
                "halt_reason": "contract-test latch" if latched_halt else None,
                "reduce_only_reservations": reduce_only_reservations,
            }
        )
        decision = engine.evaluate_reduce_only(
            request_id="contract-1",
            instrument=instrument,
            side=side,
            quantity=quantity,
            account=account,
            runtime=runtime if runtime is not None else make_runtime(),
            now=now,
        )
        if not decision.approved:
            return _rejected(decision.reason_code, reduce_only=True)
        return AdmissionOutcome(
            approved=True,
            reason_code=str(decision.reason_code),
            quantity=decision.quantity,
            notional=decision.notional,
            leverage=decision.leverage,
            max_leverage=decision.max_leverage,
            reduce_only=bool(decision.metadata.get("reduce_only")),
        )


ADMISSION_IMPLEMENTATIONS: Mapping[str, Callable[[], EntryAdmission]] = {
    "pure_policy": PurePolicyAdmission,
    "legacy_engine": LegacyEngineAdmission,
    # TODO(nautilus): "nautilus": NautilusAdmission,
}


@pytest.fixture(params=list(ADMISSION_IMPLEMENTATIONS), ids=list(ADMISSION_IMPLEMENTATIONS))
def admission(request: pytest.FixtureRequest) -> EntryAdmission:
    """Every risk contract runs against every registered implementation."""
    return ADMISSION_IMPLEMENTATIONS[request.param]()


# -- lifecycle adapter -------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class EntryAttempt:
    new_exposure_admitted: bool
    position_qty: Decimal
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ExitTick:
    """What one exit-management tick did. `reason is None` means it did nothing."""

    reason: ExitReason | None
    is_partial: bool | None
    position_closed: bool
    position_qty: Decimal


class RuntimeUnderTest(Protocol):
    """Lifecycle surface a runtime must offer to be contract-tested."""

    name: str

    def position_qty(self) -> Decimal: ...

    def open_long(self) -> Decimal:
        """Open a long through the normal signal path; return the position size."""
        ...

    def submit_signal(self, direction: Direction, signal_id: str) -> EntryAttempt: ...

    def exit_tick(
        self,
        price: Decimal,
        *,
        minutes: int = 1,
        data_age: timedelta = timedelta(0),
        kill_switch: bool = False,
    ) -> ExitTick: ...

    def halt_execution(self) -> None: ...

    def replay_last_exit_fill(self) -> None:
        """Deliver the fill of the most recent exit a second time."""
        ...

    def entry_price(self) -> Decimal: ...

    def initial_risk(self) -> Decimal: ...


class LegacyPipelineRuntime:
    """(a) Legacy paper runtime: pipeline + exit engine + paper execution."""

    name = "legacy_paper_pipeline"

    def __init__(self, **exit_policy_overrides: object) -> None:
        self._h = scenarios.build_harness(
            exit_policy=scenarios.make_exit_policy(**exit_policy_overrides)
        )
        self._now = scenarios.NOW

    def position_qty(self) -> Decimal:
        view = self._h.portfolio.positions.get(scenarios.INSTRUMENT)
        return view.quantity if view is not None else ZERO

    def open_long(self) -> Decimal:
        outcome = self.submit_signal(Direction.LONG, "contract-open")
        assert outcome.new_exposure_admitted, outcome.detail
        return outcome.position_qty

    def submit_signal(self, direction: Direction, signal_id: str) -> EntryAttempt:
        self._now = self._now + timedelta(minutes=1)
        now = self._now
        invalidation = Decimal("95") if direction is Direction.LONG else Decimal("120")
        signal = scenarios.make_signal(
            direction=direction,
            invalidation_level=invalidation,
            timestamp=now,
            signal_id=signal_id,
        )
        history = scenarios.make_history(["100", "100", "100"], start=now - timedelta(minutes=2))
        outcome = self._h.pipeline.process(
            history=history,
            strategy=scenarios.FixedStrategy(signal),
            universe=frozenset({scenarios.INSTRUMENT}),
            runtime=scenarios.make_runtime(),
            account_known=True,
            now=now,
        )
        return EntryAttempt(
            new_exposure_admitted=outcome.stage is PipelineStage.EXECUTED,
            position_qty=self.position_qty(),
            detail=outcome.stage.value,
        )

    def _position(self) -> Any:
        return self._h.pipeline._exit_positions[scenarios.INSTRUMENT]

    def entry_price(self) -> Decimal:
        return self._position().entry_price

    def initial_risk(self) -> Decimal:
        return self._position().initial_risk

    def exit_tick(
        self,
        price: Decimal,
        *,
        minutes: int = 1,
        data_age: timedelta = timedelta(0),
        kill_switch: bool = False,
    ) -> ExitTick:
        self._now = self._now + timedelta(minutes=minutes)
        now = self._now
        snapshot = scenarios.make_snapshot(
            timestamp=now - data_age,
            bid=price - Decimal("0.1"),
            ask=price + Decimal("0.1"),
            last=price,
            volatility=Decimal("0.01"),
        )
        result = self._h.pipeline.process_exits(
            snapshot=snapshot,
            runtime=scenarios.make_runtime(kill_switch=kill_switch),
            account_known=True,
            now=now,
        )
        decision = result.decision if result is not None else None
        return ExitTick(
            reason=decision.reason if decision is not None else None,
            is_partial=decision.is_partial if decision is not None else None,
            position_closed=bool(result.position_closed) if result is not None else False,
            position_qty=self.position_qty(),
        )

    def halt_execution(self) -> None:
        from execution.orders import HaltCode

        self._h.execution_engine._halt(HaltCode.OVERFILL, "induced by contract test")

    def replay_last_exit_fill(self) -> None:
        fills = list(self._h.pipeline._pending_fills)
        assert fills, "no exit fill to replay"
        last = fills[-1]
        # Market fills are keyed "<client_order_id>:<suffix>"; re-report the
        # identical fill (same id) as a duplicate delivery.
        prefix = f"{last.client_order_id}:"
        assert last.fill_id.startswith(prefix)
        self._h.pipeline.process_reported_fill(
            client_order_id=last.client_order_id,
            trade_id=last.fill_id[len(prefix) :],
            price=last.price,
            quantity=last.quantity,
            now=self._now,
        )


RUNTIME_IMPLEMENTATIONS: Mapping[str, Callable[..., RuntimeUnderTest]] = {
    "legacy_paper_pipeline": LegacyPipelineRuntime,
    # TODO(nautilus): "nautilus": NautilusRuntime,
}


@pytest.fixture(params=list(RUNTIME_IMPLEMENTATIONS), ids=list(RUNTIME_IMPLEMENTATIONS))
def runtime_factory(request: pytest.FixtureRequest) -> Callable[..., RuntimeUnderTest]:
    """Factory taking exit-policy overrides; one fresh runtime per call."""
    return RUNTIME_IMPLEMENTATIONS[request.param]
