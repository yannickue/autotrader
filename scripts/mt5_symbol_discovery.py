"""Discover and report broker symbol metadata for the configured CFD instruments.

Outputs a table with one row per canonical instrument (e.g. DAX/NASDAQ100/
WTI): canonical symbol, broker symbol, description, point, tick size, tick
value, contract size, min volume, volume step, bid, ask, spread, and a
mapping status -- without ever printing the account password.

SAFETY: the real MT5 calls happen only inside a short-lived, hard-timeout-
bounded child process (see `adapters.activtrades_mt5.bounded`) -- this
script's own main process never blocks on a stuck MT5 IPC call.

Usage:
    <PY> scripts/mt5_symbol_discovery.py
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
from adapters.activtrades_mt5.models import (  # noqa: E402
    symbol_info_raw_from_mt5,
    symbol_info_to_instrument_spec,
    tick_to_symbol_tick,
)
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import (  # noqa: E402
    MT5ConfigError,
    MT5ConnectionConfig,
    load_mt5_connection_config,
)
from instruments.discovery import (  # noqa: E402
    BrokerSymbolCandidate,
    CanonicalInstrument,
    MatchStatus,
    match_symbols,
)

# First research vertical slice (docs/OPEN_QUESTIONS.md / master directive):
# DAX, NASDAQ100, WTI. Aliases are illustrative starting points, not
# confirmed ActivTrades symbols -- config/override data, not matching logic,
# per src/instruments/discovery.py's own design. Correct these once real
# broker data is available; do not guess a real symbol into `overrides`.
CANONICAL_INSTRUMENTS = [
    CanonicalInstrument(
        canonical_symbol="DAX",
        aliases=("GER40", "DAX40", "DE40", "GERMANY 40", "GERMANY40"),
    ),
    CanonicalInstrument(
        canonical_symbol="NASDAQ100",
        aliases=("NAS100", "USTEC", "US100", "NASDAQ 100"),
    ),
    CanonicalInstrument(
        canonical_symbol="WTI",
        aliases=("USOIL", "WTI", "CRUDE OIL", "OIL.WTI", "XTIUSD"),
    ),
]


def _discover_symbols(config: MT5ConnectionConfig) -> int:
    """Fetch every broker symbol, resolve DAX/NASDAQ100/WTI, and print a
    table: canonical/broker-symbol/description/trade-mode/point/tick-size/
    tick-value/contract-size/min-vol/vol-step/bid/ask/spread/status.

    Never prints `config.password`. Returns a process exit code.
    """
    connection = MT5Connection(get_real_client())
    result = connection.connect(config)
    if not result.success:
        print(f"MT5 CONNECTION FAILED: {result.reason}", file=sys.stderr)
        return 1

    try:
        raw_symbols = connection.client.symbols_get()
        if not raw_symbols:
            print("symbols_get() returned no symbols", file=sys.stderr)
            return 1

        candidates = [
            BrokerSymbolCandidate(
                broker_symbol=str(s.name), description=str(getattr(s, "description", ""))
            )
            for s in raw_symbols
        ]
        raw_by_symbol = {str(s.name): s for s in raw_symbols}
        matches = match_symbols(candidates, CANONICAL_INSTRUMENTS)

        header = (
            f"{'CANONICAL':<12}{'BROKER':<16}{'DESCRIPTION':<24}{'STATUS':<12}"
            f"{'POINT':<10}{'TICK_SIZE':<12}{'TICK_VALUE':<12}{'CONTRACT':<10}"
            f"{'MIN_VOL':<10}{'VOL_STEP':<10}{'BID':<10}{'ASK':<10}{'SPREAD':<10}"
        )
        print(header)
        for canonical_symbol, match in matches.items():
            if match.status is not MatchStatus.MATCHED:
                print(
                    f"{canonical_symbol:<12}{'-':<16}{'-':<24}{match.status.value.upper():<12}"
                    "(no confidently-resolved broker symbol; see PENDING_USER_INPUT.md)"
                )
                continue

            broker_symbol = match.broker_symbol
            raw = raw_by_symbol.get(broker_symbol)
            description = str(getattr(raw, "description", "")) if raw else ""
            # MATCHED means confidently resolved by the deterministic
            # matcher against configured aliases -- it is still UNVERIFIED
            # against real ActivTrades data until a human confirms it (see
            # PENDING_USER_INPUT.md), so the status column always prints
            # this rather than implying broker-side confirmation exists.
            row = (
                f"{canonical_symbol:<12}{broker_symbol:<16}{description[:22]:<24}"
                f"{'UNVERIFIED':<12}"
            )
            if raw is not None:
                try:
                    spec_raw = symbol_info_raw_from_mt5(raw)
                    spec = symbol_info_to_instrument_spec(
                        spec_raw,
                        canonical_symbol=canonical_symbol,
                        retrieved_at=datetime.now(UTC),
                    )
                    row += (
                        f"{spec.point!s:<10}{spec.trade_tick_size!s:<12}"
                        f"{spec.trade_tick_value!s:<12}{spec.trade_contract_size!s:<10}"
                        f"{spec.volume_min!s:<10}{spec.volume_step!s:<10}"
                    )
                except Exception as exc:
                    row += f"(InstrumentSpec conversion failed: {exc})"
                tick_raw = connection.client.symbol_info_tick(broker_symbol)
                if tick_raw is not None:
                    tick = tick_to_symbol_tick(tick_raw, symbol=broker_symbol)
                    spread = tick.ask - tick.bid
                    row += f"{tick.bid!s:<10}{tick.ask!s:<10}{spread!s:<10}"
                else:
                    row += f"{'n/a':<10}{'n/a':<10}{'n/a':<10}"
            print(row)
    finally:
        connection.disconnect()

    unresolved = [c for c, m in matches.items() if m.status is not MatchStatus.MATCHED]
    if unresolved:
        print(
            f"\n{len(unresolved)} canonical instrument(s) not confidently resolved: "
            f"{unresolved}. Confirm/correct via PENDING_USER_INPUT.md before trading them.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    try:
        config = load_mt5_connection_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    if "--worker" in argv:
        print(f"Config loaded: server={config.server!r}, login={config.login}.")
        return _discover_symbols(config)

    timeout = default_staged_timeout_seconds()
    result = run_worker_bounded(Path(__file__), config, timeout=timeout)
    if result.timed_out:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        print(
            f"MT5 CONNECTION FAILED: MT5_IPC_TIMEOUT (exceeded {timeout}s, child terminated; "
            "the MT5 terminal itself was left untouched)",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
