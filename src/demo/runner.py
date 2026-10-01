# ruff: noqa: E501
"""DEMO runner loop: the deterministic integrator of the ActivTrades DEMO trader.

    closed M5 bar -> OpportunityEngine -> snapshot (persisted) -> shadow predictions (persisted) ->
    decision (persisted) -> [demo-auto, accepted] intent PLANNED (persisted) -> StackPort.submit ->
    events -> lifecycle transitions + Risk/Execution/Outcome records -> milestone reports.

Hard rules implemented here (no LLM / Optuna / DEAP anywhere in this module):
  * Persist BEFORE acting.  The PLANNED intent is durable before ``stack.submit``; a persistence
    failure halts new exposure and the runner exits 7 (fail closed).
  * Shadow mode never sends an order: accepted opportunities are recorded as PLANNED intents and run
    through the DRY-RUN stack (``stack.submit`` -> risk/sizing/gates only; ``order_send`` is hard-guarded).
    Accepted -> RISK_APPROVED -> CANCELLED{reason: shadow_dry_run} (never SENT/FILLED); Rejected ->
    RISK_REJECTED exactly as in demo-auto. Any Fill/Protection event in shadow fails closed.
  * No one-position rule here: every accepted opportunity is submitted. Same-symbol add-on / opposite-side
    handling (broker netting = one net position per symbol) is the STACK's job and shows up as a classified
    reject (temporary ``ADDON_*`` codes) in the rejection funnel.
  * Exactly-once: the opportunity id is deduped through the store's seen-set; the store allows one
    intent per opportunity; only a PLANNED intent is ever submitted; on restart unfinished intents
    are reconciled with ``stack.open_intents()`` and are NEVER re-sent (see ``_reconcile_restart``).
  * Fail closed (stop new exposure, keep managing exits, write the error to the heartbeat, exit 7
    after an orderly stop) on: non-demo/unknown account, persistence failure, unprotected exposure,
    clock anomaly, ``StackFailClosed``, low disk - immediately.
  * TRANSIENT conditions (reconciliation != RECONCILED, a brief disconnect, a stack halt/kill switch, all
    expected-open feeds stale) first put the runner into HALT-NEW-EXPOSURE: no new orders, but polling /
    exit management continue and the condition is re-checked every cycle with exponential backoff; the
    runner resumes on its own when the condition clears and exits 7 only if it persists beyond
    ``transient_grace_s`` (stale feeds: ``all_stale_exit_s``).  The runner-level halt does NOT latch the
    stack (``StackPort`` has no un-halt), so recovery is possible.
  * Closed markets are idle, not stale: a feed only counts as stale while the market's MarketSpec calendar
    (local weekday + cash session) says it should be open.
  * An in-doubt entry (``order_outcome_unknown``) becomes the non-terminal ``IN_DOUBT`` state (never
    CANCELLED): a late Fill / PositionClosed from broker truth still advances it and writes the outcome.
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
from collections.abc import Callable, Mapping, Sequence
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
from demo.labeling import (
    Bar,
    PathPoint,
    label_counterfactuals,
    outcome_from_fills,
    path_analytics,
    realised_entry_exit,
)
from demo.labeling import Fill as LabelFill
from demo.store import (
    CANCELLED,
    CLOSED,
    FILLED,
    IN_DOUBT,
    PLANNED,
    PROTECTED,
    RISK_APPROVED,
    RISK_REJECTED,
    SEND_FAILED,
    SENT,
    UNCENSORED_EXITS,
    AccountMismatch,
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
ACCOUNT_PHASES = ("ALPHA_EXECUTION_DISCOVERY", "SMALL_ACCOUNT_FEASIBILITY")
DEFAULT_ACCOUNT_PHASE = "ALPHA_EXECUTION_DISCOVERY"
DISCLAIMER = "DEMO_ALPHA_RESULT != LIVE_EXECUTION_PROOF"
ACCOUNT_META_FILE = "account_meta.json"
MODES = ("shadow", "demo-auto")
SHADOW_DRY_RUN = "shadow_dry_run"  # CANCELLED detail reason of a shadow-approved (would-have-traded) intent
EXPECTED_DEMO_SERVER = "ActivTradesEU-Server"
_ORDER = (PLANNED, RISK_APPROVED, SENT, FILLED, PROTECTED, CLOSED)
_M5 = timedelta(minutes=5)
# Lane U2: the FACTORY (production entry point) records out-of-window shadow observations by default because it is
# demonstrably cheap (measured on real dev bars: median ~18-40 ms per market-bar, only on bars whose window is closed, hard
# per-cycle budget ``out_of_window_budget_s``) and cannot trade; ``--no-out-of-window-shadow`` turns it off. A bare
# ``RunnerConfig()`` (library / tests) keeps it OFF. The shadow UNIVERSE is always opt-in (``--shadow-universe``).
DEFAULT_OUT_OF_WINDOW_SHADOW = True
# Lane W: shadow exit lab (12 hypothetical exit policies per labelled entry, ~25-60 ms each, run on the runner thread at label
# time).  Default OFF: opt in with --shadow-exit-lab (see docs/evidence/shadow_exit_lab.md for the measured cost).
DEFAULT_SHADOW_EXIT_LAB = False


TRANSIENT_STACK_PREFIXES = ("broker_disconnect", "not_reconciled", "runtime_not_ready", "stale_feed")


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
    """Engine ``SeenStore`` protocol (``add_if_new``) on top of the persistent ``DemoStore`` seen-set.

    The id is NOT committed here: ``DemoStore.record_snapshot`` inserts it into ``seen`` in the SAME
    transaction as the snapshot.  Until then it is only remembered in memory (``_pending``), so an
    exception between build and persist can never leave a seen id without a snapshot (the opportunity
    would be lost forever).  ``release_all`` drops the in-flight ids after a bar was processed."""

    def __init__(self, store: DemoStore) -> None:
        self._store = store
        self._pending: set[str] = set()

    def add_if_new(self, opportunity_id: str) -> bool:
        if opportunity_id in self._pending or self._store.seen(opportunity_id):
            return False
        self._pending.add(opportunity_id)
        return True

    def release_all(self) -> None:
        self._pending.clear()


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
    operating: Any | None = None,
) -> list[ClockChainRow]:
    """UTC -> market tz -> DST -> local trading minute -> session bucket -> SimWindow, per market.

    A market whose chain fails is DISABLED (``ok=False``); nothing is guessed.  ``calendar_status`` is
    copied from the spec: a ``provisional`` calendar stays ``provisional``."""
    from demo.opportunity.clock import (
        forced_flat_utc,
        local_minute_of,
        local_of,
        market_flat_utc,
        session_bucket_of,
    )

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
            # the self-check is of the MARKET-LOCAL clock exit (research semantics); the live operating policy
            # legitimately moves the effective flat earlier, which is checked separately below
            ff = market_flat_utc(spec, entry_utc, cal.forced_flat_min)
            if (ff - entry_utc) != timedelta(minutes=cal.forced_flat_min - cal.entry_start_min):
                raise ValueError("forced-flat instant inconsistent with local clock")
            win_txt = f"entry {cal.entry_start_min}-{cal.entry_end_min} flat {cal.forced_flat_min}"
            if operating is not None:
                for probe_day in (datetime(year, 1, 15).date(), datetime(year, 7, 15).date()):
                    p_entry = datetime(probe_day.year, probe_day.month, probe_day.day, cal.entry_start_min // 60,
                                       cal.entry_start_min % 60, tzinfo=tz).astimezone(UTC)
                    eff = forced_flat_utc(spec, p_entry, cal.forced_flat_min, operating)
                    if eff > operating.deadline_utc(operating.day_of(p_entry)):
                        raise ValueError(f"effective flat {eff.isoformat()} is after the global deadline")
                win_txt += f" +op {operating.version}"
            if production is not None:
                from demo.opportunity.production_spec import validate_sim_windows

                win_txt += f" +{validate_sim_windows(production, m, spec)} SimWindow ok"
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
    # Lane W: shadow exit lab (parallel hypothetical exit policies on the SAME entries) at outcome / label time, never on the
    # decision path, failure-isolated.  Default: see docs/evidence/shadow_exit_lab.md (measured cost per entry).
    shadow_exit_lab_enabled: bool = DEFAULT_SHADOW_EXIT_LAB
    shadow_lab_max_per_cycle: int = 10
    trainer_every_s: float = 3600.0
    trainer_every_trades: int = 10
    min_disk_free_bytes: int = monitor.MIN_DISK_FREE_BYTES
    unprotected_grace_s: float = 30.0
    transient_grace_s: float = 300.0  # reconciliation / disconnect / stack halt: halt-new-exposure, then exit 7
    transient_backoff_max_s: float = 30.0
    all_stale_exit_s: float = 7200.0  # expected-open feeds all stale this long => exit 7 (halt starts at all_stale_grace_s)
    train_timeout_s: float = 600.0
    forced_flat_on_shutdown: bool = False  # only honoured if the stack offers ``flatten_all`` (StackPort has none)
    max_clock_skew_s: float = 300.0
    # A broker tick timestamp is a market EVENT time, not a continuously advancing wall clock. It is only a usable
    # clock reference while the freshest tick across the markets has ADVANCED within this window (independent of the
    # local clock, otherwise a wrong local clock would look like a stale quote). Else CLOCK_REFERENCE_UNAVAILABLE.
    clock_reference_window_s: float = 120.0
    clock_backward_tolerance_s: float = 5.0
    manage_after_halt_s: float = 4 * 3600.0
    stop_file: Path | None = None
    learning: bool | None = None
    model_dir: Path | None = None
    value_per_unit_eur: dict[str, float] = field(default_factory=dict)
    # ---- closed-bar catch-up (Lane R2) ---------------------------------------------------------
    catchup_max_bars: int = 300  # evaluate at most this many missed closed bars per market and cycle
    catchup_max_age_s: float = 24 * 3600.0  # bars closed longer ago than this are skipped (counted), not evaluated
    live_max_age_s: float = 300.0  # the newest bar is processed LIVE only while it is at most one M5 bar old
    # ---- account separation (Lane R2) -----------------------------------------------------------
    # None = take it from the artifacts dir meta (account_meta.json) or the store, else the default.
    # Any string is allowed ("custom"); an explicit value that differs from the store's is refused.
    account_phase: str | None = None
    # ---- closed markets / weekend idling (Lane R2) ----------------------------------------------
    quote_fresh_s: float = 120.0  # a broker quote this young proves the market is trading whatever the calendar says
    idle_poll_interval_s: float = 30.0  # poll cadence while EVERY enabled market is closed (heartbeat stays < 90 s)
    idle_account_check_s: float = 60.0  # account snapshot / reconcile cadence while idle
    idle_transient_grace_s: float = 12 * 3600.0  # a broker disconnect with all markets closed and no exposure is tolerated this long
    # ---- live operating policy / operating day (Lane P) -----------------------------------------
    # ``operating_policy`` (demo.opportunity.operating_policy.OperatingPolicy): None = today's behaviour (cash-session
    # feed semantics, no flatten heartbeat). ``daily``: after the 22:00 Berlin deadline, once the broker is flat
    # and reconciled, finalize the day and exit 0 with stop_reason ``eod_flat_shutdown``; entries refused outside the
    # operating day (Mon-Fri Berlin date). Default off.
    operating_policy: Any = None
    daily: bool = False
    # Lane Z (H1): a STOP (file / signal) that arrives inside the flatten window with own exposure still open finishes the
    # sweep first, bounded by this grace after the 22:00 deadline; then it exits with a loud alert.
    stop_flatten_grace_s: float = 900.0
    # Lane R: EOD-RECOVERY (flatten-only) mode. Same start path as a normal run (DEMO verification, account binding, persisted-state
    # recovery, broker reconciliation) but: no market scanning / engine / shadow universe / learning, the stack entry gate is
    # permanently closed (``StackConfig.flatten_only``), the STOP file does not stop a sweep, and the run ends with exit 0 and
    # stop_reason ``eod_recovery_flat_confirmed`` as soon as the broker is flat (own magic) and reconciled.
    flatten_only: bool = False

    # ---- Lane U2: opt-in measurement-only shadow collection (inside this process; OFF by default) ---
    out_of_window_shadow_enabled: bool = False  # active markets: record what the frozen families WOULD signal with the window closed
    out_of_window_cap_per_zone_day: int = 2  # dedupe/cap per (market, family, direction, zone, UTC day)
    out_of_window_budget_s: float = 1.5  # wall budget of ALL out-of-window passes of one runner cycle
    shadow_universe: tuple[str, ...] = ()  # canonicals of configs/markets_shadow to scan (empty = off)
    shadow_universe_max_symbols_per_cycle: int = 12
    shadow_universe_budget_s: float = 2.0

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if self.phase not in ("DISCOVERY", "FROZEN"):
            raise ValueError("phase must be DISCOVERY or FROZEN")
        if not (0 < self.clock_reference_window_s < self.max_clock_skew_s):
            # the reference window must be shorter than the skew bound, or a stale reference could mask real skew
            raise ValueError("clock_reference_window_s must be > 0 and < max_clock_skew_s")
        if self.flatten_only and (self.operating_policy is None or self.mode != "demo-auto"):
            raise ValueError("flatten_only needs mode='demo-auto' and the live operating policy")
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
        self._stop_deferral_noted = False
        self._stopping = False
        self._last_close: dict[str, datetime | None] = {}
        self._feed: dict[str, dict[str, Any]] = {}
        self._stale: set[str] = set()
        self._all_stale_since: datetime | None = None
        self._unprotected_since: datetime | None = None
        self._clock_ref_tick: datetime | None = None  # freshest broker tick time seen so far
        self._clock_ref_advanced_at: datetime | None = None  # local ``now`` when that tick time last advanced
        self._clock_reference = "UNAVAILABLE"
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
        self._transient: dict[str, tuple[datetime, str]] = {}  # key -> (since, reason)
        self._transient_cycles = 0
        self._stale_halt_since: datetime | None = None
        self._train_thread: threading.Thread | None = None
        self._train_started: datetime | None = None
        self._train_result: dict[str, Any] | None = None
        self._funnel: dict[str, Any] | None = None
        self._funnel_at: datetime | None = None
        self._funnel_dirty = True
        self._market_state: dict[str, str] = {}  # FRESH | OPEN_OUT_OF_SESSION | CLOSED_IDLE | STALE_FAULT | DISABLED
        self._eod_noted_at: datetime | None = None
        self._idle_all = False  # every enabled market is closed per calendar AND quote-stale (idle, not a fault)
        self._last_account_at: datetime | None = None
        self._scan_pending: set[str] = set()  # markets whose newest bar is not fully scanned yet (retry next cycle)
        self.account_info: dict[str, Any] = {"account_id_hash": None, "account_phase": None}
        self._catchup: dict[str, dict[str, Any]] = {}  # per market catch-up counters (heartbeat / tests)
        self._day_counts.update({"intents": 0, "filled": 0})  # Lane U2 (D): intents planned / broker fills today
        self._shadow_counts: dict[str, int] = {"out_of_window": 0, "shadow_universe": 0}
        self._oow: Any | None = None  # OutOfWindowShadow (Lane U2), created when the flag is on
        self.shadow_universe: Any | None = None  # ShadowUniverseScanner, attached by the factory / tests
        from demo.shadow_exit_lab import LabStats

        self._shadow_stats = LabStats()  # Lane W: shadow exit lab cost / failure counters (diagnostic)
        self._shadow_lab_seen: set[str] = set()
        if self.cfg.out_of_window_shadow_enabled:
            from demo.shadow_universe import OutOfWindowShadow

            self._oow = OutOfWindowShadow(
                cap_per_zone_day=self.cfg.out_of_window_cap_per_zone_day, budget_s=self.cfg.out_of_window_budget_s,
            )
        self.submit_count = 0
        self.shadow_submit_count = 0  # dry-run submits (shadow mode); never counted as trades
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
            from demo.opportunity.policy import SHADOW_SCAN_GATES

            decs = [
                d for d in self.store.list_decisions()
                if d.decided_utc.startswith(day) and not (d.reasons and d.reasons[0] in SHADOW_SCAN_GATES)  # Lane U2: measurement-only rows are not opportunities
            ]
            trades = [i for i in self.store.list_intents() if i["created_utc"].startswith(day)]
            self._day_counts = {
                "raw": len(decs), "accepted": sum(d.accepted for d in decs),
                "rejected": sum(not d.accepted for d in decs), "trades": len(trades),
                "intents": len(trades), "filled": sum(1 for i in trades if i.get("state") in (FILLED, PROTECTED, CLOSED)),
            }
        except Exception:
            self._day_counts = {"raw": 0, "accepted": 0, "rejected": 0, "trades": 0, "intents": 0, "filled": 0}

    def _refresh_cum_r(self) -> None:
        with contextlib.suppress(Exception):
            self._cum_r = float(sum(o.net_r for _, _, o in self.store.list_outcomes(self.cfg.phase)))

    # ---------------------------------------------------------------------- fail-closed plumbing
    def halt(self, reason: str, now: datetime | None = None, *, latch_stack: bool = True) -> None:
        """Stop NEW exposure (idempotent). Exits keep being managed."""
        if self.halt_reason is not None:
            return
        self.halt_reason = reason
        self._halted_since = now or self._clock()
        if latch_stack:
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
        return (
            self.halt_reason is None and not self._stopping and self.fail_reason is None
            and not self._transient and self._stale_halt_since is None and not self.cfg.flatten_only
        )

    def _note_transient(self, key: str, reason: str, now: datetime) -> None:
        """Register a transient condition (first-seen time kept); exceeding the grace fails closed."""
        since, _old = self._transient.get(key, (now, reason))
        self._transient[key] = (since, reason)
        grace = self.cfg.transient_grace_s
        if self._idle_all and not self._has_open_exposure():
            grace = max(grace, self.cfg.idle_transient_grace_s)  # weekend: nothing to protect, nothing to trade
        if (now - since).total_seconds() >= grace:
            self._fail_closed(f"{reason} (persisted {(now - since).total_seconds():.0f}s)", now)

    def _clear_transient(self, keys: Sequence[str]) -> None:
        for k in keys:
            self._transient.pop(k, None)

    def _stack_failure(self, exc: StackFailClosed, now: datetime, where: str) -> None:
        """A ``StackFailClosed``: transient classes (disconnect / not reconciled) -> halt-new-exposure and
        retry; everything else fails closed at once."""
        text = str(exc)
        if text.startswith(TRANSIENT_STACK_PREFIXES) and self.fail_reason is None:
            self._note_transient(f"stack:{where}", f"stack: {text}", now)
        else:
            self._fail_closed(f"stack: {text}", now)

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
            self._check_account(snap, now, strict=True)
        except StackFailClosed as exc:
            self._fail_closed(f"stack_start: {exc}", now)
            return
        if not self._bind_account(snap, now):
            return
        self.clock_rows = [] if self.cfg.flatten_only else verify_clock_chain(  # recovery: no entry clocks are needed
            self.cfg.markets, now=now, spec_loader=self._spec_loader, production=self._production,
            operating=self.cfg.operating_policy,
        )
        for r in self.clock_rows:
            if not r.ok:
                self.disabled[r.market] = f"clock_chain_failed: {r.detail}"
        with contextlib.suppress(OSError):
            (self.cfg.artifacts_dir / "clock_chain.json").write_text(
                json.dumps([dataclasses.asdict(r) for r in self.clock_rows], indent=1), encoding="utf-8"
            )
        # Per-market start-up preflight of the stack (Phase-2 opt-ins): a market the stack disabled is recorded here
        # (heartbeat ``disabled_markets``) and never scanned; it does not stop the other markets.
        for m, why in dict(getattr(self.stack, "disabled_markets", None) or {}).items():
            if m in self.cfg.markets:
                self.disabled.setdefault(m, f"stack_preflight: {why}")
        print(render_clock_table(self.clock_rows), file=sys.stderr)
        if not self.cfg.flatten_only and self.cfg.markets and len(self.disabled) == len(self.cfg.markets):
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
        if cur == IN_DOUBT and target in _ORDER and _ORDER.index(target) >= _ORDER.index(SENT):
            # broker truth arrived for an in-doubt entry: resume the normal lifecycle at SENT
            self.store.transition(intent_id, SENT, detail={"in_doubt": "resolved_by_broker_truth", **(detail or {})}, ts=_iso(now))
            cur = SENT
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
            if row is None and self.cfg.flatten_only:
                # Lane R: recovery acts on BROKER truth (the local store may be lost / behind): an intent the store never saw is
                # reported, not fatal - the sweep below closes its position by broker truth either way
                self._warnings.append(f"orphan_broker_intent_in_recovery:{orphan}")
            elif row is None:
                self._fail_closed(f"orphan_broker_intent:{orphan}", now)
        for it in unfinished:
            iid, state = it["intent_id"], it["state"]
            if iid in broker:
                if state == IN_DOUBT:
                    continue  # the stack still resolves it (late fill / close events); stay IN_DOUBT
                # adopt: broker truth proves at least SENT; further states arrive as events
                self._advance(iid, SENT, now, {"restart": "adopted"})
                continue
            if state == IN_DOUBT:
                self.store.transition(iid, CANCELLED, detail={"restart": "in_doubt_unknown_to_broker", "needs_manual_review": True}, ts=_iso(now))
                self._warnings.append(f"needs_manual_review:{iid}:in_doubt_unknown_to_broker")
                continue
            if state in (PLANNED, RISK_APPROVED):
                self.store.transition(iid, CANCELLED, detail={"restart": "unsent_unknown_to_broker"}, ts=_iso(now))
            elif state == SENT:
                self.store.transition(iid, CANCELLED, detail={"restart": "sent_unknown_to_broker", "needs_manual_review": True}, ts=_iso(now))
                self._warnings.append(f"needs_manual_review:{iid}:sent_unknown_to_broker")
            elif state in (FILLED, PROTECTED):
                self._advance(iid, CLOSED, now, {"restart": "closed_while_down"})
                self._warnings.append(f"needs_outcome:{iid}:closed_while_down")

    # ------------------------------------------------------------------------ account separation
    def _artifact_meta_path(self) -> Path:
        return self.cfg.artifacts_dir / ACCOUNT_META_FILE

    def _read_artifact_meta(self) -> dict[str, Any]:
        try:
            return json.loads(self._artifact_meta_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _bind_account(self, snap: AccountSnapshot, now: datetime) -> bool:
        """Bind store + artifacts dir to ONE account.  A store (or artifacts dir - the stack keeps its peak
        equity / drawdown state there) that belongs to another account is REFUSED (fail closed at start):
        the 499 EUR and the 100k data must never mix.  Old DBs without the meta are recorded on first
        attach (flagged legacy when they already hold intents)."""
        meta = self._read_artifact_meta()
        phase = self.cfg.account_phase or meta.get("account_phase") or self.store.get_meta("account_phase") or DEFAULT_ACCOUNT_PHASE
        try:
            if meta.get("account_id_hash") and meta["account_id_hash"] != snap.account_id_hash:
                raise AccountMismatch(
                    f"artifacts dir belongs to account {meta['account_id_hash']}, attached {snap.account_id_hash}"
                )
            info = self.store.bind_account(
                snap.account_id_hash, phase, phase_explicit=self.cfg.account_phase is not None
            )
        except AccountMismatch as exc:
            self._fail_closed(f"account_mismatch: {exc}", now)
            return False
        except (sqlite3.Error, OSError) as exc:
            self._persistence_failure(exc, now)
            return False
        self.account_info = info
        if info["status"] == "LEGACY_RECORDED":
            self._warnings.append("account_hash_recorded_on_legacy_db")
        with contextlib.suppress(OSError):
            self._artifact_meta_path().write_text(json.dumps({
                "account_id_hash": snap.account_id_hash, "account_phase": info["account_phase"],
            }), encoding="utf-8")
        return True

    # ----------------------------------------------------------------------------------- guards
    def _check_account(self, snap: AccountSnapshot, now: datetime, *, strict: bool = False) -> None:
        """Permanent problems fail closed at once.  Transient ones (reconciliation != RECONCILED, a
        disconnect, a stack halt / kill switch) halt new exposure and are re-checked every cycle; they
        fail closed only after ``transient_grace_s`` (``strict``: at start-up, immediately)."""
        if not snap.is_demo:
            self._fail_closed("account_not_demo", now)
        active: dict[str, str] = {}
        if snap.reconciliation != "RECONCILED":
            active["reconciliation"] = f"reconciliation={snap.reconciliation}"
        if not snap.connected:
            active["disconnected"] = "broker_disconnected"
        if snap.kill_switch:
            active["stack_halt"] = "kill_switch_active"
        if strict:
            for reason in active.values():
                self._fail_closed(reason, now)
        else:
            self._clear_transient([k for k in ("reconciliation", "disconnected", "stack_halt") if k not in active])
            for key, reason in active.items():
                self._note_transient(key, reason, now)
        if snap.open_positions > 0 and not snap.all_positions_protected:
            self._unprotected_since = self._unprotected_since or now
            if (now - self._unprotected_since).total_seconds() >= self.cfg.unprotected_grace_s:
                self._fail_closed("unprotected_exposure", now)
        else:
            self._unprotected_since = None
        self._check_clock_reference(snap.server_time_utc, now)

    def _check_clock_reference(self, tick_time: datetime | None, now: datetime) -> None:
        """Local-vs-broker clock check that never mistakes quote staleness for clock skew.

        ``tick_time`` is the freshest tick over all configured markets. It is evidence about the local clock only
        while it keeps advancing (a paused market / weekend freezes it); then skew is checked in BOTH directions.
        Without a live reference the check is CLOCK_REFERENCE_UNAVAILABLE and the stale-feed / session / closed-market
        logic alone decides about exposure. A tick AHEAD of the local clock is impossible for a stale quote, so that
        direction is fatal with or without a live reference.
        """
        if tick_time is None:
            self._clock_reference = "UNAVAILABLE"
            return
        tick = tick_time.astimezone(UTC)
        if self._clock_ref_tick is not None and tick > self._clock_ref_tick:
            self._clock_ref_advanced_at = now
        if self._clock_ref_tick is None or tick > self._clock_ref_tick:
            self._clock_ref_tick = tick
        skew = (now - tick).total_seconds()  # positive: tick behind the local clock
        if -skew > self.cfg.max_clock_skew_s:
            self._clock_reference = "AHEAD"
            self._fail_closed(f"clock_anomaly: server skew {-skew:.0f}s ahead", now)
            return
        live = (
            self._clock_ref_advanced_at is not None
            and (now - self._clock_ref_advanced_at).total_seconds() <= self.cfg.clock_reference_window_s
        )
        if not live:
            self._clock_reference = "UNAVAILABLE"
            return
        self._clock_reference = "OK"
        if abs(skew) > self.cfg.max_clock_skew_s:
            self._clock_reference = "ANOMALY"
            self._fail_closed(f"clock_anomaly: server skew {abs(skew):.0f}s", now)

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
        if (
            self._idle_all and self._last_account is not None and self._last_account_at is not None
            and (now - self._last_account_at).total_seconds() < self.cfg.idle_account_check_s
            and not self._has_open_exposure()
        ):
            return  # idle cadence: account / reconcile checks are lowered (not stopped) while every market is closed
        snap = self.stack.account_snapshot()
        self._last_account = snap
        self._last_account_at = now
        self._clear_transient(["stack:guards"])
        self._check_account(snap, now)

    # ------------------------------------------------------------------------------------ feeds
    def _market_should_be_open(self, market: str, now: datetime) -> bool:
        """MarketSpec calendar: local weekday Mon-Fri and inside the cash session.  Unknown spec => True
        (conservative: a stale feed then counts).  Closed markets are IDLE, never 'stale'."""
        spec = self._spec(market)
        if spec is None:
            return True
        try:
            from demo.opportunity.clock import local_minute_of, local_of

            if local_of(spec, now).weekday() >= 5:
                return False
            cal = spec.calendar
            return cal.cash_open_min <= local_minute_of(spec, now) < cal.cash_close_min
        except Exception:
            return True

    def _refresh_feeds(self, now: datetime) -> list[str]:
        """Update freshness; return markets whose latest CLOSED M5 bar is new since last cycle.

        CLOSED is not BROKEN.  A stale feed is IDLE only if the MarketSpec calendar (local weekday + cash
        session) says the market is closed AND the broker shows no fresh quote (``quote_fresh_s``); a market
        that should be open and is stale is the fault (``STALE_FAULT``: counts towards the all-stale
        halt / exit).  A fresh quote proves the market trades whatever the calendar says.  Per-market state
        (FRESH | OPEN_OUT_OF_SESSION | CLOSED_IDLE | STALE_FAULT) goes to the heartbeat."""
        new: list[str] = []
        self._stale = set()
        idle: set[str] = set()
        src = self.stack.bar_source
        enabled = [m for m in self.cfg.markets if m not in self.disabled]
        for m in enabled:
            info: dict[str, Any] = {"bar_age_s": None, "quote_age_s": None, "stale": True}
            should_open = self._market_should_be_open(m, now)
            idle_state = "CLOSED_IDLE"
            op = self.cfg.operating_policy
            if op is not None:
                # Lane P: broker-session aware. A KNOWN daily broker pause is not a stale-feed fault; a genuinely
                # stale feed inside an OPEN broker session (also after the cash close) stays a real fault.
                session = op.session_state(m, now)
                if session is not None:
                    should_open = session == "OPEN"
                    if session == "KNOWN_SESSION_PAUSE":
                        idle_state = "KNOWN_SESSION_PAUSE"
            try:
                close = src.last_closed_bar_close_utc(m)
            except StackFailClosed:
                raise
            except Exception as exc:
                self._feed[m] = {**info, "error": f"{type(exc).__name__}: {exc}"}
                self._stale.add(m)
                if not should_open:
                    idle.add(m)
                    self._feed[m]["idle_market_closed"] = True
                    self._market_state[m] = idle_state
                else:
                    self._market_state[m] = "STALE_FAULT"
                continue
            q = None
            try:
                q = src.latest_quote(m)
            except StackFailClosed:
                raise
            except Exception:
                q = None
            quote_fresh = False
            if q is not None:
                info["quote_age_s"] = (now - q.ts_utc.astimezone(UTC)).total_seconds()
                quote_fresh = -self.cfg.clock_backward_tolerance_s <= info["quote_age_s"] <= self.cfg.quote_fresh_s
            if close is None:
                self._feed[m] = info
                self._stale.add(m)
                if not should_open and not quote_fresh:
                    idle.add(m)
                    info["idle_market_closed"] = True
                    self._market_state[m] = idle_state
                else:
                    self._market_state[m] = "STALE_FAULT"
                continue
            close = close.astimezone(UTC)
            age = (now - close).total_seconds()
            info["bar_age_s"] = age
            stale = age > self.cfg.stale_feed_s or age < -self.cfg.clock_backward_tolerance_s
            info["stale"] = stale
            self._feed[m] = info
            if stale:
                self._stale.add(m)
                if not should_open and not quote_fresh:
                    idle.add(m)
                    info["idle_market_closed"] = True
                    self._market_state[m] = idle_state
                else:
                    self._market_state[m] = "STALE_FAULT"
                    if not should_open and quote_fresh:
                        info["calendar_closed_but_quotes_live"] = True
                continue
            self._market_state[m] = "FRESH" if should_open else "OPEN_OUT_OF_SESSION"
            if self._last_close.get(m) != close or m in self._scan_pending:
                self._last_close[m] = close
                new.append(m)
        self._idle_all = bool(enabled) and len(idle) == len(enabled)
        expected = [m for m in enabled if m not in idle]
        if expected and all(m in self._stale for m in expected):
            self._all_stale_since = self._all_stale_since or now
            waited = (now - self._all_stale_since).total_seconds()
            if waited >= self.cfg.all_stale_grace_s:
                self._stale_halt_since = self._stale_halt_since or now  # HALT new exposure, keep managing
            if waited >= self.cfg.all_stale_exit_s or (self.cfg.all_stale_exit_s <= self.cfg.all_stale_grace_s and waited >= self.cfg.all_stale_grace_s):
                self._fail_closed("all_feeds_stale", now)
        else:
            self._all_stale_since = None
            self._stale_halt_since = None
        return new

    # ------------------------------------------------------------------------------- one cycle
    def run_cycle(self, now: datetime | None = None) -> None:
        now = now or self._clock()
        self._roll_day(now)
        if self.cfg.stop_file is not None and self.cfg.stop_file.exists() and not self.cfg.flatten_only:
            self.request_stop("stop_file")  # recovery mode: flattening is exposure-REDUCING, a STOP file must not prevent it
        new_bars: list[str] = []
        try:
            self._guards(now)
        except StackFailClosed as exc:
            self._stack_failure(exc, now, "guards")
        except Exception as exc:  # cannot verify the account -> fail closed
            self._fail_closed(f"guard_error: {type(exc).__name__}: {exc}", now)
        self._section(now, "manage", self._manage)
        if self.cfg.flatten_only:  # Lane R: no feeds, no scans, no shadow universe, no learning - only the sweep + the heartbeat
            self._heartbeat(now)
            return
        try:
            new_bars = self._refresh_feeds(now)
        except StackFailClosed as exc:
            self._stack_failure(exc, now, "feeds")
        except Exception as exc:
            self._note_error(now, f"feed_error: {type(exc).__name__}: {exc}")
        # Record-everything: bars are scanned (snapshot + decision persisted) whenever the runner has not
        # failed closed - also during a halt / transient condition.  ``_execute`` then cancels an accepted
        # intent with reason 'halted' (recorded, counterfactually labelled); nothing is lost silently.
        if self._oow is not None:
            self._oow.begin_cycle()
        if self.fail_reason is None and not self._stopping:
            for m in new_bars:
                if self.fail_reason is not None:
                    break
                self._section(now, f"scan:{m}", lambda n, m=m: self._scan_market(m, n), event_critical=False)
            if self.shadow_universe is not None and self.fail_reason is None:
                self._section(now, "shadow_universe", self._shadow_universe_cycle, event_critical=False)
        self._section(now, "periodic", self._periodic, event_critical=False)
        self._heartbeat(now)

    def _section(self, now: datetime, name: str, fn: Callable[[datetime], None], *, event_critical: bool = True) -> None:
        try:
            fn(now)
            self._clear_transient([f"stack:{name}"])
        except StackFailClosed as exc:
            self._stack_failure(exc, now, name)
        except (sqlite3.Error, OSError) as exc:
            self._persistence_failure(exc, now)
        except Exception as exc:
            text = f"{name}: {type(exc).__name__}: {exc}"
            if event_critical:
                self._fail_closed(f"unexpected_error {text}", now)  # broker/store state may diverge
            else:
                self._note_error(now, text)

    def _manage(self, now: datetime) -> None:
        pending: StackFailClosed | None = None
        events: list[ExecutionEvent] = []
        try:
            events += list(self.stack.poll_events())
            # Lane E1: deterministic exit-engine cycle (partials / tighten-only stops). Optional port method:
            # the real stack returns [] unless exit_policy == "staged"; stacks without it are untouched.
            manage_exits = None if self.cfg.flatten_only else getattr(self.stack, "manage_exits", None)
            if manage_exits is not None:
                events += list(manage_exits(now))
        except StackFailClosed as exc:
            if self.cfg.operating_policy is None:
                raise
            # Lane Z (C1): with an operating policy the zero-overnight sweep (inside on_clock) is exposure-REDUCING and must
            # still be attempted while the stack is latched fail-closed; the failure is re-raised right after it.
            pending = exc
        try:
            events += list(self.stack.on_clock(now))
        except StackFailClosed as exc:
            pending = pending or exc
        self._handle_events(events, now)
        self._note_eod(now)
        if pending is not None:
            raise pending

    def _eod_status(self) -> dict[str, Any] | None:
        if self.cfg.operating_policy is None:
            return None
        fn = getattr(self.stack, "eod_status", None)
        if fn is None:
            return None
        try:
            return dict(fn())
        except Exception:
            return None

    def _note_eod(self, now: datetime) -> None:
        """Never give up silently: an OVERDUE flatten (not flat at the deadline) is reported loudly, every 60 s."""
        eod = self._eod_status()
        if eod is None or eod.get("flatten_state") != "OVERDUE":
            return
        if self._eod_noted_at is None or (now - self._eod_noted_at).total_seconds() >= 60.0:
            self._eod_noted_at = now
            self._note_error(now, f"eod_flat_overdue: {eod.get('eod_detail')}")

    # ---------------------------------------------------------------------------- opportunities
    def _catchup_stats(self, market: str) -> dict[str, Any]:
        return self._catchup.setdefault(market, {
            "evaluated": 0, "live": 0, "skipped_closed": 0, "skipped_old": 0, "missed_found": 0, "last_catchup_utc": None,
        })

    def _pending_closes(self, market: str, newest: datetime) -> tuple[list[datetime], int]:
        """Closes (UTC) of the closed M5 bars to evaluate now, chronological, newest last, plus the number
        of bars skipped because they are older than the catch-up bound.  No pointer yet (first ever run on
        this store, or a legacy DB) -> only the newest bar: history is never replayed blindly."""
        ptr_s = self.store.get_bar_pointer(market)
        if ptr_s is None:
            return [newest], 0
        ptr = parse_utc(ptr_s)
        if newest <= ptr:
            return [], 0
        try:
            closes = sorted({t.astimezone(UTC) + _M5 for t, *_ in self._frame_rows(market)})
        except StackFailClosed:
            raise
        except Exception as exc:
            self._warnings.append(f"catchup_frame_unavailable:{market}:{type(exc).__name__}")
            closes = []
        pending = [c for c in closes if ptr < c <= newest]
        if not pending or pending[-1] != newest:
            pending.append(newest)
        floor = newest - timedelta(seconds=self.cfg.catchup_max_age_s)
        kept = [c for c in pending if c >= floor or c == newest]
        skipped = len(pending) - len(kept)
        if len(kept) > self.cfg.catchup_max_bars:
            skipped += len(kept) - self.cfg.catchup_max_bars
            kept = kept[-self.cfg.catchup_max_bars:]
        return kept, skipped

    def _scan_market(self, market: str, now: datetime) -> None:
        """Evaluate EVERY closed M5 bar since the persisted pointer, oldest first.  The newest bar is
        processed live (tradable) while it is at most one bar old; every older bar is a CATCH-UP bar:
        evaluated causally at its own close, recorded, counterfactually labelled, NEVER traded."""
        from demo.opportunity.engine import CatchupInfo

        newest = self._last_close.get(market)
        if newest is None:
            return
        st = self._catchup_stats(market)
        self._scan_pending.add(market)
        closes, skipped_old = self._pending_closes(market, newest)
        st["skipped_old"] += skipped_old
        for close in closes:
            live = close == newest and (now - close).total_seconds() <= self.cfg.live_max_age_s
            if close != newest and not self._market_should_be_open(market, close):
                st["skipped_closed"] += 1  # closed-market period: nothing to evaluate
                self.store.set_bar_pointer(market, _iso(close))
                continue
            if live:
                call: Callable[[], Any] = lambda: self.engine.on_m5_close(market, now)  # noqa: E731
                st["live"] += 1
            else:
                quote = None
                with contextlib.suppress(StackFailClosed, Exception):
                    quote = self.stack.bar_source.latest_quote(market)
                info = CatchupInfo(now, quote)
                call = lambda close=close, info=info: self.engine.on_m5_close(market, close, catchup=info)  # noqa: E731
                st["evaluated"] += 1
                st["last_catchup_utc"] = _iso(now)
            pairs = self._engine_bar(market, close, call, now)
            try:
                if pairs:
                    intents = {i.opportunity_id: i for i in self.engine.intents_for(list(pairs))}
                    for snap, dec in pairs:
                        if not live and not dec.accepted and "CATCHUP_MISSED" in dec.reasons:
                            st["missed_found"] += 1
                        self._process_pair(snap, dec, intents.get(snap.opportunity_id), now, catchup=not live)
            finally:
                self._release_seen()
            self.store.set_bar_pointer(market, _iso(close))  # only after the bar is fully processed
        self._oow_after_scan(market, newest, now)
        self._scan_pending.discard(market)

    def _release_seen(self) -> None:
        fn = getattr(self.engine, "release_seen", None)
        if fn is not None:
            fn()

    # ------------------------------------------------------------ Lane U2: measurement-only shadow
    def _broker_tradable(self, market: str, now: datetime) -> bool:
        """BROKER_TRADABLE: the feed is fresh (not stale/closed-idle) AND a fresh broker quote proves the market trades."""
        if self._market_state.get(market) not in ("FRESH", "OPEN_OUT_OF_SESSION") or market in self._stale:
            return False
        age = (self._feed.get(market) or {}).get("quote_age_s")
        return age is not None and -self.cfg.clock_backward_tolerance_s <= age <= self.cfg.quote_fresh_s

    def _oow_after_scan(self, market: str, newest: datetime, now: datetime) -> None:
        """OUT_OF_WINDOW_SHADOW pass for the LIVE bar (never catch-up bars). Contained: measurement must never stop trading."""
        oow = self._oow
        if oow is None or (now - newest).total_seconds() > self.cfg.live_max_age_s:
            return
        pairs = oow.scan(self.engine, market, newest, now, tradable=self._broker_tradable(market, now))
        try:
            for snap, dec in pairs:
                self._process_shadow_pair(snap, dec, now, "out_of_window")
        finally:
            self._release_seen()

    def _shadow_universe_cycle(self, now: datetime) -> None:
        su = self.shadow_universe
        pairs = su.scan_cycle(now)
        try:
            for snap, dec in pairs:
                self._process_shadow_pair(snap, dec, now, "shadow_universe")
        finally:
            su.release_seen()

    def _process_shadow_pair(self, snap: OpportunitySnapshot, dec: Decision, now: datetime, kind: str) -> None:
        """Persist a measurement-only (REJECTED) observation: snapshot + decision, nothing else. Hard guards: it can never be
        accepted, never reaches ``_execute`` / the stack, and a shadow-universe market is never a registry market."""
        if dec.accepted or (kind == "shadow_universe" and snap.market in self.cfg.markets):
            self._fail_closed(f"shadow_observation_invariant: {kind} {snap.market} accepted={dec.accepted}", now)
            return
        if snap.phase != self.cfg.phase:
            self._fail_closed(f"phase_mismatch: engine={snap.phase} runner={self.cfg.phase}", now)
            return
        try:
            self.store.record_snapshot(snap)
            self.store.record_decision(dec)
        except DemoStoreError as exc:
            self._note_error(now, f"shadow_duplicate_differs: {snap.opportunity_id}: {exc}")
            return
        self._shadow_counts[kind] = self._shadow_counts.get(kind, 0) + 1
        self._last_persist = _iso(now)
        self._funnel_dirty = True

    def _shadow_status(self) -> dict[str, Any]:
        su = self.shadow_universe
        return {
            "out_of_window_shadow": ({**self._oow.stats(), "recorded_total": self._shadow_counts["out_of_window"]}
                                     if self._oow is not None else {"enabled": False}),
            "shadow_universe": ({**su.stats(), "persisted_total": self._shadow_counts["shadow_universe"]}
                                if su is not None else {"enabled": False}),
        }

    def _engine_bar(self, market: str, close: datetime, call: Callable[[], Any], now: datetime) -> Any:
        """One engine evaluation with ONE retry.  A second failure is recorded in the store
        (``scan_errors``, status SCAN_ERROR) and the bar is skipped: auditable, never silent, and it cannot
        wedge the market.  Stack / persistence failures are not scan errors: they propagate."""
        last: Exception | None = None
        for _attempt in (1, 2):
            try:
                return call()
            except (StackFailClosed, sqlite3.Error, OSError):
                raise
            except Exception as exc:
                last = exc
                self._note_error(now, f"scan_error:{market}:{_iso(close)}: {type(exc).__name__}: {exc}")
                self._release_seen()
        assert last is not None
        self.store.record_scan_error(market, _iso(close), f"{type(last).__name__}: {last}")
        self._warnings.append(f"SCAN_ERROR:{market}:{_iso(close)}")
        return None

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

    def _process_pair(
        self, snap: OpportunitySnapshot, dec: Decision, intent: TradeIntent | None, now: datetime,
        *, catchup: bool = False,
    ) -> None:
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
        if not dec.accepted or self.cfg.mode not in ("demo-auto", "shadow"):
            return  # rejected -> counterfactual later (shadow-approved ones go through the DRY-RUN stack)
        if catchup:  # defence in depth: a catch-up bar is NEVER submitted, whatever the engine returned
            self._note_error(now, f"catchup_accepted_not_submitted: {snap.opportunity_id}")
            return
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
            "variant": sig.get("variant"),  # Lane Y: the family MODE (entry thesis) the exit profile router keys on
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

    def _exit_geometry(
        self, intent: TradeIntent, context: dict[str, Any] | None
    ) -> tuple[TradeIntent, dict[str, Any] | None, dict[str, Any] | None]:
        """Lane E2 plan producer: SHADOW family-vs-structure geometry + (staged) the ``exit_plan``.

        Only for a stack that exposes ``exit_plan_config`` (the real one; fakes are untouched). The family
        geometry stays the ACTIVE one unless the family / market opted in to ``structure`` (then the structural
        invalidation stop replaces the family stop BEFORE the intent is recorded and sized). Any failure here is
        logged in the shadow payload and never blocks the trade: the family geometry is the fallback."""
        cfg = getattr(self.stack, "exit_plan_config", None)
        if cfg is None:
            return intent, context, None
        ctx = dict(context or {})
        try:
            from demo import exit_profiles as xp
            from demo.execution.exit_manager import (
                EXIT_POLICY_PROFILES,
                EXIT_POLICY_STAGED,
                produce_exit_context,
            )

            policy_name = getattr(self.stack, "exit_policy", None)
            staged = policy_name == EXIT_POLICY_STAGED
            route = None
            if policy_name == EXIT_POLICY_PROFILES:
                # Lane Y: family/mode (entry thesis) -> ONE profile, frozen at entry. FIXED_1_5R-mapped families get NO plan
                # (unchanged fixed behaviour); the others get the plan of their profile on the SAME ExitEngine.
                signal_variant = (ctx.get("signal") or {}).get("variant") or ctx.get("variant")
                route = xp.route_for(ctx.get("family"), signal_variant)
                staged = route.profile in xp.ENGINE_PROFILES
            source = self.stack.bar_source
            frame = source.m5_frame(intent.market, cfg.bars)
            quote = source.latest_quote(intent.market)
            spread = float(quote.ask - quote.bid) if quote is not None and quote.valid else 0.0
            tick = getattr(self._spec(intent.market), "tick_size", None)
            signal_ctx = ctx.get("signal") or {}
            out = produce_exit_context(
                direction=intent.direction, entry_ref=intent.entry_ref, stop=intent.stop, target=intent.target,
                family=ctx.get("family"), market=intent.market, atr=ctx.get("atr"), frame=frame, spread=spread,
                tick_size=None if tick is None else float(tick), structure_levels=ctx.get("structure_levels"),
                target_is_structural=bool(signal_ctx.get("structural_target")), cfg=cfg, staged=staged, route=route,
            )
        except Exception as exc:  # never blocks the trade: the family geometry is the fallback
            return intent, context, {"error": f"{type(exc).__name__}:{exc}"[:200]}
        shadow = dict(out["shadow"])
        stop = out["structure_stop"]
        if stop is not None and intent.direction * (intent.entry_ref - stop) > 0 and stop != intent.stop:
            shadow["applied"] = {"family_stop": intent.stop, "structure_stop": stop}
            intent = dataclasses.replace(intent, stop=stop)
        if out["exit_plan"] is not None:
            ctx["exit_plan"] = out["exit_plan"]
            ctx["exit_meta"] = out["exit_meta"]
            shadow["exit_plan"] = out["exit_meta"]
        ctx["geometry_source"] = out["source"]
        if out.get("exit_profile") is not None:
            ctx["exit_profile"] = out["exit_profile"]
        return intent, ctx, shadow

    def _execute(self, intent: TradeIntent, now: datetime, context: dict[str, Any] | None = None) -> None:
        intent, context, geometry = self._exit_geometry(intent, context)
        try:
            self.store.record_intent(intent)  # PLANNED, durable BEFORE the stack sees it
        except DemoStoreError as exc:  # duplicate / immutable -> exactly-once: do nothing
            self._note_error(now, f"intent_rejected_by_store: {exc}")
            return
        if self.store.get_state(intent.intent_id) != PLANNED:
            return  # already handled (restart / duplicate)
        self._day_counts["intents"] += 1
        if geometry:
            with contextlib.suppress(Exception):  # shadow evidence only; never blocks a trade
                self.store.record_tca(intent.intent_id, geometry, "GEOMETRY")
        iid, ts = intent.intent_id, _iso(now)
        shadow = self.cfg.mode == "shadow"
        if self.cfg.mode not in ("demo-auto", "shadow"):  # defence in depth
            self.store.transition(iid, CANCELLED, detail={"reason": "unknown_mode"}, ts=ts)
            return
        if shadow and getattr(self.stack, "shadow", None) is not True:
            # shadow may only exercise a stack that is hard-guarded against order_send (dry_run)
            self.store.transition(iid, CANCELLED, detail={"reason": "shadow_stack_not_dry_run"}, ts=ts)
            self._fail_closed("shadow_stack_not_dry_run", now)
            return
        cancel: str | None = None
        if not self.can_trade():
            cancel = "halted"
        elif parse_utc(intent.valid_until_utc) < now:
            cancel = "expired"
        elif self.cfg.operating_policy is not None and self.cfg.operating_policy.flatten_active(now):
            cancel = G.R_FLATTEN_WINDOW  # mandatory flatten phase: no new exposure
        elif (
            self.cfg.operating_policy is not None and self.cfg.daily
            and not self.cfg.operating_policy.is_operating_day(now)
        ):
            cancel = G.R_OUTSIDE_OPERATING_DAY
        if cancel:
            self.store.transition(iid, CANCELLED, detail={"reason": cancel}, ts=ts)
            return
        if shadow:
            self.shadow_submit_count += 1  # dry-run risk/sizing/gate pipeline only; never a trade
        else:
            self._day_counts["trades"] += 1
            self.submit_count += 1
        try:
            events = self.stack.submit(intent, context=context)
        except StackFailClosed as exc:
            if str(exc).startswith(TRANSIENT_STACK_PREFIXES) and self.fail_reason is None:
                self._note_transient("stack:submit", f"submit: {exc}", now)  # halt-new-exposure, retry next signal
            else:
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
            if self.cfg.flatten_only:  # Lane R: the local store may be lost / behind; the broker close itself is what matters
                self._warnings.append(f"recovery_event_for_unknown_intent:{type(ev).__name__}:{ev.intent_id}")
                return
            self._fail_closed(f"event_for_unknown_intent:{ev.intent_id}", now)
            return
        intent = TradeIntent.from_dict({k: v for k, v in row.items() if k in {f.name for f in dataclasses.fields(TradeIntent)}})
        ts = _iso(now)
        state = row["state"]
        iid = ev.intent_id
        if self.cfg.mode == "shadow" and isinstance(ev, (Fill, ProtectionConfirmed, PositionClosed)):
            # a dry-run stack can never fill: an execution event in shadow is an invariant breach
            self._fail_closed(f"shadow_execution_event:{type(ev).__name__}:{iid}", now)
            return
        if isinstance(ev, Accepted):
            self._persist_risk_detail(iid, "ACCEPTED", ev.risk_detail, now)
            if state == PLANNED and self.cfg.mode == "shadow":
                self.store.record_risk(iid, RiskRecord(
                    equity=_f(ev.equity), risk_fraction=_f(ev.risk_fraction, intent.risk_fraction),
                    risk_budget=_f(ev.risk_budget), quantity=float(ev.quantity),
                    leverage=_f(ev.leverage), approved=True,
                ))
                self.store.transition(iid, RISK_APPROVED, ts=ts)
                # would-have-traded marker: NEVER SENT/FILLED; excluded from trade metrics by the funnel
                self.store.transition(iid, CANCELLED, detail={"reason": SHADOW_DRY_RUN}, ts=ts)
            elif state == PLANNED:
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
            elif code == G.R_OUTCOME_UNKNOWN and state in (RISK_APPROVED, SENT):
                # the order may exist at the broker: NEVER cancel; keep it non-terminal so a late
                # Fill / PositionClosed advances it and the outcome/learning record is written
                self.store.transition(iid, IN_DOUBT, detail={**info, "needs_manual_review": True}, ts=ts)
                self._warnings.append(f"in_doubt:{iid}:order_outcome_unknown")
                self.halt(f"order_outcome_unknown:{iid}", now)
            elif state in (RISK_APPROVED, SENT):
                self.store.transition(iid, SEND_FAILED if state == RISK_APPROVED else CANCELLED, detail=info, ts=ts)
        elif isinstance(ev, Fill):
            self._advance(iid, SENT, now)
            ex = self._execution_from_fill(intent, ev)
            self.store.record_execution(iid, ex)
            self.store.record_tca(iid, self._tca_entry(ev, intent), "ENTRY")
            self._advance(iid, FILLED, now)
            self._day_counts["filled"] += 1
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
    def _tca_entry(ev: Fill, intent: TradeIntent | None = None) -> dict[str, Any]:
        """Entry-side transaction-cost analysis of one fill (floats; price units unless noted).

        Decision-to-fill chain (``intent`` given): ``decision_price`` (engine ``entry_ref`` at decision) ->
        ``order_arrival_price`` (the executable quote the stack used at submit: ``Fill.reference_price``,
        else the ask/bid at send) -> ``requested_price`` (market order: NOT provided by the stack events -> None) ->
        ``actual_fill_price``.  Drift / shortfall are adverse-positive (long: higher is worse).  A field the
        stack events do not carry is None and listed in ``tca_missing_fields``."""
        d = intent.direction if intent is not None else None
        decision = None if intent is None else _num(intent.entry_ref)
        arrival = _num(ev.reference_price)
        if arrival is None and d is not None:
            arrival = _num(ev.ask_at_send if d > 0 else ev.bid_at_send)
        fill = _num(ev.price)
        risk = None if intent is None else abs(intent.entry_ref - intent.stop)
        drift = None if (d is None or decision is None or arrival is None) else d * (arrival - decision)
        shortfall = None if (d is None or decision is None or fill is None) else d * (fill - decision)
        chain = {
            "decision_price": decision, "order_arrival_price": arrival, "requested_price": None,
            "actual_fill_price": fill, "decision_to_arrival_drift": drift, "implementation_shortfall": shortfall,
            "implementation_shortfall_r": None if (shortfall is None or not risk) else shortfall / risk,
            "decision_to_arrival_drift_r": None if (drift is None or not risk) else drift / risk,
        }
        chain["tca_missing_fields"] = sorted(k for k, v in chain.items() if v is None and k in (
            "decision_price", "order_arrival_price", "requested_price", "actual_fill_price"))
        return {**chain, **DemoRunner._tca_entry_raw(ev)}

    @staticmethod
    def _tca_entry_raw(ev: Fill) -> dict[str, Any]:
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
        su = self.shadow_universe
        if su is not None and su.owns(market):  # Lane U2: shadow symbols are not in the stack registry; last scanned bars
            return su.frame_rows(market)
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
        path = self._path(intent.market, intent.direction, entry_at, closed_at)
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
                path=path,
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
        hint = getattr(ev, "exit_hint", None)
        censored = ev.exit_reason not in UNCENSORED_EXITS or bool(hint and hint not in UNCENSORED_EXITS)
        self.store.record_trade_tag(  # before the outcome: a censored exit never reaches alpha metrics
            iid, "STRATEGY", censored, exit_class=str(hint or ev.exit_reason), source="runner",
        )
        if censored:
            self._warnings.append(f"censored_exit:{iid}:{hint or ev.exit_reason}")
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
        self._record_outcome_extra(intent, ex.fill_price, exit_px, entry_at, closed_at, path, now)
        self._last_persist = ts
        self._refresh_cum_r()
        self._check_milestones()

    def _record_outcome_extra(
        self, intent: TradeIntent, fill_price: float, exit_px: float, entry_at: datetime, closed_at: datetime,
        path: list[PathPoint], now: datetime,
    ) -> None:
        """Timing analytics (signal age at fill, time to 0.25R / 0.5R / 1R, time without progress, MFE
        giveback).  Diagnostics only: a failure here never affects the outcome or trading."""
        try:
            extra = path_analytics(
                direction=intent.direction, entry_price=fill_price, initial_stop=intent.stop, entry_ts=_iso(entry_at),
                exit_price=exit_px, exit_ts=_iso(closed_at), path=path,
            )
            tp1, tp2 = self._structural_tps(intent.intent_id)
            extra["entry_exit"] = realised_entry_exit(  # Lane X: entry quality vs exit quality (None = unusable input)
                direction=intent.direction, entry_price=fill_price, initial_stop=intent.stop, entry_ts=_iso(entry_at),
                exit_price=exit_px, exit_ts=_iso(closed_at), path=path, final_gross_r=float(extra["final_gross_r"]),
                tp1=tp1, tp2=tp2,
            )
            snap = self.store.get_snapshot(intent.opportunity_id)
            if snap is not None:
                extra["signal_age_at_fill_s"] = (entry_at - parse_utc(snap.signal_ts_utc)).total_seconds()
                extra["signal_age_note"] = "runner-observed fill time minus signal (bar close) time"
            self.store.record_outcome_extra(intent.intent_id, extra)
        except (sqlite3.Error, OSError):
            raise
        except Exception as exc:
            self._note_error(now, f"outcome_extra:{intent.intent_id}: {type(exc).__name__}: {exc}")

    def _structural_tps(self, intent_id: str) -> tuple[float | None, float | None]:
        """Structural TP1 / TP2 prices of the recorded E2 GEOMETRY shadow (None when no geometry / no level)."""
        try:
            geo = self.store.get_tca(intent_id, "GEOMETRY") or {}
            structure = geo.get("structure") or {}
            out = []
            for key in ("tp1", "tp2"):
                level = structure.get(key)
                out.append(None if not level else float(level["price"]))
            return out[0], out[1]
        except (KeyError, TypeError, ValueError):
            return None, None

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
        written = label_counterfactuals(
            self.store, self._bars_provider(), _iso(now),
            shadow_exit_lab=self.cfg.shadow_exit_lab_enabled, shadow_stats=self._shadow_stats,
            shadow_cap=self.cfg.shadow_lab_max_per_cycle,
        )
        self._last_label = now
        self._last_label_n += len(written)
        if self.cfg.shadow_exit_lab_enabled:
            self._shadow_lab_trades(now)
        return len(written)

    def _shadow_lab_trades(self, now: datetime) -> int:
        """Lane W: shadow exit lab for CLOSED real trades whose flat deadline has passed (the bars after the real exit are
        needed to run the shadow policies to horizon / EOD).  Off the decision path, bounded per cycle, exception-contained:
        a failure here is counted and noted, never affects the recorded outcome or trading.  The result is merged ADDITIVELY
        into the existing ``outcome_extra`` JSON as ``shadow_exit_lab`` (insert-once)."""
        from demo.exit_policies import EntryInput
        from demo.shadow_exit_lab import failed_move_level, flat_deadline_bars, safe_evaluate_shadow

        done = 0
        try:
            outcomes = self.store.list_outcomes(self.cfg.phase, kind="strategy")
            for iid, oid, oc in reversed(outcomes):
                if done >= self.cfg.shadow_lab_max_per_cycle:
                    break
                if iid in self._shadow_lab_seen:
                    continue
                extra = self.store.get_outcome_extra(iid)
                if extra is None or extra.get("shadow_exit_lab") is not None:
                    self._shadow_lab_seen.add(iid)
                    continue
                intent = self.store.get_intent(iid)
                snap = self.store.get_snapshot(oid)
                ex = self.store.get_execution(iid)
                if intent is None or snap is None or ex is None or ex.fill_price is None:
                    continue
                signal = parse_utc(snap.signal_ts_utc)
                flat_s = intent.get("forced_flat_utc")
                flat = parse_utc(flat_s) if flat_s else signal + timedelta(hours=12)
                if now < flat + _M5:
                    continue  # the deadline (and the bar that carries it) has not passed yet: retry next cycle
                self._shadow_lab_seen.add(iid)
                rows = self._frame_rows(snap.market)
                post = [r for r in rows if r[0] >= signal]
                pre = [r for r in rows if r[0] < signal][-6:]
                if not post:
                    continue
                from demo.exit_policies import BarSeries

                def series(rs: list[Any]) -> BarSeries:
                    return BarSeries(tuple(r[0] for r in rs), tuple(r[1] for r in rs), tuple(r[2] for r in rs),
                                     tuple(r[3] for r in rs), tuple(r[4] for r in rs), tuple(r[5] for r in rs))

                tp1, tp2 = self._structural_tps(iid)
                entry = EntryInput(
                    entry_id=iid, market=snap.market, direction=int(intent["direction"]), fill=float(ex.fill_price),
                    stop=float(intent["stop"]), entry_ts=signal, atr=snap.market_state.atr, tp1=tp1, tp2=tp2, flat_utc=flat,
                    pre=series(pre) if len(pre) >= 3 else None, failed_move_level=failed_move_level(int(intent["direction"]), snap.signal),
                )
                live = {
                    "profile": str(getattr(self.stack, "exit_policy", None) or "fixed_1_5r"), "r": float(oc.gross_r),
                    "exit_reason": oc.exit_reason, "mfe_r": float(oc.mfe_r), "mae_r": float(oc.mae_r), "holding_s": float(oc.holding_s),
                }
                lab_out = safe_evaluate_shadow(entry, flat_deadline_bars(series(post), flat), live=live, stats=self._shadow_stats)
                if lab_out is not None and self.store.merge_outcome_extra(iid, "shadow_exit_lab", lab_out):
                    done += 1
        except (sqlite3.Error, OSError):
            raise
        except Exception as exc:
            self._note_error(now, f"shadow_exit_lab: {type(exc).__name__}: {exc}")
        return done

    def train_now(self, now: datetime) -> None:
        """Start the shadow-learning update in a SEPARATE thread on its own read connection (never on the
        runner / position-management thread).  At most one at a time, only while flat, exception-contained,
        timeout-flagged (a Python thread cannot be killed: a hung trainer is abandoned and reported)."""
        self._last_train = now
        if self.trainer is None:
            return
        if self._train_thread is not None and self._train_thread.is_alive():
            if self._train_started and (now - self._train_started).total_seconds() > self.cfg.train_timeout_s:
                self._note_error(now, f"trainer_timeout: running > {self.cfg.train_timeout_s:.0f}s (abandoned, not restarted)")
            return
        if self._has_open_exposure():
            return  # only when flat: position management never competes with the trainer
        self._train_started = now
        n_now = self.store.count_trades(self.cfg.phase)
        trainer, path = self.trainer, self.store.path

        def work() -> None:
            try:
                with DemoStore(path) as own:
                    report = trainer.update(own)
                self._train_result = {"ok": True, "report": report, "n_trades": n_now}
            except Exception as exc:  # shadow learning must never stop trading
                self._train_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        self._train_thread = threading.Thread(target=work, name="demo-trainer", daemon=True)
        self._train_thread.start()
        self._trained_at = n_now

    def join_training(self, timeout: float | None = None) -> None:
        t = self._train_thread
        if t is not None:
            t.join(timeout)

    def _collect_training(self, now: datetime) -> None:
        res, self._train_result = self._train_result, None
        if res is None:
            return
        if res.get("ok"):
            self._last_train_report = res.get("report")
        else:
            self._note_error(now, f"trainer: {res.get('error')}")

    def _periodic(self, now: datetime) -> None:
        self._collect_training(now)
        if (now - self._last_label).total_seconds() >= self.cfg.label_every_s:
            self.label_now(now)
        if self.trainer is not None:
            n = self.store.count_trades(self.cfg.phase)
            if (now - self._last_train).total_seconds() >= self.cfg.trainer_every_s or (
                n - self._trained_at >= self.cfg.trainer_every_trades
            ):
                self.train_now(now)

    # ----------------------------------------------------------------------------- heartbeat
    def _in_doubt_ids(self) -> list[str]:
        try:
            return [i["intent_id"] for i in self.store.list_intents(self.cfg.phase) if i["state"] == IN_DOUBT]
        except Exception:
            return []

    def funnel_summary(self, now: datetime, *, max_age_s: float = 60.0) -> dict[str, Any] | None:
        """Top-level rejection-funnel summary for the heartbeat (recomputed at most every ``max_age_s``)."""
        if (
            not self._funnel_dirty and self._funnel is not None and self._funnel_at is not None
            and (now - self._funnel_at).total_seconds() < max_age_s
        ):
            return self._funnel
        self._funnel_dirty = False
        try:
            from demo.funnel import compact as compact_funnel
            from demo.funnel import funnel

            full = funnel(self.store, self.stack, self.cfg.phase)
            self._funnel = {**full["summary"], "by_market": {m: {k: b[k] for k in ("opportunities", "engine_accepted", "stack_rejected", "traded", "shadow_would_trade", "temporary_otherwise_valid_blocked")} for m, b in full["by_market"].items()},
                            "stack_by_class": full["stack"]["by_class"], "engine_by_class": full["engine"]["by_class"],
                            "compact": compact_funnel(full)}
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
            "disclaimer": DISCLAIMER,
            "account_phase": self.account_info.get("account_phase"),
            "account_id_hash": self.account_info.get("account_id_hash"),
            "runner_mode": self.cfg.mode,
            "flatten_only": self.cfg.flatten_only,
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
            # ``trades_today`` = SUBMIT ATTEMPTS (legacy meaning, kept for compatibility; NOT fills). Lane U2 explicit keys:
            "trades_today": self._day_counts["trades"],
            "trades_today_semantics": "submit_attempts_not_fills",
            "intents_today": self._day_counts["intents"],
            "broker_trades_today": self._day_counts["filled"],
            **self._shadow_status(),
            "last_signal": self._last_signal,
            "last_fill": self._last_fill,
            "last_error": self._last_error,
            "learning_samples": n_trades,
            "champion": CHAMPION,
            "challengers": self._pred_status or {"status": self.learning_error or ("not_loaded" if self.predictor is None else "no_predictions_yet")},
            "mt5_connected": bool(acct.connected) if acct is not None else False,
            "feed": self._feed,
            "market_state": dict(self._market_state),
            "idle_all_markets_closed": self._idle_all,
            "clock_reference": self._clock_reference,
            "catchup": {m: dict(v) for m, v in self._catchup.items()},
            "stale_markets": sorted(self._stale),
            "disabled_markets": dict(self.disabled),
            "last_persistence_write": self._last_persist,
            "disk_free_bytes": free,
            "git_commit": self.commit,
            "halted": self.halt_reason,
            "transient_conditions": {k: {"since": _iso(v[0]), "reason": v[1]} for k, v in self._transient.items()},
            "halt_new_exposure": not self.can_trade(),
            "in_doubt_intents": self._in_doubt_ids(),
            "stale_halt_since": None if self._stale_halt_since is None else _iso(self._stale_halt_since),
            "trainer_running": bool(self._train_thread is not None and self._train_thread.is_alive()),
            "fail_closed": self.fail_reason,
            "stop_reason": self.stop_reason,
            "warnings": self._warnings[-20:],
            "milestones_emitted": list(self.milestones),
            "calendar_status": {r.market: r.calendar_status for r in self.clock_rows},
            **self._operating_status(now),
        }

    def _operating_status(self, now: datetime) -> dict[str, Any]:
        op = self.cfg.operating_policy
        if op is None:
            return {}
        eod = self._eod_status() or {}
        return {
            "operating_policy": op.describe(),
            "operating_day": op.is_operating_day(now),
            "daily_mode": self.cfg.daily,
            "flatten_state": eod.get("flatten_state", "UNKNOWN"),
            "eod_flat_confirmed_utc": eod.get("eod_flat_confirmed_utc"),
            "eod_detail": eod.get("eod_detail"),
            "eod_own_positions_open": eod.get("eod_own_positions_open"),
            "eod_foreign_positions": eod.get("eod_foreign_positions", []),
        }

    def _heartbeat(self, now: datetime, *, alive: bool = True) -> None:
        try:
            monitor.write_heartbeat(self.cfg.heartbeat_path, self.status(now, alive=alive))
            self._hb_fail_since = None
        except OSError as exc:
            # A single failed write (a reader briefly holding the file on Windows) must not stop
            # trading; PERSISTENT inability to report health (> 120 s) does.
            since = getattr(self, "_hb_fail_since", None)
            if since is None:
                self._hb_fail_since = now
                self._note_error(now, f"heartbeat_write_failed (transient): {exc}")
            elif (now - since).total_seconds() > 120.0:
                self._fail_closed(f"heartbeat_write_failed: {exc}", now)

    # ----------------------------------------------------------------------------- main loop
    def _eod_shutdown_ready(self, now: datetime) -> bool:
        """--daily: past the 22:00 Berlin deadline AND broker flat confirmed by the stack AND reconciled at the
        account level AND no open / in-doubt intent left. Only then may the day be finalized (exit 0)."""
        op = self.cfg.operating_policy
        if not self.cfg.daily or op is None or self.fail_reason is not None or not op.deadline_passed(now):
            return False
        eod = self._eod_status()
        acct = self._last_account
        if eod is None or eod.get("flatten_state") != "FLAT_CONFIRMED" or not eod.get("eod_flat_confirmed_utc"):
            return False
        if acct is None or not acct.connected or acct.reconciliation != "RECONCILED" or acct.open_positions:
            return False
        if self._last_account_at is None or (now - self._last_account_at).total_seconds() > max(
            2 * self.cfg.idle_account_check_s, 120.0
        ):
            return False
        try:
            return not self.store.recover_open_intents() and not self._in_doubt_ids()
        except Exception:
            return False

    def _recovery_flat_ready(self, now: datetime) -> bool:
        """--flatten-only: the sweep confirmed the broker flat for our magic (FLAT_CONFIRMED, 0 own positions, no OPEN registry row),
        the account is connected + RECONCILED with a fresh snapshot, and no open / in-doubt intent is left in the store.
        Foreign / canary positions do not count (they are reported, never closed)."""
        if not self.cfg.flatten_only:
            return False
        eod = self._eod_status()
        acct = self._last_account
        foreign = list((eod or {}).get("eod_foreign_positions") or [])
        # A foreign / manual position makes the account-level reconciliation MISMATCH (fail-closed reason ``reconciliation=...``).
        # Our own exposure is verified flat from the broker's position list by magic, independently of that state, so this single
        # fail reason does not keep the recovery alive (any other fail reason still does).
        foreign_recon_only = bool(foreign) and (self.fail_reason or "").startswith("reconciliation=")
        if self.fail_reason is not None and not foreign_recon_only:
            return False
        if eod is None or eod.get("flatten_state") != "FLAT_CONFIRMED" or not eod.get("eod_flat_confirmed_utc"):
            return False
        if int(eod.get("eod_own_positions_open") or 0) != 0:
            return False
        if acct is None or not acct.connected or (acct.reconciliation != "RECONCILED" and not foreign):
            return False
        if acct.open_positions > len(foreign):
            return False  # the account snapshot predates the closes: wait for a fresh one (the next cycle's guard) before exiting
        if self._last_account_at is None or (now - self._last_account_at).total_seconds() > max(
            2 * self.cfg.idle_account_check_s, 120.0
        ):
            return False
        try:
            return not self.store.recover_open_intents() and not self._in_doubt_ids()
        except Exception:
            return False

    def _should_exit(self, now: datetime) -> bool:
        if self._stopping:
            return not self._stop_must_finish_flatten(now)
        if self._eod_shutdown_ready(now):
            self.request_stop("eod_flat_shutdown")
            return True
        if self._recovery_flat_ready(now):
            self.request_stop(RECOVERY_STOP_REASON)
            return True
        if self.fail_reason is not None:
            # keep managing exits while a position may still be open, up to manage_after_halt_s
            if self._halted_since is None or not self._has_open_exposure():
                return True
            return (now - self._halted_since).total_seconds() >= self.cfg.manage_after_halt_s
        return False

    def _flatten_pending(self) -> bool:
        eod = self._eod_status() or {}
        try:
            own = int(eod.get("eod_own_positions_open") or 0)
        except (TypeError, ValueError):
            own = 0
        return eod.get("flatten_state") in ("WINDOW", "OVERDUE") or own > 0 or self._has_open_exposure()

    def _stop_must_finish_flatten(self, now: datetime) -> bool:
        """A STOP inside the flatten window with own exposure pending does NOT end the process: the sweep must finish
        (bounded by ``stop_flatten_grace_s`` after the deadline), otherwise a STOP file at 21:58 leaves a position
        overnight.  Flat confirmed / outside the window / grace over -> the stop proceeds."""
        op = self.cfg.operating_policy
        if op is None or self.stop_reason in ("eod_flat_shutdown", RECOVERY_STOP_REASON):
            return False
        if not op.flatten_active(now) and not self.cfg.flatten_only:  # recovery mode: the sweep is always "in the window"
            return False
        if (now - op.deadline_utc(op.day_of(now))).total_seconds() > self.cfg.stop_flatten_grace_s:
            return False
        if not self._flatten_pending():
            return False
        if not self._stop_deferral_noted:
            self._stop_deferral_noted = True
            self._note_error(now, f"stop_deferred_flatten_pending: stop_reason={self.stop_reason}; finishing the sweep first")
        return True

    def _has_open_exposure(self) -> bool:
        """Open registry / store intents ARE exposure whether or not the broker connection is up right now (a
        disconnected runner with a position must keep trying, not exit).  Own positions the stack reports count too."""
        try:
            if self.store.recover_open_intents():
                return True
        except Exception:
            return False
        acct = self._last_account
        return bool(acct is not None and acct.connected and acct.open_positions)

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
            self._sleep(self._sleep_s())
        return self.shutdown()

    def _sleep_s(self) -> float:
        """Poll interval; exponential backoff (bounded) while a transient condition is being re-checked."""
        base = min(self.cfg.poll_interval_s, 30.0)
        if self._idle_all and not self._transient and self._stale_halt_since is None:
            self._transient_cycles = 0
            return max(base, min(self.cfg.idle_poll_interval_s, 60.0))  # heartbeat must stay fresher than 90 s
        if self._transient or self._stale_halt_since is not None:
            self._transient_cycles += 1
            return min(base * (2 ** min(self._transient_cycles, 8)), max(base, self.cfg.transient_backoff_max_s))
        self._transient_cycles = 0
        return base

    def shutdown(self) -> int:
        """Orderly stop: no new exposure, open positions stay protected at the broker (NOT flattened;
        ``StackPort`` has no flatten call), final events/labels/report, stack.stop(), final heartbeat."""
        now = self._clock()
        self._stopping = True
        self.halt(self.fail_reason or self.stop_reason or "shutdown", now)
        self._section(now, "final_manage", self._manage)
        op = self.cfg.operating_policy
        if op is not None and op.flatten_active(now) and self._flatten_pending():
            self._warnings.append(
                f"overnight_exposure_at_shutdown: flatten window, own exposure still open ({self.stop_reason or self.fail_reason})"
            )
            self._note_error(now, "overnight_exposure_at_shutdown")
        if self.cfg.forced_flat_on_shutdown:
            flatten = getattr(self.stack, "flatten_all", None)
            if flatten is None:
                self._warnings.append(
                    "forced_flat_on_shutdown_unsupported: StackPort has no flatten call; "
                    "open positions stay protected by their broker-side stops"
                )
            else:
                try:
                    self._handle_events(list(flatten("shutdown")), now)
                except Exception as exc:
                    self._note_error(now, f"forced_flat_on_shutdown: {type(exc).__name__}: {exc}")
        self.join_training(2.0)
        for name, fn in (
            ("final_label", (lambda: None) if self.cfg.flatten_only else (lambda: self.label_now(now))),
            ("final_report", self._final_report),
        ):
            try:
                fn()
            except Exception as exc:
                self._note_error(now, f"{name}: {type(exc).__name__}: {exc}")
        with contextlib.suppress(Exception):
            self.stack.stop()
        recovered = self.stop_reason == RECOVERY_STOP_REASON  # Lane R: own exposure confirmed flat (foreign-only recon mismatch tolerated)
        self.exit_code = EXIT_FAIL_CLOSED if self.fail_reason and not recovered else EXIT_OK
        self._heartbeat(self._clock(), alive=False)
        return self.exit_code

    def _final_report(self) -> None:
        from demo.report import write_report

        write_report(self.store, self.cfg.reports_dir, self.cfg.phase, tag="final")


# ------------------------------------------------------------------------------------ factory
RECOVERY_STOP_REASON = "eod_recovery_flat_confirmed"


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
    forced_flat_on_shutdown: bool = False,
    stack_kwargs: Mapping[str, Any] | None = None,
    account_phase: str | None = None,
    phase2_markets: Sequence[str] | None = None,
    exit_policy: str = "fixed_1_5r",
    exit_plan: Any | None = None,
    operating_policy: Any | None = None,
    daily: bool = False,
    out_of_window_shadow: bool | None = None,
    shadow_exit_lab: bool | None = None,
    shadow_universe: str | Sequence[str] | None = None,
    shadow_source_factory: Callable[[Any, Mapping[str, str]], Any] | None = None,
    signal_sequence_metrics: bool = True,
    flatten_only: bool = False,
) -> DemoRunner:
    """Wire the runner to the REAL ``Mt5DemoStack``.

    ``learning``: ``None`` (default) means ON in ``shadow`` but OFF in ``demo-auto``; pass ``True`` (CLI
    ``--learning``) to opt in.  ``forced_flat_on_shutdown`` (default off) only takes effect if the stack
    offers ``flatten_all`` (``StackPort`` has none: positions then stay broker-protected; documented).

    * ``shadow``     -> ``Mt5DemoStack(dry_run=True)``  (``order_send`` is hard-guarded, never reachable);
    * ``demo-auto``  -> ``Mt5DemoStack(dry_run=False)`` (ActivTrades DEMO only; the stack itself verifies
      DEMO / expected login / server / netting / leverage before it can send anything).

    The real MT5 client and the attach-only connection config are obtained ONLY here, lazily (never at
    module import), and only when no ``stack_factory`` is injected.  ``MT5_ALLOW_ACCOUNT_LOGIN=1`` is
    refused (``LiveStackRefused``).  State (adapter DB + intent registry) lives in ``<artifacts>/stack``.
    ``stack_kwargs`` (tests only: ``lock_path`` / ``config`` / ``now``) is forwarded to ``Mt5DemoStack``.
    ``stack_factory(dry_run=..., state_dir=..., markets=...)`` is the test seam (FakeStack etc.); with it
    the real client is never touched.  Raises ``LiveStackUnavailable`` if the real stack / MT5 package /
    connection config is not usable in this checkout.

    ``operating_policy`` (Lane P): a ``demo.opportunity.operating_policy.OperatingPolicy`` (the CLI passes the
    versioned ``configs/live_operating_policy.toml``); None (library default) = behaviour exactly as before.  It is
    given to the engine (entry-window overlay + forced-flat), the real stack (flatten sweep + entry gate) and the
    runner (broker-session aware feed semantics, flatten heartbeat).  ``daily`` = exit 0 with stop_reason
    ``eod_flat_shutdown`` after the deadline once broker-flat + reconciled; refuses entries outside the operating day.

    ``phase2_markets`` (Lane M2): the Phase-2 markets opted in (default ``None`` = the ``enabled`` flags of
    ``configs/markets_phase2/enablement.toml``, all false as committed). With none enabled the frozen production spec v1
    and the five-market stack are used exactly as before; with any enabled, the strict-superset spec v1.1 is used, the
    markets are added to the DEMO registry and each one is verified by its own start-up preflight (a failing market is
    disabled alone, see ``Mt5DemoStack.disabled_markets``)."""
    from demo.opportunity.engine import OpportunityEngine
    from demo.opportunity.production_spec import load_production_spec_for
    from markets.phase2 import PHASE2_MARKETS, flag_enabled_markets

    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if exit_policy not in ("fixed_1_5r", "staged", "staged_profiles"):
        raise LiveStackRefused(f"unknown exit_policy {exit_policy!r} (fixed_1_5r | staged | staged_profiles)")
    if os.environ.get("MT5_ALLOW_ACCOUNT_LOGIN") == "1":
        raise LiveStackRefused("MT5_ALLOW_ACCOUNT_LOGIN=1 is not permitted; the DEMO trader only attaches")
    art = Path(artifacts_dir or "artifacts/demo_trader")
    extra = tuple(phase2_markets) if phase2_markets is not None else flag_enabled_markets()
    unknown_extra = [m for m in extra if m not in PHASE2_MARKETS]
    if unknown_extra:
        raise LiveStackRefused(f"unknown Phase-2 market(s) {unknown_extra}")
    production = load_production_spec_for(extra)
    if markets:
        names = tuple(markets)
        not_enabled = [m for m in names if m in PHASE2_MARKETS and m not in extra]
        if not_enabled:
            raise LiveStackRefused(
                f"{not_enabled} not enabled: set enabled = true in configs/markets_phase2/enablement.toml first"
            )
    else:
        names = tuple(m for m in production.market_names() if m not in PHASE2_MARKETS or m in extra)
    state_dir = art / "stack"
    dry_run = mode == "shadow"
    if stack_factory is None:
        try:
            from adapters.activtrades_mt5.real_client import get_real_client
            from adapters.config import MT5ConfigError, load_attach_only_config
            from demo.execution.live import Mt5DemoStack
            from demo.execution.market_config import load_demo_market_specs_for
        except ImportError as exc:
            raise LiveStackUnavailable(
                f"the real Mt5DemoStack cannot be imported in this checkout ({type(exc).__name__}: {exc})"
            ) from exc
        try:
            connection = load_attach_only_config()
        except MT5ConfigError as exc:
            raise LiveStackUnavailable(f"MT5 attach-only configuration unusable: {exc}") from exc
        all_specs = load_demo_market_specs_for(extra)
        missing = [m for m in names if m not in all_specs]
        if missing:
            raise LiveStackUnavailable(f"no checked-in DEMO market config for {missing}")
        try:
            client = get_real_client()
        except ImportError as exc:
            raise LiveStackUnavailable(f"MetaTrader5 package not importable: {exc}") from exc
        state_dir.mkdir(parents=True, exist_ok=True)
        art.mkdir(parents=True, exist_ok=True)
        kwargs = dict(stack_kwargs or {})
        if "config" not in kwargs:
            from demo.execution.live import StackConfig

            # The attached terminal reports account.server 'ActivTradesEU-Server' (observed on the real
            # DEMO terminal 2026-09-30, trade_mode 0) while the .env display value differs. DEMO-ness is
            # proven by trade_mode == 0 + the expected login, never by the server name; the server is
            # pinned as an identity tripwire (override: DEMO_TRADER_EXPECTED_SERVER).
            # Lane E2: ``exit_policy`` (DEFAULT fixed_1_5r = unchanged behaviour) / ``staged`` (ExitEngine manages
            # partials, tighten-only stops, structure trailing and engine exits) and the optional ExitPlanConfig
            # (geometry source family|structure, stage fractions). Both are stack configuration, not sizing.
            exit_kw: dict[str, Any] = {}
            if exit_policy in ("staged", "staged_profiles"):
                from demo.execution.exit_manager import default_staged_exit_policy

                exit_kw.update(exit_policy=exit_policy, staged_exit=default_staged_exit_policy())
            if exit_plan is not None:
                exit_kw["exit_plan"] = exit_plan
            kwargs["config"] = StackConfig(
                expected_server=os.environ.get("DEMO_TRADER_EXPECTED_SERVER", EXPECTED_DEMO_SERVER),
                **exit_kw,
                operating_policy=operating_policy,
                flatten_only=flatten_only,
            )
        stack: StackPort = Mt5DemoStack(  # type: ignore[assignment]
            client=client, connection=connection, state_dir=state_dir,
            market_specs=all_specs,  # the stack needs the FULL universe (symbol registry cross-check); ``names`` only limits what the runner scans
            extra_markets=extra,  # enabled Phase-2 markets: registered + individually preflighted by the stack
            dry_run=dry_run, **kwargs,
        )
    else:
        art.mkdir(parents=True, exist_ok=True)
        stack = stack_factory(dry_run=dry_run, state_dir=state_dir, markets=names)
    store = DemoStore(db_path or art / "demo.sqlite")
    seq = None
    if signal_sequence_metrics:  # Lane U2 (C): measure-only same-zone / flip / whipsaw metrics inside snapshot.signal
        from demo.sequence_metrics import SequenceTracker

        seq = SequenceTracker()
    engine = OpportunityEngine(
        stack.bar_source,
        production=production,
        phase=phase,  # type: ignore[arg-type]
        seen_store=StoreSeenAdapter(store),
        operating=operating_policy,
        sequence_tracker=seq,
    )
    # Lane U2: opt-in shadow collection (both OFF unless asked; never trades; lives in THIS process = the MT5 lock holder)
    su_specs: dict[str, Any] = {}
    su_pending: set[str] = set()
    if shadow_universe:
        from demo.shadow_universe import select_shadow_markets

        su_specs, su_pending = select_shadow_markets(shadow_universe)
    if flatten_only:  # Lane R: recovery never learns, never shadows, never scans
        learning, su_specs, su_pending = False, {}, set()
    if learning is None:
        learning = mode == "shadow"
    predictor, trainer, err = load_learning(art / "models", learning)
    cfg = RunnerConfig(mode=mode, phase=phase, markets=names, artifacts_dir=art, learning=learning,
                       forced_flat_on_shutdown=forced_flat_on_shutdown, account_phase=account_phase,
                       operating_policy=operating_policy, daily=daily, flatten_only=flatten_only,
                       out_of_window_shadow_enabled=False if flatten_only else (DEFAULT_OUT_OF_WINDOW_SHADOW if out_of_window_shadow is None else bool(out_of_window_shadow)),
                       shadow_exit_lab_enabled=False if flatten_only else (DEFAULT_SHADOW_EXIT_LAB if shadow_exit_lab is None else bool(shadow_exit_lab)),
                       shadow_universe=tuple(sorted(su_specs)))
    runner = DemoRunner(
        stack, engine, store, config=cfg, predictor=predictor, trainer=trainer,
        learning_error=err, production=production,
    )
    if su_specs:
        from demo.shadow_universe import ShadowUniverseScanner, make_shadow_live_source
        from markets.shadow import PRODUCTION_BROKER_SYMBOLS

        symbols = {m: sp.broker_symbol for m, sp in su_specs.items()}
        src = (shadow_source_factory or make_shadow_live_source)(stack, symbols)
        runner.shadow_universe = ShadowUniverseScanner(
            specs=su_specs, source=src, phase=phase, seen_store=StoreSeenAdapter(store), pending=su_pending,
            forbidden=(*names, *production.market_names(), *getattr(stack, "markets", ()), *PRODUCTION_BROKER_SYMBOLS),
            max_symbols_per_cycle=cfg.shadow_universe_max_symbols_per_cycle, budget_s=cfg.shadow_universe_budget_s,
            sequence_tracker=seq,
        )
    return runner
