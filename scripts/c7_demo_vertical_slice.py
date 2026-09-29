"""C7 first ActivTrades DEMO vertical slice runner.

MODES (exactly one):
  --dry-run          (default) the WHOLE path against the real demo terminal -- real quotes, real
                     Nautilus Strategy/RiskEngine/ExecutionEngine, real order_check -- but
                     order_send is NEVER called.
  --live --confirm-demo-order=I-AUTHORIZE-ONE-MIN-LOT-GER40-DEMO-TRADE
                     places ONE minimum-lot GER40 BUY with an attached broker-side stop on the
                     DEMO account, holds a few seconds, closes it (reduce-only), reconciles.
  --restart-proof    fresh runtime, same durable state, attach to the already-logged-in terminal:
                     NOT_RECONCILED -> mass status -> broker snapshot -> RECONCILED.

SAFETY: attach-only (never logs in, refuses MT5_ALLOW_ACCOUNT_LOGIN=1), demo-only (trade_mode 0
or STOP), netting-only, flat-book-only, bounded child process, single-owner terminal lock, all
MT5 IPC on ONE dedicated thread, no automatic retry of any exposure-changing request. Nothing in
the output contains credentials.

Reports: artifacts/c7/demo_slice_report.json, artifacts/c7/restart_proof.json
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from adapters.activtrades_mt5.bounded import run_worker_bounded  # noqa: E402
from adapters.activtrades_mt5.real_client import get_real_client  # noqa: E402
from adapters.config import MT5ConfigError, load_attach_only_config  # noqa: E402

CONFIRM = "--confirm-demo-order=I-AUTHORIZE-ONE-MIN-LOT-GER40-DEMO-TRADE"
OUT = REPO_ROOT / "artifacts" / "c7"
STATE = REPO_ROOT / "data" / "c7_state" / "state.db"


def _write(name: str, payload: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")


async def _snapshot(asm) -> dict:
    adapter = asm.adapter
    recon = adapter.exec_client.recon
    return {
        "state": recon.state.value,
        "source": recon.source.value if recon.source else None,
        "runtime": recon.runtime.value,
        "discrepancies": [f"{d.kind.value}: {d.detail}" for d in recon.discrepancies],
    }


async def _amain(mode: str, confirmed: bool) -> int:
    from nautilus_mt5.data_client import Mt5DataClientConfig
    from nautilus_mt5.demo_slice import (
        DemoSliceConfig,
        SliceAbort,
        assemble,
        build_report,
        collect_broker_facts,
        connect_and_verify,
        expected_margin,
        run_slice,
    )
    from nautilus_mt5.execution_client import Mt5ExecClientConfig

    if mode == "live" and not confirmed:
        print(f"REFUSED: --live requires {CONFIRM}", file=sys.stderr)
        return 2
    config = load_attach_only_config()
    loop = asyncio.get_running_loop()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    asm = assemble(
        client=get_real_client(),  # the ONLY place in the C7 code path that obtains the real client
        connection=config,
        loop=loop,
        state_path=STATE,
        exec_config=Mt5ExecClientConfig(
            autostart_sync=True,
            sync_interval_secs=1.0,
            dry_run=(mode == "dry-run"),
            require_demo_account=True,  # re-verified before EVERY exposure-changing send
            auto_reconcile_on_connect=(mode != "restart-proof"),
        ),
        data_config=Mt5DataClientConfig(poll_interval_secs=0.5),
    )
    started = datetime.now(UTC)
    code = 1
    try:
        if mode == "restart-proof":
            await asm.adapter.exec_client._connect()
            asm.adapter.exec_client._set_connected(True)
            before = await _snapshot(asm)
            mass = await asm.adapter.exec_client.generate_mass_status()
            await asm.adapter.exec_client.reconcile_async()
            after = await _snapshot(asm)
            report = {
                "before_any_comparison": before,
                "mass_status": {
                    "positions": len(mass.position_reports),
                    "orders": len(mass.order_reports),
                    "fills": len(mass.fill_reports),
                },
                "after_snapshot_comparison": after,
                "verdict": {
                    "started_not_reconciled": before["state"] == "not_reconciled",
                    "reconciled_from_venue_snapshot": after["state"] == "reconciled"
                    and after["source"] == "venue_snapshot",
                },
            }
            _write("restart_proof.json", report)
            print(json.dumps(report, indent=1))
            return 0 if all(report["verdict"].values()) else 3

        facts0 = await connect_and_verify(asm)
        print("PRE-ENTRY CHECKS OK:", json.dumps(facts0))

        margin_est = None
        from decimal import Decimal

        m = await run_slice(
            asm, DemoSliceConfig(), account_leverage=Decimal(str(facts0["leverage"]))
        )
        facts = await asm.adapter.exec_client._lane_run(collect_broker_facts, asm.adapter, started)
        if "entry_fill_px" in m.values:
            margin_est = await asm.adapter.exec_client._lane_run(
                expected_margin, asm.adapter, float(m.values["entry_fill_px"]), 0.25
            )
        recon_result = await asm.adapter.exec_client.reconcile_async()
        recon = await _snapshot(asm)
        nautilus = {
            "open_positions": len(asm.cache.positions_open()),
            "closed_positions": len(asm.cache.positions_closed()),
            "open_orders": len(asm.cache.orders_open()),
            "orders_total": len(asm.cache.orders()),
            "reconciliation_call_state": recon_result.state.value,
        }
        report = build_report(
            m,
            facts,
            reconciliation=recon,
            nautilus_state=nautilus,
            expected_margin_eur=margin_est,
            dry_run=(mode == "dry-run"),
        )
        _write("demo_slice_report.json" if mode == "live" else "dry_run_report.json", report)
        print(json.dumps(report, indent=1, default=str))
        if facts["positions"]:
            print(
                "!!! MANUAL ACTION REQUIRED: an open Ger40 position remains at the broker: "
                f"{facts['positions']} -- close it MANUALLY in the MT5 terminal (reduce-only is "
                "deliberately blocked by the gates while the book is unreconciled)",
                file=sys.stderr,
            )
            return 4
        if mode == "dry-run":
            ok = (
                m.failure is not None
                and "DRY_RUN_ORDER_CHECK_OK" in m.failure
                and not facts["positions"]
            )
            print(
                "DRY RUN",
                "OK: real order_check passed, order_send never called" if ok else "FAILED",
            )
            code = 0 if ok else 5
        else:
            code = 0 if all(report["verdict"].values()) else 6
        return code
    except SliceAbort as exc:
        print(f"ABORT (no order sent): {exc}", file=sys.stderr)
        return 7
    finally:
        try:
            await asm.adapter.exec_client._disconnect()
            await asm.adapter.data_client._disconnect()
        finally:
            asm.adapter.store.close()
            asm.adapter.lane.shutdown()


def main() -> int:
    argv = sys.argv[1:]
    mode = "dry-run"
    for flag, name in (
        ("--live", "live"),
        ("--restart-proof", "restart-proof"),
        ("--dry-run", "dry-run"),
    ):
        if flag in argv:
            mode = name
    confirmed = CONFIRM in argv
    if "--worker" in argv:
        return asyncio.run(_amain(mode, confirmed))
    if mode == "live" and not confirmed:
        print(f"REFUSED: --live requires {CONFIRM}", file=sys.stderr)
        return 2
    try:
        config = load_attach_only_config()
    except MT5ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    passthrough = tuple(a for a in argv if a in ("--live", "--restart-proof", "--dry-run", CONFIRM))
    result = run_worker_bounded(Path(__file__), config, timeout=240.0, extra_args=passthrough)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        print(
            "TIMED OUT (240 s): check the MT5 terminal for an open Ger40 position!", file=sys.stderr
        )
        return 1
    return result.returncode if result.returncode is not None else 1


if __name__ == "__main__":
    sys.exit(main())
