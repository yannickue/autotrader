# Market thesis / setup / position-thesis architecture v1 (RESEARCH / OFFLINE ONLY)

Status: design for review (CODEX-2/3). No production authority changes. Production = sprint1/integration @ 11c6aec.
Companion: `docs/authority_map_research_v1.md` (what already exists and is reused).

## 0. Principles
- Reuse facts, add semantics: swings = `demo.structure.confirmed_swings` (via `market_observer.swings`), levels/zones/role = `market_observer.levels`,
  geometry = `demo.structure.structural_geometry`, events/triggers = existing Family generators / observer events, research harness = Workbench DAG.
- CAUSAL: every object at decision time T uses only data with timestamp <= T (bar-close semantics: decision at the close of bar t, fill at the next open).
- DETERMINISTIC, VERSIONED, AUDITABLE, REPLAYABLE (batch == incremental), LONG/SHORT symmetric.
- No opaque scores: a thesis carries required / observed / missing conditions per evidence class (CONTEXT, LOCATION, STRUCTURE, LEVEL_BEHAVIOUR, TRIGGER, GEOMETRY, PARTICIPATION).
- No trade authority: nothing in this package places, sizes or exits orders; it emits research records only.

## 1. Package layout (`src/research_workbench/thesis/`, offline-only; not imported by anything production-reachable)
| Module | Content |
|---|---|
| `contracts.py` | enums + frozen dataclasses (below), `THESIS_CONTRACT_VERSION`, canonical-JSON/hash helpers (reuse `research_speed.artifact`) |
| `marketmap.py` | `build_market_map(bars, i, config) -> MarketMap` thin causal adapter; `MARKETMAP_VERSION`; definition hash |
| `main_thesis.py` | `derive_main_thesis(market_map, history) -> MarketThesis` (NO_CLEAR_THESIS is valid) |
| `setup_engine.py` | generic `SetupEngine` + declarative `SetupSpec`, `advance()` state machine, batch + incremental replay |
| `specs.py` | spec catalog: CONTINUATION_RETEST fully specified/evaluable; the other 9 archetypes representable as specs (status NOT_EVALUATED until their predicates exist) |
| `position_thesis.py` | `PositionThesis`, `OpposingEvent`, monitor + hypothetical exit variants A-D |
| `research.py` | Workbench integration (stage mapping, matched controls, ablation, hypothesis counting, reporting) |
| `../coverage.py` | Coverage Auditor (separate module, Phase 1) |

## 2. Contracts (frozen dataclasses; every one carries `contract_version`, `definition_hash`, `code_sha`, `observer_version`)
- `MarketMap`: market, decision_ts, session, market_phase, h1_context, m15_structure, m5_structure, nearest_support/resistance (+ second), active_support_zone,
  active_resistance_zone, role_reversal_zones, nearest_opposition, second_opposition, balance_state, acceptance_state, participation_state, volatility_context,
  structural_geometry (optional, direction dependent), provenance (source module + version per field), feature_versions, definition_hashes.
- `MarketPhase`: TREND, PULLBACK, BALANCE, RANGE, COMPRESSION, EXPANSION, BREAKOUT, FAILED_BREAK, REVERSAL_ATTEMPT, TRANSITION, UNDEFINED.
- `MainThesisState`: BULLISH_CONTINUATION, BEARISH_CONTINUATION, RANGE_ROTATION, BREAKOUT_EXPANSION, FAILED_BREAK_REVERSAL, TRANSITION, NO_CLEAR_THESIS.
- `SetupSpec` (declarative): archetype, allowed_market_phases, requirements per evidence class (named predicates over MarketMap fields + params), optional_evidence,
  invalidation predicates, expiry (bars), semantic_exit_profile (reference only), `spec_version`, `spec_hash`.
- `SetupState`: CANDIDATE, FORMING, LOCATION_REACHED, CONFIRMING, ARMED, TRIGGERED, INVALIDATED, EXPIRED (terminal: TRIGGERED, INVALIDATED, EXPIRED).
- `SetupThesis`: thesis_id, market, direction, archetype, state, created_at, updated_at, required/observed/missing conditions (per evidence class), invalidation_condition,
  expiry_condition, entry_zone, structural_stop, opposition_1/2, family_events, daily_thesis_alignment (ALIGNED | NEUTRAL | OPPOSED), market_map_version, spec_hash, history of
  transitions `(ts, from, to, reason)`.
- `SetupEvaluation`: per bar result of `advance` (state, transitions, newly observed/missing conditions).
- `PositionThesis`: position/intent identity (opportunity_id/intent_id), original SetupThesis snapshot (or Family trigger descriptor when no setup existed), direction,
  premise conditions (causal premise of the trade), `PositionThesisState`: HEALTHY, OPPOSING_EVENT, OPPOSING_SETUP, THESIS_AT_RISK, THESIS_INVALIDATED, CLOSED, with transition history.
