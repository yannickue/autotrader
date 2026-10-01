# ruff: noqa: E501
"""E2 EXECUTION-CONTRACT CANARY (ActivTrades DEMO only, bounded, fully reduce-only after the entry).

PURPOSE. The ``staged`` exit policy (partial reduce-only closes + tighten-only stop modifications) must not be
enabled before the REAL broker is proven to honour exactly that contract.  This script opens ONE tiny position
(2 x the broker minimum lot) through the stack's normal EntryJob path with a staged ``exit_plan`` and then walks the
SAME adapter paths the staged exit policy uses in production (``ReduceJob`` / ``ModifyStopJob`` / ``_flatten`` ->
``emergency_close``), asserting BROKER truth after every step.  It is an execution-contract test only: no strategy,
no alpha conclusion.  No new execution path exists in this file.

MODES
  --fake                       FakeMT5Broker (netting account fake of the test harness).  Used by tests / CI.
  --live                       the real ActivTrades DEMO terminal, attach-only (never logs in), requires
                               ``--confirm-demo-canary=I-AUTHORIZE-ACTIVTRADES-DEMO-CANARY-ONLY``.
  --dry-run-plan               prints the exact steps and the computed lot / stop numbers and places NOTHING.
                               Alone: purely static (checked-in market spec).  With --live / --fake: connects
                               read-only (shadow stack, ``order_send`` is hard-guarded), runs every refusal check and
                               prints the calibrated sizes.  No confirmation flag needed (nothing can be sent).

REFUSALS (exit 2, nothing sent): missing confirm flag; MT5_ALLOW_ACCOUNT_LOGIN=1; artifacts dir != artifacts/e2_canary
(live); another runner / supervisor holds its lock or a fresh heartbeat exists; the MT5 terminal lock is busy; the
account is not DEMO (trade_mode 0), not the expected login/server, or its account_id_hash differs from the one the
trader is bound to; market not tradable / quote not fresh / broker order_check refuses; ANY pre-existing position or
order on the account (the symbol in particular); sizing cannot be calibrated to exactly 2 x min lot.

ALWAYS: try/finally + SIGINT/SIGTERM + a hard overall timeout.  On any failure / exception / timeout the canary
position is flattened reduce-only through the existing verified path (``stack._flatten``; fallback
``emergency_close`` by ticket; last resort a recovery stack) and the residual broker state is reported.  Only
positions carrying the canary magic are ever touched.

Exit codes: 0 EXECUTION_CONTRACT_PASS | 1 FAIL(step n, reason) | 2 REFUSED | 3 internal error |
            4 exposure may remain at the broker (MANUAL ACTION REQUIRED).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "src"), str(REPO_ROOT / "scripts" / "autostart")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

D = Decimal

CONFIRM_FLAG = "--confirm-demo-canary"
CONFIRM_VALUE = "I-AUTHORIZE-ACTIVTRADES-DEMO-CANARY-ONLY"
CANARY_MAGIC = 740_099  # StackConfig.canary_magic of the TRADER: its accounting censors this magic (F2 convention)
UNUSED_CANARY_MAGIC = 740_098  # the canary stack's own "canary_magic": distinct so its OWN positions are "own"
TRADE_TYPE_TAG = "EXECUTION_CANARY"
REQUIRED_ARTIFACTS = REPO_ROOT / "artifacts" / "e2_canary"
TRADER_ARTIFACTS = REPO_ROOT / "artifacts" / "demo_trader"
DEFAULT_MARKET = "EURUSD"
DEFAULT_TIMEOUT_S = 120.0
SIZE_MULTIPLE = 2  # total canary size = SIZE_MULTIPLE x broker min lot (so a partial exists)
STRUCTURAL_PLACEHOLDER_FRACTION = D("0.0015")  # initial-stop distance placeholder: 0.15 % of price (~17 pips EURUSD)
MAX_NOTIONAL_EUR = D("30000")  # refuse sizes whose notional exceeds this (guards against a huge min lot)
STATIC_PLAN_STEPS = (
    "1  open 2 x min lot LONG through the normal EntryJob path (staged exit_plan: TP1 price stage + runner, no broker TP)",
    "2  broker protective stop exists (sl>0, below entry, volume covers the full position, magic = canary magic)",
    "3  partial reduce-only close of 1 x min lot through ReduceJob",
    "4  broker remaining volume == local (Nautilus) remaining volume == expected remaining",
    "5  protection stays correct after the partial (broker SL unchanged and present, stop child qty == remaining)",
    "6  stop TIGHTENS through ModifyStopJob (closer to price, still valid vs stops_level; broker sl changes)",
    "7  stop CANNOT LOOSEN (widening refused locally, broker sl unchanged)",
    "8  second staged partial if the remaining volume allows (else NOT_APPLICABLE: it ends in the final close)",
    "9  final reduce-only close: broker quantity 0, no opposite position, no orphan order",
    "10 stop the stack, start a NEW stack on the same state dir: RECONCILED, flat, no orphans, registry rows terminal",
)
STEP_NAMES = (
    "entry_staged_plan", "broker_protective_stop", "partial_reduce", "volume_broker_eq_local",
    "protection_after_partial", "stop_tighten", "stop_cannot_loosen", "second_partial",
    "final_close_flat", "restart_reconcile",
)

_SECRET_KEYS = ("password", "passwd", "token", "secret", "api_key", "apikey", "login", "credential")


class Refused(Exception):
    """A refusal condition: nothing was (or will be) sent."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class Aborted(Exception):
    """Timeout or signal: the sequence stops, the finally-flatten runs."""


class StepFail(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def _round_tick(value: Decimal, tick: Decimal, mode: str) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING if mode == "up" else ROUND_FLOOR) * tick


def _dec(value: Any) -> Decimal:
    return D(str(value))


def _scrub(obj: Any) -> Any:
    """Recursively drop anything that looks like a secret (defence in depth: none is ever put in)."""
    if isinstance(obj, Mapping):
        return {k: _scrub(v) for k, v in obj.items() if not any(s in str(k).lower() for s in _SECRET_KEYS)}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def login_hash(login: Any) -> str:
    return hashlib.sha256(str(login).encode()).hexdigest()[:16]


def compute_geometry(
    *, bid: Decimal, ask: Decimal, tick: Decimal, point: Decimal, stops_level_pts: int, freeze_level_pts: int = 0,
) -> dict[str, Decimal]:
    """Initial stop distance (documented): ``max(10 x spread, 3 x stops_level points, 0.15 % of price)`` (+ 3 x freeze level),
    rounded UP to the tick, so the stop cannot trigger during a ~1 minute canary and is valid vs the broker minimum.
    The TP1 plan stage sits 3 x that distance above the entry (never reached; no broker TP is produced for a TP1+runner
    plan, see ``exit_manager.broker_target_for_staged``)."""
    spread = ask - bid
    broker_min = 3 * D(stops_level_pts) * point
    freeze_min = 3 * D(freeze_level_pts) * point
    structural = STRUCTURAL_PLACEHOLDER_FRACTION * ask
    dist = _round_tick(max(10 * spread, broker_min, freeze_min, structural), tick, "up")
    stop = _round_tick(ask - dist, tick, "down")
    return {
        "spread": spread, "stop_distance": ask - stop, "initial_stop": stop,
        "tp1_price": _round_tick(ask + 3 * dist, tick, "up"),
        "broker_min_distance": broker_min, "structural_placeholder": structural, "ten_spreads": 10 * spread,
    }


def compute_tighten_stop(
    *, bid: Decimal, current_stop: Decimal, initial_distance: Decimal, tick: Decimal, point: Decimal,
    stops_level_pts: int, freeze_level_pts: int, spread: Decimal,
) -> Decimal | None:
    """A stop closer to price than ``current_stop`` but still valid (>= 2 x stops_level / freeze from the bid).
    ``None`` if the market moved so that no valid tighter level exists."""
    keep = max(
        D("0.6") * initial_distance, 5 * spread, 2 * D(stops_level_pts) * point + 2 * tick,
        2 * D(freeze_level_pts) * point + 2 * tick,
    )
    new = _round_tick(bid - keep, tick, "up")
    if new < current_stop + 2 * tick:
        return None
    if bid - new < (D(stops_level_pts) + D(freeze_level_pts)) * point + tick:
        return None
    return new


