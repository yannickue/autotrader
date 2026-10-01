# Research Workbench v1 — frozen interfaces (night sprint 2026-10-02)

OFFLINE ONLY. Nothing here may be imported by the production runner (`scripts/demo_trader.py`, `scripts/autostart/*`,
`src/demo/*`). Production stays `sprint1/integration @ 11c6aec`. Reuse first: no second feature store, strategy language,
fast simulator, Nautilus engine, scheduler or import-graph system.

## Package layout (new, offline-only): `src/research_workbench/`
| Module | Owner lane | Purpose |
|---|---|---|
| `status.py` | A | `PromotionStatus` StrEnum — NO live member |
| `experiment.py` | A | `ExperimentSpec` (wraps `alpha.fast.spec.StrategySpec`), deterministic `experiment_hash()` |
| `dag.py` | A | stage fingerprints + artifact store (reuses `research_speed.segments/artifact/importgraph`) |
| `fastrun.py` | A | features -> candidates -> `simulate_fast` -> screen -> `REJECT_FAST | PROMOTE_TO_FIDELITY` |
| `report.py` | A | one standard report (dict + markdown) |
| `differential.py` | C | FAST <-> Nautilus differential (types below) |
| `golden.py` | C | deterministic golden scenarios (synthetic) |
| `src/nautilus_kernel/replay_backtest.py` | C | NEW file: candidate-replay strategy + `run_candidate_replay_backtest` (does not modify proof_strategy/backtest) |
| `scripts/research_strategy.py` | A | the single CLI: `plan | fast | compare | report` |

## `status.py` (A)
```python
class PromotionStatus(StrEnum):
    REJECT_FAST = "REJECT_FAST"
    PROMOTE_TO_FIDELITY = "PROMOTE_TO_FIDELITY"
    FIDELITY_MISMATCH = "FIDELITY_MISMATCH"
    READY_FOR_ROBUSTNESS = "READY_FOR_ROBUSTNESS"
    ROBUSTNESS_FAILED = "ROBUSTNESS_FAILED"
    READY_FOR_SHADOW_RESEARCH = "READY_FOR_SHADOW_RESEARCH"
# there is deliberately NO LIVE / READY_FOR_LIVE / PROMOTE_TO_LIVE member; tests assert this.
```

## `differential.py` (C) — types A depends on
```python
class DiffClass(StrEnum):
    EXACT_MATCH; TOLERANCE_MATCH; EXPECTED_ABSTRACTION; BROKER_FIDELITY_DIFFERENCE; BUG_SUSPECTED

@dataclass(frozen=True)
class NormalizedTrade:            # one schema for both engines
    signal_ts_ns: int; direction: int; decision_ts_ns: int; entry_ref: float; fill_price: float
    stop: float; target: float | None; exit_ts_ns: int; exit_price: float; exit_reason: str
    r_multiple: float; gross_pnl: float; net_pnl: float; mfe_r: float | None; mae_r: float | None
    costs: float; holding_bars: int | None

@dataclass(frozen=True)
class FieldDiff:
    trade_index: int; field: str; fast: object; fidelity: object; delta: float | None
    diff_class: DiffClass; reason: str

@dataclass(frozen=True)
class DifferentialResult:
    scenario_id: str; status: str            # "PASS" | "FAIL" | "BLOCKED"
    fast_trade_count: int; fidelity_trade_count: int
    field_diffs: tuple[FieldDiff, ...]; blocked_reason: str | None; summary: dict

DEFAULT_TOLERANCES: dict[str, ...]   # explicit, each with a written justification; unknown difference => FAIL
def run_differential(market, candidates, cost, sizing, rules, window=None, *, scenario_id, tolerances=DEFAULT_TOLERANCES,
                     catalog_dir=None) -> DifferentialResult
```
`BUG_SUSPECTED` or an unclassified difference => `status="FAIL"`. Missing Nautilus capability for a scenario =>
`status="BLOCKED"` with the exact reason (never silently PASS).

