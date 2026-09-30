# Automated Alpha Discovery V1 - final report (2026-09-30)

Scope: GER40 M5, 75,837 development bars. Train 2025-02-01..2025-11-30, 5-day embargo,
Validation 2025-12-01..2026-04-30. Fitness is Train-only; Validation is a survival gate only.

## Verdict block

```
AUTOMATED ALPHA DISCOVERY V1:            INCOMPLETE (search + screening done; two evidence items open, see section 7)
SEARCH PERFORMANCE:                      PASS (1000 real-kernel variants ~4 s, gate <= 5 min)
TOTAL UNIQUE STRATEGIES TESTED:          29,798 (30,310 trials; campaigns main1 + main2 + main3_hf)
FAST-SCREEN SURVIVORS (Stage E):         main1 28 (old, looser gates; 2 under current gates), main2 1, main3_hf 0
NAUTILUS SURVIVORS:                      0 - stage not run (deferred, no finalist warrants it)
ROBUST POSITIVE TRAIN+VALIDATION EDGE:   NO (rule verdict INCONCLUSIVE; nothing exceeds the selection null;
                                         real results lie inside the null-data range)
OOS TOUCHED BY AD1:                      NO - but the same OOS window was already evaluated in AR1
                                         (oos_access_log: 3 evaluations), so it is NOT a clean holdout
```

## 1. Baseline and starting point
Started at 8e9f97b, tree clean; 22 AR2 commits pushed as a fast-forward before any work.
Everything up to 8df8d87 is on origin/sprint1/integration; later commits are local (see section 8).

## 2. Performance (AD1B)
| variants | wall (cold cache) | warm cache | peak RSS |
|---|---|---|---|
| 100 | 0.4 s | 0.007 s | 378 MB (incl. 16.6 s cold feature build) |
| 1000 | ~4 s | 0.04-0.08 s | 216 MB |

About 250 variants/s (simulation ~3x candidate generation); no multiprocessing needed.
Search campaigns: ~110 s for ~10k unique specs (Optuna ~90 evals/s, DEAP ~130 evals/s).

## 3. What was built
Embargo (fingerprinted); light screen; 29 new causal price-action features (truncation-invariance
tested on synthetic and real frames incl. DST); strategy grammar (catalog with rationale per feature,
Train-quantile thresholds, mirrored SHORT, complexity limits <= 6 clauses, canonical hashing,
trial ledger); Optuna (fixed structure -> parameters); DEAP (structure, lineage-capped, deterministic);
Train-only fitness (cost-stressed expectancy minus day-clustered SE, complexity/concentration/
consistency penalties, no leverage); stages C (Validation gate), D (cost stress), E (parameter
neighbourhood, regime/month concentration, entry-timing robustness); overlap clustering;
selection-aware statistics; provenance checks; degeneracy audit.

## 4. Search accounting
| campaign | unique specs | trials | note |
|---|---|---|---|
| main1 | 9,925 | 10,086 | looser gates, before defect fixes |
| main2 | 9,946 | 10,113 | after fixes: >=60 Train trades, brk floors, Validation t>=1, >=25 trades |
| main3_hf | 9,927 | 10,111 | >=250 Train trades (~1.2+ trades/day) |
| cumulative | 29,798 | 30,310 | neighbour trials (Stage E) counted separately |

## 5. Results
- main1: 294/800 pool candidates positive in Train and Validation; 28 passed Stage E (21 clusters) under
  the old gates; re-run under current gates: 2 finalists (Val t 1.18/1.08, pooled t 2.0/1.78).
  Best pooled t 3.48 < null bound 4.29. The top finalist had a degenerate 'breakout' clause
  (brk_up_20 > -1.32 ATR) - fixed.