# ---------------------------------------------------------------------------------------------
# environment / configuration
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class CanaryEnv:
    """Everything the canary needs that differs between --fake (tests) and --live."""

    mode: str  # fake | live
    client: Any
    connection: Any
    artifacts_dir: Path
    trader_artifacts_dir: Path
    mt5_lock_path: Path | None  # None = the terminal's default lock
    expected_account_hash: str | None
    expected_server: str | None
    market: str = DEFAULT_MARKET
    timeout_s: float = DEFAULT_TIMEOUT_S
    confirm: str | None = None
    stack_overrides: dict[str, Any] = field(default_factory=dict)  # tests: faster polling
    hooks: dict[str, Callable[[], None]] = field(default_factory=dict)  # TEST SEAM ONLY: after_action_<n> / before_restart
    now: Callable[[], datetime] = _utcnow
    login_env: str | None = field(default_factory=lambda: os.environ.get("MT5_ALLOW_ACCOUNT_LOGIN"))
    install_signals: bool = False
    log: Callable[[str], None] = print


# ---------------------------------------------------------------------------------------------
# local (broker-free) refusal checks
# ---------------------------------------------------------------------------------------------


def discover_expected_hash(trader_dir: Path) -> tuple[str | None, str]:
    """The account_id_hash the TRADER is bound to: its store meta, else its last heartbeat."""
    db = trader_dir / "demo.sqlite"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2.0)
            try:
                row = con.execute("SELECT value FROM meta WHERE key='account_id_hash'").fetchone()
            finally:
                con.close()
            if row and row[0]:
                return str(row[0]), "trader_store_meta"
        except sqlite3.Error:
            pass
    hb = trader_dir / "heartbeat.json"
    if hb.exists():
        try:
            value = json.loads(hb.read_text(encoding="utf-8")).get("account_id_hash")
            if value:
                return str(value), "trader_heartbeat"
        except (OSError, ValueError):
            pass
    return None, "unknown"


def mt5_lock_busy(path: Path | None) -> tuple[bool, str]:
    if path is None:
        from adapters.activtrades_mt5.lock import DEFAULT_LOCK_PATH

        path = DEFAULT_LOCK_PATH
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False, f"{path.name} absent"
    return (age <= 90.0), f"{path.name} age {age:.0f}s (stale after 90s)"


def local_preflight(env: CanaryEnv, *, plan_only: bool = False) -> list[dict[str, Any]]:
    """Broker-free refusal checks. Returns the full list of checks (each ``{name, ok, detail}``)."""
    import instance_lock

    from demo.monitor import RUNNING, heartbeat_verdict

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    if env.mode == "live" and not plan_only:  # a plan-only run can send nothing, so it needs no authorisation phrase
        add("confirm_flag", env.confirm == CONFIRM_VALUE, f"{CONFIRM_FLAG}=<exact phrase> required")
    add("no_account_login_env", (env.login_env or "").strip() != "1", "MT5_ALLOW_ACCOUNT_LOGIN=1 is refused (attach-only)")
    if env.mode == "live":
        add("artifacts_dir", env.artifacts_dir.resolve() == REQUIRED_ARTIFACTS.resolve(), f"must be {REQUIRED_ARTIFACTS}")
    trader = env.trader_artifacts_dir
    for lock_name in ("runner.lock", "supervisor.lock"):
        status = instance_lock.lock_status(trader / lock_name)
        add(f"no_{lock_name.split('.')[0]}_alive", not status["alive"], f"{lock_name}: exists={status['exists']} alive={status['alive']}")
    verdict = heartbeat_verdict(trader / "heartbeat.json", _utcnow())
    add("no_fresh_trader_heartbeat", verdict["verdict"] != RUNNING, f"heartbeat: {verdict['verdict']} ({verdict['reason']})")
    busy, detail = mt5_lock_busy(env.mt5_lock_path)
    add("mt5_terminal_lock_free", not busy, detail)
    add("expected_account_hash_known", env.expected_account_hash is not None,
        "the trader's account_id_hash binding must be discoverable (store/heartbeat) or given via --expected-account-hash")
    return checks


def failed(checks: list[dict[str, Any]]) -> list[str]:
    return [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]]


# ---------------------------------------------------------------------------------------------
# stack construction (the SAME stack the trader uses; only magic + exit policy differ)
# ---------------------------------------------------------------------------------------------


def build_stack(env: CanaryEnv, *, dry_run: bool, state_dir: Path) -> Any:
    from demo.execution.exit_manager import default_staged_exit_policy
    from demo.execution.live import Mt5DemoStack, StackConfig

    cfg = StackConfig(
        magic=CANARY_MAGIC,
        canary_magic=UNUSED_CANARY_MAGIC,
        exit_policy="staged",
        staged_exit=default_staged_exit_policy(),
        expected_server=env.expected_server,
        **env.stack_overrides,
    )
    return Mt5DemoStack(
        client=env.client, connection=env.connection, state_dir=state_dir, dry_run=dry_run,
        lock_path=env.mt5_lock_path, config=cfg, now=env.now,
    )


class BrokerView:
    """Broker truth read through the stack's OWN MT5 session on its lane thread (never a second connection)."""

    def __init__(self, stack: Any, market: str) -> None:
        self.stack = stack
        self.market = market
        self.info = stack._markets[market]
        self.symbol = self.info.broker_symbol

    def _session(self) -> Any:
        assert self.stack._adapter is not None
        return self.stack._adapter.session

    def positions(self, *, symbol_only: bool = False) -> list[Any]:
        if symbol_only:
            return list(self.stack._on_lane(self.stack._lane_symbol_positions, self.symbol, strict=True))
        return list(self.stack._on_lane(self.stack._lane_positions, strict=True))

    def orders(self) -> list[Any]:
        def read() -> list[Any]:
            s = self._session()
            return list(s.call("orders_get", s.client.orders_get) or [])

        return list(self.stack._on_lane(read, strict=True))

    def symbol_info(self) -> Any:
        def read() -> Any:
            s = self._session()
            return s.call("symbol_info", s.client.symbol_info, self.symbol)

        return self.stack._on_lane(read, strict=True)

    def quote(self) -> tuple[Decimal, Decimal, datetime]:
        q = self.stack._on_lane(self.stack.bar_source._quote, self.market, strict=True)
        if q is None:
            raise StepFail("no quote available")
        return _dec(q.bid), _dec(q.ask), q.ts_utc

    def own(self, positions: list[Any] | None = None) -> list[Any]:
        rows = self.positions(symbol_only=True) if positions is None else positions
        return [p for p in rows if int(p.magic) == CANARY_MAGIC and str(p.symbol) == self.symbol]


def _pos_dict(p: Any) -> dict[str, Any]:
    return {
        "ticket": int(p.ticket), "symbol": str(p.symbol), "magic": int(p.magic),
        "side": "BUY" if int(p.type) == 0 else "SELL", "volume": float(p.volume),
        "price_open": float(p.price_open), "sl": float(p.sl or 0.0), "tp": float(p.tp or 0.0),
    }


def _order_dict(o: Any) -> dict[str, Any]:
    return {
        "ticket": int(o.ticket), "symbol": str(getattr(o, "symbol", "")), "magic": int(getattr(o, "magic", 0)),
        "type": int(getattr(o, "type", -1)), "volume": float(getattr(o, "volume_current", getattr(o, "volume_initial", 0.0))),
        "price": float(getattr(o, "price_open", 0.0)), "sl": float(getattr(o, "sl", 0.0) or 0.0),
        "tp": float(getattr(o, "tp", 0.0) or 0.0),
    }


