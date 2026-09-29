"""Tests for `adapters.activtrades_mt5.probe` and `.diagnostics`.

Every `run_isolated_probe` call below passes an isolated, per-test
`lock_path` (under pytest's own `tmp_path`) rather than the real, process-
wide `DEFAULT_LOCK_PATH` -- keeps these tests hermetic and independent of
run order/other test files (`lock.py`/`test_lock.py` cover the lock
primitive itself; `test_probe_returns_busy_when_lock_already_held` below
only needs `run_isolated_probe` to route through whatever lock_path it's
given, which it proves with an isolated path too).
"""

from __future__ import annotations

from pathlib import Path

from adapters.activtrades_mt5.diagnostics import (
    ConnectionDiagnosticCategory,
    classify_account_info_failure,
    classify_initialize_failure,
)
from adapters.activtrades_mt5.lock import acquire_mt5_lock
from adapters.activtrades_mt5.probe import check_terminal_path, run_isolated_probe
from adapters.activtrades_mt5.testing import FakeMT5Client
from adapters.config import MT5ConnectionConfig
from tests.unit.adapters.activtrades_mt5.conftest import make_account_info

_CONFIG = MT5ConnectionConfig(login=1, password="secret-pw", server="srv", terminal_path=None)


def _probe(config: MT5ConnectionConfig, client: FakeMT5Client, tmp_path: Path):
    return run_isolated_probe(config, client, lock_path=tmp_path / "mt5.lock")


# -- diagnostics classification ----------------------------------------------


def test_classify_initialize_failure_maps_real_ipc_codes() -> None:
    assert (
        classify_initialize_failure(-10001) is ConnectionDiagnosticCategory.MT5_IPC_SEND_FAILED
    )
    assert (
        classify_initialize_failure(-10002) is ConnectionDiagnosticCategory.MT5_IPC_RECV_FAILED
    )
    assert classify_initialize_failure(-10005) is ConnectionDiagnosticCategory.MT5_IPC_TIMEOUT
    assert classify_initialize_failure(-6) is ConnectionDiagnosticCategory.AUTH_FAILED
    assert classify_initialize_failure(-4) is ConnectionDiagnosticCategory.SERVER_NOT_FOUND


def test_classify_initialize_failure_none_code_is_generic() -> None:
    assert (
        classify_initialize_failure(None) is ConnectionDiagnosticCategory.MT5_INITIALIZE_FAILED
    )


def test_classify_initialize_failure_unrecognized_code_is_unknown() -> None:
    assert classify_initialize_failure(-99999) is ConnectionDiagnosticCategory.UNKNOWN_MT5_ERROR


def test_classify_account_info_failure_defaults_to_account_info_failed() -> None:
    assert (
        classify_account_info_failure(None)
        is ConnectionDiagnosticCategory.ACCOUNT_INFO_FAILED
    )


def test_classify_account_info_failure_still_uses_ipc_code_when_present() -> None:
    assert (
        classify_account_info_failure(-10005) is ConnectionDiagnosticCategory.MT5_IPC_TIMEOUT
    )


# -- check_terminal_path ------------------------------------------------------


def test_check_terminal_path_none_is_fine() -> None:
    assert check_terminal_path(None) is None


def test_check_terminal_path_missing_file_is_terminal_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist" / "terminal64.exe"
    assert check_terminal_path(str(missing)) is ConnectionDiagnosticCategory.TERMINAL_NOT_FOUND


def test_check_terminal_path_existing_file_is_fine(tmp_path: Path) -> None:
    real_file = tmp_path / "terminal64.exe"
    real_file.write_text("not a real binary, just needs to exist", encoding="utf-8")
    assert check_terminal_path(str(real_file)) is None


# -- run_isolated_probe: success path -----------------------------------------


def test_probe_success_reports_pass_and_shuts_down(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_version(("5", "6231", "1 Jan 2026"))
    client.set_terminal_info(object())
    client.set_account_info(make_account_info(login=_CONFIG.login))

    report = _probe(_CONFIG, client, tmp_path)

    assert report.success is True
    assert report.category is None
    assert report.terminal_info_ok is True
    assert report.account_info_ok is True
    assert client.shutdown_calls == 1


# -- run_isolated_probe: failure paths, each with a distinct category --------


def test_probe_initialize_returns_false_classified_and_no_shutdown_needed(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(False, error=(-10005, "IPC timeout"))

    report = _probe(_CONFIG, client, tmp_path)

    assert report.success is False
    assert report.category is ConnectionDiagnosticCategory.MT5_IPC_TIMEOUT
    assert report.mt5_error_code == -10005
    # initialize() never succeeded, so there is nothing to shut down.
    assert client.shutdown_calls == 0


def test_probe_initialize_raises_is_classified_as_initialize_failed(tmp_path: Path) -> None:
    class RaisingClient(FakeMT5Client):
        def initialize(self, *args, **kwargs):
            raise RuntimeError("boom")

    client = RaisingClient()

    report = _probe(_CONFIG, client, tmp_path)

    assert report.success is False
    assert report.category is ConnectionDiagnosticCategory.MT5_INITIALIZE_FAILED
    assert "boom" in report.message


def test_probe_account_info_none_after_success_is_account_info_failed(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(None)

    report = _probe(_CONFIG, client, tmp_path)

    assert report.success is False
    assert report.category is ConnectionDiagnosticCategory.ACCOUNT_INFO_FAILED
    # initialize() DID succeed here -- shutdown must still be attempted.
    assert client.shutdown_calls == 1


def test_probe_missing_terminal_path_never_calls_initialize(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "terminal64.exe"
    config = MT5ConnectionConfig(login=1, password="pw", server="srv", terminal_path=str(missing))
    client = FakeMT5Client()

    report = _probe(config, client, tmp_path)

    assert report.success is False
    assert report.category is ConnectionDiagnosticCategory.TERMINAL_NOT_FOUND
    assert client.initialize_calls == []


# -- account protection invariant --------------------------------------------


def test_probe_sends_no_credentials_by_default(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info(login=_CONFIG.login))

    _probe(_CONFIG, client, tmp_path)

    assert len(client.initialize_calls) == 1
    assert client.initialize_calls[0]["login"] is None
    assert client.initialize_calls[0]["server"] is None


def test_probe_account_mismatch_fails_closed(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(make_account_info(login=_CONFIG.login + 1))

    report = _probe(_CONFIG, client, tmp_path)

    assert report.success is False
    assert report.category is ConnectionDiagnosticCategory.ACCOUNT_MISMATCH
    assert "MT5_ACCOUNT_MISMATCH" in report.message
    assert client.shutdown_calls == 1


def test_probe_returns_busy_when_lock_already_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "mt5.lock"
    held = acquire_mt5_lock(lock_path)
    assert held is not None
    try:
        client = FakeMT5Client()
        report = run_isolated_probe(_CONFIG, client, lock_path=lock_path)

        assert report.success is False
        assert report.category is ConnectionDiagnosticCategory.CONNECTION_BUSY
        assert client.initialize_calls == []
    finally:
        held.release()


# -- password never leaks -----------------------------------------------------


def test_probe_report_never_contains_password(tmp_path: Path) -> None:
    client = FakeMT5Client()
    client.set_initialize_result(False, error=(-6, "Authorization failed"))

    report = _probe(_CONFIG, client, tmp_path)

    assert "secret-pw" not in report.message
    assert report.mt5_error_description is not None
    assert "secret-pw" not in report.mt5_error_description
