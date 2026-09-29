"""Run the MT5/ActivTrades pre-live readiness check sequence, staged.

STAGE 1  config (this process, always -- never needs MT5)
STAGE 2  terminal connection (bounded child, below)
STAGE 3  account state
STAGE 4  symbol availability / canonical mapping
STAGE 5  InstrumentSpec validation (folded into symbol availability today)
STAGE 6  market data (folded into order_check's tick fetch today)
STAGE 7  margin/profit calculations (not yet implemented as a distinct
         check -- order_check itself exercises order_calc_margin internally)
STAGE 8  order_check only -- never sends a real order

If Stage 2 fails, Stages 3-8 are reported FAIL "skipped" and the script
still exits normally with a full report -- it never hangs waiting for a
connection that will never come.

SAFETY: every real MT5 call (Stages 2-8) happens inside ONE short-lived,
hard-timeout-bounded child process (see `adapters.activtrades_mt5.bounded`).
This script's own main process never blocks on a stuck MT5 IPC call; on
timeout, only the disposable child is killed, never the user's MT5 terminal.

Usage:
    <PY> scripts/mt5_preflight.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"

for path in (str(REPO_ROOT), str(SRC_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from datetime import UTC, datetime  # noqa: E402

from adapters.activtrades_mt5.bounded import (  # noqa: E402
    default_staged_timeout_seconds,
    run_worker_bounded,
)
from adapters.activtrades_mt5.connection import MT5Connection  # noqa: E402
from adapters.activtrades_mt5.diagnostics import classify_initialize_failure  # noqa: E402
from adapters.activtrades_mt5.models import (  # noqa: E402
    account_info_to_account_state,
    symbol_info_raw_from_mt5,
    symbol_info_to_instrument_spec,
)
from adapters.activtrades_mt5.orders import MarketOrderRequest, MT5OrderGateway  # noqa: E402
from adapters.activtrades_mt5.orders import TradingMode as MT5TradingMode  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_mt5_connection_config  # noqa: E402
from instruments.discovery import (  # noqa: E402
    BrokerSymbolCandidate,
    CanonicalInstrument,
    MatchStatus,
    match_symbols,
)

_CHECK_SEQUENCE = (
    "config",
    "terminal_connection",
    "account_state",
    "symbol_availability",
    "order_check",
)

# Kept identical to mt5_symbol_discovery.py's list -- correct via
# PENDING_USER_INPUT.md once real ActivTrades symbols are confirmed.
_CANONICAL_INSTRUMENTS = [
    CanonicalInstrument(canonical_symbol="DAX", aliases=("GER40", "DAX40", "DE40")),
    CanonicalInstrument(canonical_symbol="NASDAQ100", aliases=("NAS100", "USTEC", "US100")),
    CanonicalInstrument(canonical_symbol="WTI", aliases=("USOIL", "WTI", "XTIUSD")),
]


class _Reporter:
    """Accumulates (and immediately PRINTS, unbuffered) each stage's
    PASS/WARN/FAIL result as soon as it's known.

    Printing incrementally -- not batching everything until the very end --
    matters specifically for the timeout case: if a later stage hangs and
    this child process gets killed, every stage result recorded before that
    point has already reached the parent's captured output (see `-u` in
    `adapters.activtrades_mt5.bounded.run_worker_bounded`), so partial
    progress is never silently lost.
    """

    def __init__(self) -> None:
        self._results: list[tuple[str, str, str]] = []
        print("MT5 preflight report:")

    def add(self, check_name: str, status: str, message: str) -> None:
        self._results.append((check_name, status, message))
        print(f"  {len(self._results)}. [{status}] {check_name}: {message}")

    @property
    def any_fail(self) -> bool:
        return any(status == "FAIL" for _, status, _ in self._results)


def _run_worker() -> int:
    """Stages 2-8, all real MT5 calls. Only ever runs inside the bounded
    child process spawned by `main()`'s non-worker branch."""
    reporter = _Reporter()

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        reporter.add("config", "FAIL", str(exc))
        return 2

    reporter.add(
        "config",
        "PASS",
        f"server={config.server!r}, login={config.login}, "
        f"terminal_path={config.terminal_path or '<default>'!r}",
    )

    connection = MT5Connection(get_real_client())
    connect_result = connection.connect(config)
    if not connect_result.success:
        category = classify_initialize_failure(connect_result.error_code)
        reporter.add("terminal_connection", "FAIL", f"{category.value}: {connect_result.reason}")
        for name in _CHECK_SEQUENCE[2:]:
            reporter.add(name, "FAIL", "skipped -- no terminal connection")
        return 1
    reporter.add("terminal_connection", "PASS", "initialize() succeeded")

    try:
        _run_account_state_check(connection, reporter)
        matches = _run_symbol_availability_check(connection, reporter)
        _run_order_check(connection, reporter, matches)
    finally:
        connection.disconnect()

    return 1 if reporter.any_fail else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if "--worker" in argv:
        return _run_worker()

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print("MT5 preflight report:")
        print(f"  1. [FAIL] config: {exc}")
        return 2

    timeout = default_staged_timeout_seconds()
    result = run_worker_bounded(Path(__file__), config, timeout=timeout)
    if result.timed_out:
        # Whatever the child managed to print before being killed is still
        # useful -- relay it (it already includes the "MT5 preflight
        # report:" header and every stage completed before the hang), then
        # append the timeout as one more finding rather than losing partial
        # progress or printing a second, duplicate header.
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        # Rough count of "  N. [...]" lines already printed by the child.
        stage_count = result.stdout.count("\n  ")
        print(
            f"  {stage_count + 1}. [FAIL] mt5_ipc: MT5_IPC_TIMEOUT: exceeded {timeout}s, "
            "child process terminated (the MT5 terminal itself was left untouched)"
        )
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode if result.returncode is not None else 1


