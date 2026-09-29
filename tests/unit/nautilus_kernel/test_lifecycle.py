"""C4: REAL Nautilus 1.231.0 lifecycle proof on the real captured GER40 M5 week.

signal -> risk/sizing approval -> Nautilus order -> fill -> position open ->
exit -> position close -> realized PnL, all read back from Nautilus itself.
No MT5 access; nothing here is a metrics-only check.
"""

import hashlib
import json
from decimal import Decimal
from itertools import pairwise

import pytest

from data.historical import read_bar_dataset
from nautilus_kernel.catalog import bar_close_ns
from nautilus_kernel.proof import evidence_document, run_proof
from nautilus_kernel.risk_bridge import NautilusRiskBridge, technical_instrument_limits


@pytest.fixture(scope="module")
def run(c3_dataset, symbol_info_path, tmp_path_factory):
    return run_proof(
        dataset_path=c3_dataset,
        symbol_info_path=symbol_info_path,
        work_dir=tmp_path_factory.mktemp("c4a"),
    )


def digest(run_) -> str:
    fills = [
        (str(f["client_order_id"]), str(f["side"]), str(f["filled_qty"]), str(f["avg_px"]))
        for f in run_.result.fills_report
    ]
    return hashlib.sha256(json.dumps(fills).encode()).hexdigest()


def test_labels_and_scope(run):
    assert run.result.labels == ("TECHNICAL_BACKTEST", "NOT_YET_BROKER_CALIBRATED")
    m = run.result.metrics
    assert m["instrument"] == "GER40.ACTIVTRADES" and m["broker_symbol"] == "Ger40"
    assert m["timeframe"] == "5m" and m["bars"] == 1185


def test_every_lifecycle_checkpoint_is_present_and_nautilus_backed(run):
    ev = run.result.evidence
    for name in (
        "DATA_LOADED",
        "STRATEGY_INITIALIZED",
        "SIGNAL",
        "RISK_APPROVED",
        "ORDER_SUBMITTED",
        "ORDER_FILLED",
        "POSITION_OPENED",
        "STOP_SUBMITTED",
        "EXIT_REQUESTED",
        "POSITION_CLOSED",
    ):
        assert ev.of(name), f"missing lifecycle checkpoint {name}"
    init = ev.of("STRATEGY_INITIALIZED")[0]
    assert init["instrument"] == "GER40.ACTIVTRADES"
    m = run.result.metrics
    assert m["orders"] > 0 and m["fills"] > 0 and m["completed_trades"] > 0
    assert len(run.result.fills_report) == m["fills"]
    assert len(run.result.positions_report) == m["completed_trades"]
    assert all(row["status"] == "FILLED" for row in run.result.fills_report)


def test_every_entry_order_carries_its_risk_approval(run):
    approvals = {e["signal_id"] for e in run.result.evidence.of("RISK_APPROVED")}
    entry_tags = [
        t for o in run.result.orders_report for t in o["tags"] if str(t).startswith("risk:")
    ]
    assert entry_tags and len(entry_tags) == len(approvals)
    assert {t.removeprefix("risk:") for t in entry_tags} == approvals


def test_pnl_is_owned_by_nautilus_and_reconciles_with_the_account(run):
    m = run.result.metrics
    closed_pnl = sum(
        Decimal(str(row["realized_pnl"]).split()[0]) for row in run.result.positions_report
    )
    assert Decimal(m["net_pnl_eur"]) == closed_pnl
    assert Decimal(m["final_balance_eur"]) - Decimal(10000) == closed_pnl
    assert m["winners"] + m["losers"] <= m["completed_trades"]
    assert Decimal(m["max_position_size"]) <= Decimal("250")


def test_v1_position_rules_one_position_no_same_tick_flip(run):
    rows = sorted(run.result.positions_report, key=lambda r: r["ts_opened"])
    for prev, nxt in pairwise(rows):
        assert nxt["ts_opened"] > prev["ts_closed"], "overlapping positions / same-tick flip"


def test_sizes_respect_broker_volume_step_and_limits(run):
    limits = technical_instrument_limits()
    for row in run.result.positions_report:
        qty = Decimal(str(row["peak_qty"]))
        assert qty >= limits.min_quantity and qty % limits.quantity_step == 0


def test_fills_use_executable_side_never_better_than_the_bar(run, c3_dataset):
    """BUY at/above ask, SELL at/below bid of the decision bar (+/- 1 tick slippage)."""
    records, _ = read_bar_dataset(c3_dataset)
    by_close = {bar_close_ns(r, "5m"): r for r in records}
    tick = Decimal("0.01")
    checked = 0
    for fill in run.result.fills_report:
        tags = [str(t) for t in fill["tags"]]
        if not any(t.startswith("risk:") for t in tags):
            continue  # entry fills only; exits may be stop-triggered intrabar
        signal_ns = int(next(t for t in tags if t.startswith("risk:")).rsplit("-", 1)[1])
        rec = by_close[signal_ns]
        px = Decimal(str(fill["avg_px"]))
        spread = Decimal(rec.spread_points) * Decimal("0.01")
        if fill["side"] == "BUY":
            assert px >= rec.close + spread, "BUY filled better than the ask"
            assert px <= rec.close + spread + tick
        else:
            assert px <= rec.close, "SELL filled better than the bid"
            assert px >= rec.close - tick
        checked += 1
    assert checked > 5


def test_run_is_deterministic(run, c3_dataset, symbol_info_path, tmp_path):
    again = run_proof(dataset_path=c3_dataset, symbol_info_path=symbol_info_path, work_dir=tmp_path)
    assert digest(again) == digest(run)
    assert again.result.metrics == run.result.metrics
    assert again.result.evidence.events == run.result.evidence.events


def test_risk_gate_is_binding_orders_only_after_approval(c3_dataset, symbol_info_path, tmp_path):
    """A limit no bar can satisfy (max spread 0 bps) => every signal rejected => zero orders."""
    from dataclasses import replace

    from nautilus_trader.model.identifiers import InstrumentId

    bridge = NautilusRiskBridge(
        instrument_id=InstrumentId.from_str("GER40.ACTIVTRADES"),
        limits=replace(technical_instrument_limits(), max_spread_bps=Decimal("0")),
    )
    blocked = run_proof(
        dataset_path=c3_dataset, symbol_info_path=symbol_info_path, work_dir=tmp_path, bridge=bridge
    )
    ev = blocked.result.evidence
    assert ev.of("SIGNAL") and not ev.of("RISK_APPROVED")
    assert {e["reason"] for e in ev.of("RISK_REJECTED")} == {"SPREAD_TOO_WIDE"}
    assert blocked.result.metrics["orders"] == 0 and blocked.result.fills_report == []


def test_consecutive_loss_gate_reads_nautilus_history(run):
    rejected = [e["reason"] for e in run.result.evidence.of("RISK_REJECTED")]
    assert rejected, "expected the loss-streak gate to engage on this sample"
    assert set(rejected) <= {"CONSECUTIVE_LOSS_LIMIT", "DAILY_LOSS_LIMIT", "DRAWDOWN_LIMIT"}


def test_evidence_document_is_json_serialisable(run):
    doc = evidence_document(run)
    text = json.dumps(doc, default=str)
    assert "lifecycle_checkpoints" in doc and len(text) > 1000
    assert doc["dataset_provenance"]["broker_account_kind"] == "DEMO"
