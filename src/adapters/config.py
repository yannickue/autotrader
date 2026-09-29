"""Central, typed configuration loading for the MT5/ActivTrades adapter.

This is the ONE place that reads MT5 connection configuration -- every
`scripts/mt5_*.py` entry point calls `load_mt5_connection_config()` rather
than inventing its own environment/`.env` handling. This module never
imports the `MetaTrader5` package and never attempts a connection; per
`CLAUDE.md`, credentials must never live in source or tracked configuration
files.

Precedence (highest first):
    1. an already-set process environment variable
    2. the local, git-ignored `.env` file (see `.env.example`)
    3. safe defaults, only for non-secret optional settings (there are none
       today -- `MT5_TERMINAL_PATH` has no default; MT5 itself locates the
       terminal when unset)

A process environment variable is NEVER overridden by `.env` -- `.env` only
fills gaps the process environment leaves open. `.env` is parsed, never
executed (no shell interpolation, no code execution), and a parse error in
`.env` is reported clearly rather than silently ignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_REQUIRED_VARS = ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
_LOGIN_VAR = "MT5_LOGIN"
_PASSWORD_VAR = "MT5_PASSWORD"
_SERVER_VAR = "MT5_SERVER"
_TERMINAL_PATH_VAR = "MT5_TERMINAL_PATH"
_ALL_VARS = (*_REQUIRED_VARS, _TERMINAL_PATH_VAR)

_REDACTED = "<redacted>"


class MT5ConfigError(RuntimeError):
    """Raised when required MT5 connection configuration is missing or invalid.

    Distinct from `KeyError`/`ValueError` so callers (scripts, tests) can
    catch exactly this and print an operator-actionable message instead of a
    raw traceback. Never includes a secret value in its message.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class MT5ConnectionConfig:
    """MT5 terminal/account connection parameters, loaded from the environment.

    `__repr__`/`__str__` are overridden to redact `password` -- the default
    dataclass repr would otherwise print it verbatim into any log, traceback,
    or debugger output that touches this object. Never print `.password`
    directly either; it exists only to be handed to the MT5 client's
    `initialize()` call.
    """

    login: int
    password: str
    server: str
    terminal_path: str | None = None

    def __repr__(self) -> str:
        return (
            f"MT5ConnectionConfig(login={self.login!r}, password={_REDACTED}, "
            f"server={self.server!r}, terminal_path={self.terminal_path!r})"
        )

    __str__ = __repr__


def _find_repo_root(start: Path) -> Path | None:
    """Walk upward from `start` looking for a directory containing
    `pyproject.toml`. Returns `None` if none is found (e.g. this module is
    used outside a checkout) rather than guessing."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return None


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a `.env`-style file into a plain dict.

    Supports `KEY=value`, `KEY="quoted value"`, `KEY='quoted value'`, blank
    lines, and `#`-prefixed comment lines. Never executes the file's
    content (no shell interpolation, no `eval`) -- this is a deliberately
    minimal, dependency-free parser, not a shell.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise MT5ConfigError(f"{path}:{lineno}: expected KEY=value, got: {raw_line!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_mt5_connection_config(
    env: dict[str, str] | None = None, *, env_file: Path | None = None
) -> MT5ConnectionConfig:
    """Load `MT5ConnectionConfig`, merging the process environment with the
    local `.env` file (process environment always wins).

    Reads `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` (all required) and the
    optional `MT5_TERMINAL_PATH`. Raises `MT5ConfigError` with a clear,
    actionable message (naming every missing variable, never a bare
    `KeyError`, never a secret value) when a required value is missing or
    `MT5_LOGIN` is not a valid integer account number.

    `env` defaults to `os.environ` but can be overridden (e.g. in tests) to
    avoid mutating global process state -- when overridden, `.env` is still
    consulted for any variable `env` itself doesn't set, matching real
    process-environment-first precedence. `env_file` defaults to `.env` at
    the detected repository root; pass an explicit path (e.g. a `tmp_path`
    fixture) in tests.
    """
    process_env = dict(os.environ) if env is None else dict(env)

    if env_file is None:
        repo_root = _find_repo_root(Path(__file__).parent)
        env_file = (repo_root / ".env") if repo_root is not None else None

    file_values = _parse_env_file(env_file) if env_file is not None else {}

    merged: dict[str, str] = {}
    for name in _ALL_VARS:
        if process_env.get(name):
            merged[name] = process_env[name]
        elif file_values.get(name):
            merged[name] = file_values[name]

    missing = [name for name in _REQUIRED_VARS if name not in merged]
    if missing:
        raise MT5ConfigError(
            "Missing required MT5 connection environment variable(s): "
            f"{', '.join(missing)}. Set them locally (see .env.example at the repo root) -- "
            "never commit real values."
        )

    raw_login = merged[_LOGIN_VAR]
    try:
        login = int(raw_login)
    except ValueError as exc:
        raise MT5ConfigError(
            f"{_LOGIN_VAR} must be an integer MT5 account number, got {raw_login!r}."
        ) from exc

    return MT5ConnectionConfig(
        login=login,
        password=merged[_PASSWORD_VAR],
        server=merged[_SERVER_VAR],
        terminal_path=merged.get(_TERMINAL_PATH_VAR) or None,
    )
