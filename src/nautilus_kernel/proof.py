"""End-to-end C3 -> C4 pipeline:

validated C3 Parquet dataset (verified hash/provenance)
  -> Nautilus Cfd instrument (from real symbol_info snapshot)
  -> Nautilus Bars (BID + synthesized ASK)
  -> ParquetDataCatalog write -> read
  -> BacktestEngine -> ProofStrategy -> RiskPolicy/PositionSizer bridge
  -> Nautilus order/fill/position/portfolio/PnL
  -> machine-verifiable evidence
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from data.historical import DatasetProvenance, read_bar_dataset
from nautilus_kernel.backtest import BacktestResult, run_technical_backtest
from nautilus_kernel.catalog import bar_close_ns, records_to_nautilus_bars, write_catalog
from nautilus_kernel.instrument import load_symbol_info_snapshot, spec_to_nautilus_cfd
from nautilus_kernel.risk_bridge import NautilusRiskBridge


@dataclass(slots=True)
class ProofRun:
    provenance: DatasetProvenance
    result: BacktestResult
    catalog_path: Path


def run_proof(
    *,
    dataset_path: Path | str,
    symbol_info_path: Path | str,
    work_dir: Path | str,
    bridge: NautilusRiskBridge | None = None,
    strategy_overrides: dict | None = None,
) -> ProofRun:
    records, provenance = read_bar_dataset(dataset_path)  # verifies content hash + schema
    snapshot = load_symbol_info_snapshot(
        symbol_info_path, canonical=provenance.canonical_instrument
    )
    if snapshot.spec.broker_symbol != provenance.broker_symbol:
        raise ValueError("symbol_info broker symbol does not match the dataset provenance")
    instrument = spec_to_nautilus_cfd(snapshot.spec)
    bid_bars, ask_bars = records_to_nautilus_bars(records, instrument, snapshot.spec.point)
    catalog_path = Path(work_dir) / "catalog"
    if catalog_path.exists():
        shutil.rmtree(catalog_path)  # deterministic rebuild, never append into a stale catalog
    write_catalog(catalog_path, instrument, bid_bars, ask_bars)
    spreads = {bar_close_ns(r, r.timeframe): int(r.spread_points) for r in records}
    result = run_technical_backtest(
        catalog_path=catalog_path,
        instrument=instrument,
        timeframe=provenance.timeframe,
        spread_points_by_close_ns=spreads,
        point=snapshot.spec.point,
        bridge=bridge,
        strategy_overrides=strategy_overrides,
    )
    return ProofRun(provenance=provenance, result=result, catalog_path=catalog_path)


def evidence_document(run: ProofRun) -> dict:
    """JSON-serialisable evidence bundle (Nautilus reports + strategy checkpoints)."""
    result = run.result
    return {
        "labels": list(result.labels),
        "dataset_provenance": json.loads(run.provenance.to_json()),
        "metrics": result.metrics,
        "lifecycle_checkpoints": result.evidence.events,
        "nautilus_orders_report": result.orders_report,
        "nautilus_fills_report": result.fills_report,
        "nautilus_positions_report": result.positions_report,
    }
