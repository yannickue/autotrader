"""Shared MT5 session for the Nautilus data + execution clients.

Wraps the existing `MT5Connection` (ATTACH ONLY, expected-account verification,
single-owner lock, stale-lock recovery). Adds deterministic connection state:

    DISCONNECTED -> CONNECTED -> DEGRADED -> (reconnect) CONNECTED
                        \\-> FAILED (account mismatch / lock busy: terminal, no auto retry)

Safety properties (tested):
- `MT5_ALLOW_ACCOUNT_LOGIN=1` configs are REFUSED at construction: the adapter
  never logs in and never switches account, on first connect or on reconnect.
- Every (re)connect bumps `generation`; consumers must treat a changed
  generation as "reconciliation required" -- reconnect NEVER implies RECONCILED.
- A failed call (exception / None result) probes `terminal_info().connected`;
  a lost terminal flips the session to DEGRADED instead of raising into the
  Nautilus event loop as an unexplained error.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from adapters.activtrades_mt5.connection import ConnectionResult, MT5Connection
from adapters.activtrades_mt5.history import ServerTimePolicy
from adapters.config import MT5ConnectionConfig
from nautilus_mt5.constants import MarginMode


class AttachOnlyViolation(RuntimeError):
    """The adapter was configured to authenticate; C5 forbids that."""


class _LaneGuardedClient:
    """Attribute-access proxy: every MT5 client method asserts it runs on the MT5 lane."""

    def __init__(self, client: Any, lane: Any) -> None:
        self._client = client
        self._lane = lane

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def guarded(*args: Any, **kwargs: Any) -> Any:
            self._lane.assert_in_lane(f"MT5 client.{name}")
            # REAL-API QUIRK (found on the ActivTrades terminal): forwarding an EMPTY **kwargs makes
            # MetaTrader5 builtins reject positional arguments ("Unnamed arguments not allowed").
            return attr(*args, **kwargs) if kwargs else attr(*args)

        return guarded


class Mt5CallError(RuntimeError):
    """An MT5 call failed (exception or None result). `last_error` is preserved."""

    def __init__(self, what: str, last_error: tuple[int, str] | None) -> None:
        super().__init__(f"{what} failed (last_error={last_error})")
        self.what = what
        self.last_error = last_error


class UnsupportedAccountMode(RuntimeError):
    """Account margin mode is not one v1 can honour (fail closed)."""


class SessionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class Mt5Session:
    def __init__(
        self,
        client: Any,
        config: MT5ConnectionConfig,
        *,
        lock_path: Path | None = None,
        time_policy: ServerTimePolicy | None = None,
        lane: Any = None,
    ) -> None:
        if config.allow_account_login:
            raise AttachOnlyViolation(
                "MT5_ALLOW_ACCOUNT_LOGIN is set: the Nautilus adapter is attach-only "
                "(no login, no account switching, not even on reconnect)"
            )
        self._client = client
        self._config = config
        # Optional MT5 execution lane (nautilus_mt5.executor.Mt5Executor). When set, EVERY MT5
        # call must run on its thread; anything else raises LaneViolation.
        self.lane = lane
        self._connection = MT5Connection(client, lock_path=lock_path)
        self.time_policy = time_policy or ServerTimePolicy()
        self.state = SessionState.DISCONNECTED
        self.generation = 0
        self.last_failure: str | None = None
        self.connect_attempts = 0
        self._users = 0
        self._listeners: list[Callable[[SessionState], None]] = []

    # -- state -----------------------------------------------------------------

    @property
    def expected_login(self) -> int | None:
        """The account this session was pinned to at construction (never changes)."""
        return int(self._config.login)

    @property
    def client(self) -> Any:
        """The MT5 client. With a lane configured this is a guard proxy that refuses calls made
        from any thread but the lane's (raw access would bypass the serialization)."""
        if self.lane is None:
            return self._client
        return _LaneGuardedClient(self._client, self.lane)

    @property
    def is_connected(self) -> bool:
        return self.state is SessionState.CONNECTED

    def subscribe(self, listener: Callable[[SessionState], None]) -> None:
        self._listeners.append(listener)

    def _set(self, state: SessionState) -> None:
        if state is not self.state:
            self.state = state
            for listener in list(self._listeners):
                listener(state)

    # -- connect / disconnect / reconnect -----------------------------------------

    def acquire(self) -> ConnectionResult | None:
        """Reference-counted connect: first user connects, others share the session."""
        if self.is_connected:
            self._users += 1
            return None
        result = self.connect()
        if result.success:
            self._users += 1  # count only users that actually hold a live connection
        return result

    def release(self) -> None:
        self._users = max(0, self._users - 1)
        if self._users == 0:
            self.disconnect()

    def _guard_lane(self, what: str) -> None:
        if self.lane is not None:
            self.lane.assert_in_lane(what)

    def connect(self) -> ConnectionResult:
        self._guard_lane("MT5 connect")
        self.connect_attempts += 1
        result = self._connection.connect(self._config)
        if result.success:
            self.generation += 1
            self.last_failure = None
            self._set(SessionState.CONNECTED)
        else:
            self.last_failure = result.reason
            # Account mismatch / lock busy are not transient: do not pretend we can retry.
            self._set(SessionState.FAILED)
        return result

    def disconnect(self) -> None:
        self._guard_lane("MT5 disconnect")
        self._connection.disconnect()
        self._set(SessionState.DISCONNECTED)

    def reconnect(self) -> ConnectionResult:
        """Drop and re-attach. Attach-only: never logs in, never switches account."""
        self._guard_lane("MT5 reconnect")
        self._connection.disconnect()
        return self.connect()

    def mark_degraded(self, reason: str) -> None:
        self.last_failure = reason
        if self.state is SessionState.CONNECTED:
            self._set(SessionState.DEGRADED)

    # -- guarded calls -----------------------------------------------------------------

    def last_error(self) -> tuple[int, str] | None:
        try:
            return self._client.last_error()
        except Exception:
            return None

    def _terminal_connected(self) -> bool:
        try:
            info = self._client.terminal_info()
        except Exception:
            return False
        return info is not None and bool(getattr(info, "connected", False))

    def call(
        self,
        what: str,
        function: Callable[..., Any],
        *args: Any,
        none_ok: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Run one MT5 call. Exceptions and None results become `Mt5CallError`; a lost
        terminal additionally degrades the session (=> reconciliation required)."""
        self._guard_lane(f"MT5 call {what}")
        if self.state not in (SessionState.CONNECTED,):
            raise Mt5CallError(what, (0, f"session {self.state}"))
        try:
            # Never forward an empty **kwargs to a MetaTrader5 builtin (see _LaneGuardedClient).
            result = function(*args, **kwargs) if kwargs else function(*args)
        except Exception as exc:
            self._on_failure(what, str(exc))
            raise Mt5CallError(what, self.last_error()) from exc
        if result is None and not none_ok:
            self._on_failure(what, "None result")
            raise Mt5CallError(what, self.last_error())
        return result

    def _on_failure(self, what: str, detail: str) -> None:
        if not self._terminal_connected():
            self.mark_degraded(f"{what}: {detail}; terminal not connected")

    # -- account mode --------------------------------------------------------------------

    def account_mode(self) -> MarginMode:
        """Detect (never assume) the account margin mode; only RETAIL_NETTING is honoured.

        Observed ActivTrades demo: margin_mode=0 (RETAIL_NETTING). HEDGING would let a
        second same-symbol position open and silently break the v1 one-position invariant;
        EXCHANGE is netting-like but unobserved. Both fail closed."""
        account = self.call("account_info", self._client.account_info)
        raw = getattr(account, "margin_mode", None)
        try:
            mode = MarginMode(int(raw))
        except (TypeError, ValueError):
            raise UnsupportedAccountMode(f"unknown account margin_mode {raw!r}") from None
        if mode is not MarginMode.RETAIL_NETTING:
            raise UnsupportedAccountMode(
                f"account margin_mode {mode.name} unsupported in v1 (only RETAIL_NETTING)"
            )
        return mode