- `OpposingEvent`: ts, opposing direction, source (Family event / setup), whether the stack gate rejected it (`OPPOSITE_SIDE_WHILE_OPEN_NOT_SUPPORTED_V1`), future-path completeness flag.

## 3. SetupEngine
- `advance(thesis_or_None, market_map, events, spec) -> SetupEvaluation`: pure function of (previous state, MarketMap at T, events at T); no look at later bars. The state at T never changes retroactively.
- Batch replay `replay(bars, spec)` and incremental `Replayer.step()` must produce identical state/timestamps/triggers/invalidations (tested).
- Long/short symmetry: `mirror(market_map)` test helper; transitions must be symmetric under mirroring.
- Adding a hypothesis = adding a `SetupSpec`; no engine per pattern.

## 4. CONTINUATION_RETEST v0 (vertical slice; LONG shown, SHORT mirrors)
CONTEXT: H1 not clearly bearish; M15 UP_SEQUENCE (or compatible transition). LOCATION: at support / FLIPPED_TO_SUPPORT zone (role reversal). STRUCTURE/SEQUENCE:
break -> acceptance -> pullback/retest. LEVEL_BEHAVIOUR: no acceptance below support, plus hold / rejection / reclaim / successful retest. TRIGGER: existing STRUCT retest
or causal M5 structure transition event. GEOMETRY: structural invalidation + stop exist, nearest/second opposition known, space measured. INVALIDATION: structural break against
thesis, acceptance against thesis, expiry. No tuning before a result exists; thresholds frozen in the spec (`spec_hash`).

## 5. Position thesis monitor (research/shadow only)
After a fill the entry thesis becomes a PositionThesis. Opposing information is classified, never acted on:
OPPOSING_EVENT (single opposite trigger, e.g. ROUND_REJECT_SHORT while long) -> OPPOSING_SETUP (an independent complete opposite SetupThesis) -> THESIS_AT_RISK (premise deteriorating:
structure weakens, reclaim fails, acceptance begins against the position) -> THESIS_INVALIDATED (the causal premise objectively failed, e.g. FLIPPED_TO_SUPPORT broke and was accepted below).
INVARIANT exit != reverse: two independent decisions (A: should the position remain open; B: is there a valid NEW setup in the opposite direction). No function returns "close and reverse";
a new opposite setup must satisfy ALL its own entry requirements independently. Hypothetical management variants (offline, on the same entries):
CONTROL (existing exit behaviour), A exit on ANY opposing event, B on independent OPPOSING_SETUP, C on THESIS_AT_RISK, D only on THESIS_INVALIDATED. Metrics after an opposing event: future MFE/MAE,
target/stop hit, giveback, capture, realized R, time to adverse, time to recovery, false early exit, whipsaw, and the conditional probabilities P(target|opposing event), P(stop|...), etc.
Opposing events whose future path is still open are PENDING, not missing (Coverage Auditor).

## 6. Workbench integration (no new DAG engine)
FEATURES = observer facts + MarketMap + descriptive thesis inputs; SIGNALS = setup state events / ARMED / TRIGGERED / hypothesis ids; SIMULATION = existing fast path on triggered entries;
METRICS = coverage, entry/exit quality, matched controls, ablation, conditional results, right-tail; FIDELITY = Nautilus survivors via the existing differential. Stage keys include
MARKETMAP_VERSION, observer version, SetupSpec hash, THESIS_CONTRACT_VERSION, position-thesis version and the import-closure code hash (no stale semantic artifacts).

## 7. Research design guards
Discovery/confirmation split with frozen definitions (new version => new untouched validation data or label EXPLORATORY); every tested hypothesis/variant/market/slice/threshold counted via
`coverage_analysis.observer_lab.stats.HypothesisRegistry` (never only the winner); matched controls (market, direction, trigger/family, session, volatility regime, timing, cost regime;
insufficient match => INCONCLUSIVE, never loosen); ablations FULL minus {CONTEXT, LOCATION, HTF, LEVEL ROLE, ACCEPTANCE, RETEST, GEOMETRY, PARTICIPATION, MAIN-THESIS ALIGNMENT}; cost-aware
(no gross-only promotion); classifications REJECT / INCONCLUSIVE / PROMISING_FOR_FIDELITY / FORWARD_SHADOW_CANDIDATE (no LIVE).

## 8. Tests (mandatory)
Prefix invariance (full history vs prefix to T) for MarketMap, main thesis, setup state, position thesis, geometry; batch == incremental replay; long/short mirror symmetry; state-machine legality
(only declared transitions, terminal states absorbing); exit != reverse invariant; no look-ahead in level age/touch counts; determinism; contract hash sensitivity.
