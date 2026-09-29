"""Tests for `adapters.activtrades_mt5.probe` and `.diagnostics`."""

from __future__ import annotations

from adapters.activtrades_mt5.diagnostics import (
    ConnectionDiagnosticCategory,
    classify_account_info_failure,
    classify_initialize_failure,
)
from adapters.activtrades_mt5.probe import check_terminal_path, run_isolated_probe
from adapters.activtrades_mt5.testing import FakeMT5Client
from adapters.config import MT5ConnectionConfig

_CONFIG = MT5ConnectionConfig(login=1, password="secret-pw", server="srv", terminal_path=None)


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


def test_check_terminal_path_missing_file_is_terminal_not_found(tmp_path) -> None:
    missing = tmp_path / "does-not-exist" / "terminal64.exe"
    assert check_terminal_path(str(missing)) is ConnectionDiagnosticCategory.TERMINAL_NOT_FOUND


def test_check_terminal_path_existing_file_is_fine(tmp_path) -> None:
    real_file = tmp_path / "terminal64.exe"
    real_file.write_text("not a real binary, just needs to exist", encoding="utf-8")
    assert check_terminal_path(str(real_file)) is None


# -- run_isolated_probe: success path -----------------------------------------


def test_probe_success_reports_pass_and_shuts_down() -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_version(("5", "6231", "1 Jan 2026"))
    client.set_terminal_info(object())
    client.set_account_info(object())

    report = run_isolated_probe(_CONFIG, client)

    assert report.success is True
    assert report.category is None
    assert report.terminal_info_ok is True
    assert report.account_info_ok is True
    assert client.shutdown_calls == 1


# -- run_isolated_probe: failure paths, each with a distinct category --------


def test_probe_initialize_returns_false_classified_and_no_shutdown_needed() -> None:
    client = FakeMT5Client()
    client.set_initialize_result(False, error=(-10005, "IPC timeout"))

    report = run_isolated_probe(_CONFIG, client)

    assert report.success is False
    assert report.category is ConnectionDiagnosticCategory.MT5_IPC_TIMEOUT
    assert report.mt5_error_code == -10005
    # initialize() never succeeded, so there is nothing to shut down.
    assert client.shutdown_calls == 0


def test_probe_initialize_raises_is_classified_as_initialize_failed() -> None:
    class RaisingClient(FakeMT5Client):
        def initialize(self, *args, **kwargs):
            raise RuntimeError("boom")

    client = RaisingClient()

    report = run_isolated_probe(_CONFIG, client)

    assert report.success is False
    assert report.category is ConnectionDiagnosticCategory.MT5_INITIALIZE_FAILED
    assert "boom" in report.message


def test_probe_account_info_none_after_success_is_account_info_failed() -> None:
    client = FakeMT5Client()
    client.set_initialize_result(True)
    client.set_account_info(None)

    report = run_isolated_probe(_CONFIG, client)

    assert report.success is False
    assert report.category is ConnectionDiagnosticCategory.ACCOUNT_INFO_FAILED
    # initialize() DID succeed here -- shutdown must still be attempted.
    assert client.shutdown_calls == 1


def test_probe_missing_terminal_path_never_calls_initialize(tmp_path) -> None:
    missing = tmp_path / "nope" / "terminal64.exe"
    config = MT5ConnectionConfig(login=1, password="pw", server="srv", terminal_path=str(missing))
    client = FakeMT5Client()

    report = run_isolated_probe(config, client)

    assert report.success is False
    assert report.category is ConnectionDiagnosticCategory.TERMINAL_NOT_FOUND
    assert client.initialize_calls == []


# -- password never leaks -----------------------------------------------------


def test_probe_report_never_contains_password() -> None:
    client = FakeMT5Client()
    client.set_initialize_result(False, error=(-6, "Authorization failed"))

    report = run_isolated_probe(_CONFIG, client)

    assert "secret-pw" not in report.message
    assert report.mt5_error_description is not None
    assert "secret-pw" not in report.mt5_error_description
