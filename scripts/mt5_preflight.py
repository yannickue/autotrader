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

from adapters.config import MT5ConfigError, load_mt5_connection_config  # noqa: E402

# The full sequence this script will eventually run, in order. Every check
# after "config" is a TODO(mt5-adapter) placeholder until the real adapter
# and src/instruments mapping exist.
_CHECK_SEQUENCE = (
    "config",
    "terminal_connection",
    "account_state",
    "symbol_availability",
    "order_check",
)


def _run_not_yet_implemented_check(name: str) -> tuple[str, str]:
    """TODO(mt5-adapter): once a real MT5 client exists in `src/adapters`,
    replace this placeholder with the real check for `name` (terminal
    connection / account state / symbol availability / a dry-run
    `order_check` that never sends a real order). Returns (status, message).
    """
    raise NotImplementedError(f"preflight check {name!r} not yet implemented")


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

    for check_name in _CHECK_SEQUENCE[1:]:
        try:
            status, message = _run_not_yet_implemented_check(check_name)
        except NotImplementedError:
            results.append((check_name, "WARN", "not yet implemented -- adapter pending"))
        else:  # pragma: no cover -- unreachable until the adapter lands
            results.append((check_name, status, message))

    _print_report(results)
    any_fail = any(status == "FAIL" for _, status, _ in results)
    return 1 if any_fail else 0


def _print_report(results: list[tuple[str, str, str]]) -> None:
    print("MT5 preflight report:")
    for index, (check_name, status, message) in enumerate(results, start=1):
        print(f"  {index}. [{status}] {check_name}: {message}")


if __name__ == "__main__":
    sys.exit(main())
