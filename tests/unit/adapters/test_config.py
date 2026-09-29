"""Unit tests for `adapters.config` -- MT5 connection config loading.

No real MT5 env vars are ever used here; synthetic test values only.

Most tests pass an explicit `env_file` pointing at a `tmp_path` location (or
a guaranteed-nonexistent path) so they are fully isolated from this
checkout's own real, git-ignored `.env` file -- without that isolation, a
developer's real local `.env` would make "missing config" tests spuriously
pass.
"""

from pathlib import Path

import pytest

from adapters.config import MT5ConfigError, MT5ConnectionConfig, load_mt5_connection_config


def _no_env_file(tmp_path: Path) -> Path:
    """A `tmp_path` location that is guaranteed not to exist as a file."""
    return tmp_path / "does-not-exist.env"


def test_loads_all_four_values_when_present(tmp_path: Path) -> None:
    env = {
        "MT5_LOGIN": "12345678",
        "MT5_PASSWORD": "test-password",
        "MT5_SERVER": "ActivTrades-Demo",
        "MT5_TERMINAL_PATH": r"C:\Program Files\ActivTrades MT5\terminal64.exe",
    }

    config = load_mt5_connection_config(env=env, env_file=_no_env_file(tmp_path))

    assert config == MT5ConnectionConfig(
        login=12345678,
        password="test-password",
        server="ActivTrades-Demo",
        terminal_path=r"C:\Program Files\ActivTrades MT5\terminal64.exe",
    )


def test_terminal_path_is_optional_and_defaults_to_none(tmp_path: Path) -> None:
    env = {
        "MT5_LOGIN": "1",
        "MT5_PASSWORD": "p",
        "MT5_SERVER": "s",
    }

    config = load_mt5_connection_config(env=env, env_file=_no_env_file(tmp_path))

    assert config.terminal_path is None


def test_raises_clear_error_when_all_required_vars_missing(tmp_path: Path) -> None:
    with pytest.raises(MT5ConfigError, match=r"MT5_LOGIN.*MT5_PASSWORD.*MT5_SERVER"):
        load_mt5_connection_config(env={}, env_file=_no_env_file(tmp_path))


def test_raises_clear_error_naming_each_missing_var(tmp_path: Path) -> None:
    with pytest.raises(MT5ConfigError, match="MT5_SERVER"):
        load_mt5_connection_config(
            env={"MT5_LOGIN": "1", "MT5_PASSWORD": "p"}, env_file=_no_env_file(tmp_path)
        )


def test_raises_clear_error_not_a_bare_key_error(tmp_path: Path) -> None:
    # A bare KeyError would be an unhelpful crash for an operator; this must
    # be a typed, catchable, descriptive error instead.
    try:
        load_mt5_connection_config(env={}, env_file=_no_env_file(tmp_path))
    except KeyError:
        pytest.fail("load_mt5_connection_config must not raise a bare KeyError")
    except MT5ConfigError:
        pass


def test_raises_clear_error_when_login_is_not_an_integer(tmp_path: Path) -> None:
    env = {"MT5_LOGIN": "not-a-number", "MT5_PASSWORD": "p", "MT5_SERVER": "s"}

    with pytest.raises(MT5ConfigError, match="MT5_LOGIN must be an integer"):
        load_mt5_connection_config(env=env, env_file=_no_env_file(tmp_path))


def test_reads_from_os_environ_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MT5_LOGIN", "99")
    monkeypatch.setenv("MT5_PASSWORD", "pw")
    monkeypatch.setenv("MT5_SERVER", "srv")
    monkeypatch.delenv("MT5_TERMINAL_PATH", raising=False)

    config = load_mt5_connection_config(env_file=_no_env_file(tmp_path))

    assert config.login == 99
    assert config.server == "srv"


# -- .env file loading -------------------------------------------------------


def test_loads_values_from_env_file_when_process_env_unset(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'MT5_LOGIN=555\nMT5_PASSWORD="pw-from-file"\nMT5_SERVER=Demo-Server\n', encoding="utf-8"
    )

    config = load_mt5_connection_config(env={}, env_file=env_file)

    assert config.login == 555
    assert config.password == "pw-from-file"
    assert config.server == "Demo-Server"


