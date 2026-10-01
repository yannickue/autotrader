# ruff: noqa: E501
"""Live shadow adapter of the Market Structure Observer (OBSERVATION ONLY / SHADOW ONLY / NOT ALPHA VALIDATED). Flag-gated, DEFAULT OFF.

The hook follows the Lane-U2 shadow pattern (``demo.shadow_universe.OutOfWindowShadow``): flag-gated, failure-isolated (EVERY exception is caught and
counted, nothing is ever raised into the engine or the runner), bounded by a per-cycle wall budget, additive persistence. It only READS: the snapshot /
decision / frame it receives are never mutated, nothing it computes flows back (the engine and the runner never read observer output), and it imports
nothing from execution / risk / exits (static test).

TIMING CONTRACT (Lane H): the observer must not add latency to the live decisions of ANY market, so the work is split in two phases.

1. ``on_bar`` (called inside the live scan, after the decisions of a bar are final) is O(1): it stashes a REFERENCE to the engine's closed-bar frame
   (the engine builds a fresh frame per call and never mutates it afterwards) and one small plain-data record per opportunity, into a bounded queue.
   No array conversion, no buffer sync, no registry step, no feature computation, no I/O.
2. ``drain_cycle`` runs ONCE per runner cycle in the post-scan ``market_observer`` section, i.e. AFTER every market's live scan and order handling. One
   per-cycle wall budget covers everything: ``frame_arrays``, ``BarBuffer.sync`` (chunked: a 6000-bar cold start or a reset never runs in one block),
   the level-registry catch-up, feature building, and (``charge``) the runner's persistence of the returned records. When the budget is exhausted the
   remaining work stays queued (bounded; overflow is dropped oldest-first and counted as ``skipped_budget``) and continues in the next cycle.

What it does per closed bar of a market
---------------------------------------
* ``BarBuffer.sync`` appends the bars of the engine's frame that are newer than its own (the engine frame is a sliding window; the level registry
  is sequential in absolute indices, so the observer keeps its own append-only arrays).
* Every opportunity of that bar (accepted, engine-rejected, counterfactual / catch-up: the control-relevant population) becomes an ``ObservedEvent``
  (direction + the decision bar's close) and, once the incremental ``MarketStructureObserver`` has reached that bar, an ``ObserverRecord``. Events are
  keyed by the TIMESTAMP of their decision bar (not a buffer index), so they survive a buffer reset and are still observed at their OWN bar.
* The level registry costs ~1 ms per bar, so a cold start (6000 bars) needs seconds and is spread over cycles by the budget.

Failure isolation: a bad snapshot is skipped and counted (the market state is NOT dropped for it); an error while building one record drops that event
only; an error in the buffer / registry (state possibly inconsistent) drops the market state, counts every dropped pending event (``dropped_pending``) and
rebuilds from the next frame.

Warm-up facts: the engine's live frame is up to ``DEFAULT_WINDOW_BARS`` = 6000 closed M5 bars. That covers levels (603), swings (480), acceptance
(98) and balance (48); the participation TOD baseline needs 21 PREVIOUS trading days, which 6000 bars only reach on markets with ~> 22 h of bars per
weekday. A record whose history requirement is not met is written with ``warmup_ok = False`` (never presented as complete; heartbeat
``warmup_false_count``). The hook uses the engine's own frame (no extra bar-source read); a larger observer-only window would need an extra
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
DEFAULT_BUDGET_S = 0.4  # wall budget of ALL observer work of one runner cycle (frame arrays, buffer sync, registry, features, persistence)
DEFAULT_MAX_PENDING = 64  # events waiting per market; beyond this the oldest are dropped (counted)
SYNC_CHUNK_BARS = 500  # bars appended to a BarBuffer per budget check (ATR per bar is the expensive part of a cold start / reset)
PERSIST_RESERVE_S = 0.02  # share of the cycle budget reserved for the runner's one-transaction persistence of the cycle's records
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
class _Raw:
    """One opportunity of a closed bar, plain data (the O(1) stash of ``on_bar``). ``ts_ns`` = OPEN time of its decision bar."""

    ts_ns: int
    direction: int
    family: str | None
    variant: str | None
    structure_event_id: str | None
    opportunity_id: str | None


@dataclass
class _Inbox:
    """Per market: what ``on_bar`` stashed. ``frame`` is a REFERENCE to the engine's frame (never mutated by anyone), ``arrs`` its array conversion
    (made lazily in ``drain_cycle`` under the budget), ``target_last`` the open time of the newest stashed frame's last bar."""

    mspec: Any
    frame: pd.DataFrame | None = None
    target_last: int | None = None
    arrs: dict[str, Any] | None = None
    raw: deque = field(default_factory=deque)  # _Raw, oldest first


