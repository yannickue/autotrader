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

Lane E2 (chart first, R second)
-------------------------------
* PLAN PRODUCER: ``produce_exit_context`` builds the per-position ``exit_plan`` (TP1 / TP2 price levels from the
  chart, stage fractions configurable per family) and the SHADOW comparison of the family geometry against the
  structure geometry of ``demo.structure``. ``ExitPlanConfig.geometry_source`` = ``family`` (DEFAULT: the initial
  stop and sizing stay family-derived, nothing changes for the five core markets) or ``structure`` (opt-in per
  family/market: the structural invalidation stop replaces the family stop BEFORE sizing).
* REMAINDER / RUNNER POLICY (decided): stage fractions may sum to < 1. The remainder (``runner = 1 - sum``) carries
  NO broker take-profit; it stays protected by the broker stop (tighten-only: cost-adjusted break-even after TP1,
  then behind the newest confirmed structure swing) and is closed by that stop, by structure failure / momentum /
  MFE-giveback / time-alpha decay, by a late-session loser rule, or by the forced flat (Lane P, which always wins).
  When no defensible second target exists the plan is TP1 + runner and is marked
  ``SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED``; a TP2 is never invented from an R multiple.
* Partial ENTRY fills: the registry keeps the FILLED quantity as the position's ``initial_quantity`` (stage
  accounting uses it, never the requested size).
* Engine full closes carry explicit ``EXIT_ENGINE_*`` exit reasons (uncensored strategy exits).

Reduce-only risk reservation: ``DemoRiskGate`` holds no reservation for reduce-only orders (it only
sizes ENTRIES), so ``ExitEngine.notify_terminal`` is fed a recording no-op release gate: the call is
made on every terminal outcome (fill / cancel / reject) for audit symmetry, N/A for the risk book.
"""

from __future__ import annotations

import collections
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

from demo import exit_profiles as xp
from demo import structure as st
from demo.contracts import ENGINE_EXIT_REASONS
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
    ExitReason,
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
# Lane Y: per-intent exit PROFILES (CONTINUATION / REVERSION / FAILED_MOVE on the same ExitEngine; FIXED_1_5R-mapped
# families keep the unchanged fixed behaviour). ``staged`` itself is unchanged (legacy E2 behaviour, no profile routing).
EXIT_POLICY_PROFILES = "staged_profiles"
EXIT_POLICIES = (EXIT_POLICY_FIXED, EXIT_POLICY_STAGED, EXIT_POLICY_PROFILES)
MANAGED_EXIT_POLICIES = (EXIT_POLICY_STAGED, EXIT_POLICY_PROFILES)  # the policies that run the StagedExitManager

R_EXIT_TRANCHE_LIMITATION = "EXIT_PLAN_MULTI_TRANCHE_NOT_SUPPORTED"  # TEMPORARY, explicit
R_EXIT_QUOTE_UNAVAILABLE = "EXIT_QUOTE_MISSING_OR_STALE"
R_EXIT_SIZE_BELOW_MIN_LOT = "EXIT_STAGE_SIZE_BELOW_BROKER_MIN_LOT"


GEOMETRY_FAMILY = "family"
GEOMETRY_STRUCTURE = "structure"
GEOMETRY_SOURCES = (GEOMETRY_FAMILY, GEOMETRY_STRUCTURE)
# TP1 / TP2 fractions of the ORIGINAL (filled) quantity; the runner is the remainder (here 25 %). Documented
# DEFAULT only - every family/market can override it through ``ExitPlanConfig.family_fractions``.
DEFAULT_STAGE_FRACTIONS: tuple[Decimal, ...] = (Decimal("0.5"), Decimal("0.25"))


@dataclass(frozen=True, slots=True)
class ExitPlanConfig:
    """Configuration of the exit-plan producer (Lane E2). Defaults keep today's family geometry."""

    geometry_source: str = GEOMETRY_FAMILY  # family | structure (applies to every market/family)
    structure_families: frozenset[str] = frozenset()  # families that OPT IN to the structure initial stop
    structure_markets: frozenset[str] = frozenset()  # markets that OPT IN to the structure initial stop
    default_fractions: tuple[Decimal, ...] = DEFAULT_STAGE_FRACTIONS
    family_fractions: Mapping[str, tuple[Decimal, ...]] = field(default_factory=dict)
    bars: int = 120  # closed M5 bars read for structure
    swing_n: int = 2
    range_lookback: int = 24
    atr_buffer_mult: float = 0.25
    min_movement_to_cost: float = 3.0

    def __post_init__(self) -> None:
        if self.geometry_source not in GEOMETRY_SOURCES:
            raise ValueError(f"geometry_source must be one of {GEOMETRY_SOURCES}")
        for fr in (self.default_fractions, *self.family_fractions.values()):
            if not fr or any(not (Decimal(0) < f <= Decimal(1)) for f in fr) or sum(fr, Decimal(0)) > Decimal(1):
                raise ValueError("stage fractions must be in (0, 1] and sum to at most 1 (the rest is the runner)")

    def fractions_for(self, family: str | None) -> tuple[Decimal, ...]:
        return tuple(self.family_fractions.get(family or "", self.default_fractions))

    def source_for(self, family: str | None, market: str | None) -> str:
        if self.geometry_source == GEOMETRY_STRUCTURE or (family or "") in self.structure_families or (market or "") in self.structure_markets:
            return GEOMETRY_STRUCTURE
        return GEOMETRY_FAMILY


