# AR2 Research Engine Rebuild - Report (2026-09-29/30)

Branch `sprint1/integration`. Starting HEAD of this rebuild: `fe1bbc2` (AR1 end). Perf checkpoint `3fde3e2`.
Final code HEAD for the results below: `d7c616d` (+ the commit adding this report). Nothing was pushed
after the AR1 push `fe1bbc2`; all AR2 work is local. OOS was never touched.

## Verdicts

```
RESEARCH ENGINE REBUILD:            COMPLETE (fast core); INCOMPLETE items listed under "Deferred"
22-VARIANT AR2 TRAIN+VALIDATION:    COMPLETE (EXIT=0, 68 s cold, run into research/reports/ar2_phase2)
CANDIDATE EQUIVALENCE:              PASS (12k reference AND full 75,837-bar dev frame, bit-exact)
PERFORMANCE TARGET:                 PASS for 22 x 70k; 1000-variant needs a light screening mode (see below)
PROVEN EDGE FOUND:                  NO
```

## Uncommitted-state resolution / Opus changes
Opus' performance changes (label cache, `MtfView.at` speedup, positional label lookup, opening_drive) were
NOT accepted on trust: all 22 variants (1311 candidates) reproduced the pre-optimization reference exactly
before commit (`3fde3e2`); generation 90.9 s -> 28.4 s on the 12k slice. Retained (they also serve the
semantic reference path). The old slow runner `research/runners/ar2_compare.py` is kept as the semantic
reference only; it never completed a real-data run and is no longer used.

## Architecture (what was built)
- **Feature Store** `src/alpha/fast/store.py`: 86 causal arrays on M5 length (base, session/prev-day levels,
  M15/H1 forward-mapped only after bucket completion, regime/context codes reused from the existing
  classifiers, M5/M15/H1 TA-Lib set, swing highs/lows stamped at their confirmation bar, structure state, BOS,
  compression ratio, M15 range bounds). Cache key = dataset hash + schema version + params + code
  fingerprint + ta-lib/numpy/pandas versions. Cold 17 s (dev frame), warm load 0.3 s, ~8 MB.
- **Fast simulator** `src/alpha/fast/sim.py` (numba): AR1 semantics (next-bar-open fills, spread/slippage,
  stop-first, gap handling, forced flat 21:30, fixed-R sizing, MFE/MAE). EXACT vs `evaluate_candidates` on all
  22 variants x all cost scenarios. 1000 simulations on the 75.8k-bar frame: 0.1 s.
- **Kernels** `src/alpha/fast/kernels/*`: 10 families / 22 variants as numba generators, registered through
  `alpha.fast.registry`. Semantic strategy classes remain the reference.
- **Alpha boundary** `src/alpha/fast/spec.py`, `provider.py`: `StrategySpec` (declarative rules, stop/target,
  hash), `evaluate_spec`, `AlphaProvider` protocol + adapters (kernel families, specs) and
  `to_signal_candidates` (-> standard `SignalCandidate` for Nautilus validation). Risk/Execution/Nautilus/MT5
  untouched.
- **Runner** `research/runners/ar2_fast.py`: staged `[stage]` progress lines, `EXIT` file + last line
  `EXIT=<code>`, result cache (fingerprint incl. sizing, rules, sim/frame sources), trial accounting, frozen
  set with dev provenance, gated `--oos` (never automatic, refuses on hash or provenance mismatch).
- **Dependencies**: `research` group (default): numba 0.67.0, TA-Lib 0.8.1. VectorBT NOT used (stateful
  strategies, heavy deps, would duplicate fill semantics). Optuna 5.0.0 / DEAP 1.4.4 resolve under the pins
  but are not added yet.

## Equivalence (gates)
1. Sim vs AR1 simulator, golden 12k, 22 variants x 5 cost scenarios: exact. This test initially SKIPPED in the
   worker's tree (no dataset) and hid a one-bar decision-mapping bug; fixed by Claude.
2. Kernels vs golden 12k slice: bit-exact (all 22 variants).
3. Kernels vs semantic strategies on the FULL dev frame (`research/reference/full_frame_equivalence.py`):
   22 variants, 8650 candidates, identical. Prompted by the Codex review (a mean-reversion kernel rebuilt M15
   buckets by OHLC change; fixed by reading the context range bounds from the store).
4. Independent Claude re-run of the fast runner reproduced Codex's `dev_results.json` and freeze hash exactly.

## Performance
| workload | old | new |
|---|---|---|
| 22 variants x 12k bars (candidates) | ~91 s (6 workers) | seconds (kernels), 8 s incl. numba cache load |
| 22 variants x 75.8k bars, full Train+Validation run | 40+ min, never completed | 68 s cold (17 s features), ~33-44 s warm |
| 1000 simulations, 75.8k bars | n/a | 0.1 s |
| feature store load | recompute every run | 0.3 s |
The remaining ~40 s of the run is the per-variant Python metric set (day-clustered bootstrap CIs, matrices),
about 1.3 s per variant with full metrics. For 1000 variants use a LIGHT screening mode (kernel + fast
metrics, no CI/matrix) before running large search; not built yet (deferred).

