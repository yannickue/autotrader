# ruff: noqa: E501
"""DEMO runner loop: the deterministic integrator of the ActivTrades DEMO trader.

    closed M5 bar -> OpportunityEngine -> snapshot (persisted) -> shadow predictions (persisted) ->
    decision (persisted) -> [demo-auto, accepted] intent PLANNED (persisted) -> StackPort.submit ->
    events -> lifecycle transitions + Risk/Execution/Outcome records -> milestone reports.

Hard rules implemented here (no LLM / Optuna / DEAP anywhere in this module):
  * Persist BEFORE acting.  The PLANNED intent is durable before ``stack.submit``; a persistence
    failure halts new exposure and the runner exits 7 (fail closed).
  * Shadow mode never submits: no intent is even created and the stack is told to be in shadow mode.
  * No one-position rule here: every accepted opportunity is submitted. Same-symbol add-on / opposite-side
    handling (broker netting = one net position per symbol) is the STACK's job and shows up as a classified
    reject (temporary ``ADDON_*`` codes) in the rejection funnel.
  * Exactly-once: the opportunity id is deduped through the store's seen-set; the store allows one
    intent per opportunity; only a PLANNED intent is ever submitted; on restart unfinished intents
    are reconciled with ``stack.open_intents()`` and are NEVER re-sent (see ``_reconcile_restart``).
  * Fail closed (stop new exposure, keep managing exits, write the error to the heartbeat, exit 7
    after an orderly stop) on: non-demo/unknown account, stale feeds (all markets), reconciliation
    != RECONCILED, persistence failure, unprotected exposure, disconnect, clock anomaly,
    ``StackFailClosed``, low disk.
  * Learning libraries are imported lazily and only if enabled; an import failure never stops trading.

Restart rules for intents found unfinished in the store (documented contract):
  * PLANNED / RISK_APPROVED and unknown to the broker -> CANCELLED (never re-driven blindly).
  * SENT and unknown to the broker -> CANCELLED + warning ``needs_manual_review`` (the order may have
    been filled and closed while we were down; the outcome cannot be reconstructed here).
  * FILLED / PROTECTED and unknown to the broker -> CLOSED (``closed_while_down``) without outcome;
    ``store.closed_without_outcome()`` lists them and a late ``PositionClosed`` event still fills the
    outcome in.
  * Known to the broker -> adopted (state advanced as far as the broker truth proves), management
    continues via ``poll_events``/``on_clock``.
  * Broker knows an intent that is not in the store -> fail closed (orphan exposure).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import os
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from demo import monitor
from demo.contracts import (
    Decision,
    ExecutionRecord,
    OpportunitySnapshot,
    RiskRecord,
    TradeIntent,
)
from demo.execution import gates as G
from demo.execution.events import (
    Accepted,
    ExecutionEvent,
    Fill,
    PositionClosed,
    ProtectionConfirmed,
    Rejected,
)
from demo.execution.stack_port import AccountSnapshot, StackFailClosed, StackPort
from demo.labeling import Bar, PathPoint, label_counterfactuals, outcome_from_fills
from demo.labeling import Fill as LabelFill
from demo.store import (
    CANCELLED,
    CLOSED,
    FILLED,
    PLANNED,
    PROTECTED,
    RISK_APPROVED,
    RISK_REJECTED,
    SEND_FAILED,
    SENT,
    DemoStore,
    DemoStoreError,
    parse_utc,
)

Decimal_like = Any
EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_STATUS_UNAVAILABLE = 3
EXIT_FAIL_CLOSED = 7
EXIT_UNAVAILABLE = 8

CHAMPION = "static-demo-policy-v1"
MODES = ("shadow", "demo-auto")
_ORDER = (PLANNED, RISK_APPROVED, SENT, FILLED, PROTECTED, CLOSED)
_M5 = timedelta(minutes=5)


class LiveStackUnavailable(RuntimeError):
    """``demo.execution.live.Mt5DemoStack`` is not importable (yet)."""


# ------------------------------------------------------------------------------------ helpers
def _f(x: Any, default: float = 0.0) -> float:
    return default if x is None else float(x)


def _num(x: Any) -> float | None:
    """Finite float or None (Decimal / str / number tolerant)."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def floor_m5(dt: datetime) -> datetime:
    dt = dt.astimezone(UTC)
    return dt.replace(minute=dt.minute - dt.minute % 5, second=0, microsecond=0)


class StoreSeenAdapter:
    """Engine ``SeenStore`` protocol (``add_if_new``) on top of the persistent ``DemoStore`` seen-set."""

    def __init__(self, store: DemoStore) -> None:
        self._store = store

    def add_if_new(self, opportunity_id: str) -> bool:
        if self._store.seen(opportunity_id):
            return False
        return self._store.mark_seen(opportunity_id)


def load_learning(model_dir: str | Path | None, enabled: bool | None) -> tuple[Any, Any, str | None]:
    """(predictor, trainer, error). ``enabled`` False -> nothing imported; None -> best effort.
    Any failure (missing lightgbm/river/mlflow/sklearn...) returns ``(None, None, reason)``."""
    if enabled is False:
        return None, None, "disabled"
    try:
        from demo.learning.shadow import build_shadow

        pred, trainer = build_shadow(model_dir=model_dir)
        return pred, trainer, None
    except Exception as exc:  # never stops trading
        return None, None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- clock verification
@dataclass(frozen=True, slots=True)
class ClockChainRow:
    market: str
    ok: bool
    calendar_status: str
    tz: str
    utc_offset_min: int | None
    local_minute: int | None
    session_bucket: str | None
    window: str
    detail: str


