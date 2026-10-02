# Authority map and duplication ledger — research programme v1 (OFFLINE / RESEARCH ONLY)

Rule: REUSE > ADAPTER > REFACTOR > REWRITE. Production authority is unchanged (sprint1/integration @ 11c6aec); nothing below
changes production behaviour. Evidence: VERIFIED = code read/grep in this worktree; SCOUT = Haiku scout candidate (not relied on).
Ledger classes: EXISTS · REUSE · ADAPT · MISSING · DUPLICATE · REPLACE_LATER · DELETE_LATER · DO_NOT_TOUCH.

| Area | Canonical authority | Evidence | Class | Research v1 action |
|---|---|---|---|---|
| Swings (fractal, N bars each side, M5/M15) | `demo.structure.confirmed_swings` (production) | VERIFIED: `market_observer/swings.py:62,84-87,312,332,424` and `levels.py:108` import and default to it (`detector=` hook, detector name recorded in the definition hash) | EXISTS / REUSE | MarketMap consumes `market_observer.swings` state built on that detector. **No second swing detector.** (Scout claim "swings duplicated" was wrong.) |
| Swing STATE (HH/HL/LH/LL sequence, EMA agreement, confirmation delay) | `market_observer.swings.SwingStructureState` / `swing_state` / `SwingReplay` (research-only, release-candidate branch) | VERIFIED defs `swings.py:109,312,323` | EXISTS (not production) | REUSE as the causal structure fact source |
| Levels / zones / level memory / role reversal | `market_observer.levels` (`LevelRegistry`, `Zone`, `RoleEvent`, `next_role`, `build_level_context`, `LevelConfig`) | VERIFIED defs `levels.py:178,218,273,303,320,427,720` | EXISTS (not production) | REUSE for MarketMap support/resistance/role-reversal. **No second S/R engine.** |
| Prior-day / session / round-number levels | `alpha/fast/store.py` features (previous_day_*, session_*), `alpha/families/roundnum.py` (live), `demo.structure.prior_range_edges` | VERIFIED names from store/structure; roundnum = SCOUT | EXISTS | feed MarketMap `nearest_*` only via the existing facts |
| Structural geometry (stop/TP1/TP2, invalidation, nearest opposition) | `demo.structure.structural_geometry` (+ `StructuralGeometry`, `Level`, `levels_beyond`, `management_signals`) | VERIFIED `structure.py:72,197,228,345,379` | EXISTS / REUSE | pre-entry geometry measurement reads this; **no second geometry engine** |
| MTF (M15/H1) | `demo.structure.resample_m15`, `market_observer.bars_adapter` (BarBuffer), FeatureStore `m15_*`/`h1_*` (causal; truncation-invariance verified) | VERIFIED `structure.py:130`, FeatureStore causality test | EXISTS / ADAPT | H1 context = FeatureStore `h1_*` arrays; add a thin causal adapter only if a needed field is missing |
| Balance / acceptance / participation features | `market_observer.{balance,acceptance,participation}` (DEFINITION_HASH + GROUP_VERSIONS) | VERIFIED module list; names SCOUT | EXISTS (not production) | REUSE; participation = MT5 tick-activity PROXY, not exchange volume |
| Family event generators | `alpha.families.*` (STRUCT, ROUND, ORB, VOLREV, GAP, EOD, LEADLAG, OVERNIGHT) -> `CandidateArrays` | VERIFIED (production closure) | EXISTS / DO_NOT_TOUCH | treated as EVENT/TRIGGER generators; production Family authority unchanged |
| Opposite-side handling while a position is open | stack gate `R_OPPOSITE = OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1` (`demo/execution/gates.py:85,218`, `live.py:1557,1583`, `tranches.py:90,134`), funnel class PORTFOLIO (`funnel.py:66,83`), arbitration (`demo/opportunity/arbitration.py:12,70`) | VERIFIED | EXISTS (TEMPORARY_STRUCTURAL_LIMITATION) | Position-Thesis monitor is RESEARCH/SHADOW only: it observes the rejected opposite event, it does not change the gate |
| Exit infrastructure | `src/exits/{engine,models}.py`, `demo/execution/exit_manager.py`, `demo/exit_{policies,profiles}.py`, `ExitReason` incl. SIGNAL_REVERSAL / STRUCTURE_FAILURE (scout) | VERIFIED existence of modules/gates; enum members SCOUT | EXISTS / DO_NOT_TOUCH | hypothetical exit variants are computed offline; no production exit authority changes |
| Entry/exit quality, shadow exit lab, right-tail | `demo/entry_exit_quality.py`, `demo/shadow_exit_lab.py`, `demo/exit_policies.py` via `research_workbench/entry_exit_adapter.py` | VERIFIED | EXISTS / REUSE | reused as is |
| Counterfactuals / funnel / gate classes / coverage | `demo/labeling.py`, `demo/funnel.py`, `demo/sequence_metrics.py`, `coverage_analysis/*` | scout inventory pending | EXISTS / ADAPT | Coverage Auditor reuses persisted facts (no new tracking system) |
| Hypothesis counting / multiple testing | `coverage_analysis/observer_lab/stats.py` `HypothesisRegistry` (persistent, `n_hypotheses` ever tested, Holm/BH) | VERIFIED docstring lines 11-13, 308 | EXISTS / REUSE | multiplicity visibility reuses it |
| Research harness (features/sim/fidelity/report) | `src/research_workbench/*` + `alpha/fast/*` + `nautilus_kernel/*` | VERIFIED | EXISTS | no second backtester/FeatureStore/fast sim/fidelity/report engine |
| "Thesis" / "setup state machine" concepts | none in production (only own-stop thesis invalidation wording, `ExitReason.SIGNAL_REVERSAL`; `alpha/discovery/temporal_archetypes.py` armed/invalidate helpers are discovery-only) | SCOUT + grep | MISSING | NEW, offline: MarketMap adapter, MarketThesis/SetupThesis contracts, SetupSpec, generic state machine, Position Thesis monitor (module `src/research_workbench/thesis/`) |

## Duplicates found
None requiring deletion tonight. Candidate overlaps (all research-only, no action): FeatureStore swing features vs `market_observer` swing state
(both ultimately use the same fractal definition family; FeatureStore arrays are for vectorised research, observer state for event semantics).
REPLACE_LATER: none. DELETE_LATER: none. Alpha contract extraction (CandidateArrays/EXIT_TRAIL) stays blocked by live consumers.

## Reuse pledges for the new code
1. MarketMap = thin adapter over `market_observer` facts + FeatureStore arrays + `demo.structure` geometry; stores provenance/versions/hashes.
2. Setup engine = one generic state machine driven by declarative `SetupSpec` (no engine per pattern).
3. Events/triggers = existing Family generators / observer events; no new pattern engine.
4. Coverage = existing store tables + labeling/funnel semantics.
5. Research = Workbench DAG stages (FEATURES->SIGNALS->SIMULATION->METRICS->FIDELITY) with semantic version hashes in the stage keys.