## Delegation
Codex: feature store, fast simulator, three kernel groups, fast runner, alpha boundary, one read-only review.
Opus: one perf pass (stopped by the user before verification; result verified by Claude afterwards).
Gemini (via agy, text-only red-team): keep optimization/ML after the baseline (adopted); count all trials and
cap search budgets (adopted, trial accounting added); embargo between Train and Validation (TODO for search
phase); its claim that stop-first inflates win rates is wrong (stop-first is the pessimistic direction).
Claude: orchestration, exact-equivalence gates, defect fixes after Codex hit its usage limit (decision-bar
mapping, short-series slope edge case), review-fix integration.

## Codex review (one focused pass, read-only) - all findings handled
High: result cache ignored sizing/rules/sim sources -> fixed. High: `--oos` accepted a frozen file from another
experiment version -> provenance stored and checked before the OOS gate -> fixed. Medium: mean-reversion
kernel bucket reconstruction -> fixed. Medium: restricted stress expectancy not partition-masked -> fixed.
Medium: regime-restriction search was not flagged as data-mined -> `VALIDATION_MINED_BEST_OF_MANY` flag and
`regime_restrictions_considered` trial count. Low: library versions in the feature cache key -> fixed.
No findings for lookahead/timestamp alignment, stop/target ordering, cost application, metrics, production
coupling. Not verifiable by that review: DST/holiday behaviour on the full frame (covered afterwards by the
full-frame equivalence run above).

## Train + Validation result (22 variants, BASE costs)
Dataset: 75,837 M5 bars, TRAIN 2025-02..2025-11, VALIDATION 2025-12..2026-04 (OOS physically absent).
See `report.md` (per-variant table, matrix, survivors), `regime_context_matrix.csv`, `session_matrix.csv`.
- No variant passes the pre-registered freeze rule as a WHOLE variant.
- 5 "survivors" are all `REGIME_RESTRICTED` slices (COMPRESSION_EXPANSION#0 VOLATILITY=LOW,
  FAILED_BREAKOUT#1 DIRECTION in NEUTRAL/UP, ORB-retest #0/#1/#2 each with a DIFFERENT restriction). They are
  the best of ~4 dimensions x their values per variant, mined on Train+Validation: hypotheses for OOS at most,
  not evidence.
- Central question (does H1/M15 context improve M5 performance?): 199 (variant x H1 dimension/value x M15
  context) slices have N_train>=30 and N_validation>=15; only 32 (16%) are positive in BOTH partitions (about
  25% would occur by chance with random signs); mean slice expectancy is -0.098 R in Train and +0.000 R in
  Validation; train/validation expectancy correlation across slices 0.24 (inflated: slices overlap and
  duplicate variants). Verdict: no evidence of robust conditional edge from this hypothesis set.
- Families with tiny samples (`INSUFFICIENT_SAMPLE`): momentum, opening drive, trend pullback, session sweep,
  session TWAP.
- REJECTED_EARLY (negative in Train and Validation): trend pullback, previous-day levels, session TWAP.
- SESSION_TWAP_REFERENCE: all its candidates fall OUTSIDE the AR1 entry window (`outside_window`), so the
  family is effectively untestable as defined (identical in the AR1 simulator); a definition problem, not an
  engine defect. Needs a new strategy version if it should be tested (deferred, not silently changed).
- Cadence (313 trading days, zero-trade days included): the busiest single variant is RANGE_MEAN_REVERSION#0/#1
  with 1.77 trades/day (553 trades, negative Train expectancy), then PREVIOUS_DAY_LEVELS 1.04/day, ORB-retest
  0.56-0.68/day; the sum of all 22 variants' rates is 11.6/day but that is an upper bound (signals overlap and
  most variants are negative). Every trade count with positive Train+Validation expectancy is well below the
  3-8 trades/day goal; no quota was forced.

## Tests / gates
Full `pytest` on the final code (`d7c616d`): 1848 passed, 1 skipped (pre-existing), 158 s. `ruff check .` clean,
compileall clean, 0 real MT5 calls. Fast-engine suites (sim, store, three kernel groups, runner, spec) all green;
full-frame equivalence run PASS. Contract/property/chaos/replay suites are part of the full run.

## Deferred (not activated, by instruction)
Optuna, DEAP, Qlib/RD-Agent spike, RL, NASDAQ100, Partial-TP/exit research, light 1000-variant screening
mode, Nautilus validation of survivors, walk-forward, embargo between Train/Validation, OOS.