def broker_snapshot(view: BrokerView, label: str) -> dict[str, Any]:
    positions = view.positions()
    orders = view.orders()
    return {
        "label": label, "utc": _iso(_utcnow()),
        "positions": [_pos_dict(p) for p in positions],
        "orders": [_order_dict(o) for o in orders],
    }


def local_state(view: BrokerView) -> dict[str, Any]:
    """Local (Nautilus cache + intent registry) truth for the canary instrument."""
    from nautilus_trader.model.enums import OrderType

    stack = view.stack
    cache = stack._strategy.cache
    inst = view.info.instrument_id
    positions = cache.positions_open(instrument_id=inst)
    stops = [o for o in cache.orders_open(instrument_id=inst) if o.order_type == OrderType.STOP_MARKET and o.is_reduce_only]
    return {
        "position_qty": sum((abs(_dec(p.signed_qty)) for p in positions), D(0)),
        "n_positions": len(positions),
        "stop_child_qty": sum((_dec(o.quantity) for o in stops), D(0)),
        "stop_child_trigger": None if len(stops) != 1 else _dec(stops[0].trigger_price),
        "n_stop_children": len(stops),
    }


# ---------------------------------------------------------------------------------------------
# plan (shadow stack: real quotes + real order_check, order_send hard-guarded)
# ---------------------------------------------------------------------------------------------


def make_intent(plan: Mapping[str, Any], intent_id: str, now: datetime, risk_fraction: float) -> Any:
    from demo.contracts import TradeIntent

    return TradeIntent(
        opportunity_id="opp-" + intent_id, phase="DISCOVERY", intent_id=intent_id, market=plan["market"],
        broker_symbol=plan["broker_symbol"], direction=1, entry_ref=float(plan["ask"]), stop=float(plan["initial_stop"]),
        target=None, min_space_r=0.0, valid_until_utc=(now + timedelta(seconds=60)).isoformat(),
        forced_flat_utc=(now + timedelta(minutes=10)).isoformat(), risk_fraction=risk_fraction,
        context={"trade_type": TRADE_TYPE_TAG},
    )


def intent_context(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "family": "e2_canary",
        "atr": None,
        "exit_plan": {"stages": [{
            "target_price": str(plan["tp1_price"]), "close_fraction": "0.5", "stage_id": "tp1", "source": "CANARY:price",
        }]},
        "exit_meta": {"geometry_source": "canary", "trade_type": TRADE_TYPE_TAG},
    }


