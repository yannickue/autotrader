"""Structure-free raw edge scan (V2 research): forced-entry cells x time of day x day of week."""

from alpha.rawscan.engine import (
    FLAT,
    HORIZONS,
    CellSpace,
    CostParams,
    WindowSpec,
    build_cell_space,
    build_pairs,
    cell_table,
    cost_params_for,
)
from alpha.rawscan.gate import GateAlreadyUsed, persistence_gate
from alpha.rawscan.grid import DayGrid, build_day_grid, causal_atr
from alpha.rawscan.ledger import TrialLedger
from alpha.rawscan.nulls import day_bootstrap_null, day_shift_null, draw_shifts, null_summary
from alpha.rawscan.scan import DataDisciplineError, ScanResult, preregister, scan_train
from alpha.rawscan.spike import spread_spike_diagnostic
from alpha.rawscan.stats import bh_qvalues, cluster_t

__all__ = (
    "FLAT", "HORIZONS", "CellSpace", "CostParams", "DataDisciplineError", "DayGrid",
    "GateAlreadyUsed", "ScanResult", "TrialLedger", "WindowSpec", "bh_qvalues", "build_cell_space",
    "build_day_grid", "build_pairs", "causal_atr", "cell_table", "cluster_t", "cost_params_for",
    "day_bootstrap_null", "day_shift_null", "draw_shifts", "null_summary", "persistence_gate",
    "preregister", "scan_train", "spread_spike_diagnostic",
)