def test_env_file_supports_comments_and_blank_lines(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n\nMT5_LOGIN=1\nMT5_PASSWORD=p\nMT5_SERVER=s\n# trailing comment\n",
        encoding="utf-8",
    )

    config = load_mt5_connection_config(env={}, env_file=env_file)

    assert config.login == 1


def test_process_env_overrides_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MT5_LOGIN=111\nMT5_PASSWORD=file-pw\nMT5_SERVER=file-server\n", encoding="utf-8"
    )
    # Process env sets only MT5_LOGIN -- it must win over the file's value
    # for that one variable, while MT5_PASSWORD/MT5_SERVER still fall back
    # to the file (process env leaves those unset).
    process_env = {"MT5_LOGIN": "222"}

    config = load_mt5_connection_config(env=process_env, env_file=env_file)

    assert config.login == 222  # process env wins
    assert config.password == "file-pw"  # falls back to .env
    assert config.server == "file-server"  # falls back to .env


def test_missing_env_file_is_not_an_error_when_process_env_is_complete(tmp_path: Path) -> None:
    env = {"MT5_LOGIN": "1", "MT5_PASSWORD": "p", "MT5_SERVER": "s"}

    config = load_mt5_connection_config(env=env, env_file=_no_env_file(tmp_path))

    assert config.login == 1


def test_missing_required_value_still_fails_clearly_when_env_file_absent(tmp_path: Path) -> None:
    with pytest.raises(MT5ConfigError, match="MT5_SERVER"):
        load_mt5_connection_config(
            env={"MT5_LOGIN": "1", "MT5_PASSWORD": "p"}, env_file=_no_env_file(tmp_path)
        )


# -- secrets never appear in repr/log/error output ---------------------------


def test_password_never_appears_in_repr(tmp_path: Path) -> None:
    env = {"MT5_LOGIN": "1", "MT5_PASSWORD": "super-secret-value", "MT5_SERVER": "s"}

    config = load_mt5_connection_config(env=env, env_file=_no_env_file(tmp_path))

    assert "super-secret-value" not in repr(config)
    assert "super-secret-value" not in str(config)
    assert "<redacted>" in repr(config)


def test_password_never_appears_in_error_message(tmp_path: Path) -> None:
    env = {"MT5_LOGIN": "not-a-number", "MT5_PASSWORD": "super-secret-value", "MT5_SERVER": "s"}

    with pytest.raises(MT5ConfigError) as exc_info:
        load_mt5_connection_config(env=env, env_file=_no_env_file(tmp_path))

    assert "super-secret-value" not in str(exc_info.value)


# -- allow_account_login: explicit, off-by-default real-auth gate -----------


def test_allow_account_login_defaults_false_when_unset(tmp_path: Path) -> None:
    env = {"MT5_LOGIN": "1", "MT5_PASSWORD": "p", "MT5_SERVER": "s"}

    config = load_mt5_connection_config(env=env, env_file=_no_env_file(tmp_path))

    assert config.allow_account_login is False


def test_allow_account_login_true_only_for_exact_string_one(tmp_path: Path) -> None:
    for value, expected in (
        ("1", True),
        ("0", False),
        ("true", False),
        ("True", False),
        ("yes", False),
        ("", False),
    ):
        env = {
            "MT5_LOGIN": "1",
            "MT5_PASSWORD": "p",
            "MT5_SERVER": "s",
            "MT5_ALLOW_ACCOUNT_LOGIN": value,
        }
        config = load_mt5_connection_config(env=env, env_file=_no_env_file(tmp_path))
        assert config.allow_account_login is expected, f"value {value!r} -> {expected}"


def test_allow_account_login_is_never_read_from_env_file(tmp_path: Path) -> None:
    """Must stay a one-off, explicit-invocation flag -- never something a
    checked-in or locally-saved `.env` can silently turn on for every
    future run."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MT5_LOGIN=1\nMT5_PASSWORD=p\nMT5_SERVER=s\nMT5_ALLOW_ACCOUNT_LOGIN=1\n",
        encoding="utf-8",
    )

    config = load_mt5_connection_config(env={}, env_file=env_file)

    assert config.allow_account_login is False
