"""Summarize a deterministic stream of completed backtest trades."""

import argparse
import json
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(project_root), str(project_root / "src")]

from monitoring.metrics import TradeOutcome, calculate_trade_metrics


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades", type=Path, required=True, help="JSON Lines trade outcomes")
    parser.add_argument("--initial-equity", type=Decimal, required=True)
    parser.add_argument("--split", choices=("is", "oos"), required=True)
    return parser


def _load_trades(path: Path) -> tuple[TradeOutcome, ...]:
    trades: list[TradeOutcome] = []
    for line_number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        try:
            trades.append(
                TradeOutcome(
                    gross_pnl=Decimal(payload["gross_pnl"]),
                    notional=Decimal(payload["notional"]),
                    fees=Decimal(payload.get("fees", "0")),
                    spread=Decimal(payload.get("spread", "0")),
                    slippage=Decimal(payload.get("slippage", "0")),
                    funding=Decimal(payload.get("funding", "0")),
                )
            )
        except KeyError as error:
            raise ValueError(f"line {line_number} is missing {error.args[0]}") from error
    return tuple(trades)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    trades = _load_trades(args.trades)
    metrics = calculate_trade_metrics(trades, initial_equity=args.initial_equity)
    print(
        json.dumps(
            {"metrics": metrics.to_dict(), "split": args.split},
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

