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

## 9. REVISION after CODEX-2/3 (design gate) — these supersede earlier text above
1. **SwingReplay is NOT an incremental authority** (it computes whole segments incl. bars after i and warns about stale caches). MarketMap calls the causal
   `market_observer.swings.swing_state(bars, i, timeframe)` only. SwingReplay is out of the design.
2. **MarketMap call contract (field -> call):** structure fields <- `swing_state` (M5, M15; `None` during warm-up); support/resistance/zones/role-reversal <-
   `market_observer.levels.build_level_context(bars, i, config)` (+ `LevelRegistry.update` for incremental replay); balance_state <- `balance_features(bars, i, config)`;
   acceptance_state <- `acceptance_features(bars, i, level, direction, config)` evaluated per (nearest level, direction) and stored as a dict keyed `"<LONG|SHORT>:<level_id>"`
   (acceptance is level- and direction-specific, never one global state); participation_state <- `participation_features` (tick-activity PROXY); h1_context <- FeatureStore
   `h1_*` arrays or the existing opportunity context builder (`demo/opportunity/snapshot.py` context) — only a missing field gets a minimal causal adapter; all warm-up gaps are `None`.
3. **Timestamp convention:** `decision_ts = bars.decision_ts_ns(i)` (bar close). Every level confirmation, role event, baseline window and trigger timestamp must be <= decision_ts.
4. **Geometry adapter:** `demo.structure.structural_geometry(direction, entry, bars: DataFrame, spread, atr, ...)` needs a prefix-only OHLC DataFrame through i and a proposed
   entry + direction. Geometry is computed ONLY for a proposed entry (setup evaluation / position thesis), never as unconditional MarketMap state.
5. **State machine:** the legal transition table and precedence are frozen in `thesis/contracts.py` (`SETUP_FORWARD_EDGES`, `legal_setup_transition`, `SETUP_PRECEDENCE`:
   INVALIDATED > EXPIRED > forward progress; terminal states absorbing; several forward edges may chain on one bar, each stamped with the same decision ts; first-observed timestamps frozen).
6. **Opposite-side facts:** the live stack rejects an opposite signal while a position is open (`live.py:1557`, `gates.R_OPPOSITE`, `gates.py:85`); funnel rows persist the reject code,
   `otherwise_valid`, snapshot and decision (`store.py:1026`); counterfactual labels exist separately (`store.py:1312`). That reconstructs a rejected OPPOSING_EVENT. OPPOSING_SETUP and
   THESIS_AT_RISK are NOT persisted: they are derived OFFLINE by replaying persisted opportunity facts + versioned bars + specs.
7. **Exit variants A-D** are computed as shadow policies over ONE immutable entry cohort via `shadow_exit_lab.evaluate_shadow(entry, bars)` (existing signal-reversal exit: `exits/engine.py:230`);
   entries are never regenerated per policy.
8. **Reuse list (ban on parallel implementations):** matched controls + `incremental_ablation` (`coverage_analysis/observer_lab/enrichment.py`), persistent `HypothesisRegistry`
   (`observer_lab/stats.py`), FeatureStore swing/H1 arrays (`alpha/fast/store.py`), alpha temporal armed/invalidation presets (`alpha/discovery/temporal_archetypes.py`), the opportunity
   context builder (`demo/opportunity/snapshot.py`).
9. **V1 scope discipline:** one fully evaluable spec (CONTINUATION_RETEST); the other archetypes are representable specs flagged `implemented=False` (NOT_EVALUATED, never silently zero);
   position-thesis states are all representable, variants A-D are computed on the same cohort; no custom control/report layers.

## 10. Thesis study adapter: limitations (CODEX-4/5 follow-up; `thesis/study.py`)

The study adapter is **exploratory, never validation**. Every result carries these as `limitations`, plus `no_promotion_reasons`:

1. **No end-to-end OOS / embargo validation.** The adapter reuses the observer-lab partitions, purge/embargo and controls, but it does not run
   a pre-registered train / validation / OOS protocol; a result here can at most be a research candidate (`PROMOTE_TO_FIDELITY`).
2. **Retrospective control matching.** `match_controls` picks control bars on BOTH sides of the event in time, and by default the volatility /
   spread percentile ranks are partition-wide (they use later bars of the partition). `StudyConfig(causal_controls=True)` switches the ranks to
   expanding past-only; the candidate pool remains two-sided.
3. **Caller-supplied trigger events** (`STRUCT_RETEST_*`): not recomputed by the adapter.
4. **Label risk** is `risk_atr_mult x ATR`, a study convention, not the thesis structural stop.
5. **Exit variants A-D** are only descriptive vs CONTROL; each variant x contrast (mean R delta, whipsaw rate) is registered in the
   `HypothesisRegistry` before it is computed, so `n_hypotheses` includes them.
6. **Multiplicity scope.** Without a persistent `HypothesisRegistry(path=...)` the result is `multiplicity_scope="RUN_LOCAL_ONLY"` (warning +
   no-promotion reason): hypotheses of other runs are not counted. The registry state at start is part of the cache key.
7. **Cache key** (`THESIS_STUDY` stage of the existing ArtifactStore) covers bars, events, MarketMap content hashes (or replay-from-bars),
   geometry (an override needs a `geometry_version` token, else caching is refused), partition, registry state and exit variants, besides the
   semantic versions (`MARKETMAP_VERSION`, `MAIN_THESIS_VERSION`, `spec_hash`, ...). A cache hit replays the stored hypotheses into the
   caller's registry.
8. **Coverage:** the top-level coverage `no_promotion_claim` is true whenever ANY section claims it (position_thesis NOT_AVAILABLE / pending /
   unexpected-missing / late events ignored); `promotion_claim_allowed` is true only when every section is complete. A GREEN status is not a
   promotion permit.
9. `PositionThesis` observation counts `late_events_ignored` (opposing events supplied after their own decision bar are not used, but visible).