def verify_clock_chain(
    markets: Sequence[str],
    *,
    now: datetime,
    spec_loader: Callable[[str], Any] | None = None,
    production: Any | None = None,
) -> list[ClockChainRow]:
    """UTC -> market tz -> DST -> local trading minute -> session bucket -> SimWindow, per market.

    A market whose chain fails is DISABLED (``ok=False``); nothing is guessed.  ``calendar_status`` is
    copied from the spec: a ``provisional`` calendar stays ``provisional``."""
    from demo.opportunity.clock import forced_flat_utc, local_minute_of, local_of, session_bucket_of

    if spec_loader is None:
        from markets.spec import load_market_spec as spec_loader
    rows: list[ClockChainRow] = []
    for m in markets:
        tz_name, status = "?", "unknown"
        try:
            spec = spec_loader(m)
            cal = spec.calendar
            tz_name, status = cal.tz, cal.status
            tz = ZoneInfo(tz_name)
            year = now.year
            probes = [now.astimezone(UTC), datetime(year, 1, 15, 12, tzinfo=UTC), datetime(year, 7, 15, 12, tzinfo=UTC)]
            offsets = set()
            for u in probes:
                off_td = u.astimezone(tz).utcoffset()
                if off_td is None:
                    raise ValueError("tz has no utcoffset")
                off = int(off_td.total_seconds() // 60)
                offsets.add(off)
                expected = ((u.hour * 60 + u.minute) + off) % 1440
                got = local_minute_of(spec, u)
                if got != expected:
                    raise ValueError(f"local minute mismatch {got} != {expected} at {u.isoformat()}")
                if local_of(spec, u).utcoffset() != off_td:
                    raise ValueError("offset mismatch")
                session_bucket_of(spec, got)
            for minute in range(1440):
                session_bucket_of(spec, minute)  # buckets must tile the day
            if not (0 <= cal.entry_start_min < cal.entry_end_min <= cal.forced_flat_min <= 1440):
                raise ValueError(
                    f"window order broken: entry {cal.entry_start_min}-{cal.entry_end_min} flat {cal.forced_flat_min}"
                )
            day = datetime(year, 7, 15).date()
            entry_local = datetime(day.year, day.month, day.day, cal.entry_start_min // 60, cal.entry_start_min % 60, tzinfo=tz)
            entry_utc = entry_local.astimezone(UTC)
            ff = forced_flat_utc(spec, entry_utc, cal.forced_flat_min)
            if (ff - entry_utc) != timedelta(minutes=cal.forced_flat_min - cal.entry_start_min):
                raise ValueError("forced-flat instant inconsistent with local clock")
            win_txt = f"entry {cal.entry_start_min}-{cal.entry_end_min} flat {cal.forced_flat_min}"
            if production is not None:
                from alpha.families.spec import MarketCalendar

                mcal = MarketCalendar.from_market_spec(spec)
                for fs in production.specs_for(m):
                    w = fs.spec.effective_window(mcal)
                    if not (0 <= w.entry_start_min < w.entry_end_min <= w.exit_min <= 1440):
                        raise ValueError(f"SimWindow invalid for {fs.strategy_id}")
                win_txt += f" +{len(production.specs_for(m))} SimWindow ok"
            now_local = local_of(spec, now)
            rows.append(ClockChainRow(
                m, True, status, tz_name, int(now_local.utcoffset().total_seconds() // 60),  # type: ignore[union-attr]
                local_minute_of(spec, now), session_bucket_of(spec, local_minute_of(spec, now)), win_txt,
                "dst_observed" if len(offsets) > 1 else "no_dst_change",
            ))
        except Exception as exc:
            rows.append(ClockChainRow(m, False, status, tz_name, None, None, None, "-", f"{type(exc).__name__}: {exc}"))
    return rows


def render_clock_table(rows: Sequence[ClockChainRow]) -> str:
    head = f"{'market':<8} {'ok':<5} {'calendar':<26} {'tz':<20} {'off':>5} {'lmin':>5} {'bucket':<10} window / detail"
    lines = [head]
    for r in rows:
        lines.append(
            f"{r.market:<8} {r.ok!s:<5} {r.calendar_status:<26} {r.tz:<20} "
            f"{'-' if r.utc_offset_min is None else r.utc_offset_min:>5} "
            f"{'-' if r.local_minute is None else r.local_minute:>5} {r.session_bucket or '-':<10} {r.window} | {r.detail}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------------------------- config
@dataclass(slots=True)
class RunnerConfig:
    mode: str = "shadow"
    phase: str = "DISCOVERY"
    markets: tuple[str, ...] = ()
    artifacts_dir: Path = Path("artifacts/demo_trader")
    poll_interval_s: float = 5.0
    stale_feed_s: float = 660.0
    all_stale_grace_s: float = 900.0
    label_every_s: float = 900.0
    trainer_every_s: float = 3600.0
    trainer_every_trades: int = 10
    min_disk_free_bytes: int = monitor.MIN_DISK_FREE_BYTES
    unprotected_grace_s: float = 30.0
    max_clock_skew_s: float = 300.0
    clock_backward_tolerance_s: float = 5.0
    manage_after_halt_s: float = 4 * 3600.0
    stop_file: Path | None = None
    learning: bool | None = None
    model_dir: Path | None = None
    value_per_unit_eur: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.phase not in ("DISCOVERY", "FROZEN"):
            raise ValueError("phase must be DISCOVERY or FROZEN")
        self.artifacts_dir = Path(self.artifacts_dir)
        if self.stop_file is None:
            self.stop_file = self.artifacts_dir / "STOP"

    @property
    def heartbeat_path(self) -> Path:
        return self.artifacts_dir / "heartbeat.json"

    @property
    def reports_dir(self) -> Path:
        return self.artifacts_dir / "reports"

    @property
    def export_dir(self) -> Path:
        return self.artifacts_dir / "export"


# --------------------------------------------------------------------------------------- runner
class DemoRunner:
    def __init__(
        self,
        stack: StackPort,
        engine: Any,
        store: DemoStore,
        *,
        config: RunnerConfig,
        predictor: Any | None = None,
        trainer: Any | None = None,
        learning_error: str | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        disk_free: Callable[[], int] | None = None,
        spec_loader: Callable[[str], Any] | None = None,
        production: Any | None = None,
    ) -> None:
        self.stack, self.engine, self.store, self.cfg = stack, engine, store, config
        self.predictor, self.trainer = predictor, trainer
        self.learning_error = learning_error
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._disk_free = disk_free or (lambda: monitor.disk_free_bytes(self.cfg.artifacts_dir))
        self._spec_loader = spec_loader
        self._production = production
        self._specs: dict[str, Any] = {}
        self.pid = os.getpid()
        self.commit = monitor.git_commit()
        self.started_utc: datetime = self._clock()
        self.clock_rows: list[ClockChainRow] = []
        self.disabled: dict[str, str] = {}
        self.halt_reason: str | None = None  # new exposure stopped
        self.fail_reason: str | None = None  # fail-closed (=> exit 7)
        self.stop_reason: str | None = None
        self._stopping = False
        self._last_close: dict[str, datetime | None] = {}
        self._feed: dict[str, dict[str, Any]] = {}
        self._stale: set[str] = set()
        self._all_stale_since: datetime | None = None
        self._unprotected_since: datetime | None = None
        self._halted_since: datetime | None = None
        self._last_now: datetime | None = None
        self._last_account: AccountSnapshot | None = None
        self._last_persist: str | None = None
        self._last_signal: dict[str, Any] | None = None
        self._last_fill: dict[str, Any] | None = None
        self._last_error: dict[str, Any] | None = None
        self._warnings: list[str] = []
        self._pred_status: dict[str, str] = {}
        self._day: str = ""
        self._day_counts: dict[str, int] = {"raw": 0, "accepted": 0, "rejected": 0, "trades": 0}
        self._cum_r = 0.0
        self._last_label = self._last_train = self.started_utc
        self._trained_at = 0
        self._last_train_report: dict[str, Any] | None = None
        self._last_label_n = 0
        self._funnel: dict[str, Any] | None = None
        self._funnel_at: datetime | None = None
        self._funnel_dirty = True
        self.submit_count = 0
        self.milestones: list[int] = []
        self.exit_code: int | None = None

    # ---------------------------------------------------------------------------- bookkeeping
    def _note_error(self, now: datetime, text: str) -> None:
        self._last_error = {"ts": _iso(now), "text": text}

    def _spec(self, market: str) -> Any | None:
        if market not in self._specs:
            try:
                loader = self._spec_loader
                if loader is None:
                    from markets.spec import load_market_spec as loader
                self._specs[market] = loader(market)
            except Exception:
                self._specs[market] = None
        return self._specs[market]

    def _roll_day(self, now: datetime) -> None:
        day = now.astimezone(UTC).date().isoformat()
        if day == self._day:
            return
        self._day = day
        try:
            decs = [d for d in self.store.list_decisions() if d.decided_utc.startswith(day)]
            trades = [i for i in self.store.list_intents() if i["created_utc"].startswith(day)]
            self._day_counts = {
                "raw": len(decs), "accepted": sum(d.accepted for d in decs),
                "rejected": sum(not d.accepted for d in decs), "trades": len(trades),
            }
        except Exception:
            self._day_counts = {"raw": 0, "accepted": 0, "rejected": 0, "trades": 0}

    def _refresh_cum_r(self) -> None:
        with contextlib.suppress(Exception):
            self._cum_r = float(sum(o.net_r for _, _, o in self.store.list_outcomes(self.cfg.phase)))

    # ---------------------------------------------------------------------- fail-closed plumbing
    def halt(self, reason: str, now: datetime | None = None) -> None:
        """Stop NEW exposure (idempotent). Exits keep being managed."""
        if self.halt_reason is not None:
            return
        self.halt_reason = reason
        self._halted_since = now or self._clock()
        with contextlib.suppress(Exception):
            self.stack.halt_new_exposure(reason)

    def _fail_closed(self, reason: str, now: datetime | None = None) -> None:
        if self.fail_reason is None:
            self.fail_reason = reason
        self._note_error(now or self._clock(), reason)
        self.halt(reason, now)

    def _persistence_failure(self, exc: BaseException, now: datetime) -> None:
        self._fail_closed(f"persistence_failure: {type(exc).__name__}: {exc}", now)

    def request_stop(self, reason: str = "requested") -> None:
        self.stop_reason = self.stop_reason or reason
        self._stopping = True

    def install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def _handler(signum: int, _frame: Any) -> None:
            self.request_stop(f"signal_{signum}")

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handler)

    def can_trade(self) -> bool:
        return self.halt_reason is None and not self._stopping and self.fail_reason is None

    # -------------------------------------------------------------------------------- start-up
    def start(self) -> None:
        now = self._clock()
        self._last_now = now
        self.cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._roll_day(now)
        self._refresh_cum_r()
        try:
            snap = self.stack.start()
            self._last_account = snap
            self._check_account(snap, now)
        except StackFailClosed as exc:
            self._fail_closed(f"stack_start: {exc}", now)
            return
        self.clock_rows = verify_clock_chain(
            self.cfg.markets, now=now, spec_loader=self._spec_loader, production=self._production
        )
        for r in self.clock_rows:
            if not r.ok:
                self.disabled[r.market] = f"clock_chain_failed: {r.detail}"
        with contextlib.suppress(OSError):
            (self.cfg.artifacts_dir / "clock_chain.json").write_text(
                json.dumps([dataclasses.asdict(r) for r in self.clock_rows], indent=1), encoding="utf-8"
            )
        print(render_clock_table(self.clock_rows), file=sys.stderr)
        if self.cfg.markets and len(self.disabled) == len(self.cfg.markets):
            self._fail_closed("all markets disabled by clock chain", now)
            return
        try:
            self._reconcile_restart(now)
        except (sqlite3.Error, OSError) as exc:
            self._persistence_failure(exc, now)
        except StackFailClosed as exc:
            self._fail_closed(f"reconcile: {exc}", now)
        self._heartbeat(now)

    def _advance(self, intent_id: str, target: str, now: datetime, detail: dict[str, Any] | None = None) -> None:
        cur = self.store.get_state(intent_id)
        if cur not in _ORDER or target not in _ORDER:
            return
        for st in _ORDER[_ORDER.index(cur) + 1 : _ORDER.index(target) + 1]:
            self.store.transition(intent_id, st, detail=detail, ts=_iso(now))

    def _reconcile_restart(self, now: datetime) -> None:
        broker = set(self.stack.open_intents())
        unfinished = self.store.unfinished_intents()
        known = {i["intent_id"] for i in unfinished}
        for orphan in sorted(broker - known):
            row = self.store.get_intent(orphan)
            if row is None:
                self._fail_closed(f"orphan_broker_intent:{orphan}", now)
        for it in unfinished:
            iid, state = it["intent_id"], it["state"]
            if iid in broker:
                # adopt: broker truth proves at least SENT; further states arrive as events
                self._advance(iid, SENT, now, {"restart": "adopted"})
                continue
            if state in (PLANNED, RISK_APPROVED):
                self.store.transition(iid, CANCELLED, detail={"restart": "unsent_unknown_to_broker"}, ts=_iso(now))
            elif state == SENT:
                self.store.transition(iid, CANCELLED, detail={"restart": "sent_unknown_to_broker", "needs_manual_review": True}, ts=_iso(now))
                self._warnings.append(f"needs_manual_review:{iid}:sent_unknown_to_broker")
            elif state in (FILLED, PROTECTED):
                self._advance(iid, CLOSED, now, {"restart": "closed_while_down"})
                self._warnings.append(f"needs_outcome:{iid}:closed_while_down")

    # ----------------------------------------------------------------------------------- guards
    def _check_account(self, snap: AccountSnapshot, now: datetime) -> None:
        if not snap.is_demo:
            self._fail_closed("account_not_demo", now)
        if snap.reconciliation != "RECONCILED":
            self._fail_closed(f"reconciliation={snap.reconciliation}", now)
        if not snap.connected:
            self._fail_closed("broker_disconnected", now)
        if snap.kill_switch:
            self._fail_closed("kill_switch_active", now)
        if snap.open_positions > 0 and not snap.all_positions_protected:
            self._unprotected_since = self._unprotected_since or now
            if (now - self._unprotected_since).total_seconds() >= self.cfg.unprotected_grace_s:
                self._fail_closed("unprotected_exposure", now)
        else:
            self._unprotected_since = None
        if snap.server_time_utc is not None:
            skew = abs((now - snap.server_time_utc.astimezone(UTC)).total_seconds())
            if skew > self.cfg.max_clock_skew_s:
                self._fail_closed(f"clock_anomaly: server skew {skew:.0f}s", now)

    def _guards(self, now: datetime) -> None:
        if now.tzinfo is None:
            self._fail_closed("clock_anomaly: naive clock", now)
            return
        if self._last_now is not None and (self._last_now - now).total_seconds() > self.cfg.clock_backward_tolerance_s:
            self._fail_closed(f"clock_anomaly: went backwards {(self._last_now - now).total_seconds():.0f}s", now)
        self._last_now = max(now, self._last_now) if self._last_now else now
        free = self._disk_free()
        if free < self.cfg.min_disk_free_bytes:
            self._fail_closed(f"disk_low: {free} bytes free", now)
        snap = self.stack.account_snapshot()
        self._last_account = snap
        self._check_account(snap, now)

    # ------------------------------------------------------------------------------------ feeds
    def _refresh_feeds(self, now: datetime) -> list[str]:
        """Update freshness; return markets whose latest CLOSED M5 bar is new since last cycle.

        Uses the stack's ``LiveBarSource`` surface: ``last_closed_bar_close_utc`` (refreshes its own
        cache when a new bar should exist) and ``latest_quote``."""
        new: list[str] = []
        self._stale = set()
        src = self.stack.bar_source
        enabled = [m for m in self.cfg.markets if m not in self.disabled]
        for m in enabled:
            info: dict[str, Any] = {"bar_age_s": None, "quote_age_s": None, "stale": True}
            try:
                close = src.last_closed_bar_close_utc(m)
            except StackFailClosed:
                raise
            except Exception as exc:
                self._feed[m] = {**info, "error": f"{type(exc).__name__}: {exc}"}
                self._stale.add(m)
                continue
            if close is None:
                self._feed[m] = info
                self._stale.add(m)
                continue
            close = close.astimezone(UTC)
            age = (now - close).total_seconds()
            info["bar_age_s"] = age
            try:
                q = src.latest_quote(m)
            except StackFailClosed:
                raise
            except Exception:
                q = None
            if q is not None:
                info["quote_age_s"] = (now - q.ts_utc.astimezone(UTC)).total_seconds()
            stale = age > self.cfg.stale_feed_s or age < -self.cfg.clock_backward_tolerance_s
            info["stale"] = stale
            self._feed[m] = info
            if stale:
                self._stale.add(m)
                continue
            if self._last_close.get(m) != close:
                self._last_close[m] = close
                new.append(m)
        if enabled and len(self._stale) == len(enabled):
            self._all_stale_since = self._all_stale_since or now
            if (now - self._all_stale_since).total_seconds() >= self.cfg.all_stale_grace_s:
                self._fail_closed("all_feeds_stale", now)
        else:
            self._all_stale_since = None
        return new

    # ------------------------------------------------------------------------------- one cycle
    def run_cycle(self, now: datetime | None = None) -> None:
        now = now or self._clock()
        self._roll_day(now)
        if self.cfg.stop_file is not None and self.cfg.stop_file.exists():
            self.request_stop("stop_file")
        new_bars: list[str] = []
        try:
            self._guards(now)
        except StackFailClosed as exc:
            self._fail_closed(f"stack: {exc}", now)
        except Exception as exc:  # cannot verify the account -> fail closed
            self._fail_closed(f"guard_error: {type(exc).__name__}: {exc}", now)
        self._section(now, "manage", self._manage)
        try:
            new_bars = self._refresh_feeds(now)
        except StackFailClosed as exc:
            self._fail_closed(f"stack: {exc}", now)
        except Exception as exc:
            self._note_error(now, f"feed_error: {type(exc).__name__}: {exc}")
        if self.can_trade():
            for m in new_bars:
                if not self.can_trade():
                    break
                self._section(now, f"scan:{m}", lambda n, m=m: self._scan_market(m, n), event_critical=False)
        self._section(now, "periodic", self._periodic, event_critical=False)
        self._heartbeat(now)

    def _section(self, now: datetime, name: str, fn: Callable[[datetime], None], *, event_critical: bool = True) -> None:
        try:
            fn(now)
        except StackFailClosed as exc:
            self._fail_closed(f"stack: {exc}", now)
        except (sqlite3.Error, OSError) as exc:
            self._persistence_failure(exc, now)
        except Exception as exc:
            text = f"{name}: {type(exc).__name__}: {exc}"
            if event_critical:
                self._fail_closed(f"unexpected_error {text}", now)  # broker/store state may diverge
            else:
                self._note_error(now, text)

    def _manage(self, now: datetime) -> None:
        events = list(self.stack.poll_events())
        events += list(self.stack.on_clock(now))
        self._handle_events(events, now)

    # ---------------------------------------------------------------------------- opportunities
    def _scan_market(self, market: str, now: datetime) -> None:
        pairs = self.engine.on_m5_close(market, now)
        if not pairs:
            return
        intents = {i.opportunity_id: i for i in self.engine.intents_for(list(pairs))}
        for snap, dec in pairs:
            self._process_pair(snap, dec, intents.get(snap.opportunity_id), now)

    def _with_shadow(self, snap: OpportunitySnapshot, dec: Decision, now: datetime) -> Decision:
        """Predictions are persisted BEFORE the decision row (created_utc = decision time, never later)."""
        if self.predictor is None:
            return dec
        try:
            preds = self.predictor.predict(snap, predicted_utc=dec.decided_utc)
        except Exception as exc:
            self._note_error(now, f"shadow_predict: {type(exc).__name__}: {exc}")
            return dec
        stored: dict[str, Any] = {}
        for name, pred in preds.items():
            try:
                self.store.record_shadow_prediction(snap.opportunity_id, name, pred, created_utc=dec.decided_utc)
                stored[name] = pred
            except (sqlite3.Error, OSError):
                raise
            except Exception as exc:  # LeakageError / immutability: contained
                self._note_error(now, f"shadow_store: {type(exc).__name__}: {exc}")
        self._pred_status = {n: str(p.get("status")) for n, p in preds.items()}
        return dataclasses.replace(dec, shadow=stored) if stored and not dec.shadow else dec

    def _process_pair(self, snap: OpportunitySnapshot, dec: Decision, intent: TradeIntent | None, now: datetime) -> None:
        if snap.phase != self.cfg.phase:  # DISCOVERY and FROZEN data must never be mixed by one runner
            self._fail_closed(f"phase_mismatch: engine={snap.phase} runner={self.cfg.phase}", now)
            return
        try:
            new_snap = self.store.record_snapshot(snap)
        except DemoStoreError as exc:  # same id, different content: never reprocess
            self._note_error(now, f"duplicate_snapshot_differs: {snap.opportunity_id}: {exc}")
            return
        dec = self._with_shadow(snap, dec, now)
        try:
            new_dec = self.store.record_decision(dec)
        except DemoStoreError as exc:
            self._note_error(now, f"duplicate_decision_differs: {snap.opportunity_id}: {exc}")
            return
        self._last_persist = _iso(now)
        self._funnel_dirty = True
        if new_snap or new_dec:
            self._day_counts["raw"] += 1
            self._day_counts["accepted" if dec.accepted else "rejected"] += 1
            self._last_signal = {
                "ts": _iso(now), "market": snap.market, "direction": snap.direction,
                "accepted": dec.accepted, "reasons": list(dec.reasons), "opportunity_id": snap.opportunity_id,
            }
        if not dec.accepted or self.cfg.mode != "demo-auto":
            return  # rejected -> counterfactual later; shadow -> recorded, never submitted
        if intent is None:
            self._note_error(now, f"accepted_without_intent: {snap.opportunity_id}")
            return
        self._execute(intent, now, self._context_for(snap, dec))

    @staticmethod
    def _shadow_estimates(dec: Decision) -> tuple[float | None, float | None]:
        """(win_probability, expected_payoff_r) from the persisted challenger predictions, ``None`` if
        absent. LOGGED ONLY by the stack: they never size a trade or gate an entry."""
        win = pay = None
        for name in sorted(dec.shadow or {}):
            pred = dec.shadow[name]
            if not isinstance(pred, dict) or pred.get("status") not in (None, "ok"):
                continue
            if win is None and pred.get("p_target_before_stop") is not None:
                win = float(pred["p_target_before_stop"])
            if pay is None and pred.get("expected_r") is not None:
                pay = float(pred["expected_r"])
        return win, pay

    def _context_for(self, snap: OpportunitySnapshot, dec: Decision) -> dict[str, Any]:
        """What the runner knows about the opportunity, handed to ``stack.submit(context=...)``.
        ``family`` drives the stack's concentration cap; everything else is QUALITY / diagnostics that
        the stack only logs."""
        sig = snap.signal
        win, pay = self._shadow_estimates(dec)
        atr = snap.market_state.atr
        return {
            "family": sig.get("family"),
            "atr": None if atr is None else float(atr),
            "confidence": sig.get("confidence"),
            "confluence": sig.get("confluence"),
            "family_score": sig.get("family_score"),
            "quality": sig.get("quality"),
            "quality_components": sig.get("quality_components"),
            "win_probability": win,
            "expected_payoff_r": pay,
            "signal": {
                "strategy_id": sig.get("strategy_id"),
                "independent_clusters": sig.get("independent_clusters"),
                "opposing_specs": sig.get("opposing_specs"),
                "structural_target": sig.get("structural_target"),
                "opportunity_id": snap.opportunity_id,
            },
            "independent_clusters": sig.get("independent_clusters"),
        }

    def _execute(self, intent: TradeIntent, now: datetime, context: dict[str, Any] | None = None) -> None:
        try:
            self.store.record_intent(intent)  # PLANNED, durable BEFORE the stack sees it
        except DemoStoreError as exc:  # duplicate / immutable -> exactly-once: do nothing
            self._note_error(now, f"intent_rejected_by_store: {exc}")
            return
        if self.store.get_state(intent.intent_id) != PLANNED:
            return  # already handled (restart / duplicate)
        iid, ts = intent.intent_id, _iso(now)
        if self.cfg.mode != "demo-auto":  # defence in depth
            self.store.transition(iid, CANCELLED, detail={"reason": "shadow_mode"}, ts=ts)
            return
        cancel: str | None = None
        if not self.can_trade():
            cancel = "halted"
        elif parse_utc(intent.valid_until_utc) < now:
            cancel = "expired"
        if cancel:
            self.store.transition(iid, CANCELLED, detail={"reason": cancel}, ts=ts)
            return
        self._day_counts["trades"] += 1
        self.submit_count += 1
        try:
            events = self.stack.submit(intent, context=context)
        except StackFailClosed as exc:
            self._fail_closed(f"submit: {exc}", now)
            with contextlib.suppress(Exception):
                if iid not in set(self.stack.open_intents()):
                    self.store.transition(iid, CANCELLED, detail={"reason": "stack_fail_closed_before_send"}, ts=ts)
            return
        except Exception as exc:  # outcome unknown: NEVER retry; restart reconciliation decides
            self._fail_closed(f"submit_unknown_outcome: {type(exc).__name__}: {exc}", now)
            return
        self._handle_events(list(events), now)

    # ------------------------------------------------------------------------------- events
    def _handle_events(self, events: Sequence[ExecutionEvent], now: datetime) -> None:
        if events:
            self._funnel_dirty = True
        for ev in events:
            try:
                self._handle_event(ev, now)
            except (sqlite3.Error, OSError):
                raise
            except StackFailClosed:
                raise
            except Exception as exc:  # store/broker divergence risk
                self._fail_closed(f"event_error {type(ev).__name__}:{ev.intent_id}: {type(exc).__name__}: {exc}", now)

    def _handle_event(self, ev: ExecutionEvent, now: datetime) -> None:
        row = self.store.get_intent(ev.intent_id)
        if row is None:
            self._fail_closed(f"event_for_unknown_intent:{ev.intent_id}", now)
            return
        intent = TradeIntent.from_dict({k: v for k, v in row.items() if k in {f.name for f in dataclasses.fields(TradeIntent)}})
        ts = _iso(now)
        state = row["state"]
        iid = ev.intent_id
        if isinstance(ev, Accepted):
            self._persist_risk_detail(iid, "ACCEPTED", ev.risk_detail, now)
            if state == PLANNED:
                risk = RiskRecord(
                    equity=_f(ev.equity), risk_fraction=_f(ev.risk_fraction, intent.risk_fraction),
                    risk_budget=_f(ev.risk_budget), quantity=float(ev.quantity),
                    leverage=_f(ev.leverage), approved=True,
                )
                self.store.record_risk(iid, risk)
                self.store.transition(iid, RISK_APPROVED, ts=ts)
                self.store.transition(iid, SENT, detail={"note": "sent inside stack.submit"}, ts=ts)
        elif isinstance(ev, Rejected):
            code = ev.reason
            det = dict(ev.risk_detail or {})
            gate = G.gate_for(code)
            cls = det.get("gate_reject_class") or (gate.gate_class.value if gate else None)
            det.setdefault("decision", "SKIP")
            det.setdefault("reject_code", code)
            det["gate_reject_class"] = cls
            self._persist_risk_detail(iid, "REJECTED", det, now)
            info = {"reason": code, "gate_class": cls}
            if state == PLANNED:
                eq = _num(det.get("equity"))
                frac = _num(det.get("target_risk_fraction"))
                mult = _num(det.get("risk_budget_multiplier"))
                self.store.record_risk(iid, RiskRecord(
                    equity=eq or 0.0, risk_fraction=frac if frac is not None else intent.risk_fraction,
                    risk_budget=(eq or 0.0) * (frac or 0.0) * (1.0 if mult is None else mult),
                    quantity=0.0, leverage=_num(det.get("leverage")) or 0.0, approved=False,
                    reject_reason=code))
                self.store.transition(iid, RISK_REJECTED, detail=info, ts=ts)
            elif state in (RISK_APPROVED, SENT):
                self.store.transition(iid, SEND_FAILED if state == RISK_APPROVED else CANCELLED, detail=info, ts=ts)
        elif isinstance(ev, Fill):
            self._advance(iid, SENT, now)
            ex = self._execution_from_fill(intent, ev)
            self.store.record_execution(iid, ex)
            self.store.record_tca(iid, self._tca_entry(ev), "ENTRY")
            self._advance(iid, FILLED, now)
            self._last_fill = {"ts": ts, "market": intent.market, "intent_id": iid, "price": float(ev.price), "quantity": float(ev.quantity)}
        elif isinstance(ev, ProtectionConfirmed):
            self._advance(iid, SENT, now)
            cur = self.store.get_execution(iid)
            if cur is not None:
                self.store.record_execution(iid, dataclasses.replace(cur, protection_confirmed=True))
            self._advance(iid, PROTECTED, now)
        elif isinstance(ev, PositionClosed):
            self._on_closed(intent, ev, state, now)

    def _persist_risk_detail(self, iid: str, kind: str, detail: dict[str, Any] | None, now: datetime) -> None:
        if not detail:
            return
        try:
            self.store.record_risk_detail(iid, kind, detail)
        except DemoStoreError as exc:  # differing duplicate: keep the first, surface the divergence
            self._note_error(now, f"risk_detail_{kind.lower()}:{iid}: {exc}")

    @staticmethod
    def _tca_entry(ev: Fill) -> dict[str, Any]:
        """Entry-side transaction-cost analysis of one fill (floats; price units unless noted)."""
        return {
            "fill_price": _num(ev.price), "quantity": _num(ev.quantity),
            "intended_price": _num(ev.intended_price), "reference_price": _num(ev.reference_price),
            "bid_at_send": _num(ev.bid_at_send), "ask_at_send": _num(ev.ask_at_send),
            "spread": _num(ev.spread), "slippage": _num(ev.slippage),
            "slippage_vs_intended": _num(ev.slippage_vs_intended), "fill_vs_mid": _num(ev.fill_vs_mid),
            "fees_price_units": _num(ev.fees_price_units), "cost_price_units": _num(ev.cost_price_units),
            "movement_to_cost": _num(ev.movement_to_cost),
            "entry_commission_eur": _num(ev.commission), "entry_swap_eur": _num(ev.swap),
            "latency_total_ms": ev.latency_total_ms, "latency_send_to_fill_ms": ev.latency_send_to_fill_ms,
            "latency_send_to_ack_ms": ev.latency_send_to_ack_ms, "latency_ack_to_fill_ms": ev.latency_ack_to_fill_ms,
        }

    def _execution_from_fill(self, intent: TradeIntent, ev: Fill) -> ExecutionRecord:
        """ExecutionRecord of the ENTRY fill. ``fees`` / ``swap`` are the ENTRY deal's signed broker
        amounts (negative = cost); closing-deal costs arrive with ``PositionClosed`` and are added ONCE
        at close time (see ``_on_closed``), so nothing is counted twice."""
        px = float(ev.price)
        long = intent.direction > 0
        target_crossed = intent.target is not None and ((px >= intent.target) if long else (px <= intent.target))
        stop_crossed = (px <= intent.stop) if long else (px >= intent.stop)
        return ExecutionRecord(
            intended_entry=intent.entry_ref, fill_price=px, quantity=float(ev.quantity),
            spread_at_send=float(ev.spread), slippage=float(ev.slippage), fees=float(ev.commission),
            swap=float(ev.swap), broker_order_id=ev.broker_order_id, broker_position_id=ev.broker_position_id,
            protection_confirmed=False,
            parity_checks={"target_crossed_at_fill": bool(target_crossed), "stop_crossed_at_fill": bool(stop_crossed)},
            cost_status="provisional",
        )

    def _entry_ts(self, intent_id: str, fallback: datetime) -> datetime:
        for e in self.store.intent_events(intent_id):
            if e["to_state"] == FILLED:
                return parse_utc(e["ts"])
        return fallback

    def _frame_rows(self, market: str) -> list[tuple[datetime, float, float, float, float, float]]:
        """(bar open UTC, open, high, low, close, spread in PRICE units) from the stack's closed M5 frame
        (``m5_frame``: ts/open/high/low/close/tick_volume/spread_pts)."""
        fr = self.stack.bar_source.m5_frame(market)
        spec = self._spec(market)
        point = float(spec.point_size) if spec is not None else 0.0
        out = []
        for row in fr.itertuples(index=False):
            t = row.ts.to_pydatetime() if hasattr(row.ts, "to_pydatetime") else row.ts
            out.append((t, float(row.open), float(row.high), float(row.low), float(row.close),
                        float(getattr(row, "spread_pts", 0.0)) * point))
        return out

    def _path(self, market: str, direction: int, start: datetime, end: datetime) -> list[PathPoint]:
        try:
            rows = self._frame_rows(market)
        except StackFailClosed:
            raise
        except Exception as exc:
            self._warnings.append(f"path_unavailable:{market}:{type(exc).__name__}")
            return []
        pts: list[PathPoint] = []
        for t, _o, hi, lo, _c, spread_px in rows:
            if not (start - _M5 <= t <= end):
                continue
            sp = 0.0 if direction > 0 else spread_px  # short exits at ask
            pts.append(PathPoint(_iso(t), hi + sp, lo + sp))
        return pts

    def _value_per_unit(self, market: str, direction: int, entry: float, exit_px: float, qty: float, profit: Decimal_like) -> float:
        move = (exit_px - entry) * direction
        if profit is not None and abs(move * qty) > 1e-12:
            v = float(profit) / (move * qty)
            if v > 0 and math.isfinite(v):
                return v
        if market in self.cfg.value_per_unit_eur:
            return self.cfg.value_per_unit_eur[market]
        spec = self._spec(market)
        if spec is not None:
            self._warnings.append(f"value_per_unit_approx_contract_size:{market}")
            return float(spec.contract_size)
        return 1.0

    def _on_closed(self, intent: TradeIntent, ev: PositionClosed, state: str, now: datetime) -> None:
        iid, ts = ev.intent_id, _iso(now)
        if state in (RISK_REJECTED, SEND_FAILED, CANCELLED):
            self._note_error(now, f"close_for_terminal_intent:{iid}:{state}")
            return
        if state != CLOSED:
            self._advance(iid, SENT, now)
            if self.store.get_state(iid) == SENT:
                self._advance(iid, FILLED, now, {"note": "fill event missing"})
            self._advance(iid, CLOSED, now, {"exit_reason": ev.exit_reason})
        if self.store.get_outcome(iid) is not None:
            return
        ex = self.store.get_execution(iid)
        if ex is None or ex.fill_price is None or ev.exit_price is None:
            self._warnings.append(f"needs_outcome:{iid}:missing_fill_or_exit")
            self._note_error(now, f"outcome_not_computable:{iid}")
            return
        closed_at = parse_utc(ev.closed_utc) if ev.closed_utc else now
        entry_at = self._entry_ts(iid, closed_at)
        qty = float(ev.exit_quantity) if ev.exit_quantity is not None else ex.quantity
        exit_px = float(ev.exit_price)
        # COST SEMANTICS (signed broker amounts, negative = cost): the FIRST execution record is the
        # ENTRY fill (its fees/swap = entry deal only); ``PositionClosed.commission/swap`` = CLOSING
        # deal(s) only.  Total = entry + closing, recomputed from the immutable first record so that a
        # re-run after a crash can never double count.
        hist = self.store.execution_history(iid)
        entry_rec = hist[0] if hist else ex
        entry_fees, entry_swap = _f(entry_rec.fees), _f(entry_rec.swap)
        commission = entry_fees + _f(ev.commission)
        swap = entry_swap + _f(ev.swap)
        verified = ev.commission is not None and ev.swap is not None  # the broker's closing deals told us
        value = self._value_per_unit(intent.market, intent.direction, ex.fill_price, exit_px, qty, ev.profit_eur)
        try:
            outcome = outcome_from_fills(
                direction=intent.direction,
                entry_fill=LabelFill(ex.fill_price, ex.quantity, _iso(entry_at)),
                initial_stop=intent.stop,
                exit_fills=[LabelFill(exit_px, qty, _iso(closed_at))],
                exit_reason=ev.exit_reason,
                value_per_unit_eur=value,
                fees_eur=commission,
                swap_eur=swap,
                path=self._path(intent.market, intent.direction, entry_at, closed_at),
            )
        except ValueError as exc:
            self._warnings.append(f"needs_outcome:{iid}:{exc}")
            self._note_error(now, f"outcome_error:{iid}:{exc}")
            return
        if verified:
            self.store.record_execution(iid, dataclasses.replace(
                ex, fees=commission, swap=swap, cost_status="verified"))
        else:
            self._warnings.append(f"cost_status_provisional:{iid}:closing_deal_costs_unknown")
        broker_net = _num(ev.net_pnl_eur)
        if broker_net is not None and abs(broker_net - outcome.pnl_eur) > max(0.05, 0.005 * abs(broker_net)):
            self._warnings.append(f"pnl_mismatch:{iid}:broker={broker_net:.2f}:computed={outcome.pnl_eur:.2f}")
        self.store.record_tca(iid, {
            "cost_status": "verified" if verified else "provisional",
            "entry_commission_eur": entry_fees, "entry_swap_eur": entry_swap,
            "close_commission_eur": _num(ev.commission), "close_swap_eur": _num(ev.swap),
            "total_commission_eur": commission, "total_swap_eur": swap,
            "exit_price": exit_px, "exit_slippage_vs_level": _num(ev.exit_slippage_vs_level),
            "broker_profit_eur": _num(ev.profit_eur), "broker_net_pnl_eur": broker_net,
            "computed_pnl_eur": outcome.pnl_eur, "holding_seconds": ev.holding_seconds,
            "exit_reason": ev.exit_reason,
        }, "EXIT")
        self.store.record_outcome(iid, outcome)  # only AFTER the intent is CLOSED
        self._last_persist = ts
        self._refresh_cum_r()
        self._check_milestones()

    # ------------------------------------------------------------------------------- periodic
    def _check_milestones(self) -> None:
        try:
            done = monitor.check_milestone(self.store, self.cfg.phase, self.cfg.reports_dir)
        except (sqlite3.Error, OSError):
            raise
        except Exception as exc:
            self._note_error(self._clock(), f"milestone_report: {type(exc).__name__}: {exc}")
            return
        if done is not None:
            self.milestones.append(done[0])

    def check_milestones(self) -> None:
        self._check_milestones()

    def _bars_provider(self) -> Callable[[str, str, str], list[Bar]]:
        cache: dict[str, Any] = {}

        def provider(market: str, start: str, end: str) -> list[Bar]:
            if market not in cache:
                cache[market] = self._frame_rows(market)
            s, e = parse_utc(start), parse_utc(end)
            return [Bar(_iso(t), o, h, lo, c, sp) for t, o, h, lo, c, sp in cache[market] if s <= t < e]

        return provider

    def label_now(self, now: datetime) -> int:
        """Counterfactual labels for REJECTED opportunities whose horizon elapsed (post-horizon only)."""
        written = label_counterfactuals(self.store, self._bars_provider(), _iso(now))
        self._last_label = now
        self._last_label_n += len(written)
        return len(written)

    def train_now(self, now: datetime) -> None:
        self._last_train = now
        if self.trainer is None:
            return
        try:
            self._last_train_report = self.trainer.update(self.store)
            self._trained_at = self.store.count_trades(self.cfg.phase)
        except (sqlite3.Error, OSError):
            raise
        except Exception as exc:  # shadow learning must never stop trading
            self._note_error(now, f"trainer: {type(exc).__name__}: {exc}")

    def _periodic(self, now: datetime) -> None:
        if (now - self._last_label).total_seconds() >= self.cfg.label_every_s:
            self.label_now(now)
        if self.trainer is not None:
            n = self.store.count_trades(self.cfg.phase)
            if (now - self._last_train).total_seconds() >= self.cfg.trainer_every_s or (
                n - self._trained_at >= self.cfg.trainer_every_trades
            ):
                self.train_now(now)

    # ----------------------------------------------------------------------------- heartbeat
    def funnel_summary(self, now: datetime, *, max_age_s: float = 60.0) -> dict[str, Any] | None:
        """Top-level rejection-funnel summary for the heartbeat (recomputed at most every ``max_age_s``)."""
        if (
            not self._funnel_dirty and self._funnel is not None and self._funnel_at is not None
            and (now - self._funnel_at).total_seconds() < max_age_s
        ):
            return self._funnel
        self._funnel_dirty = False
        try:
            from demo.funnel import funnel

            full = funnel(self.store, self.stack, self.cfg.phase)
            self._funnel = {**full["summary"], "by_market": {m: {k: b[k] for k in ("opportunities", "engine_accepted", "stack_rejected", "traded", "temporary_otherwise_valid_blocked")} for m, b in full["by_market"].items()},
                            "stack_by_class": full["stack"]["by_class"], "engine_by_class": full["engine"]["by_class"]}
            self._funnel_at = now
        except Exception as exc:  # a diagnostics read never stops trading
            self._funnel = {"error": f"{type(exc).__name__}: {exc}"}
            self._funnel_at = now
        return self._funnel

    def status(self, now: datetime | None = None, *, alive: bool = True) -> dict[str, Any]:
        now = now or self._clock()
        acct = self._last_account
        try:
            free = self._disk_free()
        except OSError:
            free = -1
        open_intents = 0
        with contextlib.suppress(Exception):
            open_intents = len(self.store.recover_open_intents())
        try:
            n_trades = self.store.count_trades(self.cfg.phase)
        except Exception:
            n_trades = -1
        protection = "NONE_OPEN"
        if acct is not None and acct.open_positions:
            protection = "ALL_PROTECTED" if acct.all_positions_protected else "UNPROTECTED"
        return {
            "mode": "DEMO MODE",
            "runner_mode": self.cfg.mode,
            "phase": self.cfg.phase,
            "updated_utc": _iso(now),
            "process_alive": alive,
            "pid": self.pid,
            "started_utc": _iso(self.started_utc),
            "equity": None if acct is None else acct.equity,
            "balance": None if acct is None else acct.balance,
            "pnl": None if acct is None else acct.profit,
            "floating_pl": None if acct is None else acct.profit,
            "cumulative_r": self._cum_r,
            "reconciliation": "NOT_RECONCILED" if acct is None else acct.reconciliation,
            "open_positions": 0 if acct is None else acct.open_positions,
            "open_orders": 0 if acct is None else acct.open_orders,
            "open_intents": open_intents,
            "protection_state": protection,
            "opportunities_today": {k: self._day_counts[k] for k in ("raw", "accepted", "rejected")},
            "rejection_funnel": self.funnel_summary(now),
            "trades_today": self._day_counts["trades"],
            "last_signal": self._last_signal,
            "last_fill": self._last_fill,
            "last_error": self._last_error,
            "learning_samples": n_trades,
            "champion": CHAMPION,
            "challengers": self._pred_status or {"status": self.learning_error or ("not_loaded" if self.predictor is None else "no_predictions_yet")},
            "mt5_connected": bool(acct.connected) if acct is not None else False,
            "feed": self._feed,
            "stale_markets": sorted(self._stale),
            "disabled_markets": dict(self.disabled),
            "last_persistence_write": self._last_persist,
            "disk_free_bytes": free,
            "git_commit": self.commit,
            "halted": self.halt_reason,
            "fail_closed": self.fail_reason,
            "stop_reason": self.stop_reason,
            "warnings": self._warnings[-20:],
            "milestones_emitted": list(self.milestones),
            "calendar_status": {r.market: r.calendar_status for r in self.clock_rows},
        }

    def _heartbeat(self, now: datetime, *, alive: bool = True) -> None:
        try:
            monitor.write_heartbeat(self.cfg.heartbeat_path, self.status(now, alive=alive))
        except OSError as exc:  # cannot report health -> stop trading
            self._fail_closed(f"heartbeat_write_failed: {exc}", now)

    # ----------------------------------------------------------------------------- main loop
    def _should_exit(self, now: datetime) -> bool:
        if self._stopping:
            return True
        if self.fail_reason is not None:
            # keep managing exits while a position may still be open, up to manage_after_halt_s
            if self._halted_since is None or not self._has_open_exposure():
                return True
            return (now - self._halted_since).total_seconds() >= self.cfg.manage_after_halt_s
        return False

    def _has_open_exposure(self) -> bool:
        try:
            return bool(self.store.recover_open_intents()) and bool(self._last_account and self._last_account.connected)
        except Exception:
            return False

    def run(self, max_cycles: int | None = None, *, install_signals: bool = False) -> int:
        if install_signals:
            self.install_signal_handlers()
        self.start()
        cycles = 0
        while not self._should_exit(self._clock()):
            if max_cycles is not None and cycles >= max_cycles:
                break
            self.run_cycle()
            cycles += 1
            if self._should_exit(self._clock()):
                break
            self._sleep(min(self.cfg.poll_interval_s, 30.0))
        return self.shutdown()

    def shutdown(self) -> int:
        """Orderly stop: no new exposure, open positions stay protected at the broker (NOT flattened;
        ``StackPort`` has no flatten call), final events/labels/report, stack.stop(), final heartbeat."""
        now = self._clock()
        self._stopping = True
        self.halt(self.fail_reason or self.stop_reason or "shutdown", now)
        self._section(now, "final_manage", self._manage)
        for name, fn in (
            ("final_label", lambda: self.label_now(now)),
            ("final_report", self._final_report),
        ):
            try:
                fn()
            except Exception as exc:
                self._note_error(now, f"{name}: {type(exc).__name__}: {exc}")
        with contextlib.suppress(Exception):
            self.stack.stop()
        self.exit_code = EXIT_FAIL_CLOSED if self.fail_reason else EXIT_OK
        self._heartbeat(self._clock(), alive=False)
        return self.exit_code

    def _final_report(self) -> None:
        from demo.report import write_report

        write_report(self.store, self.cfg.reports_dir, self.cfg.phase, tag="final")


# ------------------------------------------------------------------------------------ factory
class LiveStackRefused(RuntimeError):
    """The factory refuses to build a live stack (e.g. ``MT5_ALLOW_ACCOUNT_LOGIN=1``)."""


def build_live_runner(
    mode: str,
    *,
    phase: str = "DISCOVERY",
    db_path: Path | None = None,
    artifacts_dir: Path | None = None,
    markets: Sequence[str] | None = None,
    learning: bool | None = None,
    stack_factory: Callable[..., StackPort] | None = None,
) -> DemoRunner:
    """Wire the runner to the REAL ``Mt5DemoStack``.

    * ``shadow``     -> ``Mt5DemoStack(dry_run=True)``  (``order_send`` is hard-guarded, never reachable);
    * ``demo-auto``  -> ``Mt5DemoStack(dry_run=False)`` (ActivTrades DEMO only; the stack itself verifies
      DEMO / expected login / server / netting / leverage before it can send anything).

    The real MT5 client and the attach-only connection config are obtained ONLY here, lazily (never at
    module import), and only when no ``stack_factory`` is injected.  ``MT5_ALLOW_ACCOUNT_LOGIN=1`` is
    refused (``LiveStackRefused``).  State (adapter DB + intent registry) lives in ``<artifacts>/stack``.
    ``stack_factory(dry_run=..., state_dir=..., markets=...)`` is the test seam (FakeStack etc.); with it
    the real client is never touched.  Raises ``LiveStackUnavailable`` if the real stack / MT5 package /
    connection config is not usable in this checkout."""
    from demo.opportunity.engine import OpportunityEngine
    from demo.opportunity.production_spec import load_production_spec

    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if os.environ.get("MT5_ALLOW_ACCOUNT_LOGIN") == "1":
        raise LiveStackRefused("MT5_ALLOW_ACCOUNT_LOGIN=1 is not permitted; the DEMO trader only attaches")
    art = Path(artifacts_dir or "artifacts/demo_trader")
    production = load_production_spec()
    names = tuple(markets) if markets else production.market_names()
    state_dir = art / "stack"
    dry_run = mode == "shadow"
    if stack_factory is None:
        try:
            from adapters.activtrades_mt5.real_client import get_real_client
            from adapters.config import MT5ConfigError, load_attach_only_config
            from demo.execution.live import Mt5DemoStack
            from demo.execution.market_config import load_demo_market_specs
        except ImportError as exc:
            raise LiveStackUnavailable(
                f"the real Mt5DemoStack cannot be imported in this checkout ({type(exc).__name__}: {exc})"
            ) from exc
        try:
            connection = load_attach_only_config()
        except MT5ConfigError as exc:
            raise LiveStackUnavailable(f"MT5 attach-only configuration unusable: {exc}") from exc
        all_specs = load_demo_market_specs()
        missing = [m for m in names if m not in all_specs]
        if missing:
            raise LiveStackUnavailable(f"no checked-in DEMO market config for {missing}")
        try:
            client = get_real_client()
        except ImportError as exc:
            raise LiveStackUnavailable(f"MetaTrader5 package not importable: {exc}") from exc
        state_dir.mkdir(parents=True, exist_ok=True)
        art.mkdir(parents=True, exist_ok=True)
        stack: StackPort = Mt5DemoStack(  # type: ignore[assignment]
            client=client, connection=connection, state_dir=state_dir,
            market_specs={m: all_specs[m] for m in names}, dry_run=dry_run,
        )
    else:
        art.mkdir(parents=True, exist_ok=True)
        stack = stack_factory(dry_run=dry_run, state_dir=state_dir, markets=names)
    store = DemoStore(db_path or art / "demo.sqlite")
    engine = OpportunityEngine(
        stack.bar_source,
        production=production,
        phase=phase,  # type: ignore[arg-type]
        seen_store=StoreSeenAdapter(store),
    )
    predictor, trainer, err = load_learning(art / "models", learning)
    cfg = RunnerConfig(mode=mode, phase=phase, markets=names, artifacts_dir=art, learning=learning)
    return DemoRunner(
        stack, engine, store, config=cfg, predictor=predictor, trainer=trainer,
        learning_error=err, production=production,
    )