- main2: 314/800 positive in both; 15 passed Stage C/D; 1 finalist (TREND_PULLBACK): Train E +0.18 R
  (113 trades), Validation E +0.20 R (52 trades, t 1.13), pooled t 1.84 vs null bound 4.45, Train
  fitness slightly negative. Entry delays of 1/2/3 bars: +0.12/+0.10/+0.10 R (graceful, not knife-edge).
  Validation null bound (N_val_looks = 800): 3.66. ~0.3 trades/day.
- main3_hf (>=250 Train trades): 108 positive in both, 0 pass Stage C -> NO.
- Trade frequency of every surviving candidate is far below 'several per day'. The frequent-trading
  region of this grammar/feature space looks empty (best Train fitness ~0.1, none survives Validation).

## 6. Null calibration (v1, 3 seeds, block-shuffled returns, drift-preserving)
Stage-E 'finalists' appeared on structure-free data in 2 of 3 seeds (10 and 5; Val t up to 1.87, pooled
t up to 2.76; 0 in the third). Real main2 (C 15, E 1, Val t 1.13, pooled t 1.84) lies inside the null
range. Consequence: the stage gates alone are not a discovery criterion; only the selection null bound
rejects candidates - and none exceeds it.

## 7. Open items (NOT done; do not read the verdict as stronger than this)
Decision (user, 2026-09-30): items 1 and 2 are NOT restarted in V1; they are to be caught up in V2.
1. No random-entry / intraday-drift baseline. All finalists are LONG and the 09:00-21:30 window drifted
   +4.7 pts/day (Train) and +16.1 pts/day (Validation); a LONG 'edge' may be buy-the-day drift. The
   verdict rule now makes YES impossible without such a baseline (`--drift-baseline-json`). The
   builder was stopped before finishing it; nothing of it is committed.
2. The v1 null is biased (spreads real drift across Train/Validation) and underpowered (3 seeds). A
   zero-drift, time-of-day-stratified, >=12-seed null was started and stopped unfinished.
3. Nautilus survivor validation (Stage F) not run: needs a SignalCandidate-consuming Nautilus strategy,
   a BacktestResult->TradeArrays exporter and cost-model parity (Nautilus lacks explicit slippage and the
   trades/day cap). No finalist justifies the effort yet.
4. Walk-forward, Qlib/RD-Agent spike, exit research, dynamic sizing, RL: deferred by design.

## 8. Reviews
- Gemini (agy, tool-free, `pro`): accepted - Validation is a survival filter (partly in-sample), entry-delay
  perturbation, frequency experiment. Rejected - 'SE penalty favours few trades' (backwards: SE shrinks with n),
  'lock a candidate and run OOS' (violates the hard stop), missing volume/intermarket data (unavailable),
  time-of-day features (already a gene).
- Opus read-only audit: no lookahead and no Train->Validation leak (115 arrays bit-identical on prefixes; seal,
  embargo, mirror/SHORT semantics, fitness signs, determinism verified). Evidence findings HIGH: OOS already seen
  in AR1; Validation reused (N undercounted); no drift baseline; weak null. Code findings fixed: provenance
  checks + OOS assertion in the survivors runner, cache-fingerprint sources/versions + numba window constants,
  canonicalisation fixed point + unique_behaviors, partition-wise Stage D, Validation-only verdict statistic, OOS
  provenance banner. Not fixed (documented limitations): context flags are not directional (a 'LONG pullback'
  also fires in downtrends); session levels span the whole calendar day incl. overnight (gap_atr is the overnight
  gap); thresholds use all 24 h Train bars; costs in R are exact for target exits.
- Codex final review: not run (weekly quota exhausted during the phase); replaced by the Opus audit.

## 9. Recommendation
Do not promote any AD1 candidate. Before spending more search budget: (a) finish the drift baseline and the
zero-drift null, (b) decide how to obtain a genuinely unseen forward period (the 2026-05..08 OOS was viewed in
AR1), (c) consider adding directional context flags and multi-market data to raise trade frequency. The OOS
period remains untouched by AD1 and is not to be run without an explicit decision.
