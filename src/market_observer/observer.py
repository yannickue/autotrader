# ruff: noqa: E501
"""Orchestrator of the Market Structure Observer (OBSERVATION ONLY / SHADOW ONLY / NOT ALPHA VALIDATED).

For ONE event (decision bar ``i`` = last closed bar, direction, event price, optional family / variant / structure_event_id) it assembles the
``DecisionFeatures`` of every group (levels -> reference level -> acceptance, swings, balance, participation) into an ``ObserverRecord``. It never
produces labels (``labels=None``: outcomes are retrospective and belong to the lab) and it never feeds anything back into a decision.

Two implementations of the SAME computation:

* :func:`observe_event` is the REFERENCE: a pure function of ``(bars, i, event, config)`` that replays the level registry from bar 0.
* :class:`MarketStructureObserver` is the incremental object for the live path: the level registry is advanced once per bar, the other groups are
  evaluated at the event bar (they are pure, prefix-invariant functions of the bar arrays). Its record equals the reference exactly (tested).

``warmup_ok`` (stored as meta ``m_warmup_ok`` and as a column of the persisted row): True only if every group's documented history requirement is met
at the decision bar: levels ``MIN_HISTORY_BARS`` (603), swings 480, acceptance 98, balance 48 closed bars, participation 21 PREVIOUS trading days
(``MIN_HISTORY_PREV_DAYS``), and a finite ATR. Below it the record is still written but flagged; consumers must filter on the flag. The
requirements are DOCUMENTED CONSTANTS of the groups (never re-derived here).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace

import numpy as np

from market_observer import acceptance as A
from market_observer import balance as B
from market_observer import levels as L
from market_observer import participation as P
from market_observer import swings as S
from market_observer.schema import (
    OBSERVER_VERSION,
    SCHEMA_VERSION,
    DecisionFeatures,
    FeatureResult,
    JsonScalar,
    ObserverBars,
    ObserverRecord,
)

GROUPS_OBSERVED: tuple[str, ...] = ("levels", "swings", "acceptance", "balance", "participation")


# ---------------------------------------------------------------------------------------------- configuration
@dataclass(frozen=True)
class ObserverConfig:
    """Every group's config (predeclared constants). ``levels`` carries the per-market round steps the adapter supplies (never guessed)."""

    levels: L.LevelConfig = field(default_factory=L.LevelConfig)
    acceptance: A.AcceptanceConfig = field(default_factory=A.AcceptanceConfig)
    balance: B.BalanceConfig = field(default_factory=B.BalanceConfig)
    participation: P.ParticipationConfig = field(default_factory=P.ParticipationConfig)

    def with_round_steps(self, minor: float | None, major: float | None) -> ObserverConfig:
        """Per-market config: round-number steps in PRICE units from the market's MarketSpec tick scale (``alpha.families.data.round_steps``)."""
        return replace(self, levels=replace(self.levels, round_minor_step=minor, round_major_step=major))

    def min_history(self) -> dict[str, int]:
        return {
            "levels": L.min_history_bars(self.levels), "swings": S.MIN_HISTORY_BARS, "acceptance": A.min_history_bars(self.acceptance),
            "balance": B.min_history_bars(self.balance), "participation_prev_days": P.min_history_prev_days(self.participation),
        }

    def definition_hashes(self) -> dict[str, str]:
        return {
            "levels": L.definition_hash(self.levels), "swings": S.definition_hash(), "acceptance": A.definition_hash(self.acceptance),
            "balance": B.definition_hash(self.balance), "participation": P.definition_hash(self.participation),
        }

    def config_hash(self) -> str:
        payload = {"observer": OBSERVER_VERSION, "schema": SCHEMA_VERSION, "hashes": self.definition_hashes(), "cfg": asdict(self)}
        return hashlib.sha256(repr(sorted(payload.items(), key=lambda kv: kv[0])).encode()).hexdigest()[:16]


OBSERVER_CONFIG = ObserverConfig()
MAX_MIN_HISTORY_BARS = max(OBSERVER_CONFIG.min_history()[k] for k in ("levels", "swings", "acceptance", "balance"))


@dataclass(frozen=True)
class ObservedEvent:
    """What the caller knows about the event. Only ``direction`` and ``price`` enter the features; the rest is identity / audit."""

    direction: int  # +1 / -1
    price: float  # event price (the decision bar's close for a live opportunity)
    family: str | None = None
    variant: str | None = None
    structure_event_id: str | None = None
    opportunity_id: str | None = None
    is_control: bool = False
    control_of: str | None = None

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError("direction must be +1 or -1")
        if not np.isfinite(self.price):
            raise ValueError("event price must be finite")