def default_staged_exit_policy() -> ExitPolicy:
    """The DEMO ``staged`` ExitPolicy (documented defaults; tune per family later, nothing here is proven edge).

    Chart first: break-even only after TP1 (cost-adjusted), ATR trailing OFF, trailing behind confirmed
    structure ON, structural-failure exit ON. The R based thresholds are deliberately conservative guards,
    not targets: momentum exit at -1.0 ATR over 3 bars, MFE giveback 60 % of a >= 1.5R excursion, time stop
    6 h unless the trade showed >= 0.5R, late-session window 60 min before the forced flat."""
    return ExitPolicy(
        policy_id="staged-e2-v1",
        breakeven_trigger_r_multiple=Decimal("99"),
        breakeven_after_first_stage=True,
        breakeven_buffer_bps=Decimal("0"),
        trailing_activation_r_multiple=Decimal("99"),
        trailing_distance_volatility_multiplier=Decimal("2"),
        structure_trailing=True,
        structure_failure_exit=True,
        momentum_deterioration_threshold=Decimal("-1.0"),
        max_giveback_fraction=Decimal("0.6"),
        giveback_min_mfe_r=Decimal("1.5"),
        max_holding_duration=timedelta(hours=6),
        time_stop_min_mfe_r=Decimal("0.5"),
        late_window=timedelta(minutes=60),
        late_loser_momentum_threshold=Decimal("-0.3"),
        max_market_data_age=timedelta(seconds=30),
    )


def engine_exit_reason(decision: Any) -> str:
    """Explicit exit reason of an engine FULL close (see ``demo.contracts.ENGINE_EXIT_REASONS``).

    An engine EMERGENCY_RISK_EXIT is a safety action -> ``SAFETY_FLATTEN`` (censored), not a strategy exit."""
    reason = decision.reason
    meta = decision.metadata or {}
    if reason is ExitReason.TAKE_PROFIT:
        idx = int(meta.get("stage_index", 0)) + 1
        code = f"EXIT_ENGINE_TP{min(idx, 4)}"
    elif reason is ExitReason.INVALIDATION_STOP:
        code = "EXIT_ENGINE_BREAK_EVEN" if meta.get("stop_stage") == "break_even" else "EXIT_ENGINE_STOP"
    elif reason is ExitReason.TRAILING_STOP:
        code = "EXIT_ENGINE_TRAIL"
    elif reason in (ExitReason.STRUCTURE_FAILURE, ExitReason.SIGNAL_REVERSAL):
        code = "EXIT_ENGINE_STRUCTURE"
    elif reason is ExitReason.MOMENTUM_DETERIORATION:
        code = "EXIT_ENGINE_MOMENTUM"
    elif reason is ExitReason.LIQUIDITY_DETERIORATION:
        code = "EXIT_ENGINE_LIQUIDITY"
    elif reason is ExitReason.TIME_STOP:
        code = "EXIT_ENGINE_TIME_STOP"
    elif reason is ExitReason.LATE_SESSION_DETERIORATION:
        code = "EXIT_ENGINE_EOD"
    elif reason is ExitReason.MFE_GIVEBACK:
        code = "EXIT_ENGINE_GIVEBACK"
    else:
        return "SAFETY_FLATTEN"
    assert code in ENGINE_EXIT_REASONS, code
    return code


