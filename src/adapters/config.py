"""Typed configuration loading for the (not-yet-built) MT5/ActivTrades adapter.

This module only reads environment variables into a typed, validated
dataclass. It never imports the `MetaTrader5` package and never attempts a
connection -- that is the parallel adapter worker's job. Per `CLAUDE.md`,
credentials must never live in source or configuration files; the only
supported source is the process environment (see `.env.example` at the repo
root for the local-only `.env` convention).
"""

from dataclasses import dataclass

_REQUIRED_VARS = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
_LOGIN_VAR = "MT5_LOGIN"
_PASSWORD_VAR = "MT5_PASSWORD"
_SERVER_VAR = "MT5_SERVER"
_TERMINAL_PATH_VAR = "MT5_TERMINAL_PATH"


class MT5ConfigError(RuntimeError):
    """Raised when required MT5 connection configuration is missing or invalid.

    Distinct from `KeyError`/`ValueError` so callers (scripts, tests) can
    catch exactly this and print an operator-actionable message instead of a
    raw traceback.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class MT5ConnectionConfig:
    """MT5 terminal/account connection parameters, loaded from the environment.

    Never logged or printed in full by any caller -- in particular
    `password` must never appear in output (see `scripts/mt5_*.py` headers).
    """

    login: int
    password: str
    server: str
    terminal_path: str | None = None


def load_mt5_connection_config(env: dict[str, str] | None = None) -> MT5ConnectionConfig:
    """Load `MT5ConnectionConfig` from environment variables.

    Reads `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` (all required) and the
    optional `MT5_TERMINAL_PATH`. Raises `MT5ConfigError` with a clear,
    actionable message (naming every missing variable, never a bare
    `KeyError`) when a required value is missing or `MT5_LOGIN` is not a
    valid integer account number.

    `env` defaults to `os.environ` but can be overridden (e.g. in tests) to
    avoid mutating global process state.
    """
    if env is None:
        import os

        env = dict(os.environ)

    missing = [name for name in _REQUIRED_VARS if not env.get(name)]
    if missing:
        raise MT5ConfigError(
            "Missing required MT5 connection environment variable(s): "
            f"{', '.join(missing)}. Set them locally (see .env.example at the repo root) -- "
            "never commit real values."
        )

    raw_login = env[_LOGIN_VAR]
    try:
        login = int(raw_login)
    except ValueError as exc:
        raise MT5ConfigError(
            f"{_LOGIN_VAR} must be an integer MT5 account number, got {raw_login!r}."
        ) from exc

    return MT5ConnectionConfig(
        login=login,
        password=env[_PASSWORD_VAR],
        server=env[_SERVER_VAR],
        terminal_path=env.get(_TERMINAL_PATH_VAR) or None,
    )