@dataclass
class _Market:
    buf: BA.BarBuffer
    obs: MarketStructureObserver
    config: ObserverConfig


@dataclass
class ObserverStats:
    records_built: int = 0
    errors: int = 0
    budget_exhausted: int = 0  # times a drain found / hit the cycle budget
    skipped_budget: int = 0  # events dropped (queue overflow while behind)
    skipped_mismatch: int = 0  # event bar != the frame's last bar (never forced)
    skipped_stale: int = 0
    deferred: int = 0  # events queued (observed at the cycle drain, possibly cycles later)
    dropped_pending: int = 0  # queued events lost because the market state had to be dropped (error)
    warmup_false: int = 0
    resets: int = 0
    last_cycle_ms: float = 0.0
    persist_ms_total: float = 0.0
    enqueue_ms_max: float = 0.0  # slowest in-scan ``on_bar`` call (the O(1) stash)
    last_error: str | None = None


class ObserverShadow:
    """See the module docstring. ``clock`` is injectable (tests); all state is in this object, nothing is shared with the engine."""

    def __init__(
        self, *, budget_s: float = DEFAULT_BUDGET_S, max_pending: int = DEFAULT_MAX_PENDING, config: ObserverConfig = OBSERVER_CONFIG,
        clock: Callable[[], float] = time.perf_counter, max_buffer_bars: int = BA.DEFAULT_MAX_BUFFER_BARS, sync_chunk_bars: int = SYNC_CHUNK_BARS,
        persist_reserve_s: float = PERSIST_RESERVE_S,
    ) -> None:
        self.budget_s = float(budget_s)
        self.max_pending = int(max_pending)
        self._base = config
        self._clock = clock
        self._max_buffer = int(max_buffer_bars)
        self._chunk = int(sync_chunk_bars)
        self._reserve_cfg = float(persist_reserve_s)
        self._inbox: dict[str, _Inbox] = {}
        self._m: dict[str, _Market] = {}
        self._used = 0.0
        self._event_cost = 0.0  # moving average of the wall cost of one observed event
        self.stats_ = ObserverStats()
        self.version = OBSERVER_VERSION

    # ---------------------------------------------------------------------------------------- budget
    def begin_cycle(self) -> None:
        self._used = 0.0
        self.stats_.last_cycle_ms = 0.0

    def _left(self, t0: float) -> float:
        """Seconds of the cycle budget still available for drain work (a share is reserved for the runner's persistence of the records)."""
        reserve = min(self._reserve_cfg, 0.25 * self.budget_s)
        return self.budget_s - reserve - self._used - (self._clock() - t0)

    def _spend(self, t0: float) -> None:
        self._used += self._clock() - t0
        self.stats_.last_cycle_ms = self._used * 1000.0

    def charge(self, seconds: float) -> None:
        """Count wall time the RUNNER spent on this cycle's observer work (persistence of the records) against the cycle budget."""
        self._used += max(0.0, float(seconds))
        self.stats_.persist_ms_total += max(0.0, float(seconds)) * 1000.0
        self.stats_.last_cycle_ms = self._used * 1000.0

    # ---------------------------------------------------------------------------------------- in-scan: O(1) stash
    def on_bar(self, market: str, mspec: Any, frame: pd.DataFrame | None, pairs: Sequence[tuple[Any, Any]] | None) -> None:
        """Called inside the live scan after the decisions of a closed bar are final. ``frame`` = the engine's closed-bar frame of that evaluation
        (READ ONLY; only a reference is kept), ``pairs`` = its ``(snapshot, decision)`` tuples. O(1): queues plain data; never raises, never computes."""
        t0 = self._clock()
        try:
            self._enqueue(market, mspec, frame, pairs or ())
        except Exception as exc:
            self._note_error(market, exc)
        finally:
            self.stats_.enqueue_ms_max = max(self.stats_.enqueue_ms_max, (self._clock() - t0) * 1000.0)

    def _enqueue(self, market: str, mspec: Any, frame: pd.DataFrame | None, pairs: Sequence[tuple[Any, Any]]) -> None:
        if frame is None or len(frame) == 0:
            return
        box = self._inbox.get(market)
        if box is None:
            box = self._inbox[market] = _Inbox(mspec)
        box.mspec = mspec
        frame_last = int(pd.Timestamp(frame["ts"].iloc[-1]).as_unit("ns").value)
        if box.target_last is None or frame_last >= box.target_last:
            box.frame, box.target_last, box.arrs = frame, frame_last, None  # newest frame wins; an older one only contributes its events
        for snap, _dec in pairs:
            try:
                if int(pd.Timestamp(snap.signal_ts_utc).as_unit("ns").value) != frame_last + M5_SECONDS * NS:
                    self.stats_.skipped_mismatch += 1  # the event's bar is not the frame's last bar
                    continue
                sig: Mapping[str, Any] = snap.signal or {}
                box.raw.append(_Raw(frame_last, int(snap.direction), sig.get("family"), sig.get("variant"), sig.get("structure_event_id"), snap.opportunity_id))
            except Exception as exc:  # a bad snapshot is skipped and counted; the market state is untouched
                self._note_error(market, exc)
                continue
            self.stats_.deferred += 1
            while len(box.raw) > self.max_pending:
                box.raw.popleft()
                self.stats_.skipped_budget += 1

    # ---------------------------------------------------------------------------------------- post-scan: budgeted work
    def drain_cycle(self) -> list[ObserverRecord]:
        """THE observer work of a runner cycle (call it AFTER every market's live scan): array conversion, buffer sync, registry catch-up and record
        building for every market with queued work, under the ONE per-cycle budget. Returns the records built (the runner persists them in one
        transaction and reports the time via ``charge``). Never raises."""
        t0 = self._clock()
        out: list[ObserverRecord] = []
        try:
            for market in sorted((m for m, b in self._inbox.items() if self._has_work(m, b)), key=self._progress):
                if self._left(t0) <= 0:
                    self.stats_.budget_exhausted += 1
                    break
                self._drain_market(market, t0, out)
        except Exception as exc:
            self._fail("*", exc)
        finally:
            self._spend(t0)
        return out

    def _has_work(self, market: str, box: _Inbox) -> bool:
        st = self._m.get(market)
        return bool(box.raw) or box.frame is not None or box.arrs is not None or (st is not None and st.obs.bars_seen < len(st.buf))

    def _progress(self, market: str) -> float:
        st = self._m.get(market)
        return 0.0 if st is None else st.obs.bars_seen / max(1, len(st.buf))

    def _state(self, market: str, mspec: Any) -> _Market:
        st = self._m.get(market)
        if st is None:
            cfg = config_for(mspec, self._base)
            buf = BA.BarBuffer(market, tick_size=float(mspec.tick_size), session=session_for(mspec), max_bars=self._max_buffer)
            st = _Market(buf, MarketStructureObserver(cfg), cfg)
            self._m[market] = st
        return st

    def _drain_market(self, market: str, t0: float, out: list[ObserverRecord]) -> None:
        box = self._inbox[market]
        try:
            st = self._state(market, box.mspec)
            if not self._sync(st, box, t0):
                return
            self._observe(market, st, box, t0, out)
        except Exception as exc:
            self._fail(market, exc)

    def _sync(self, st: _Market, box: _Inbox, t0: float) -> bool:
        """Bring the buffer up to the newest stashed frame in budgeted chunks. True = caught up (or nothing to do), False = out of budget."""
        if box.arrs is None:
            if box.frame is None:
                return True
            if self._left(t0) <= 0:
                self.stats_.budget_exhausted += 1
                return False
            box.arrs = BA.frame_arrays(box.frame, float(box.mspec.point_size))
            box.frame = None  # the reference is released; the arrays are copies
        arrs = box.arrs
        last = int(arrs["ts_ns"][-1])
        for _ in range(len(arrs["ts_ns"]) // max(1, self._chunk) + 4):
            if st.buf.last_ts_ns == last:
                break
            if self._left(t0) <= 0:
                self.stats_.budget_exhausted += 1
                return False
            status = st.buf.sync(**arrs, max_append=self._chunk)
            if status == "reset":
                self.stats_.resets += 1
                st.obs = MarketStructureObserver(st.config)  # the registry is sequential in buffer indices: restart it over the rebuilt buffer
            elif status == "noop":
                break
        box.arrs = None
        return True

    def _observe(self, market: str, st: _Market, box: _Inbox, t0: float, out: list[ObserverRecord]) -> None:
        """Advance the registry (budget permitting) and observe every queued event whose bar the registry has reached; with nothing queued the
        registry is simply kept current."""
        bars: ObserverBars = st.buf.bars()
        observed = 0
        while True:
            rem = self._left(t0)
            if rem <= 0 or (observed and rem < self._event_cost):
                self.stats_.budget_exhausted += 1
                return
            if not box.raw:
                upto = len(st.buf) - 1
                if st.obs.bars_seen <= upto:
                    st.obs.advance(bars, upto, deadline=self._clock() + rem, clock=self._clock)
                    if st.obs.bars_seen <= upto:
                        self.stats_.budget_exhausted += 1
                return
            ev = box.raw[0]
            idx = st.buf.index_of(ev.ts_ns)
            if idx is None or st.obs.bars_seen > idx + 1:
                box.raw.popleft()  # not in the buffer / the registry is already beyond this event's bar: it can no longer be observed exactly
                self.stats_.skipped_stale += 1
                continue
            if st.obs.bars_seen <= idx:
                st.obs.advance(bars, idx, deadline=self._clock() + rem, clock=self._clock)
                if st.obs.bars_seen <= idx:
                    self.stats_.budget_exhausted += 1
                    return
            box.raw.popleft()
            t_ev = self._clock()
            try:
                rec = st.obs.observe(
                    bars, idx,
                    ObservedEvent(direction=ev.direction, price=float(bars.c[idx]), family=ev.family, variant=ev.variant,
                                  structure_event_id=ev.structure_event_id, opportunity_id=ev.opportunity_id),
                )
            except StaleEventError:
                self.stats_.skipped_stale += 1
                continue
            except Exception as exc:  # this event only; the (read-only) registry state is intact
                self._note_error(market, exc)
                continue
            finally:
                self._event_cost = 0.7 * self._event_cost + 0.3 * (self._clock() - t_ev)
            observed += 1
            self.stats_.records_built += 1
            if not rec.meta.get("warmup_ok"):
                self.stats_.warmup_false += 1
            out.append(rec)

    def _note_error(self, market: str, exc: BaseException) -> None:
        self.stats_.errors += 1
        self.stats_.last_error = f"{market}: {type(exc).__name__}: {exc}"[:240]

    def _fail(self, market: str, exc: BaseException) -> None:
        """Buffer / registry level error: the state may be inconsistent -> drop it (rebuilt from the next frame); queued events are counted, not lost silently."""
        self._note_error(market, exc)
        self._m.pop(market, None)
        box = self._inbox.get(market)
        if box is not None:
            self.stats_.dropped_pending += len(box.raw)
            box.raw.clear()
            box.frame, box.arrs, box.target_last = None, None, None

    def note_persist_error(self, exc: BaseException) -> None:
        self.stats_.errors += 1
        self.stats_.last_error = f"persist: {type(exc).__name__}: {exc}"[:240]

    # ---------------------------------------------------------------------------------------- reporting
    def stats(self) -> dict[str, Any]:
        s = self.stats_
        return {
            "enabled": True, "version": self.version, "records_built": s.records_built, "errors": s.errors, "last_cycle_ms": round(s.last_cycle_ms, 3),
            "warmup_false_count": s.warmup_false, "budget_s": self.budget_s, "budget_exhausted": s.budget_exhausted, "skipped_budget": s.skipped_budget,
            "skipped_mismatch": s.skipped_mismatch, "skipped_stale": s.skipped_stale, "deferred": s.deferred, "dropped_pending": s.dropped_pending,
            "resets": s.resets, "persist_ms_total": round(s.persist_ms_total, 3), "enqueue_ms_max": round(s.enqueue_ms_max, 3),
            "pending": {m: len(b.raw) for m, b in self._inbox.items() if b.raw}, "last_error": s.last_error,
            "bars_seen": {m: [st.obs.bars_seen, len(st.buf)] for m, st in self._m.items()},
        }
