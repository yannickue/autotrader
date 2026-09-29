"""Connection/health state machine for the MT5/ActivTrades adapter.

Mirrors this repo's existing fail-closed philosophy
(`src/risk/engine.py`, `src/execution/paper.py`): an explicit state enum, and
a single method (`is_new_exposure_allowed`) that computes "is new exposure
currently allowed" from that state -- never inferred implicitly elsewhere.

Critical distinction (directive-mandated): `CONNECTED != READY`. `READY`
additionally requires account state to be readable, every required symbol to
be usable with a fresh quote, and (in spirit, until a real reconciliation
data source exists for this adapter) a `RECONCILING` pass to have run.
"""

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from adapters.activtrades_mt5.client import MT5ClientProtocol
from adapters.activtrades_mt5.diagnostics import ConnectionDiagnosticCategory
from adapters.activtrades_mt5.lock import DEFAULT_LOCK_PATH, MT5Lock, acquire_mt5_lock
from adapters.config import MT5ConnectionConfig


class ConnectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    RECONCILING = "RECONCILING"
    READY = "READY"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionResult:
    """Outcome of a `connect()` call. Never raises past `connect()`'s
    boundary -- a caller always gets a typed result to inspect instead."""

    success: bool
    state: ConnectionState
    reason: str
    error_code: int | None = None
    error_description: str | None = None
    category: ConnectionDiagnosticCategory | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadinessResult:
    """Outcome of a `check_ready()` call."""

    ready: bool
    state: ConnectionState
    reason: str
    missing_symbols: tuple[str, ...] = ()
    stale_symbols: tuple[str, ...] = ()


