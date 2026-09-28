"""Unit tests for `adapters.config` -- MT5 connection config loading.

No real MT5 env vars are ever used here; `monkeypatch.setenv` sets synthetic
test values only, and `load_mt5_connection_config` is always called with an
explicit `env` dict (never `os.environ`) so these tests cannot leak into or
be affected by the real process environment.
"""

import pytest

from adapters.config import MT5ConfigError, MT5ConnectionConfig, load_mt5_connection_config


def test_loads_all_four_values_when_present() -> None:
    env = {
        "MT5_LOGIN": "12345678",
        "MT5_PASSWORD": "test-password",
        "MT5_SERVER": "ActivTrades-Demo",
        "MT5_TERMINAL_PATH": r"C:\Program Files\ActivTrades MT5\terminal64.exe",
    }

    config = load_mt5_connection_config(env=env)

    assert config == MT5ConnectionConfig(
        login=12345678,
        password="test-password",
        server="ActivTrades-Demo",
        terminal_path=r"C:\Program Files\ActivTrades MT5\terminal64.exe",
    )


def test_terminal_path_is_optional_and_defaults_to_none() -> None:
    env = {
        "MT5_LOGIN": "1",
        "MT5_PASSWORD": "p",
        "MT5_SERVER": "s",
    }

    config = load_mt5_connection_config(env=env)

    assert config.terminal_path is None


def test_raises_clear_error_when_all_required_vars_missing() -> None:
    with pytest.raises(MT5ConfigError, match=r"MT5_LOGIN.*MT5_PASSWORD.*MT5_SERVER"):
        load_mt5_connection_config(env={})


def test_raises_clear_error_naming_each_missing_var() -> None:
    with pytest.raises(MT5ConfigError, match="MT5_SERVER"):
        load_mt5_connection_config(env={"MT5_LOGIN": "1", "MT5_PASSWORD": "p"})


def test_raises_clear_error_not_a_bare_key_error() -> None:
    # A bare KeyError would be an unhelpful crash for an operator; this must
    # be a typed, catchable, descriptive error instead.
    try:
        load_mt5_connection_config(env={})
    except KeyError:
        pytest.fail("load_mt5_connection_config must not raise a bare KeyError")
    except MT5ConfigError:
        pass


def test_raises_clear_error_when_login_is_not_an_integer() -> None:
    env = {"MT5_LOGIN": "not-a-number", "MT5_PASSWORD": "p", "MT5_SERVER": "s"}

    with pytest.raises(MT5ConfigError, match="MT5_LOGIN must be an integer"):
        load_mt5_connection_config(env=env)


def test_reads_from_os_environ_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MT5_LOGIN", "99")
    monkeypatch.setenv("MT5_PASSWORD", "pw")
    monkeypatch.setenv("MT5_SERVER", "srv")
    monkeypatch.delenv("MT5_TERMINAL_PATH", raising=False)

    config = load_mt5_connection_config()

    assert config.login == 99
    assert config.server == "srv"
