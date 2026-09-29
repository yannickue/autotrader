"""Precise connection-diagnostic categories for the MT5/ActivTrades adapter.

Replaces one generic "connection failed" with a specific, actionable
category, classified from the real `MetaTrader5.last_error()` code (or from
conditions this adapter itself detects, e.g. a missing terminal path, before
ever calling into MT5). Real `RES_E_*` values (verified directly against the
installed `metatrader5==5.0.6231` package -- plain module attributes, not
guessed):

    RES_S_OK                    =  1
    RES_E_FAIL                  = -1
    RES_E_INVALID_PARAMS        = -2
    RES_E_NO_MEMORY             = -3
    RES_E_NOT_FOUND             = -4
    RES_E_INVALID_VERSION       = -5
    RES_E_AUTH_FAILED           = -6
    RES_E_UNSUPPORTED           = -7
    RES_E_AUTO_TRADING_DISABLED = -8
    RES_E_INTERNAL_FAIL         = -10000
    RES_E_INTERNAL_FAIL_SEND    = -10001
    RES_E_INTERNAL_FAIL_RECEIVE = -10002
    RES_E_INTERNAL_FAIL_INIT    = -10003
    RES_E_INTERNAL_FAIL_CONNECT = -10004
    RES_E_INTERNAL_FAIL_TIMEOUT = -10005

MT5 does not expose a distinct "server not found" or "account info failed"
error code separately from the generic ones above -- those two categories
are assigned by this adapter based on WHICH call failed (`initialize()` vs.
`account_info()`), not from `last_error()` alone.
"""

from __future__ import annotations

from enum import StrEnum


class ConnectionDiagnosticCategory(StrEnum):
    CONFIG_MISSING = "CONFIG_MISSING"
    TERMINAL_NOT_FOUND = "TERMINAL_NOT_FOUND"
    TERMINAL_NOT_RUNNING = "TERMINAL_NOT_RUNNING"
    MT5_INITIALIZE_FAILED = "MT5_INITIALIZE_FAILED"
    MT5_IPC_SEND_FAILED = "MT5_IPC_SEND_FAILED"
    MT5_IPC_RECV_FAILED = "MT5_IPC_RECV_FAILED"
    MT5_IPC_TIMEOUT = "MT5_IPC_TIMEOUT"
    AUTH_FAILED = "AUTH_FAILED"
    SERVER_NOT_FOUND = "SERVER_NOT_FOUND"
    ACCOUNT_INFO_FAILED = "ACCOUNT_INFO_FAILED"
    WRONG_TERMINAL_SESSION = "WRONG_TERMINAL_SESSION"
    UNKNOWN_MT5_ERROR = "UNKNOWN_MT5_ERROR"


# Real RES_E_* integer values, duplicated here (not imported from
# `MetaTrader5`) so this module stays importable/testable without the
# `metatrader5` package installed -- see module docstring for how each was
# verified against the real installed package.
_RES_E_NOT_FOUND = -4
_RES_E_AUTH_FAILED = -6
_RES_E_INTERNAL_FAIL_SEND = -10001
_RES_E_INTERNAL_FAIL_RECEIVE = -10002
_RES_E_INTERNAL_FAIL_TIMEOUT = -10005

_CODE_TO_CATEGORY: dict[int, ConnectionDiagnosticCategory] = {
    _RES_E_AUTH_FAILED: ConnectionDiagnosticCategory.AUTH_FAILED,
    _RES_E_NOT_FOUND: ConnectionDiagnosticCategory.SERVER_NOT_FOUND,
    _RES_E_INTERNAL_FAIL_SEND: ConnectionDiagnosticCategory.MT5_IPC_SEND_FAILED,
    _RES_E_INTERNAL_FAIL_RECEIVE: ConnectionDiagnosticCategory.MT5_IPC_RECV_FAILED,
    _RES_E_INTERNAL_FAIL_TIMEOUT: ConnectionDiagnosticCategory.MT5_IPC_TIMEOUT,
}


def classify_initialize_failure(error_code: int | None) -> ConnectionDiagnosticCategory:
    """Classify a failed `initialize()` call from its `last_error()` code.

    `error_code=None` (e.g. `last_error()` itself could not be read) maps to
    `MT5_INITIALIZE_FAILED` -- a generic-but-still-specific-to-initialize
    category, never silently merged with a later stage's failure.
    """
    if error_code is None:
        return ConnectionDiagnosticCategory.MT5_INITIALIZE_FAILED
    return _CODE_TO_CATEGORY.get(error_code, ConnectionDiagnosticCategory.UNKNOWN_MT5_ERROR)


def classify_account_info_failure(error_code: int | None) -> ConnectionDiagnosticCategory:
    """Classify `account_info()` returning `None` after a successful
    `initialize()` -- distinct from an initialize-stage failure, since the
    IPC channel itself is known-good at this point."""
    if error_code is not None and error_code in _CODE_TO_CATEGORY:
        return _CODE_TO_CATEGORY[error_code]
    return ConnectionDiagnosticCategory.ACCOUNT_INFO_FAILED
