# ruff: noqa: E501
"""Live shadow adapter of the Market Structure Observer (OBSERVATION ONLY / SHADOW ONLY / NOT ALPHA VALIDATED). Flag-gated, DEFAULT OFF.

The hook follows the Lane-U2 shadow pattern (``demo.shadow_universe.OutOfWindowShadow``): flag-gated, failure-isolated (EVERY exception is caught and
counted, nothing is ever raised into the engine or the runner), bounded by a per-cycle wall budget, additive persistence. It is called ONLY AFTER the
decisions of a bar are final and persisted, and it only READS: the snapshot / decision / frame it receives are never mutated, nothing it computes
flows back (the engine and the runner never read observer output), and it imports nothing from execution / risk / exits (static test).

What it does per closed bar of a market
---------------------------------------
1. ``BarBuffer.sync`` appends the bars of the engine's frame that are newer than its own (the engine frame is a sliding window; the level registry is
   sequential in absolute indices, so the observer keeps its own append-only arrays).
2. Every opportunity of that bar (accepted, engine-rejected, counterfactual / catch-up: the control-relevant population) becomes an ``ObservedEvent``
   (direction + the decision bar's close) and, once the incremental ``MarketStructureObserver`` has reached that bar, an ``ObserverRecord``.
3. The level registry costs ~1 ms per bar, so a cold start (6000 bars) needs seconds. The work is spread over runner cycles by ``warm_step``
   under the cycle budget; events of bars the registry has not reached yet wait in a bounded queue and are observed (at their OWN bar, so the record is
   exactly what the reference computes) as soon as it gets there. Over-budget / over-queue events are skipped and counted, never forced.

Warm-up facts: the engine's live frame is up to ``DEFAULT_WINDOW_BARS`` = 6000 closed M5 bars. That covers levels (603), swings (480), acceptance
(98) and balance (48); the participation TOD baseline needs 21 PREVIOUS trading days, which 6000 bars only reach on markets with ~> 22 h of bars per
weekday. A record whose history requirement is not met is written with ``warmup_ok = False`` (never presented as complete). The hook uses the engine's own frame (no extra bar-source read); a larger observer-only window would need an extra
read-only ``m5_frame`` request per market and is NOT implemented (see docs/OBSERVER.md).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from alpha.families.data import round_steps
from demo.opportunity.bar_source import M5_SECONDS
from market_observer import bars_adapter as BA
from market_observer.observer import (
    OBSERVER_CONFIG,
    MarketStructureObserver,
    ObservedEvent,
    ObserverConfig,
    StaleEventError,
)
from market_observer.schema import OBSERVER_VERSION, ObserverBars, ObserverRecord

NS = 10**9
DEFAULT_BUDGET_S = 0.4  # wall budget of ALL observer work of one runner cycle (events + registry catch-up)
DEFAULT_MAX_PENDING = 64  # events waiting for the registry per market; beyond this the oldest are dropped (counted)
NO_DEFENSIBLE_OPEN_ASSET_CLASSES = frozenset({"crypto_cfd"})  # 24 h markets: the provisional calendar open is a bootstrap schedule, not a session open


def session_for(mspec: Any) -> BA.SessionSpec:
    """``SessionSpec`` of a MarketSpec: tz always, cash minutes only where an open is defensible (not for crypto CFDs)."""
    cal = mspec.calendar
    return BA.session_spec(
        cal.tz, int(cal.cash_open_min), int(cal.cash_close_min), defensible_open=getattr(mspec, "asset_class", "") not in NO_DEFENSIBLE_OPEN_ASSET_CLASSES
    )


def config_for(mspec: Any, base: ObserverConfig = OBSERVER_CONFIG) -> ObserverConfig:
    """Per-market observer config: round steps from the MarketSpec tick scale (``alpha.families.data.round_steps``), never guessed."""
    try:
        minor, major = round_steps(mspec.asset_class, mspec.tick_size)
    except ValueError:
        return base.with_round_steps(None, None)
    return base.with_round_steps(minor, major)


@dataclass
class _Market:
    buf: BA.BarBuffer
    obs: MarketStructureObserver
    config: ObserverConfig
    pending: deque = field(default_factory=deque)  # (bar_index, ObservedEvent)


@dataclass
class ObserverStats:
    records_built: int = 0
    errors: int = 0
    budget_exhausted: int = 0  # times a hook call found / hit the cycle budget
    skipped_budget: int = 0  # events dropped (queue overflow while behind)
    skipped_mismatch: int = 0  # event bar != the buffer's last bar (never forced)
    skipped_stale: int = 0
    deferred: int = 0  # events queued behind the registry (observed later)
    warmup_false: int = 0
    resets: int = 0
    last_cycle_ms: float = 0.0
    last_error: str | None = None


class ObserverShadow:
    """See the module docstring. ``clock`` is injectable (tests); all state is in this object, nothing is shared with the engine."""

    def __init__(
        self, *, budget_s: float = DEFAULT_BUDGET_S, max_pending: int = DEFAULT_MAX_PENDING, config: ObserverConfig = OBSERVER_CONFIG,
        clock: Callable[[], float] = time.perf_counter, max_buffer_bars: int = BA.DEFAULT_MAX_BUFFER_BARS,
    ) -> None:
        self.budget_s = float(budget_s)
        self.max_pending = int(max_pending)
        self._base = config
        self._clock = clock
        self._max_buffer = int(max_buffer_bars)
        self._m: dict[str, _Market] = {}
        self._used = 0.0
        self.stats_ = ObserverStats()
        self.version = OBSERVER_VERSION

    # ---------------------------------------------------------------------------------------- budget
    def begin_cycle(self) -> None:
        self._used = 0.0

    def _remaining(self) -> float:
        return self.budget_s - self._used

    def _spend(self, t0: float) -> None:
        dt = self._clock() - t0
        self._used += dt
        self.stats_.last_cycle_ms = self._used * 1000.0

    # ---------------------------------------------------------------------------------------- per bar
    def on_bar(self, market: str, mspec: Any, frame: pd.DataFrame | None, pairs: Sequence[tuple[Any, Any]] | None) -> list[ObserverRecord]:
        """Called after the decisions of a closed bar are final. ``frame`` = the engine's closed-bar frame of that evaluation (read only),
        ``pairs`` = its ``(snapshot, decision)`` tuples (possibly empty). Returns the records to persist; never raises."""
        t0 = self._clock()
        try:
            return self._on_bar(market, mspec, frame, pairs or (), t0)
        except Exception as exc:
            self._fail(market, exc)
            return []
        finally:
            self._spend(t0)

    def warm_step(self) -> list[ObserverRecord]:
        """Spend the remaining cycle budget on registry catch-up (cold start / after a reset); returns records of events that became observable."""
        t0 = self._clock()
        out: list[ObserverRecord] = []
        try:
            for market in sorted(self._m, key=lambda k: self._m[k].obs.bars_seen / max(1, len(self._m[k].buf))):
                if self._remaining() <= 0:
                    self.stats_.budget_exhausted += 1
                    break
                out.extend(self._drain(market, t0))
        except Exception as exc:
            self._fail("*", exc)
        finally:
            self._spend(t0)
        return out

    def _state(self, market: str, mspec: Any) -> _Market:
        st = self._m.get(market)
        if st is None:
            cfg = config_for(mspec, self._base)
            buf = BA.BarBuffer(market, tick_size=float(mspec.tick_size), session=session_for(mspec), max_bars=self._max_buffer)
            st = _Market(buf, MarketStructureObserver(cfg), cfg)
            self._m[market] = st
        return st

    def _on_bar(self, market: str, mspec: Any, frame: pd.DataFrame | None, pairs: Sequence[tuple[Any, Any]], t0: float) -> list[ObserverRecord]:
        if frame is None or len(frame) == 0:
            return []
        st = self._state(market, mspec)
        arrs = BA.frame_arrays(frame, float(mspec.point_size))
        if st.buf.sync(**arrs) == "reset":
            self.stats_.resets += 1
            st.obs = MarketStructureObserver(st.config)  # the registry is sequential in buffer indices: restart it over the rebuilt buffer
            st.pending.clear()
        frame_last = int(arrs["ts_ns"][-1])
        last_i = len(st.buf) - 1
        c_last = float(arrs["c"][-1])
        for snap, _dec in pairs:
            try:
                if st.buf.last_ts_ns != frame_last:
                    self.stats_.skipped_stale += 1  # the frame is older than what the observer already saw: never forced
                    continue
                if int(pd.Timestamp(snap.signal_ts_utc).as_unit("ns").value) != frame_last + M5_SECONDS * NS:
                    self.stats_.skipped_mismatch += 1  # the event's bar is not the frame's last bar
                    continue
                sig: Mapping[str, Any] = snap.signal or {}
                ev = ObservedEvent(
                    direction=int(snap.direction), price=c_last, family=sig.get("family"), variant=sig.get("variant"),
                    structure_event_id=sig.get("structure_event_id"), opportunity_id=snap.opportunity_id,
                )
            except Exception as exc:
                self._fail(market, exc)
                return []
            st.pending.append((last_i, ev))
            self.stats_.deferred += 1
            while len(st.pending) > self.max_pending:
                st.pending.popleft()
                self.stats_.skipped_budget += 1
        return self._drain(market, t0)

    def _drain(self, market: str, t0: float) -> list[ObserverRecord]:
        """Advance the registry (budget permitting) and observe every queued event whose bar the registry has reached; with nothing queued the
        registry is simply kept current."""
        st = self._m[market]
        out: list[ObserverRecord] = []
        bars: ObserverBars = st.buf.bars()
        while True:
            rem = self._remaining() - (self._clock() - t0)
            if rem <= 0:
                self.stats_.budget_exhausted += 1
                return out
            upto = st.pending[0][0] if st.pending else len(st.buf) - 1
            if st.pending and st.obs.bars_seen > upto + 1:
                st.pending.popleft()  # the registry is already beyond this event's bar: it can no longer be observed exactly
                self.stats_.skipped_stale += 1
                continue
            if st.obs.bars_seen <= upto:
                st.obs.advance(bars, upto, deadline=self._clock() + rem, clock=self._clock)
                if st.obs.bars_seen <= upto:
                    self.stats_.budget_exhausted += 1
                    return out
            if not st.pending:
                return out
            i, ev = st.pending.popleft()
            try:
                rec = st.obs.observe(bars, i, ev)
            except StaleEventError:
                self.stats_.skipped_stale += 1
                continue
            self.stats_.records_built += 1
            if not rec.meta.get("warmup_ok"):
                self.stats_.warmup_false += 1
            out.append(rec)

    def _fail(self, market: str, exc: BaseException) -> None:
        self.stats_.errors += 1
        self.stats_.last_error = f"{market}: {type(exc).__name__}: {exc}"[:240]
        self._m.pop(market, None)  # rebuild from the next frame: never continue from a possibly inconsistent state

    def note_persist_error(self, exc: BaseException) -> None:
        self.stats_.errors += 1
        self.stats_.last_error = f"persist: {type(exc).__name__}: {exc}"[:240]

    # ---------------------------------------------------------------------------------------- reporting
    def stats(self) -> dict[str, Any]:
        s = self.stats_
        return {
            "enabled": True, "version": self.version, "records_built": s.records_built, "errors": s.errors, "last_cycle_ms": round(s.last_cycle_ms, 3),
            "warmup_false_count": s.warmup_false, "budget_s": self.budget_s, "budget_exhausted": s.budget_exhausted, "skipped_budget": s.skipped_budget,
            "skipped_mismatch": s.skipped_mismatch, "skipped_stale": s.skipped_stale, "deferred": s.deferred, "resets": s.resets,
            "pending": {m: len(st.pending) for m, st in self._m.items() if st.pending}, "last_error": s.last_error,
            "bars_seen": {m: [st.obs.bars_seen, len(st.buf)] for m, st in self._m.items()},
        }