def _run_account_state_check(connection: MT5Connection, reporter: _Reporter) -> None:
    account_raw = connection.client.account_info()
    if account_raw is None:
        reporter.add("account_state", "FAIL", "account_info() returned None")
        return
    account = account_info_to_account_state(account_raw)
    if not account.is_demo:
        reporter.add(
            "account_state", "FAIL", "account is NOT a demo account -- refusing to proceed"
        )
        return
    if not account.trade_allowed:
        reporter.add("account_state", "WARN", "account readable but trade_allowed=False")
        return
    reporter.add(
        "account_state",
        "PASS",
        f"demo account, currency={account.currency}, equity={account.equity}",
    )


def _run_symbol_availability_check(
    connection: MT5Connection, reporter: _Reporter
) -> dict[str, object]:
    raw_symbols = connection.client.symbols_get()
    if not raw_symbols:
        reporter.add("symbol_availability", "FAIL", "symbols_get() returned no symbols")
        return {}
    candidates = [
        BrokerSymbolCandidate(
            broker_symbol=str(s.name), description=str(getattr(s, "description", ""))
        )
        for s in raw_symbols
    ]
    matches = match_symbols(candidates, _CANONICAL_INSTRUMENTS)
    unresolved = [c for c, m in matches.items() if m.status is not MatchStatus.MATCHED]
    if unresolved:
        reporter.add(
            "symbol_availability",
            "WARN",
            f"{len(unresolved)}/{len(matches)} canonical instrument(s) not confidently "
            f"resolved: {unresolved} -- see PENDING_USER_INPUT.md",
        )
    else:
        reporter.add(
            "symbol_availability", "PASS", f"all {len(matches)} canonical instruments resolved"
        )
    return matches


def _run_order_check(
    connection: MT5Connection, reporter: _Reporter, matches: dict[str, object]
) -> None:
    resolved = [m for m in matches.values() if m.status is MatchStatus.MATCHED]  # type: ignore[union-attr]
    if not resolved:
        reporter.add("order_check", "WARN", "no resolved symbol available to dry-run check")
        return
    broker_symbol = resolved[0].broker_symbol  # type: ignore[union-attr]
    raw_symbol = connection.client.symbol_info(broker_symbol)
    tick = connection.client.symbol_info_tick(broker_symbol)
    if raw_symbol is None or tick is None:
        reporter.add("order_check", "FAIL", f"symbol_info/tick unavailable for {broker_symbol}")
        return

    spec_raw = symbol_info_raw_from_mt5(raw_symbol)
    spec = symbol_info_to_instrument_spec(
        spec_raw, canonical_symbol=broker_symbol, retrieved_at=datetime.now(UTC)
    )
    gateway = MT5OrderGateway(connection.client, mode=MT5TradingMode.DEMO)
    request = MarketOrderRequest(symbol=broker_symbol, side="BUY", volume=spec.volume_min)
    outcome = gateway.preflight_check(request, price=tick.ask)
    if outcome.accepted:
        reporter.add(
            "order_check",
            "PASS",
            f"dry-run order_check accepted for {broker_symbol} "
            f"(volume={spec.volume_min}) -- no real order sent",
        )
    else:
        reporter.add("order_check", "FAIL", f"{broker_symbol}: {outcome.reason}")


if __name__ == "__main__":
    sys.exit(main())