def _level_prices(raw: Any, direction: int, entry: Decimal) -> list[tuple[Decimal, str]]:
    """Family-supplied ``structure_levels`` (numbers or {"price","id"}) -> (price, id) beyond entry, nearest first."""
    out: list[tuple[Decimal, str]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for i, item in enumerate(raw):
        try:
            price = Decimal(str(item["price"] if isinstance(item, Mapping) else item))
            ident = str(item.get("id") or f"level{i}") if isinstance(item, Mapping) else f"level{i}"
        except (KeyError, ValueError, ArithmeticError, TypeError):
            continue
        if price.is_finite() and price > 0 and ((price > entry) if direction == 1 else (price < entry)):
            out.append((price, ident))
    out.sort(key=lambda t: abs(t[0] - entry))
    deduped: list[tuple[Decimal, str]] = []
    for price, ident in out:
        if not deduped or price != deduped[-1][0]:
            deduped.append((price, ident))
    return deduped


def build_exit_plan(
    *,
    direction: int,
    entry_ref: Decimal,
    stop: Decimal,
    fractions: Sequence[Decimal],
    geometry: st.StructuralGeometry | None,
    source: str,
    family_target: Decimal | None,
    target_is_structural: bool,
    structure_levels: Any = None,
    prefer_family_target: bool = False,
    record_tp2_shadow: bool = False,
) -> dict[str, Any]:
    """``{"stages": [...], ...}`` exit plan: TP1 (+ TP2 only if structurally justified) + runner remainder.

    Level priority: family-supplied ``structure_levels`` > the structure geometry (when ``source`` is
    ``structure``) > the family's own target (a structural target as a price stage, a fixed-R family target as
    an honest ``R`` stage). A TP2 is never invented: no second level -> ``SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED``."""
    markers: list[str] = []
    levels: list[tuple[Decimal, str]] = []
    r_stage: tuple[Decimal, str] | None = None
    supplied = _level_prices(structure_levels, direction, entry_ref)
    fam_ok = family_target is not None and ((family_target > entry_ref) if direction == 1 else (family_target < entry_ref))
    if supplied:
        levels = supplied
        origin = "family_structure_levels"
    elif prefer_family_target and fam_ok and target_is_structural:
        # Lane Y REVERSION: the family's own structural mean target (anchor / previous close / range) comes first
        levels = [(family_target, "family_target")]  # type: ignore[list-item]
        origin = "family_target"
    elif source == GEOMETRY_STRUCTURE and geometry is not None and geometry.tp1 is not None:
        levels = [(geometry.tp1.price, geometry.tp1.structure_id)]
        if geometry.tp2 is not None:
            levels.append((geometry.tp2.price, geometry.tp2.structure_id))
        markers.extend(m for m in geometry.markers if m.startswith(("NO_", "SECOND_")))
        origin = "structure"
    elif family_target is not None and ((family_target > entry_ref) if direction == 1 else (family_target < entry_ref)):
        if target_is_structural:
            levels = [(family_target, "family_target")]
        else:
            risk = abs(entry_ref - stop)
            if risk > 0:
                r_stage = (abs(family_target - entry_ref) / risk, "family_fixed_r_target")
        origin = "family_target"
    else:
        origin = "none"
    stages: list[dict[str, Any]] = []
    fr = list(fractions)
    if r_stage is not None:
        stages.append({"r_multiple": str(r_stage[0]), "close_fraction": str(fr[0]), "stage_id": "tp1", "source": "R"})
    else:
        for i, (price, ident) in enumerate(levels[: len(fr)]):
            stages.append({"target_price": str(price), "close_fraction": str(fr[i]), "stage_id": f"tp{i + 1}", "source": f"STRUCTURE:{ident}"})
    # direction integrity (asserted): LONG stop < entry < TP1 < TP2, SHORT mirrored
    sign = Decimal(direction)
    if not (stop - entry_ref) * sign < 0:
        raise ValueError("exit plan direction integrity: stop must be on the losing side of entry")
    prev = entry_ref
    for stg in stages:
        if "target_price" in stg:
            price = Decimal(stg["target_price"])
            if not (price - prev) * sign > 0:
                raise ValueError("exit plan direction integrity: targets must be ordered beyond entry")
            prev = price
    if not stages:
        markers.append(st.NO_STRUCTURAL_TP1)
    elif len(stages) < 2 and st.SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED not in markers:
        markers.append(st.SECOND_TARGET_NOT_STRUCTURALLY_JUSTIFIED)
    used = [Decimal(x["close_fraction"]) for x in stages]
    # Lane Y: TP2 is implemented but DORMANT - its structural level is only recorded (shadow) for the exit lab
    tp2_shadow = None
    if geometry is not None and geometry.tp2 is not None and len(stages) < 2:
        tp2_shadow = {"price": str(geometry.tp2.price), "id": geometry.tp2.structure_id}
    extra = {"tp2_shadow": tp2_shadow} if record_tp2_shadow else {}
    return {
        **extra,
        "stages": stages,
        "geometry_source": source,
        "target_origin": origin,
        "fractions": {
            "tp1": str(used[0]) if used else None,
            "tp2": str(used[1]) if len(used) > 1 else None,
            "runner": str(Decimal(1) - sum(used, Decimal(0))),
        },
        "markers": markers,
    }


def produce_exit_context(
    *,
    direction: int,
    entry_ref: float,
    stop: float,
    target: float | None,
    family: str | None,
    market: str | None,
    atr: float | None,
    frame: Any,
    spread: float,
    tick_size: float | None,
    structure_levels: Any,
    target_is_structural: bool,
    cfg: ExitPlanConfig,
    staged: bool,
    route: xp.FamilyRoute | None = None,
) -> dict[str, Any]:
    """Runner-side producer (pure given ``frame``): SHADOW geometry comparison + (staged) ``exit_plan``.

    Returns ``{"shadow": {...}, "exit_plan": dict | None, "exit_meta": {...}, "structure_stop": float | None,
    "source": str}``. ``structure_stop`` is only offered when the family/market OPTED IN and a defensible
    structural stop exists; the caller decides whether to apply it (before sizing)."""
    source = cfg.source_for(family, market)
    engine_profile = route is not None and route.profile in xp.ENGINE_PROFILES
    plan_source = source
    if engine_profile:
        assert route is not None
        # Lane Y: the PROFILE (not the stack-wide geometry config) decides where the initial stop comes from: families whose
        # own stop is already the thesis invalidation keep it; ATR-defined family stops are replaced by the chart stop.
        source = GEOMETRY_STRUCTURE if route.stop_basis == xp.STOP_BASIS_CHART else GEOMETRY_FAMILY
        plan_source = GEOMETRY_STRUCTURE  # targets (TP1 / return target) always come from the chart / family structural target
    entry_d, stop_d = Decimal(str(entry_ref)), Decimal(str(stop))
    geometry: st.StructuralGeometry | None = None
    error: str | None = None
    try:
        geometry = st.structural_geometry(
            direction, entry_ref, frame, spread, atr, cost=spread, swing_n=cfg.swing_n,
            range_lookback=cfg.range_lookback, atr_buffer_mult=cfg.atr_buffer_mult,
            min_movement_to_cost=cfg.min_movement_to_cost, tick_size=tick_size,
        )
    except Exception as exc:  # bars unusable: log it, the family geometry is unaffected
        error = f"{type(exc).__name__}:{exc}"[:160]
    risk = abs(entry_d - stop_d)
    family_geo = {
        "stop": str(stop_d), "target": None if target is None else str(target),
        "risk": str(risk),
        "target_r": None if target is None or risk == 0 else str(abs(Decimal(str(target)) - entry_d) / risk),
    }
    shadow = {"source_active": source, "family": family_geo, "structure": None if geometry is None else geometry.as_dict(), "error": error}
    structure_stop = None
    if source == GEOMETRY_STRUCTURE and geometry is not None and geometry.stop is not None:
        structure_stop = float(geometry.stop)
    plan: dict[str, Any] | None = None
    if staged:
        eff_stop = Decimal(str(structure_stop)) if structure_stop is not None else stop_d
        plan = build_exit_plan(
            direction=direction, entry_ref=entry_d, stop=eff_stop,
            fractions=xp.PROFILE_FRACTIONS[route.profile] if engine_profile and route is not None else cfg.fractions_for(family),
            geometry=geometry, source=plan_source, family_target=None if target is None else Decimal(str(target)),
            target_is_structural=target_is_structural, structure_levels=structure_levels,
            prefer_family_target=bool(engine_profile and route is not None and route.profile == xp.PROFILE_REVERSION),
            record_tp2_shadow=engine_profile,
        )
    exit_profile = None if route is None else xp.attribution(route)
    if exit_profile is not None:
        shadow["exit_profile"] = exit_profile  # hook for the shadow exit lab: profile + TP2 / return-target geometry above
    return {
        "shadow": shadow, "exit_plan": plan, "structure_stop": structure_stop, "source": source,
        "exit_profile": exit_profile,
        "exit_meta": None if plan is None else {k: plan[k] for k in ("geometry_source", "target_origin", "fractions", "markers")},
    }


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
    def __init__(self, stack: Mt5DemoStack, policy: ExitPolicy, *, profiles: bool = False) -> None:
        self._stack = stack
        self._policy = policy  # ``profiles``: the BASE policy every engine profile is derived from (demo.exit_profiles)
        self._profiles = profiles
        self._gate = _ReleaseRecorder()
        self._engines: dict[tuple[str, str], ExitEngine] = {}
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

    def _engine_for(self, market: str, base: ExitPolicy | None = None) -> ExitEngine:
        """ONE ``ExitEngine`` per (market, policy): a profile only selects the ``ExitPolicy`` it is configured with."""
        base = base or self._policy
        engine = self._engines.get((market, base.policy_id))
        if engine is None:
            spec = self._stack._markets[market].spec
            policy = replace(
                base,
                quantity_step=spec.volume_step,
                min_remaining_quantity=max(base.min_remaining_quantity, spec.volume_min),
            )
            engine = ExitEngine(policy=policy, risk_gate=self._gate)
            self._engines[(market, base.policy_id)] = engine
        return engine

    def _row_policy(self, row: reg.IntentRow, ctx: dict[str, Any], state: dict[str, Any]) -> tuple[ExitPolicy | None, dict[str, Any] | None]:
        """(policy, attribution) of one row. ``staged``: the single legacy policy. ``staged_profiles``: the policy of the
        profile FROZEN at entry (registry context); ``None`` = the engine must not touch the row (FIXED_1_5R-mapped,
        unmapped, legacy row without a profile, or a profile that changed after entry = fail-safe skip)."""
        if not self._profiles:
            return self._policy, None
        attr = ctx.get("exit_profile")
        if not isinstance(attr, dict) or attr.get("profile") not in xp.ENGINE_PROFILES:
            self.counters["fixed_rows_skipped"] += 1
            return None, attr if isinstance(attr, dict) else None
        profile = str(attr["profile"])
        seen = state.get("exit_profile")
        if seen is not None and seen != profile:
            self.counters["profile_mutation_refused"] += 1
            self._log("profile_mutation_refused", row, frozen=seen, found=profile)
            return None, attr
        if seen is None:
            state["exit_profile"] = profile
            self._save_state(row, ctx, state)
        minutes = attr.get("time_stop_minutes")
        return xp.profile_policy(profile, self._policy, time_stop_minutes=None if minutes is None else int(minutes)), attr

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
        policy, profile_attr = self._row_policy(row, ctx, state)
        if policy is None:
            return []

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
            # the FILLED quantity (Lane E2: saved after the entry fill); never the requested size, or a
            # partial ENTRY fill would look like an already-taken partial exit
            original = Decimal(str(ctx.get("initial_quantity") if ctx.get("initial_quantity") is not None else ctx["quantity"]))
        except (KeyError, ValueError, ArithmeticError):
            self._log("skip_no_original_quantity", row)
            return []
        realized = max(ZERO, original - volume)
        try:
            plan_stages = parse_exit_plan(ctx.get("exit_plan"))
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            self._log("skip_bad_exit_plan", row, error=str(exc)[:160])
            return []
        ladder = plan_stages or policy.take_profit_stages
        stages_completed = self._stages_done(state, ladder, original, realized, info.spec.volume_step)
        broker_stop = Decimal(str(position.sl or 0))
        initial_stop = Decimal(row.stop)
        current_stop = broker_stop if broker_stop > 0 else initial_stop
        price = bid if side is PositionSide.LONG else ask
        atr = ctx.get("atr")
        vol = (Decimal(str(atr)) / price) if atr not in (None, "") and price > 0 else None
        hwm = state.get("high_water_mark")
        spread = ask - bid
        try:
            fees_price = Decimal(str(ctx.get("fees_price") or 0))
        except (ValueError, ArithmeticError):
            fees_price = ZERO
        expected_cost = spread + 2 * fees_price  # close now (spread + closing fee) + the entry fee already paid
        signals = self._signals(row, side, current_stop, price, atr, spread, now, policy)
        if (
            profile_attr is not None and signals is not None and signals.momentum_score is not None
            and policy.momentum_deterioration_threshold is None
            and signals.momentum_score <= xp.SHADOW_MOMENTUM_THRESHOLD
            and (row.intent_id, "SHADOW_MOMENTUM_EXIT") not in self._noted
        ):
            # SHADOW only: the momentum rule is recorded, never an order (profiles run without it)
            self._noted.add((row.intent_id, "SHADOW_MOMENTUM_EXIT"))
            self.counters["shadow_momentum_signals"] += 1
            self._log("shadow_momentum_exit", row, momentum_score=str(signals.momentum_score), profile=profile_attr.get("profile"))
        time_left: timedelta | None = None
        if row.forced_flat_utc:
            try:
                time_left = max(timedelta(0), parse_utc(row.forced_flat_utc) - now)
            except (ValueError, TypeError):
                time_left = None
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
                expected_exit_cost=expected_cost,
                structure_trail_price=None if signals is None else signals.trail_candidate,
                structure_failure=False if signals is None else signals.structure_failure,
                momentum_score=None if signals is None else signals.momentum_score,
                time_to_forced_flat=time_left,
            )
        except ValueError as exc:
            self._log("skip_invalid_position", row, error=str(exc)[:200])
            return []

        evaluation = self._engine_for(row.market, policy).evaluate(
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

    def _signals(
        self, row: reg.IntentRow, side: PositionSide, current_stop: Decimal, price: Decimal,
        atr: Any, spread: Decimal, now: datetime, pol: ExitPolicy | None = None,
    ) -> st.ManagementSignals | None:
        """Cheap structure / momentum inputs from the closed M5 frame (only when a rule needs them).
        ``staged_profiles`` always evaluates them: the momentum score feeds the SHADOW momentum record."""
        pol = pol or self._policy
        if not (
            self._profiles or pol.structure_trailing or pol.structure_failure_exit
            or pol.momentum_deterioration_threshold is not None or pol.late_loser_momentum_threshold is not None
        ):
            return None
        stack = self._stack
        cfg = stack._cfg.exit_plan
        try:
            frame = stack.bar_source.m5_frame(row.market, cfg.bars)
            return st.management_signals(
                row.direction, frame, entered_at=parse_utc(row.created_utc), current_stop=current_stop,
                price=price, atr=None if atr in (None, "") else Decimal(str(atr)), spread=spread,
                swing_n=cfg.swing_n, atr_buffer_mult=cfg.atr_buffer_mult,
            )
        except Exception as exc:
            if type(exc).__name__ == "StackFailClosed":
                raise
            self.counters["bars_unavailable"] += 1
            self._log("skip_bars", row, error=f"{type(exc).__name__}:{exc}"[:160])
            return None

    def log_entry_plan(self, row: reg.IntentRow, **fields: Any) -> None:
        """Audit record at the entry fill: requested vs FILLED (= initial) quantity and the plan fractions."""
        self._log("entry_plan", row, **fields)

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
            state["current_stop"] = str(new_stop)  # the stop in force (exit slippage is measured against it)
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
            "exit_profile": (ctx.get("exit_profile") or {}).get("profile") if isinstance(ctx.get("exit_profile"), dict) else None,
            "mapping_version": (ctx.get("exit_profile") or {}).get("mapping_version") if isinstance(ctx.get("exit_profile"), dict) else None,
            "stage_index": stages_completed,
            "stage_id": decision.metadata.get("stage_id"),
            "stage_source": decision.metadata.get("stage_source"),
            "reason": decision.reason.value,
            "initial_quantity": str(original),
            "plan_fractions": (ctx.get("exit_plan") or {}).get("fractions") if isinstance(ctx.get("exit_plan"), dict) else None,
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
        code = engine_exit_reason(decision)
        stack._registry.update(row.intent_id, detail=f"exit_engine:{decision.reason.value}"[:200])
        # the hint becomes the PositionClosed exit reason (EXIT_ENGINE_* = uncensored strategy exit)
        ok = stack._flatten(info, tag=f"exit:{decision.request_id}", hint=code)
        self._terminal(row.market, decision.request_id, ExitOutcome.FILLED if ok else ExitOutcome.REJECTED)
        prof = self._ctx(row).get("exit_profile")
        self._log("full_close", row, reason=decision.reason.value, exit_reason=code, flat=ok,
                  exit_profile=prof.get("profile") if isinstance(prof, dict) else None)
        if not ok:
            return []
        fresh = stack._registry.get(row.intent_id) or row
        closed = stack._on_lane(stack._lane_build_closed, fresh, strict=True)
        return [closed] if closed is not None else []


__all__ = [
    "DEFAULT_STAGE_FRACTIONS",
    "EXIT_POLICIES",
    "EXIT_POLICY_FIXED",
    "EXIT_POLICY_PROFILES",
    "EXIT_POLICY_STAGED",
    "GEOMETRY_FAMILY",
    "GEOMETRY_SOURCES",
    "GEOMETRY_STRUCTURE",
    "MANAGED_EXIT_POLICIES",
    "R_EXIT_QUOTE_UNAVAILABLE",
    "R_EXIT_SIZE_BELOW_MIN_LOT",
    "R_EXIT_TRANCHE_LIMITATION",
    "ExitOutcome",
    "ExitPlanConfig",
    "StagedExitManager",
    "broker_target_for_staged",
    "build_exit_plan",
    "default_staged_exit_policy",
    "engine_exit_reason",
    "parse_exit_plan",
    "produce_exit_context",
    "stage_target_price",
]