## `experiment.py` (A)
`ExperimentSpec` frozen dataclass: `experiment_version, strategy_spec (StrategySpec), markets (tuple[str]), dataset (DatasetRef),
date_range, split (SplitPlan identity), cost_model (CostScenario), sizing (SizingSpec), rules (SimRules), seed, engine_mode
("fast"|"fidelity"|"both"), feature_config (FeatureConfig), artifact_root, code_commit, notes`. `experiment_hash()` =
sha256 of canonical JSON (reuse `research_speed.artifact.canonical_json/config_hash`); forward/OOS data never read unless
the split says so, and the report states TRAIN/VALIDATION/OOS/FORWARD actually read.

## Artifact DAG (A) — stage keys (each includes the previous stage hash)
- FEATURES: dataset_hash + data_range + feature_config + feature_code_hash (importgraph closure) + library versions
- SIGNALS: features_hash + strategy_spec_hash + signal_code_hash
- SIMULATION: signals_hash + cost_model + sizing + rules + window + sim_code_hash
- METRICS: simulation_hash + metric_version
- FIDELITY: signals_hash + cost + replay config + nautilus version
- REPORT: all of the above
Speed invariants (tested): metric-only change => features/signals/simulation HIT, metrics MISS; cost-only change =>
features/signals HIT, simulation MISS; entry-rule change => features HIT, signals/simulation MISS; identical run => all HIT.
Uncertainty => MISS. Atomic publish: temp file -> fsync -> os.replace; manifest (commit marker) last.

## Resource policy
`plan` is LIGHT (read-only). `fast`/`compare` are HEAVY: refuse (exit 3) while `demo_trader.py`/`supervisor.py` is running
unless `--allow-light-only` applies; use `research_speed.parallel` clamps; BLAS threads = 1.

## ADDENDUM — Entry / Exit quality (reuse only; no second entry/exit system)
Source of truth stays `src/coverage_analysis/entry_exit.py` (+ `scripts/entry_exit_quality.py`) and
`src/coverage_analysis/shadow_exit_lab_study.py` (+ `scripts/shadow_exit_lab.py`, `src/demo/shadow_exit_lab.py`). The workbench only
ADAPTS fast/fidelity trades to their inputs and calls them. No new MFE/MAE engine, no duplicated exit-lab logic.
- Report has 3 separated sections: **ENTRY QUALITY** (MFE_R, MAE_R, time-to-MFE/MAE, MFE-first/MAE-first, P(MFE>=0.25/0.5/0.75/1.0R),
  useful/failure share, sample N, event-cluster N — independent of the real exit), **EXIT QUALITY / CAPTURE** (same-entry exit-policy
  comparison only: realized R, capture, giveback, applicability, TP1/TP2/runner where existing; the random-entry control is kept),
  **JOINT STRATEGY RESULT** (expectancy, PnL, drawdown, cost burden). A negative final trade is not a bad entry and vice versa.
- `ENTRY_EXIT_DIAGNOSTIC` (optional step after fast/fidelity) is DIAGNOSTIC / RESEARCH ONLY: the existing analytics are
  spread-adjusted gross, NOT commission/slippage/swap complete, so it must never be the sole cost-adjusted promotion gate; the
  report carries that caveat verbatim. Promotion still uses the full cost / fidelity path.
- Differential classification additionally per leg: ENTRY_MISMATCH | EXIT_MISMATCH | BOTH (Lane C).

## CLARIFICATION — right-tail reporting only (no exit change)
No existing exit policy is changed or tuned; the runner/trailing policies P4/P5/P10/P11 stay as they are and nothing may cap
profits earlier. The workbench ADDITIONALLY reports right-tail metrics in the EXIT QUALITY section (measurement only, never an
optimisation instruction): P(realized_R >= 2R / 3R / 5R) and mean/median realized_R conditional on MFE >= 2R / 3R / 5R (with
sample N per bucket). Purpose: an exit must not look better only through higher capture/giveback numbers while it cuts large
winners. Same-entry comparison rule applies; the report must flag when a profile has better capture but a lower right tail.
