"""Attach-only tooling must refuse an inherited MT5_ALLOW_ACCOUNT_LOGIN=1."""

import pytest

from adapters.config import MT5ConfigError, load_attach_only_config, load_mt5_connection_config

BASE_ENV = {
    "MT5_LOGIN": "12345",
    "MT5_PASSWORD": "x",
    "MT5_SERVER": "Demo-Server",
    "MT5_TERMINAL_PATH": "C:/terminal64.exe",
}


def test_attach_only_config_loads_without_opt_in(tmp_path):
    cfg = load_attach_only_config(dict(BASE_ENV), env_file=tmp_path / "none.env")
    assert cfg.allow_account_login is False


def test_attach_only_config_refuses_login_opt_in(tmp_path):
    env = {**BASE_ENV, "MT5_ALLOW_ACCOUNT_LOGIN": "1"}
    assert load_mt5_connection_config(env, env_file=tmp_path / "none.env").allow_account_login
    with pytest.raises(MT5ConfigError, match="attach-only"):
        load_attach_only_config(env, env_file=tmp_path / "none.env")