class MT5Connection:
    """Wraps an `MT5ClientProtocol` instance (constructor-injected, so tests
    pass `testing.FakeMT5Client`) and tracks explicit connection/health
    state.

    Never keeps an `MT5ConnectionConfig` as a long-lived attribute: `connect`
    takes it as a local parameter only, so the password it carries is never
    reachable from `self` after `client.initialize(...)` returns -- it lives
    only as long as this one call's stack frame (and whatever the injected
    `client` itself may retain internally, which is outside this class's
    control). This is the "your judgement" choice called for in the task:
    the simplest way to avoid holding a password longer than the one call
    that needs it, without adding an explicit-clear/`__del__` mechanism that
    Python cannot guarantee actually scrubs memory anyway.
    """

    def __init__(self, client: MT5ClientProtocol, *, lock_path: Path | None = None) -> None:
        self._client = client
        self.state: ConnectionState = ConnectionState.DISCONNECTED
        self._lock: MT5Lock | None = None
        # Injectable so tests never share this process's single real
        # `DEFAULT_LOCK_PATH` file (which would make unrelated tests
        # order-dependent/flaky) -- every real script still gets the real
        # shared lock by leaving this at its default.
        self._lock_path = lock_path if lock_path is not None else DEFAULT_LOCK_PATH

    @property
    def client(self) -> MT5ClientProtocol:
        """The wrapped `MT5ClientProtocol` instance, for callers (e.g.
        `scripts/mt5_*.py`) that need read-only calls this class doesn't
        itself expose a dedicated method for (e.g. `symbols_get()`)."""
        return self._client

    # -- connect / disconnect --------------------------------------------

    def connect(self, config: MT5ConnectionConfig) -> ConnectionResult:
        """Attach to the real MT5 terminal.

        ACCOUNT PROTECTION INVARIANT (do not weaken without an explicit,
        separate directive): by default (`config.allow_account_login is
        False`, the normal case), this calls `initialize(terminal_path)`
        with NO login/password/server -- it only ATTACHES to whatever
        account is already logged into the terminal, exactly like a second
        manual instance of the terminal would. It never authenticates and
        never can switch or log out the terminal's current account. The
        observed account's `login` is then compared against
        `config.login`; on any mismatch this fails closed
        (`ACCOUNT_MISMATCH`) rather than silently proceeding or attempting
        to log in as the configured account.

        Real authentication (`login=`/`password=`/`server=` passed to
        `initialize()`) only happens when `config.allow_account_login` is
        explicitly `True` (set via `MT5_ALLOW_ACCOUNT_LOGIN=1`, see
        `adapters/config.py`) -- never on a normal test/preflight/discovery/
        downloader run.

        Also enforces single ownership: a real MT5 connection lock
        (`adapters/activtrades_mt5/lock.py`) is acquired before any
        `initialize()` call; if another process already holds it, this
        returns `CONNECTION_BUSY` immediately without touching the real
        client at all.
        """
        self.state = ConnectionState.CONNECTING

        lock = acquire_mt5_lock(self._lock_path)
        if lock is None:
            self.state = ConnectionState.ERROR
            return ConnectionResult(
                success=False,
                state=self.state,
                reason=(
                    "MT5_CONNECTION_BUSY: another process already holds the real MT5 "
                    "connection lock -- refusing to attempt a second concurrent real "
                    "MT5 access rather than race it"
                ),
                category=ConnectionDiagnosticCategory.CONNECTION_BUSY,
            )
        self._lock = lock

        try:
            if config.allow_account_login:
                success = self._client.initialize(
                    config.terminal_path,
                    login=config.login,
                    password=config.password,
                    server=config.server,
                )
            else:
                success = self._client.initialize(config.terminal_path)
        except Exception as exc:  # never raise past this boundary
            self._release_lock()
            self.state = ConnectionState.ERROR
            return ConnectionResult(
                success=False,
                state=self.state,
                reason=f"initialize() raised: {exc}",
            )

        if not success:
            error_code, error_description = self._safe_last_error()
            self._release_lock()
            self.state = ConnectionState.ERROR
            return ConnectionResult(
                success=False,
                state=self.state,
                reason="initialize() returned False",
                error_code=error_code,
                error_description=error_description,
            )

        if not config.allow_account_login:
            mismatch = self._check_account_identity(config)
            if mismatch is not None:
                self._safe_shutdown()
                self._release_lock()
                self.state = ConnectionState.ERROR
                return mismatch

        self.state = ConnectionState.CONNECTED
        return ConnectionResult(success=True, state=self.state, reason="connected")

    def _check_account_identity(self, config: MT5ConnectionConfig) -> ConnectionResult | None:
        """Verify the account the terminal is already attached to (no login
        performed) is the expected one. Returns a failed `ConnectionResult`
        on any mismatch/unreadable state (fail closed, never guess), or
        `None` when the identity is confirmed and `connect()` may proceed.
        """
        try:
            account = self._client.account_info()
        except Exception as exc:
            return ConnectionResult(
                success=False,
                state=ConnectionState.ERROR,
                reason=f"account_info() raised after read-only attach: {exc}",
                category=ConnectionDiagnosticCategory.ACCOUNT_INFO_FAILED,
            )
        if account is None:
            return ConnectionResult(
                success=False,
                state=ConnectionState.ERROR,
                reason=(
                    "account_info() returned None after read-only attach -- cannot verify "
                    "account identity, and this adapter never logs in automatically to find "
                    "out; log in manually in the terminal first"
                ),
                category=ConnectionDiagnosticCategory.ACCOUNT_INFO_FAILED,
            )

        observed_login = getattr(account, "login", None)
        if observed_login != config.login:
            return ConnectionResult(
                success=False,
                state=ConnectionState.ERROR,
                reason=(
                    f"MT5_ACCOUNT_MISMATCH: expected login {config.login}, the terminal is "
                    f"currently attached to login {observed_login!r} instead. This adapter "
                    "never logs in or switches the terminal's account automatically -- log "
                    "in to the expected account manually, or set MT5_ALLOW_ACCOUNT_LOGIN=1 "
                    "for a one-off explicit authentication."
                ),
                category=ConnectionDiagnosticCategory.ACCOUNT_MISMATCH,
            )
        return None

    def _safe_last_error(self) -> tuple[int | None, str | None]:
        try:
            code, description = self._client.last_error()
            return int(code), str(description)
        except Exception:
            return None, None

    def _safe_shutdown(self) -> None:
        """Tear down THIS process's own IPC handle only -- never affects the
        terminal's logged-in account (MT5's `shutdown()` just closes this
        Python session's connection to the terminal, it does not log the
        terminal out)."""
        with contextlib.suppress(Exception):
            self._client.shutdown()

    def _release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def disconnect(self) -> None:
        try:
            self._client.shutdown()
        finally:
            self.state = ConnectionState.DISCONNECTED
            self._release_lock()

    # -- readiness ---------------------------------------------------------

    def check_ready(
        self,
        *,
        required_symbols: Sequence[str],
        max_quote_age: timedelta,
        now: datetime,
    ) -> ReadinessResult:
        """Caller-driven readiness check -- never runs on a hidden schedule
        (mirrors `RiskEngine`/`PaperExecutionEngine`, which only ever react
        to explicit calls, never poll on their own).

        Not callable from `DISCONNECTED`/`CONNECTING`/`ERROR`: there is no
        live client session to check anything against yet.
        """
        if self.state in (
            ConnectionState.DISCONNECTED,
            ConnectionState.CONNECTING,
            ConnectionState.ERROR,
        ):
            return ReadinessResult(
                ready=False,
                state=self.state,
                reason=f"cannot check readiness from state {self.state}; call connect() first",
            )

        self.state = ConnectionState.RECONCILING

        try:
            account = self._client.account_info()
        except Exception as exc:
            return self._degrade(f"account_info() raised: {exc}")
        if account is None:
            return self._degrade("account_info() returned None -- account state unreadable")

        missing: list[str] = []
        stale: list[str] = []
        for symbol in required_symbols:
            try:
                selected = self._client.symbol_select(symbol, True)
            except Exception:
                selected = False
            if not selected:
                missing.append(symbol)
                continue

            try:
                tick = self._client.symbol_info_tick(symbol)
            except Exception:
                tick = None
            if tick is None:
                missing.append(symbol)
                continue

            tick_time = datetime.fromtimestamp(int(tick.time), tz=now.tzinfo)
            if now - tick_time > max_quote_age:
                stale.append(symbol)

        if missing or stale:
            reason_parts = []
            if missing:
                reason_parts.append(f"missing/unselectable symbols: {', '.join(missing)}")
            if stale:
                reason_parts.append(f"stale quotes: {', '.join(stale)}")
            return self._degrade(
                "; ".join(reason_parts), missing_symbols=tuple(missing), stale_symbols=tuple(stale)
            )

        self.state = ConnectionState.READY
        return ReadinessResult(ready=True, state=self.state, reason="ready")

    def _degrade(
        self,
        reason: str,
        *,
        missing_symbols: tuple[str, ...] = (),
        stale_symbols: tuple[str, ...] = (),
    ) -> ReadinessResult:
        self.state = ConnectionState.DEGRADED
        return ReadinessResult(
            ready=False,
            state=self.state,
            reason=reason,
            missing_symbols=missing_symbols,
            stale_symbols=stale_symbols,
        )

    # -- fail-closed exposure gate -----------------------------------------

    def is_new_exposure_allowed(self) -> bool:
        """New exposure is allowed only in `READY` -- an unknown/indeterminate
        state means "NO NEW EXPOSURE", computed explicitly here rather than
        inferred by callers at each use site (mirrors
        `RiskEngine`/`PaperExecutionEngine`'s own fail-closed gates)."""
        return self.state is ConnectionState.READY