def build_plan(env: CanaryEnv, state_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Start a SHADOW stack, run every broker-side refusal check, compute and VERIFY the sizes. Places nothing."""
    from demo.execution.events import Rejected
    from demo.execution.stack_port import StackFailClosed

    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    shadow = build_stack(env, dry_run=True, state_dir=state_dir / "plan")
    try:
        try:
            snap = shadow.start()
        except StackFailClosed as exc:
            add("stack_attach_and_verify", False, f"start refused: {exc}")
            return {}, checks
        add("stack_attach_and_verify", True, "attached; DEMO / expected login+server / netting / reconciled verified by the stack")
        add("account_is_demo", bool(snap.is_demo), f"is_demo={snap.is_demo}")
        add("account_hash_matches_trader", env.expected_account_hash == snap.account_id_hash,
            f"attached {snap.account_id_hash} vs trader-bound {env.expected_account_hash}")
        add("reconciled", snap.reconciliation == "RECONCILED", snap.reconciliation)
        market = env.market
        if market not in shadow.markets or market in shadow.disabled_markets:
            add("market_available", False, f"{market} not registered/enabled in the stack ({shadow.disabled_markets})")
            return {}, checks
        view = BrokerView(shadow, market)
        info = view.info
        positions, orders = view.positions(), view.orders()
        add("account_flat_no_orders", not positions and not orders,
            f"account positions={len(positions)} orders={len(orders)} (any magic; the symbol in particular)")
        sym_pos = [p for p in positions if str(p.symbol) == view.symbol]
        add("no_position_on_symbol", not sym_pos, f"{view.symbol}: {len(sym_pos)} position(s)")
        quote_age = shadow.bar_source.quote_age_seconds(market)
        fresh = shadow.bar_source.is_fresh(market)
        add("fresh_quote_market_open", fresh, f"quote age {quote_age}s (max {shadow._cfg.max_quote_age_s}s)")
        sinfo = view.symbol_info()
        trade_mode = getattr(sinfo, "trade_mode", None)
        add("symbol_trade_mode_full", trade_mode in (None, 4), f"trade_mode={trade_mode} (4 = full)")
        if failed(checks):
            return {}, checks
        bid, ask, quote_ts = view.quote()
        tick, point = info.spec.tick_size, _dec(getattr(sinfo, "point", info.spec.tick_size))
        stops_level = int(getattr(sinfo, "trade_stops_level", 0) or 0)
        freeze_level = int(getattr(sinfo, "trade_freeze_level", 0) or 0)
        geo = compute_geometry(bid=bid, ask=ask, tick=tick, point=point, stops_level_pts=stops_level, freeze_level_pts=freeze_level)
        min_lot, step = info.spec.volume_min, info.spec.volume_step
        lots = min_lot * SIZE_MULTIPLE
        plan: dict[str, Any] = {
            "market": market, "broker_symbol": view.symbol, "direction": "BUY", "bid": bid, "ask": ask,
            "quote_utc": _iso(quote_ts), "spread": geo["spread"], "tick_size": tick, "point": point,
            "stops_level_points": stops_level, "freeze_level_points": freeze_level,
            "min_lot": min_lot, "lot_step": step, "total_lots": lots, "partial_lots": min_lot,
            "stop_distance": geo["stop_distance"], "initial_stop": geo["initial_stop"], "tp1_price": geo["tp1_price"],
            "stop_distance_rule": "round_up_tick(max(10 x spread, 3 x stops_level points, 3 x freeze_level points, 0.15 % of price))",
            "stop_distance_components": {
                "ten_spreads": geo["ten_spreads"], "three_stops_levels": geo["broker_min_distance"],
                "structural_placeholder_0.15pct": geo["structural_placeholder"],
            },
            "broker_tp": None, "exit_plan": "TP1 price stage (close_fraction 0.5) + runner; no broker TP (sum of fractions < 1)",
            "magic": CANARY_MAGIC, "trade_type": TRADE_TYPE_TAG,
        }
        if (lots / step) != (lots / step).to_integral_value():
            add("lots_on_step", False, f"{lots} not a multiple of step {step}")
            return plan, checks
        # -- calibrate the risk fraction through the REAL sizer until it fits EXACTLY 2 x min lot.  Every probe uses a
        # FRESH shadow stack: a dry-run submit leaves a local (never sent) order that a later venue comparison on the
        # same stack would report as a reconciliation mismatch. --
        sizing = _probe_size(shadow, plan, D("0.001"), 0)
    finally:
        with contextlib.suppress(Exception):
            shadow.stop()
    rf = D("0.001")
    for i in range(1, 8):
        head, detail, refusal = sizing
        if refusal:
            add("shadow_order_check_ok", False, f"the broker's order_check refused the canary entry: {refusal}")
            return plan, checks
        if isinstance(head, Rejected) and "size_below_min" not in head.reason:
            add("shadow_entry_accepted", False, f"sizing/order_check refused the canary intent: {head.reason} {detail.get('message', '')}")
            return plan, checks
        quantity = None if isinstance(head, Rejected) else _dec(head.quantity)
        if quantity == lots:
            plan["risk_fraction"], plan["shadow_sized_quantity"] = rf, quantity
            add("size_calibrated_to_2x_min_lot", True, f"risk_fraction {rf} sizes {quantity} lots (target {lots})")
            notional = detail.get("notional_per_lot")
            if notional is not None:
                plan["notional_eur"] = _dec(notional) * lots
                add("notional_bounded", plan["notional_eur"] <= MAX_NOTIONAL_EUR, f"notional {plan['notional_eur']:.0f} EUR <= {MAX_NOTIONAL_EUR}")
            return plan, checks
        loss, equity = detail.get("loss_per_lot_at_stop"), detail.get("equity")
        mult = _dec(detail.get("risk_budget_multiplier") or 1)
        if loss is not None and equity is not None and _dec(equity) > 0:
            rf = (lots * D("1.25") * _dec(loss)) / (_dec(equity) * mult)
        elif quantity:
            rf = rf * (lots * D("1.25")) / quantity
        else:
            rf *= 2
        rf = rf.quantize(D("0.000001"), rounding=ROUND_CEILING)
        probe = build_stack(env, dry_run=True, state_dir=state_dir / f"plan{i}")
        try:
            probe.start()
            sizing = _probe_size(probe, plan, rf, i)
        except StackFailClosed as exc:
            add("size_calibrated_to_2x_min_lot", False, f"probe stack refused: {exc}")
            return plan, checks
        finally:
            with contextlib.suppress(Exception):
                probe.stop()
    add("size_calibrated_to_2x_min_lot", False, "could not calibrate the risk fraction to exactly 2 x min lot")
    return plan, checks


def _probe_size(stack: Any, plan: Mapping[str, Any], rf: Decimal, i: int) -> tuple[Any, dict[str, Any], str | None]:
    """One dry-run submit of the canary intent at risk fraction ``rf`` -> (head event, its risk_detail, refusal).
    A shadow stack answers ``[Accepted]`` when sizing AND the broker's order_check passed (the send itself is
    hard-guarded); ``[Accepted, Rejected(reason)]`` means the broker refused the order (market closed, margin, ...)."""
    intent = make_intent(plan, f"e2canary-plan-{i}", stack._now(), float(rf))
    events = stack.submit(intent, intent_context(plan))
    head = events[0]
    refusal = events[1].reason if len(events) > 1 and type(events[1]).__name__ == "Rejected" else None
    return head, dict(head.risk_detail or {}), refusal


# ---------------------------------------------------------------------------------------------
# the canary run
# ---------------------------------------------------------------------------------------------


@dataclass
class Canary:
    env: CanaryEnv
    plan: dict[str, Any]
    state_dir: Path
    stack: Any = None
    view: BrokerView | None = None
    abort: threading.Event = field(default_factory=threading.Event)
    abort_reason: str = ""
    deadline: float = 0.0
    intent_id: str = ""
    ticket: int | None = None
    lots_total: Decimal = field(default_factory=Decimal)
    steps: list[dict[str, Any]] = field(default_factory=list)
    latencies: dict[str, float | None] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    events: list[Any] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    initial_stop: Decimal = field(default_factory=Decimal)
    current_stop: Decimal = field(default_factory=Decimal)

    def check_abort(self) -> None:
        if self.abort.is_set():
            raise Aborted(self.abort_reason or "aborted")
        if time.monotonic() > self.deadline:
            self.abort_reason = "timeout"
            self.abort.set()
            raise Aborted("timeout")

    def hook(self, name: str) -> None:
        fn = self.env.hooks.get(name)
        if fn is not None:
            fn()


def _expect(cond: bool, message: str, problems: list[str]) -> None:
    if not cond:
        problems.append(message)


def _poll_until(canary: Canary, predicate: Callable[[], bool], *, timeout_s: float = 12.0, interval_s: float = 0.4) -> bool:
    """Poll the stack (ingests broker deals into local truth) until ``predicate`` holds or the bounded wait ends."""
    end = time.monotonic() + timeout_s
    while True:
        canary.events.extend(canary.stack.poll_events())
        try:
            if predicate():
                return True
        except Exception:
            pass
        if time.monotonic() >= end:
            return False
        canary.check_abort()
        time.sleep(interval_s)


def _own_one(canary: Canary) -> Any | None:
    own = canary.view.own()
    return own[0] if len(own) == 1 else None


# -- the ten steps -----------------------------------------------------------------------------


def step1_entry(c: Canary) -> dict[str, Any]:
    plan = c.plan
    now = c.env.now()
    c.intent_id = f"e2canary-{_stamp(now)}"
    intent = make_intent(plan, c.intent_id, now, float(plan["risk_fraction"]))
    t0 = time.perf_counter()
    events = c.stack.submit(intent, intent_context(plan))
    submit_ms = (time.perf_counter() - t0) * 1000.0
    c.events.extend(events)
    kinds = [type(e).__name__ for e in events]
    c.hook("after_action_1")
    problems: list[str] = []
    _expect(kinds == ["Accepted", "Fill", "ProtectionConfirmed"], f"unexpected entry events {kinds}", problems)
    fill = next((e for e in events if type(e).__name__ == "Fill"), None)
    own = c.view.own()
    others = [p for p in c.view.positions(symbol_only=True) if int(p.magic) != CANARY_MAGIC]
    _expect(len(own) == 1, f"expected exactly 1 canary position at the broker, got {len(own)}", problems)
    _expect(not others, f"foreign positions on the symbol: {len(others)}", problems)
    if own:
        c.ticket = int(own[0].ticket)
        c.lots_total = _dec(own[0].volume)
        _expect(c.lots_total == plan["total_lots"], f"broker volume {c.lots_total} != planned {plan['total_lots']}", problems)
        _expect(int(own[0].magic) == CANARY_MAGIC, "position magic is not the canary magic", problems)
        _expect(int(own[0].type) == 0, "position is not BUY", problems)
        _expect(float(own[0].tp or 0.0) == 0.0, f"unexpected broker TP {own[0].tp} (a TP1+runner plan has none)", problems)
    if fill is not None:
        _expect(_dec(fill.quantity) == plan["total_lots"], f"Fill quantity {fill.quantity} != planned", problems)
        c.latencies["order_submit_total_ms"] = submit_ms
        c.latencies["order_fill_ms"] = fill.latency_total_ms
        c.quality["entry"] = {
            "intended_ask": plan["ask"], "bid_at_send": fill.bid_at_send, "ask_at_send": fill.ask_at_send,
            "fill_price": fill.price, "slippage_adverse_positive": fill.slippage, "spread": fill.spread,
            "slippage_vs_intended": fill.slippage_vs_intended, "commission": fill.commission,
        }
    row = c.stack._registry.get(c.intent_id)
    _expect(row is not None and row.status == "OPEN", f"registry row not OPEN: {None if row is None else row.status}", problems)
    if row is not None and row.context:
        ctx = json.loads(row.context)
        _expect(bool(ctx.get("exit_plan")), "registry context carries no staged exit_plan", problems)
    if problems:
        raise StepFail("; ".join(problems))
    return {"ticket": c.ticket, "events": kinds, "lots": c.lots_total}


def step2_protective_stop(c: Canary) -> dict[str, Any]:
    c.hook("after_action_2")
    pos = _own_one(c)
    problems: list[str] = []
    if pos is None:
        raise StepFail("no single canary position at the broker")
    tick = c.plan["tick_size"]
    sl = _dec(pos.sl or 0)
    _expect(sl > 0, "broker stop-loss missing (sl == 0)", problems)
    _expect(sl < _dec(pos.price_open), "stop is not on the protective side (must be below a long entry)", problems)
    _expect(abs(sl - c.plan["initial_stop"]) <= tick * c.stack._cfg.protection_tolerance_ticks,
            f"broker sl {sl} differs from requested {c.plan['initial_stop']}", problems)
    _expect(_dec(pos.volume) == c.lots_total, "position volume changed since entry", problems)
    ls = local_state(c.view)
    _expect(ls["stop_child_qty"] == _dec(pos.volume), f"local stop child qty {ls['stop_child_qty']} != position {pos.volume} (full coverage)", problems)
    _expect(ls["n_stop_children"] == 1, f"{ls['n_stop_children']} local stop children (expected 1)", problems)
    if problems:
        raise StepFail("; ".join(problems))
    c.initial_stop = c.current_stop = sl
    return {"sl": sl, "covers_volume": _dec(pos.volume), "local": ls}


def step3_partial(c: Canary) -> dict[str, Any]:
    from demo.execution.strategy import ReduceJob

    qty = c.plan["partial_lots"]
    bid_before, ask_before, _ = c.view.quote()
    job = ReduceJob(instrument_id=c.view.info.instrument_id, quantity=qty, tag=f"exit:e2canary-partial-1:{c.intent_id}")
    t0 = time.perf_counter()
    c.stack._strategy.enqueue(job)
    outcome = job.future.result(timeout=c.stack._cfg.flatten_wait_s)
    c.latencies["partial_1_ms"] = (time.perf_counter() - t0) * 1000.0
    c.hook("after_action_3")
    problems: list[str] = []
    _expect(outcome.status == "reduced", f"ReduceJob outcome {outcome.status}:{outcome.reason}", problems)
    filled = sum((q for q, _ in outcome.fills), D(0))
    _expect(filled == qty, f"filled {filled} != requested {qty}", problems)
    if outcome.fills and filled > 0:
        avg = sum((q * p for q, p in outcome.fills), D(0)) / filled
        c.quality["partial_1"] = {"bid_before": bid_before, "fill_price": avg, "slippage_adverse_positive": bid_before - avg, "spread": ask_before - bid_before}
    if problems:
        raise StepFail("; ".join(problems))
    return {"status": outcome.status, "filled": filled}


def step4_volume_equality(c: Canary) -> dict[str, Any]:
    expected = c.lots_total - c.plan["partial_lots"]
    ok = _poll_until(c, lambda: (
        _dec(_own_one(c).volume) == expected and local_state(c.view)["position_qty"] == expected
    ))
    c.hook("after_action_4")
    pos = _own_one(c)
    ls = local_state(c.view)
    problems: list[str] = []
    broker_vol = None if pos is None else _dec(pos.volume)
    _expect(pos is not None, "canary position vanished at the broker", problems)
    _expect(broker_vol == expected, f"broker remaining {broker_vol} != expected {expected}", problems)
    _expect(ls["position_qty"] == expected, f"local remaining {ls['position_qty']} != expected {expected}", problems)
    _expect(broker_vol == ls["position_qty"], "broker volume != local volume", problems)
    if not ok and not problems:
        problems.append("volumes did not converge")
    if problems:
        raise StepFail("; ".join(problems))
    return {"expected_remaining": expected, "broker": broker_vol, "local": ls["position_qty"]}


def step5_protection_after_partial(c: Canary) -> dict[str, Any]:
    c.hook("after_action_5")
    pos = _own_one(c)
    if pos is None:
        raise StepFail("canary position vanished at the broker")
    remaining = c.lots_total - c.plan["partial_lots"]
    sl = _dec(pos.sl or 0)
    problems: list[str] = []
    _expect(sl > 0, "broker stop-loss missing after the partial", problems)
    _expect(sl == c.current_stop, f"broker sl changed by the partial: {c.current_stop} -> {sl}", problems)
    # the adapter resizes the protective child to the remaining volume (it is applied with the deal sync)
    _poll_until(c, lambda: local_state(c.view)["stop_child_qty"] == remaining, timeout_s=8.0)
    ls = local_state(c.view)
    _expect(ls["n_stop_children"] == 1, f"{ls['n_stop_children']} local stop children (expected 1)", problems)
    _expect(ls["stop_child_qty"] == remaining, f"adapter stop child qty {ls['stop_child_qty']} != remaining {remaining}", problems)
    _expect(_dec(pos.volume) == ls["stop_child_qty"],
            "PROTECTIVE_QUANTITY_MUST_EQUAL_POSITION violated (stop child qty != broker position volume)", problems)
    pending = [o for o in c.view.orders() if int(getattr(o, "magic", 0)) == CANARY_MAGIC]
    _expect(not pending, f"{len(pending)} pending canary order(s) at the broker (orphan?)", problems)
    if problems:
        raise StepFail("; ".join(problems))
    return {"broker_sl": sl, "broker_volume": _dec(pos.volume), "stop_child_qty": ls["stop_child_qty"]}


def step6_tighten(c: Canary) -> dict[str, Any]:
    from demo.execution.strategy import ModifyStopJob

    pos = _own_one(c)
    if pos is None:
        raise StepFail("canary position vanished at the broker")
    bid, ask, _ = c.view.quote()
    sinfo = c.view.symbol_info()
    new_stop = compute_tighten_stop(
        bid=bid, current_stop=c.current_stop, initial_distance=c.plan["stop_distance"], tick=c.plan["tick_size"],
        point=c.plan["point"], stops_level_pts=c.plan["stops_level_points"],
        freeze_level_pts=int(getattr(sinfo, "trade_freeze_level", 0) or 0), spread=ask - bid,
    )
    if new_stop is None:
        raise StepFail(f"no valid tighter stop exists at bid {bid} (market moved away; re-run) - not a contract verdict")
    c.data["tighten_target"] = new_stop
    c.data["tighten_from"] = c.current_stop
    job = ModifyStopJob(instrument_id=c.view.info.instrument_id, new_stop=new_stop, tag=f"exit-stop:{c.intent_id}")
    t0 = time.perf_counter()
    c.stack._strategy.enqueue(job)
    outcome = job.future.result(timeout=c.stack._cfg.exposure_timeout_s)
    c.latencies["modify_ms"] = (time.perf_counter() - t0) * 1000.0
    c.hook("after_action_6")
    pos = _own_one(c)
    problems: list[str] = []
    _expect(outcome.status == "modified", f"ModifyStopJob outcome {outcome.status}:{outcome.reason}", problems)
    _expect(pos is not None, "canary position vanished", problems)
    if pos is not None:
        seen = _dec(pos.sl or 0)
        tick = c.plan["tick_size"]
        _expect(abs(seen - new_stop) < tick, f"broker sl {seen} != requested {new_stop}", problems)
        _expect(seen > c.current_stop, f"broker sl did not tighten: {c.current_stop} -> {seen}", problems)
        _expect(_dec(pos.volume) == c.lots_total - c.plan["partial_lots"], "volume changed by the stop move", problems)
        if not problems:
            c.current_stop = seen
    if problems:
        raise StepFail("; ".join(problems))
    return {"old_sl": c.data.get("tighten_from", None), "new_sl": new_stop, "status": outcome.status}


def step7_cannot_loosen(c: Canary) -> dict[str, Any]:
    from demo.execution.strategy import ModifyStopJob
    from exits.models import PositionSide, stop_is_unchanged_or_tighter

    pos = _own_one(c)
    if pos is None:
        raise StepFail("canary position vanished at the broker")
    before = _dec(pos.sl or 0)
    wider = _round_tick(before - c.plan["stop_distance"] / 2, c.plan["tick_size"], "down")
    problems: list[str] = []
    # (a) the production guard the staged manager applies BEFORE any modify
    _expect(not stop_is_unchanged_or_tighter(PositionSide.LONG, old=before, new=wider), "engine guard accepted a widening", problems)
    # (b) the strategy-level tighten-only check on a real job
    job = ModifyStopJob(instrument_id=c.view.info.instrument_id, new_stop=wider, tag=f"exit-stop-loosen-test:{c.intent_id}")
    c.stack._strategy.enqueue(job)
    outcome = job.future.result(timeout=c.stack._cfg.exposure_timeout_s)
    c.hook("after_action_7")
    _expect(outcome.status == "denied" and "stop_not_tighter" in outcome.reason,
            f"widening was not refused locally: {outcome.status}:{outcome.reason}", problems)
    # (c) broker truth: stop unchanged
    pos = _own_one(c)
    after = None if pos is None else _dec(pos.sl or 0)
    _expect(after == before, f"broker sl changed by a refused widening: {before} -> {after}", problems)
    if problems:
        raise StepFail("; ".join(problems))
    return {"attempted_wider_stop": wider, "outcome": f"{outcome.status}:{outcome.reason}", "broker_sl_unchanged": after}


def step8_second_partial(c: Canary) -> dict[str, Any]:
    from demo.execution.strategy import ReduceJob

    remaining = c.lots_total - c.plan["partial_lots"]
    qty = c.plan["partial_lots"]
    if remaining - qty < c.plan["min_lot"]:
        return {"not_applicable": f"remaining {remaining} - {qty} would leave less than the minimum lot: the second stage is the final close"}
    job = ReduceJob(instrument_id=c.view.info.instrument_id, quantity=qty, tag=f"exit:e2canary-partial-2:{c.intent_id}")
    t0 = time.perf_counter()
    c.stack._strategy.enqueue(job)
    outcome = job.future.result(timeout=c.stack._cfg.flatten_wait_s)
    c.latencies["partial_2_ms"] = (time.perf_counter() - t0) * 1000.0
    c.hook("after_action_8")
    expected = remaining - qty
    _poll_until(c, lambda: _dec(_own_one(c).volume) == expected and local_state(c.view)["stop_child_qty"] == expected)
    pos = _own_one(c)
    ls = local_state(c.view)
    problems: list[str] = []
    _expect(outcome.status == "reduced", f"ReduceJob outcome {outcome.status}:{outcome.reason}", problems)
    _expect(pos is not None and _dec(pos.volume) == expected, "broker remaining volume != expected after the second partial", problems)
    _expect(pos is not None and float(pos.sl or 0.0) > 0, "broker stop missing after the second partial", problems)
    _expect(ls["stop_child_qty"] == expected, f"stop child qty {ls['stop_child_qty']} != remaining {expected}", problems)
    if problems:
        raise StepFail("; ".join(problems))
    c.lots_total = c.lots_total - qty  # the position is smaller now: later steps use the live remainder
    c.plan["partial_lots"] = qty
    return {"remaining": expected}


def step9_final_close(c: Canary) -> dict[str, Any]:
    t0 = time.perf_counter()
    ok = c.stack._flatten(c.view.info, tag=f"exit:e2canary-final:{c.intent_id}", hint="MANUAL", escalate=False)
    c.latencies["final_close_ms"] = (time.perf_counter() - t0) * 1000.0
    c.hook("after_action_9")
    problems: list[str] = []
    _expect(ok, "stack._flatten reported failure", problems)
    sym = c.view.positions(symbol_only=True)
    _expect(not c.view.own(sym), f"{len(c.view.own(sym))} canary position(s) still at the broker", problems)
    _expect(not sym, f"{len(sym)} position(s) on the symbol after the close (opposite position created?)", problems)
    orders = [o for o in c.view.orders() if str(getattr(o, "symbol", "")) == c.view.symbol]
    _expect(not orders, f"{len(orders)} order(s) left on the symbol", problems)
    def _closed() -> bool:
        r = c.stack._registry.get(c.intent_id)
        return r is not None and r.status == "CLOSED"

    _poll_until(c, _closed, timeout_s=12.0)
    row = c.stack._registry.get(c.intent_id)
    closed = [e for e in c.events if type(e).__name__ == "PositionClosed"]
    c.data["registry_status_after_close"] = None if row is None else row.status
    if closed:
        e = closed[-1]
        c.data["closed_event"] = {
            "exit_reason": e.exit_reason, "exit_price": e.exit_price, "exit_quantity": e.exit_quantity, "commission": e.commission,
            "swap": e.swap, "profit_eur": e.profit_eur, "net_pnl_eur": e.net_pnl_eur, "holding_seconds": e.holding_seconds,
        }
    if problems:
        raise StepFail("; ".join(problems))
    return {"flat": True, "registry_status": c.data["registry_status_after_close"]}


def step10_restart(c: Canary) -> dict[str, Any]:
    c.hook("before_restart")
    c.stack.stop()
    c.stack = None
    c.view = None
    new = build_stack(c.env, dry_run=False, state_dir=c.state_dir)
    c.stack = new
    snap = new.start()
    c.view = BrokerView(new, c.env.market)
    problems: list[str] = []
    _expect(snap.reconciliation == "RECONCILED", f"restart reconciliation {snap.reconciliation}", problems)
    _expect(snap.is_demo, "restart: account is not DEMO", problems)
    _expect(snap.account_id_hash == c.env.expected_account_hash, "restart: account hash differs", problems)
    _expect(snap.open_positions == 0 and snap.open_orders == 0, f"restart sees positions={snap.open_positions} orders={snap.open_orders}", problems)
    positions, orders = c.view.positions(), c.view.orders()
    _expect(not positions, f"{len(positions)} position(s) at the broker after restart", problems)
    _expect(not orders, f"{len(orders)} order(s) at the broker after restart", problems)
    _expect(not new.open_intents(), f"registry reports open intents: {list(new.open_intents())}", problems)
    row = new._registry.get(c.intent_id)
    _expect(row is not None and row.status == "CLOSED", f"registry row not terminal after restart: {None if row is None else row.status}", problems)
    foreign = (snap.extra or {}).get("foreign_positions") or []
    _expect(not foreign, f"foreign positions seen after restart: {foreign}", problems)
    c.hook("after_action_10")
    c.data["restart"] = {"reconciliation": snap.reconciliation, "open_positions": snap.open_positions, "open_orders": snap.open_orders,
                         "registry_status": row.status if row else None, "foreign_positions": list(foreign)}
    if problems:
        raise StepFail("; ".join(problems))
    return {"reconciliation": snap.reconciliation, "registry_status": row.status if row else None}


STEP_FUNCS: tuple[Callable[[Canary], dict[str, Any]], ...] = (
    step1_entry, step2_protective_stop, step3_partial, step4_volume_equality, step5_protection_after_partial,
    step6_tighten, step7_cannot_loosen, step8_second_partial, step9_final_close, step10_restart,
)


def run_sequence(c: Canary) -> None:
    """Steps 1..10 in order; stops at the first failure (later steps are recorded SKIPPED)."""
    failed_at: int | None = None
    for n, (name, fn) in enumerate(zip(STEP_NAMES, STEP_FUNCS, strict=True), start=1):
        record: dict[str, Any] = {"n": n, "name": name, "status": "SKIPPED", "detail": "", "before": None, "after": None,
                                  "started_utc": None, "duration_ms": None, "data": None}
        c.steps.append(record)
        if failed_at is not None:
            record["detail"] = f"skipped after the failure of step {failed_at}"
            continue
        record["started_utc"] = _iso(_utcnow())
        t0 = time.perf_counter()
        try:
            c.check_abort()
            if c.view is not None and n != 10:
                with contextlib.suppress(Exception):
                    record["before"] = broker_snapshot(c.view, f"before_step_{n}")
            data = fn(c)
            if "not_applicable" in data:
                record["status"] = "NOT_APPLICABLE"
                record["detail"] = data["not_applicable"]
            else:
                record["status"] = "PASS"
            record["data"] = data
        except Aborted as exc:
            record["status"], record["detail"] = "FAIL", f"aborted: {exc}"
            failed_at = n
        except StepFail as exc:
            record["status"], record["detail"] = "FAIL", str(exc)
            failed_at = n
        except BaseException as exc:
            record["status"], record["detail"] = "FAIL", f"{type(exc).__name__}: {exc}"
            failed_at = n
        record["duration_ms"] = (time.perf_counter() - t0) * 1000.0
        if c.view is not None and c.stack is not None:
            with contextlib.suppress(Exception):
                record["after"] = broker_snapshot(c.view, f"after_step_{n}")
        c.env.log(f"  step {n:>2} {name:<26} {record['status']:<14} {record['detail']}")


# ---------------------------------------------------------------------------------------------
# the always-flatten safety net
# ---------------------------------------------------------------------------------------------


def safety_flatten(c: Canary) -> dict[str, Any]:
    """Make sure NO canary exposure remains. Never touches a position of another magic."""
    result: dict[str, Any] = {"attempts": [], "residual_canary_positions": None, "residual_orders": None, "recovery_stack_used": False}

    def residual(stack: Any) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
        try:
            view = BrokerView(stack, c.env.market)
            return [_pos_dict(p) for p in view.own(view.positions(symbol_only=True))], [
                _order_dict(o) for o in view.orders() if int(getattr(o, "magic", 0)) == CANARY_MAGIC]
        except Exception as exc:
            result["attempts"].append(f"residual read failed: {type(exc).__name__}: {exc}")
            return None, None

    def try_flatten(stack: Any, label: str) -> None:
        info = BrokerView(stack, c.env.market).info
        for n in (1, 2, 3):
            try:
                ok = stack._flatten(info, tag=f"e2canary-safety-flatten:{label}:{n}", hint="SAFETY_FLATTEN", escalate=False)
                result["attempts"].append(f"{label}: _flatten #{n} -> {ok}")
            except Exception as exc:
                result["attempts"].append(f"{label}: _flatten #{n} raised {type(exc).__name__}: {exc}")
            pos, _ = residual(stack)
            if pos == []:
                return
            try:  # existing verified fallback: emergency_close by ticket (own magic only)
                denials = stack._on_lane(stack._lane_close_by_ticket, info, f"e2canary-ticket-close:{label}:{n}", retry_reads=False)
                result["attempts"].append(f"{label}: emergency_close by ticket -> denials={denials}")
            except Exception as exc:
                result["attempts"].append(f"{label}: ticket close raised {type(exc).__name__}: {exc}")
            pos, _ = residual(stack)
            if pos == []:
                return
            time.sleep(0.5)

    stack = c.stack
    usable = stack is not None and not getattr(stack, "_stopped", True) and getattr(stack, "_lane", None) is not None
    if usable:
        pos, orders = residual(stack)
        if pos != []:  # open OR unknown: always attempt (a flatten with nothing open is a no-op)
            try_flatten(stack, "stack")
        pos, orders = residual(stack)
        result["residual_canary_positions"], result["residual_orders"] = pos, orders
    if not usable or result["residual_canary_positions"] != []:
        if usable and stack is not None:
            with contextlib.suppress(Exception):
                stack.stop()
        recovery = None
        try:
            recovery = build_stack(c.env, dry_run=False, state_dir=c.state_dir)
            recovery.start()
            result["recovery_stack_used"] = True
            pos, _ = residual(recovery)
            if pos != []:
                try_flatten(recovery, "recovery")
            result["residual_canary_positions"], result["residual_orders"] = residual(recovery)
        except Exception as exc:
            result["attempts"].append(f"recovery stack failed: {type(exc).__name__}: {exc}")
        finally:
            if recovery is not None:
                with contextlib.suppress(Exception):
                    recovery.stop()
        c.stack = None
    return result


# ---------------------------------------------------------------------------------------------
# orchestration + report
# ---------------------------------------------------------------------------------------------


def _verdict(steps: list[dict[str, Any]], flatten: dict[str, Any] | None, timed_out: bool) -> tuple[str, int]:
    residual = None if flatten is None else flatten.get("residual_canary_positions")
    if flatten is not None and residual != []:
        return "FAIL(exposure_may_remain_at_broker: MANUAL ACTION REQUIRED)", 4
    for s in steps:
        if s["status"] == "FAIL":
            return f"FAIL(step {s['n']}, {s['detail'][:300]})", 1
    if timed_out:
        return "FAIL(timeout)", 1
    if steps and all(s["status"] in ("PASS", "NOT_APPLICABLE") for s in steps) and len(steps) == len(STEP_NAMES):
        return "EXECUTION_CONTRACT_PASS", 0
    return "FAIL(incomplete)", 1


def write_report(env: CanaryEnv, report: dict[str, Any], stamp: str) -> Path:
    env.artifacts_dir.mkdir(parents=True, exist_ok=True)
    path = env.artifacts_dir / f"canary_report_{stamp}.json"
    path.write_text(json.dumps(_scrub(report), indent=1, default=str), encoding="utf-8")
    return path


def _canary_trade_record(c: Canary, account_hash: str | None) -> dict[str, Any] | None:
    """A ``demo_trader.py --record-canary`` compatible record (EXECUTION_CANARY, censored) from the broker facts."""
    entry = c.quality.get("entry") or {}
    closed = c.data.get("closed_event")
    if not entry or not closed or c.ticket is None:
        return None
    now = _utcnow()
    return {
        "position_id": str(c.ticket), "market": c.env.market, "broker_symbol": c.plan.get("broker_symbol"), "direction": 1,
        "volume": float(c.plan["total_lots"]), "entry_price": float(entry["fill_price"]), "exit_price": float(closed["exit_price"] or 0),
        "open_time_utc": _iso(now - timedelta(minutes=5)), "close_time_utc": _iso(now),
        "profit_eur": float(closed["profit_eur"] or 0), "commission_eur": float(closed["commission"] or 0), "swap_eur": float(closed["swap"] or 0),
        "stop": float(c.initial_stop), "magic": CANARY_MAGIC, "comment": "E2 execution-contract canary (approximate open/close times)",
        "exit_reason": "MANUAL", "trade_type": TRADE_TYPE_TAG, "account_id_hash": account_hash,
    }


def run_canary(env: CanaryEnv, *, plan_only: bool = False) -> tuple[int, dict[str, Any]]:
    """Run (or, with ``plan_only``, only plan) the canary.  Returns ``(exit_code, report)``; the report is also written."""
    started = _utcnow()
    stamp = _stamp(started)
    log = env.log
    report: dict[str, Any] = {
        "schema": "e2_canary_report/1", "generated_utc": _iso(started), "mode": env.mode, "plan_only": plan_only,
        "demo_only": True, "market": env.market, "canary_magic": CANARY_MAGIC, "trade_type": TRADE_TYPE_TAG,
        "timeout_s": env.timeout_s, "preflight": [], "plan": None, "steps": [], "latencies_ms": {}, "execution_quality": {},
        "final_flatten": None, "verdict": None, "exit_code": None, "notes": [],
    }
    log(f"E2 execution-contract canary [{env.mode}] market={env.market} {'(PLAN ONLY)' if plan_only else ''}")
    checks = local_preflight(env, plan_only=plan_only)
    report["preflight"] = checks
    plan: dict[str, Any] = {}
    state_dir = env.artifacts_dir / f"state_{stamp}"
    if not failed(checks):
        plan, broker_checks = build_plan(env, state_dir)
        report["preflight"] = checks + broker_checks
        checks = report["preflight"]
        report["plan"] = plan
    for ch in checks:
        log(f"  preflight {'ok ' if ch['ok'] else 'REFUSED'} {ch['name']}: {ch['detail']}")
    if failed(checks):
        report["verdict"] = "REFUSED(" + " | ".join(failed(checks))[:600] + ")"
        report["exit_code"] = 2
        path = write_report(env, report, stamp)
        log(f"{report['verdict']}\nreport: {path}")
        return 2, report
    if plan_only:
        report["verdict"], report["exit_code"] = "PLAN_OK (nothing was sent)", 0
        path = write_report(env, report, stamp)
        log("PLAN (nothing sent):\n" + json.dumps(_scrub(plan), indent=1, default=str) + f"\nreport: {path}")
        return 0, report

    canary = Canary(env=env, plan=plan, state_dir=state_dir)
    canary.stack = build_stack(env, dry_run=False, state_dir=state_dir)
    refusal: str | None = None
    try:
        snap = canary.stack.start()
        report["account_id_hash"] = snap.account_id_hash
        canary.view = BrokerView(canary.stack, env.market)
        # re-verify the book is still flat now that the live stack is attached
        leftover, orders = canary.view.positions(), canary.view.orders()
        if leftover or orders:
            refusal = f"book not flat at start: positions={len(leftover)} orders={len(orders)}"
    except Exception as exc:
        refusal = f"live stack start failed: {type(exc).__name__}: {exc}"
    if refusal is not None:  # nothing was sent: no flatten needed, keep the REFUSED verdict
        if canary.stack is not None:
            with contextlib.suppress(Exception):
                canary.stack.stop()
        report["verdict"], report["exit_code"] = f"REFUSED({refusal})", 2
        report["final_flatten"] = {"skipped": "no order was attempted", "residual_canary_positions": []}
        path = write_report(env, report, stamp)
        log(f"{report['verdict']}\nreport: {path}")
        return 2, report

    old_handlers: dict[int, Any] = {}
    flatten: dict[str, Any] | None = None
    timed_out = False
    try:
        if env.install_signals and threading.current_thread() is threading.main_thread():
            def on_signal(signum: int, _frame: Any) -> None:
                canary.abort_reason = f"signal:{signum}"
                canary.abort.set()

            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    with contextlib.suppress(ValueError, OSError):
                        old_handlers[sig] = signal.signal(sig, on_signal)

        canary.deadline = time.monotonic() + env.timeout_s
        worker = threading.Thread(target=run_sequence, args=(canary,), name="e2-canary-seq", daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.25)
            if time.monotonic() > canary.deadline and worker.is_alive() and not canary.abort.is_set():
                canary.abort_reason = "timeout"
                canary.abort.set()
                timed_out = True
            if canary.abort.is_set():
                if canary.abort_reason == "timeout":
                    timed_out = True
                worker.join(15.0)  # the sequence stops at the next check; a stuck broker call is bounded by its own timeouts
                break
        report["steps"] = canary.steps
        if worker.is_alive():
            report["notes"].append("sequence thread still busy after the abort grace: flattening concurrently")
    finally:
        for sig, handler in old_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)
        try:
            flatten = safety_flatten(canary)
        except Exception as exc:
            flatten = {"attempts": [f"safety_flatten crashed: {type(exc).__name__}: {exc}"], "residual_canary_positions": None}
        if canary.stack is not None:
            with contextlib.suppress(Exception):
                canary.stack.stop()
        report["steps"] = canary.steps
        report["latencies_ms"] = canary.latencies
        report["execution_quality"] = canary.quality
        report["final_flatten"] = flatten
        report["restart"] = canary.data.get("restart")
        report["registry_status_after_close"] = canary.data.get("registry_status_after_close")
        verdict, code = _verdict(canary.steps, flatten, timed_out)
        report["verdict"], report["exit_code"] = verdict, code
        record = _canary_trade_record(canary, report.get("account_id_hash"))
        path = write_report(env, report, stamp)
        if record is not None:
            (env.artifacts_dir / f"canary_trade_{stamp}.json").write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
        log(f"VERDICT: {verdict}\nreport: {path}")
    return report["exit_code"], report


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _static_plan(market: str, price: Decimal | None) -> int:
    from demo.execution.market_config import load_demo_market_specs

    specs = load_demo_market_specs()
    spec = specs.get(market)
    if spec is None:
        print(f"REFUSED: no checked-in market spec for {market}", file=sys.stderr)
        return 2
    print(f"E2 execution-contract canary - STATIC PLAN (no broker interaction), market {market}")
    print(f"  checked-in spec: min lot {spec.volume_min}, step {spec.volume_step}, tick {spec.tick_size}, max_spread {spec.max_spread}")
    print(f"  total size {SIZE_MULTIPLE} x min lot = {spec.volume_min * SIZE_MULTIPLE}; partial = {spec.volume_min}")
    print("  initial stop distance = round_up_tick(max(10 x spread, 3 x stops_level points, 3 x freeze_level points, 0.15 % of price))")
    if price is not None:
        dist = _round_tick(STRUCTURAL_PLACEHOLDER_FRACTION * price, spec.tick_size, "up")
        print(f"  at price {price}: structural placeholder distance {dist} -> stop {price - dist}, TP1 plan stage {price + 3 * dist} (spread / stops_level terms need the live broker)")
    print(f"  magic {CANARY_MAGIC}, tag {TRADE_TYPE_TAG}, exit policy staged (TP1 price stage close_fraction 0.5 + runner, no broker TP)")
    for line in STATIC_PLAN_STEPS:
        print("  " + line)
    print("  ALWAYS: try/finally + signals + hard timeout -> flatten reduce-only (emergency_close by ticket as fallback)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="E2 execution-contract canary (ActivTrades DEMO only)")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--fake", action="store_true", help="FakeMT5Broker (tests / CI)")
    modes.add_argument("--live", action="store_true", help="real ActivTrades DEMO terminal (attach-only)")
    p.add_argument("--dry-run-plan", action="store_true", help="print steps + computed numbers; places nothing")
    p.add_argument("--market", default=DEFAULT_MARKET)
    p.add_argument("--confirm-demo-canary", default=None, metavar="PHRASE")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--artifacts", type=Path, default=None, help="--fake only (live is pinned to artifacts/e2_canary)")
    p.add_argument("--trader-artifacts", type=Path, default=None)
    p.add_argument("--expected-account-hash", default=None)
    p.add_argument("--plan-price", type=Decimal, default=None, help="static plan only: reference price for the example geometry")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (os.environ.get("MT5_ALLOW_ACCOUNT_LOGIN") or "").strip() == "1":
        print("REFUSED: MT5_ALLOW_ACCOUNT_LOGIN=1 is not permitted; the canary only attaches.", file=sys.stderr)
        return 2
    if not (args.fake or args.live):
        if args.dry_run_plan:
            return _static_plan(args.market, args.plan_price)
        print("REFUSED: choose --fake, --live or --dry-run-plan", file=sys.stderr)
        return 2
    plan_only = bool(args.dry_run_plan)
    if args.live:
        if not plan_only and args.confirm_demo_canary != CONFIRM_VALUE:
            print(f"REFUSED: --live requires {CONFIRM_FLAG}={CONFIRM_VALUE}", file=sys.stderr)
            return 2
        if args.artifacts is not None and args.artifacts.resolve() != REQUIRED_ARTIFACTS.resolve():
            print(f"REFUSED: --live artifacts dir is pinned to {REQUIRED_ARTIFACTS}", file=sys.stderr)
            return 2
        from adapters.activtrades_mt5.real_client import get_real_client
        from adapters.config import MT5ConfigError, load_attach_only_config
        from demo.runner import EXPECTED_DEMO_SERVER

        try:
            connection = load_attach_only_config()
        except MT5ConfigError as exc:
            print(f"REFUSED: attach-only config unusable: {exc}", file=sys.stderr)
            return 2
        trader = args.trader_artifacts or TRADER_ARTIFACTS
        expected = args.expected_account_hash or discover_expected_hash(trader)[0]
        env = CanaryEnv(
            mode="live", client=get_real_client(), connection=connection, artifacts_dir=REQUIRED_ARTIFACTS,
            trader_artifacts_dir=trader, mt5_lock_path=None, expected_account_hash=expected,
            expected_server=os.environ.get("DEMO_TRADER_EXPECTED_SERVER", EXPECTED_DEMO_SERVER), market=args.market,
            timeout_s=args.timeout, confirm=args.confirm_demo_canary, install_signals=True,
        )
    else:
        from adapters.config import MT5ConnectionConfig
        from tests.unit.demo.execution.stack_harness import build_broker, connection

        art = args.artifacts or (REPO_ROOT / "artifacts" / "e2_canary_fake")
        broker = build_broker()
        conn: MT5ConnectionConfig = connection(broker)
        env = CanaryEnv(
            mode="fake", client=broker, connection=conn, artifacts_dir=art,
            trader_artifacts_dir=args.trader_artifacts or (art / "no_trader"), mt5_lock_path=art / "terminal.lock",
            expected_account_hash=args.expected_account_hash or login_hash(broker.cfg.login), expected_server=None,
            market=args.market, timeout_s=args.timeout, confirm=args.confirm_demo_canary, install_signals=True,
            stack_overrides=dict(sync_interval_s=0.05, lock_heartbeat_s=0.2, bar_min_refetch_s=0.0, reconcile_retry_s=0.0,
                                 disconnect_grace_s=0.5, close_grace_s=0.5, start_timeout_s=30.0),
        )
    try:
        code, _report = run_canary(env, plan_only=plan_only)
    except Exception as exc:
        print(f"INTERNAL ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    return code


if __name__ == "__main__":
    sys.exit(main())
