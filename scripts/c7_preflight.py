"""C7 real-terminal PREFLIGHT (read-only). NEVER sends an order.

Attach-only (no login), bounded child process, single-owner lock, one MT5 execution lane.
Verifies against the REAL ActivTrades terminal:

  * account: DEMO (trade_mode 0), expected login (Mt5Session/MT5Connection verify it), EUR,
    RETAIL_NETTING margin mode, leverage
  * symbol_info -> Nautilus instrument via the real InstrumentProvider
  * flat book: no positions, no working orders (else STOP)
  * fresh quote (server-time policy)
  * order_check() -- success is retcode == 0 / comment 'Done' (a DIFFERENT namespace than
    order_send) -- for the EXACT request shapes the adapter would send: a min-lot BUY and SELL
    market entry with an attached SL (and SL+TP), using the real filling mode and deviation.

order_check does not execute anything. Results are written to artifacts/c7/preflight.json
(no credentials). Exit 0 = every check passed; non-zero = STOP (fix the adapter first).

Usage: <PY> scripts/c7_preflight.py [--deviation N]
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from adapters.activtrades_mt5.bounded import (  # noqa: E402
    default_staged_timeout_seconds,
    run_worker_bounded,
)
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_attach_only_config  # noqa: E402

OUT = REPO_ROOT / "artifacts" / "c7"
STOP_DISTANCE = Decimal("40")  # index points; well beyond the broker stop level (1.00)
DEVIATION = 20


def _check(client, request: dict) -> dict:
    result = client.order_check(request)
    if result is None:
        return {"ok": False, "retcode": None, "comment": "None", "last_error": client.last_error()}
    return {
        "ok": int(result.retcode) == 0,  # order_check success == retcode 0 ("Done")
        "retcode": int(result.retcode),
        "comment": str(result.comment),
        "margin": float(result.margin),
        "margin_free": float(result.margin_free),
        "balance": float(result.balance),
        "equity": float(result.equity),
    }


def _worker(deviation: int) -> int:
    from nautilus_mt5.executor import Mt5Executor
    from nautilus_mt5.instruments import InstrumentAssumptions, Mt5InstrumentProvider
    from nautilus_mt5.session import Mt5Session, UnsupportedAccountMode
    from nautilus_mt5.translate import Quote, market_entry_request

    config = load_attach_only_config()
    lane = Mt5Executor("c7-preflight-lane")
    session = Mt5Session(get_real_client(), config, lane=lane)
    report: dict = {"started_utc": datetime.now(UTC).isoformat(), "checks": {}, "stop": []}

    def core() -> int:
        result = session.connect()
        if not result.success:
            report["stop"].append(f"CONNECT: {result.reason}")
            return 1
        try:
            client = session.client
            account = session.call("account_info", client.account_info)
            report["account"] = {
                "trade_mode": int(account.trade_mode),
                "is_demo": int(account.trade_mode) == 0,
                "currency": str(account.currency),
                "margin_mode": int(account.margin_mode),
                "leverage": int(account.leverage),
                "balance": float(account.balance),
                "equity": float(account.equity),
                "margin_free": float(account.margin_free),
                "trade_allowed": bool(account.trade_allowed),
            }
            if int(account.trade_mode) != 0:
                report["stop"].append("NOT A DEMO ACCOUNT (trade_mode != 0) -- refusing")
                return 2
            try:
                session.account_mode()
            except UnsupportedAccountMode as exc:
                report["stop"].append(f"ACCOUNT MODE: {exc}")
                return 2
            terminal = session.call("terminal_info", client.terminal_info)
            report["terminal"] = {
                "connected": bool(terminal.connected),
                "trade_allowed": bool(terminal.trade_allowed),
                "build": int(terminal.build),
            }

            provider = Mt5InstrumentProvider(
                client,
                assumptions=InstrumentAssumptions(
                    margin_init=Decimal("0.05"), margin_maint=Decimal("0.05")
                ),
            )
            provider.load_all_sync()
            instrument = next(iter(provider.list_all()))
            spec = provider.spec(instrument.id)
            info = session.call("symbol_info", client.symbol_info, "Ger40")
            report["instrument"] = {
                "id": str(instrument.id),
                "broker_symbol": spec.broker_symbol,
                "digits": spec.digits,
                "volume_min": str(spec.volume_min),
                "volume_step": str(spec.volume_step),
                "volume_max": str(spec.volume_max),
                "stop_level_price": str(spec.stop_level),
                "filling_mode_mask": int(info.filling_mode),
                "trade_mode": int(info.trade_mode),
                "execution_mode": int(info.trade_exemode),
                "calc_mode": int(info.trade_calc_mode),
            }

            positions = session.call("positions_get", client.positions_get)
            orders = session.call("orders_get", client.orders_get)
            report["book"] = {"positions": len(positions), "working_orders": len(orders)}
            if positions or orders:
                report["stop"].append(
                    f"BOOK NOT FLAT: {len(positions)} positions, {len(orders)} orders"
                )
                return 2

            tick = session.call("symbol_info_tick", client.symbol_info_tick, "Ger40")
            quote_time = session.time_policy.server_epoch_to_utc(tick.time)
            age = (datetime.now(UTC) - quote_time).total_seconds()
            report["quote"] = {
                "bid": float(tick.bid),
                "ask": float(tick.ask),
                "spread": round(float(tick.ask) - float(tick.bid), 2),
                "age_seconds": round(age, 1),
            }
            if age > 120:
                report["stop"].append(f"STALE QUOTE ({age:.0f}s) -- market closed or feed stale")
                return 2

            quote = Quote(bid=Decimal(str(tick.bid)), ask=Decimal(str(tick.ask)))
            base = dict(
                spec=spec,
                symbol_filling_mask=int(info.filling_mode),
                quantity=spec.volume_min,
                quote=quote,
                magic=730001,
                deviation_points=deviation,
            )
            shapes = {
                "buy_market_with_sl": market_entry_request(
                    **base, is_buy=True, token="NT-PREFLIGHT", stop_loss=quote.bid - STOP_DISTANCE
                ),
                "buy_market_with_sl_tp": market_entry_request(
                    **base,
                    is_buy=True,
                    token="NT-PREFLIGHT",
                    stop_loss=quote.bid - STOP_DISTANCE,
                    take_profit=quote.ask + 2 * STOP_DISTANCE,
                ),
                "sell_market_with_sl": market_entry_request(
                    **base, is_buy=False, token="NT-PREFLIGHT", stop_loss=quote.ask + STOP_DISTANCE
                ),
            }
            all_ok = True
            for name, request in shapes.items():
                outcome = _check(client, request)
                outcome["request"] = request
                report["checks"][name] = outcome
                all_ok = all_ok and outcome["ok"]
            # A deliberately BAD shape proves order_check discriminates (it must not say 0/Done).
            bad = dict(shapes["buy_market_with_sl"], sl=float(quote.bid + STOP_DISTANCE))
            negative = _check(client, bad)
            report["checks"]["negative_control_sl_above_bid"] = negative
            if negative["ok"]:
                report["stop"].append(
                    "order_check accepted an invalid stop: check is not discriminating"
                )
                all_ok = False
            if not all_ok:
                report["stop"].append(
                    "order_check rejected an adapter request shape -- fix the adapter"
                )
                return 3
            return 0
        finally:
            session.disconnect()

    started = time.perf_counter()
    code = lane.run_sync(core)
    report["elapsed_s"] = round(time.perf_counter() - started, 2)
    report["exit_code"] = code
    report["lane"] = {
        "threads": len(lane.stats.worker_thread_ids),
        "max_concurrent": lane.stats.max_concurrent,
    }
    lane.shutdown()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "preflight.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(json.dumps(report, indent=1, default=str))
    return code


def main() -> int:
    argv = sys.argv[1:]
    deviation = DEVIATION
    if "--deviation" in argv:
        deviation = int(argv[argv.index("--deviation") + 1])
    if "--worker" in argv:
        return _worker(deviation)
    try:
        config = load_attach_only_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    timeout = default_staged_timeout_seconds() * 2
    result = run_worker_bounded(Path(__file__), config, timeout=timeout, extra_args=())
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        print(f"TIMED OUT after {timeout}s", file=sys.stderr)
        return 1
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
