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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from adapters.activtrades_mt5.client import MT5ClientProtocol
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

    def __init__(self, client: MT5ClientProtocol) -> None:
        self._client = client
        self.state: ConnectionState = ConnectionState.DISCONNECTED

    # -- connect / disconnect --------------------------------------------

    def connect(self, config: MT5ConnectionConfig) -> ConnectionResult:
        self.state = ConnectionState.CONNECTING
        try:
            success = self._client.initialize(
                config.terminal_path,
                login=config.login,
                password=config.password,
                server=config.server,
            )
        except Exception as exc:  # never raise past this boundary
            self.state = ConnectionState.ERROR
            return ConnectionResult(
                success=False,
                state=self.state,
                reason=f"initialize() raised: {exc}",
            )

        if not success:
            error_code, error_description = self._safe_last_error()
            self.state = ConnectionState.ERROR
            return ConnectionResult(
                success=False,
                state=self.state,
                reason="initialize() returned False",
                error_code=error_code,
                error_description=error_description,
            )

        self.state = ConnectionState.CONNECTED
        return ConnectionResult(success=True, state=self.state, reason="connected")

    def _safe_last_error(self) -> tuple[int | None, str | None]:
        try:
            code, description = self._client.last_error()
            return int(code), str(description)
        except Exception:
            return None, None

    def disconnect(self) -> None:
        try:
            self._client.shutdown()
        finally:
            self.state = ConnectionState.DISCONNECTED

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
