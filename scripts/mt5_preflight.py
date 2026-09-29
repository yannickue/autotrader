"""Run the MT5/ActivTrades pre-live readiness check sequence.

Once the MT5 adapter (owned by a parallel worker, not yet built) exists,
this script will run a numbered sequence of checks -- config, terminal
connection, account state, symbol availability/mapping, and a broker
`order_check` (a dry-run margin/validity check that never sends a real
order) -- reporting PASS/WARN/FAIL per check, without ever printing the
account password. Today only the `config` check is real; every later check
reports clearly that it is not yet implemented rather than silently passing
or crashing.

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

from adapters.activtrades_mt5.connection import MT5Connection  # noqa: E402
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


def main(argv: list[str] | None = None) -> int:
    results: list[tuple[str, str, str]] = []  # (check, status, message)

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        results.append(("config", "FAIL", str(exc)))
        _print_report(results)
        return 2

    results.append(
        (
            "config",
            "PASS",
            f"server={config.server!r}, login={config.login}, "
            f"terminal_path={config.terminal_path or '<default>'!r}",
        )
    )

    connection = MT5Connection(get_real_client())
    connect_result = connection.connect(config)
    if not connect_result.success:
        results.append(("terminal_connection", "FAIL", connect_result.reason))
        for name in _CHECK_SEQUENCE[2:]:
            results.append((name, "FAIL", "skipped -- no terminal connection"))
        _print_report(results)
        return 1
    results.append(("terminal_connection", "PASS", "initialize() succeeded"))

    try:
        _run_account_state_check(connection, results)
        matches = _run_symbol_availability_check(connection, results)
        _run_order_check(connection, results, matches)
    finally:
        connection.disconnect()

    _print_report(results)
    any_fail = any(status == "FAIL" for _, status, _ in results)
    return 1 if any_fail else 0


def _run_account_state_check(
    connection: MT5Connection, results: list[tuple[str, str, str]]
) -> None:
    account_raw = connection.client.account_info()
    if account_raw is None:
        results.append(("account_state", "FAIL", "account_info() returned None"))
        return
    account = account_info_to_account_state(account_raw)
    if not account.is_demo:
        results.append(
            ("account_state", "FAIL", "account is NOT a demo account -- refusing to proceed")
        )
        return
    if not account.trade_allowed:
        results.append(("account_state", "WARN", "account readable but trade_allowed=False"))
        return
    results.append(
        (
            "account_state",
            "PASS",
            f"demo account, currency={account.currency}, equity={account.equity}",
        )
    )


def _run_symbol_availability_check(
    connection: MT5Connection, results: list[tuple[str, str, str]]
) -> dict[str, object]:
    raw_symbols = connection.client.symbols_get()
    if not raw_symbols:
        results.append(("symbol_availability", "FAIL", "symbols_get() returned no symbols"))
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
        results.append(
            (
                "symbol_availability",
                "WARN",
                f"{len(unresolved)}/{len(matches)} canonical instrument(s) not confidently "
                f"resolved: {unresolved} -- see PENDING_USER_INPUT.md",
            )
        )
    else:
        results.append(
            ("symbol_availability", "PASS", f"all {len(matches)} canonical instruments resolved")
        )
    return matches


def _run_order_check(
    connection: MT5Connection, results: list[tuple[str, str, str]], matches: dict[str, object]
) -> None:
    resolved = [m for m in matches.values() if m.status is MatchStatus.MATCHED]  # type: ignore[union-attr]
    if not resolved:
        results.append(("order_check", "WARN", "no resolved symbol available to dry-run check"))
        return
    broker_symbol = resolved[0].broker_symbol  # type: ignore[union-attr]
    raw_symbol = connection.client.symbol_info(broker_symbol)
    tick = connection.client.symbol_info_tick(broker_symbol)
    if raw_symbol is None or tick is None:
        results.append(("order_check", "FAIL", f"symbol_info/tick unavailable for {broker_symbol}"))
        return

    spec_raw = symbol_info_raw_from_mt5(raw_symbol)
    spec = symbol_info_to_instrument_spec(
        spec_raw, canonical_symbol=broker_symbol, retrieved_at=datetime.now(UTC)
    )
    gateway = MT5OrderGateway(connection.client, mode=MT5TradingMode.DEMO)
    request = MarketOrderRequest(symbol=broker_symbol, side="BUY", volume=spec.volume_min)
    outcome = gateway.preflight_check(request, price=tick.ask)
    if outcome.accepted:
        results.append(
            ("order_check", "PASS", f"dry-run order_check accepted for {broker_symbol} "
             f"(volume={spec.volume_min}) -- no real order sent")
        )
    else:
        results.append(("order_check", "FAIL", f"{broker_symbol}: {outcome.reason}"))


def _print_report(results: list[tuple[str, str, str]]) -> None:
    print("MT5 preflight report:")
    for index, (check_name, status, message) in enumerate(results, start=1):
        print(f"  {index}. [{status}] {check_name}: {message}")


if __name__ == "__main__":
    sys.exit(main())