def event_id_for(market: str, decision_ts_ns: int, family: str | None, variant: str | None, direction: int) -> str:
    """Deterministic id: hash of (market, decision_ts_ns, family, variant, direction, OBSERVER_VERSION)."""
    key = "|".join([market, str(int(decision_ts_ns)), "" if family is None else family, "" if variant is None else variant, str(int(direction)), OBSERVER_VERSION])
    return hashlib.sha256(key.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------------------------- warm-up
def previous_trading_days(bars: ObserverBars, i: int) -> int:
    """Distinct ``local_day`` values before the decision bar's day among bars ``<= i``."""
    ld = bars.local_day[: i + 1]
    return int(len(np.unique(ld)) - 1)


def warmup_status(bars: ObserverBars, i: int, config: ObserverConfig = OBSERVER_CONFIG) -> tuple[bool, dict[str, JsonScalar]]:
    """(warmup_ok, audit values): the documented MIN_HISTORY constants of every group against what the loaded bars provide."""
    mh = config.min_history()
    n = i + 1
    prev_days = previous_trading_days(bars, i)
    atr_ok = bool(np.isfinite(bars.atr[i]) and bars.atr[i] > 0)
    ok = (
        n >= mh["levels"] and n >= mh["swings"] and n >= mh["acceptance"] and n >= mh["balance"]
        and prev_days >= mh["participation_prev_days"] and atr_ok
    )
    detail: dict[str, JsonScalar] = {
        "bars_available": n, "prev_days_available": prev_days, "atr_ok": atr_ok, "min_bars_levels": mh["levels"], "min_bars_swings": mh["swings"],
        "min_bars_acceptance": mh["acceptance"], "min_bars_balance": mh["balance"], "min_prev_days_participation": mh["participation_prev_days"],
    }
    return ok, detail


# ---------------------------------------------------------------------------------------------- assembly (shared by reference + incremental)
def _assemble(
    bars: ObserverBars, i: int, event: ObservedEvent, config: ObserverConfig, ctx: L.LevelContext, extra_meta: Mapping[str, JsonScalar] | None = None,
) -> ObserverRecord:
    if ctx.index != i:
        raise ValueError(f"level context is for bar {ctx.index}, not {i}")
    dts = bars.decision_ts_ns(i)
    level_res = L.level_features(ctx, event.direction, event.price)
    ref = L.reference_level(ctx, event.direction, event.price)
    results: list[FeatureResult] = [
        level_res,
        S.swing_features(bars, i),
        A.acceptance_features(bars, i, ref, event.direction, config.acceptance),
        B.balance_features(bars, i, config.balance),
        P.participation_features(bars, i, config.participation),
    ]
    feats = DecisionFeatures.from_results(dts, results)
    ok, detail = warmup_status(bars, i, config)
    meta: dict[str, JsonScalar] = {
        "warmup_ok": ok, **detail, "event_price": float(event.price), "reference_level_id": None if ref is None else ref.level_id,
        "config_hash": config.config_hash(), "opportunity_id": event.opportunity_id, "structure_event_id": event.structure_event_id,
        **{f"hash_{g}": h for g, h in config.definition_hashes().items()},
    }
    if extra_meta:
        meta.update(extra_meta)
    return ObserverRecord(
        event_id=event_id_for(bars.market, dts, event.family, event.variant, event.direction), market=bars.market, family=event.family,
        variant=event.variant, direction=event.direction, is_control=event.is_control, control_of=event.control_of, features=feats, labels=None,
        observer_version=OBSERVER_VERSION, schema_version=SCHEMA_VERSION, meta=meta,
    )


def observe_event(bars: ObserverBars, i: int, event: ObservedEvent, config: ObserverConfig = OBSERVER_CONFIG) -> ObserverRecord:
    """REFERENCE implementation: replay the level registry from bar 0 to ``i`` (reads bars ``<= i`` only) and assemble the record."""
    if not 0 <= i < len(bars):
        raise IndexError(i)
    ctx = L.build_level_context(bars, i, config.levels)
    return _assemble(bars, i, event, config, ctx)


class StaleEventError(ValueError):
    """The incremental observer already advanced beyond the requested decision bar (events must be observed in non-decreasing bar order)."""


class MarketStructureObserver:
    """Incremental observer of ONE market. ``bars`` is the growing bar object of the market (only indices ``<= i`` are read).

    State = the level registry (plain data: deepcopy / pickle safe). Restart = a fresh object replayed over at least ``MAX_MIN_HISTORY_BARS`` bars
    (the groups' history-invariance contract makes the record identical once warm), or a pickled copy."""

    def __init__(self, config: ObserverConfig = OBSERVER_CONFIG) -> None:
        self.config = config
        self._reg = L.LevelRegistry(config.levels)
        self._n = 0

    @property
    def bars_seen(self) -> int:
        return self._n

    def advance(self, bars: ObserverBars, upto: int, *, deadline: float | None = None, clock=time.perf_counter) -> int:
        """Advance the registry through bar ``upto`` (inclusive). Stops early (state stays consistent) when ``clock() >= deadline``. Returns the number
        of bars seen."""
        while self._n <= upto:
            if deadline is not None and clock() >= deadline:
                break
            self._reg.update(bars, self._n, build_context=False)
            self._n += 1
        return self._n

    def observe(self, bars: ObserverBars, i: int, event: ObservedEvent, *, extra_meta: Mapping[str, JsonScalar] | None = None) -> ObserverRecord:
        if self._n > i + 1:
            raise StaleEventError(f"observer already at bar {self._n - 1}, event is at {i}")
        if self._n < i + 1:
            raise ValueError(f"observer is behind: at bar {self._n - 1}, event is at {i} (call advance first)")
        return _assemble(bars, i, event, self.config, self._reg.context(bars), extra_meta)
