# Alpha Discovery V2 - final report (2026-09-30)

## Verdict

```
ALPHA DISCOVERY V2:                 INCOMPLETE (engineering complete; discovery evidence negative; two lanes deferred)
SPEEDBUILDER:                       COMPLETE
SPEEDUP:                            4.57x cold / 3.62x warm on 800 genomes (bit-identical candidates, hashes, fitness); 2.79x on the campaign phase, 1.23x incl. setup
MARKETS TESTED:                     GER40, NAS100 (UsaTec), SPX500 (Usa500), XAUUSD (GOLD), EURUSD (4 with PROVISIONAL calendars)
TIMEFRAMES:                         D1, H4, H1, M15, M5 in events/features; M1 downloaded (only ~2.5 months) but not used in search
UNIQUE STRATEGIES TESTED (V2):      7,500 temporal state-machine specs (5 x 1,500) + survival-stage evaluations (plan max 4,350; exact count in the ledger)
                                    + 5,114 formula/GP trials (+ ~7,200 untracked exploratory ones used to tune novelty weight)
                                    + 47,754 trades / 500 specs for meta-labelling + 7 alpha families (<=400 specs each, GER40 smoke)
                                    + 48,324 raw-scan cells (41,628 unique)
                                    CUMULATIVE TRIALS in the survival ledger: 40,443 (V1 30,310 carried in)
TEMPORAL/STATE STRATEGIES:          7,500 probe specs, 10 timeframe sets, ~230 event signatures per market, 145 quality-diversity niches (GER40)
CHART PATTERN STRATEGIES:           PATTERN_CONFIRM_RETEST 130 specs (GER40): 0 passers; double top/bottom, inside-bar; no edge
ALPHAGEN OUTPUT:                    own re-implementation (AlphaGen has NO licence): 50 causal operators, DEAP GP vs random formulas on GER40 Train:
                                    best fitness 0.067 vs 0.073 (null <= 0.058); expectancy ~0 R (BASE), -0.11 R (adverse): NO EDGE
RD-AGENT/QLIB OUTPUT:               NOT RUN. RD-Agent classified Linux/Docker-blocked FROM DOCUMENTATION ONLY (no install attempt); Qlib heavy, MIT
                                    (isolated experiment not attempted). Replaced by the FormulaAlpha module.
ML META-LABEL:                      INCONCLUSIVE / NOT USEFUL (GER40 Train, 47,754 trades): GBDT OOF AUC 0.583 [0.555, 0.609] but top-decile mean R -0.034
                                    (all trades -0.130, t 0.98); AUC comes from R:R geometry (risk_atr, plan_rr), not from expectancy
RAW OPPORTUNITIES/DAY:              per market, median passing candidate 0.54-0.64 trades/day (p95 1.65-2.65); NOT edge-qualified; ~3/day summed over 5 markets ceiling
INDEPENDENT OPPORTUNITY CLUSTERS/DAY: not measurable without an accepted edge; effective independent behaviours among passers (corr>0.8):
                                    GER40 185/250, NAS100 126/193, SPX500 116/186, XAUUSD 130/197, EURUSD 98/193
ACCEPTED TRADES/DAY:                0 (no strategy accepted)
ECONOMICALLY MEANINGFUL TRADES/DAY: 0
ROBUST EDGE FOUND:                  NO (inconclusive on the finalists' upside hints; see below)
INDEPENDENT EDGE CLUSTERS:          0
BEST CANDIDATE:                     none promoted. Best-looking survival hint: XAUUSD ec244f13c8 (test +0.28 R, t 1.33, 51 trades, 3/3 folds positive) -
                                    statistically indistinguishable from chance among 150 finalists / 40,443 cumulative trials
CONFLUENCE VALUE:                   INCONCLUSIVE (leaning negative): mean R k=1/2/3+ independent clusters = -0.159 / -0.171 / -0.178 (k>=2 vs 1: -0.017 R, t -0.69)
NAUTILUS VALIDATED:                 NO (Stage F not built: no candidate justifies it)
NEW CLEAN FORWARD HOLDOUT TOUCHED:  NO (nothing after 2026-08-31 was ever loaded; enforced by loader guard + tests)
GROWTH SIMULATION READY:            YES (as a tool: scales an R stream, never creates edge; not yet exercised on real trade streams)
```

## Evidence chain (all Train-side selection, sealed fold tests read once)

1. **Structure-free raw scan** (5 markets pooled: 0 of 31,906 cells reach q<0.05; 48,324 total trials incl. the GER40 ar1 duplicate series): NO cell survives BH-FDR; best net t 3.0-4.5 vs sign-flip null
   p95 3.9-4.4. Descriptive only: overnight-long positive in 5/6 datasets (indices), post-hoc (BH q 0.04-0.08), needs a pre-registered test on the forward
   holdout. Sweep bars are NOT more frequent at spread spikes (no feed-artefact evidence).
2. **Seven new families** (ORB, GAP, OVERNIGHT, VOLREV, ROUND, LEADLAG, EOD; GER40 smoke, Train, 150 specs/family): none survives BH or its best-of-N null;
   ORB best E +0.235 R (t 1.98, q~1).
3. **Formula alpha**: GP not better than random formulas (above).
4. **Temporal state-machine probe** (5 x 1,500 unique structures, purged fold 0 Train, COMBINED_ADVERSE cost): median passer expectancy negative in all
   markets (-0.07..-0.24 R after the local-clock fix); vs identical searches on 63 structure-free null runs (sign-flip, day-block shuffle, zero-drift)
   the real best-of-1500 is NOT distinguishable from noise in any market (empirical p 0.16-0.80; deflated Sharpe 0.11 .. 0.0001).
5. **Survival stage** (frozen hash 34aba8bf..., 150 Pareto finalists, fold tests 1..3, once): 8/150 pooled test E>0 (~10 expected by chance), 0 with t>2,
   77 with t<-2, mean test E -0.315 R; 11 with >=2/3 folds positive, 0 pass all 7 checks.
6. **ML meta-label** and **confluence**: inconclusive (above).
7. **Independent Gemini review** (adversarial): adopted new families, block-bootstrap nulls, sibling clustering, cross-market and execution-shock checks,
   raw time profiling; rejected hard t>3/DSR>1.5 pre-gates (contradicts "do not over-filter positive-expectancy volatile strategies"), the claim that 500 EUR
   sizing "rounds" (it skips), and the "2-3 ATR next-open" overstatement.

## Engineering delivered (all merged, 3,087 tests green at 5e45e5b)

- Phase 0: evaluator speed-up + disk safety (bounded cache, free-space guard, run-scoped cleanup).
- Phase 1: cash-session levels without overnight, directional context flags, session-conditioned quantile features (V1 arrays bit-identical).
- Temporal engine: event layer (439 arrays, causal, prefix/perturbation/DST tests), StateMachineStrategySpec with canonicalisation/mirroring, streaming
  oracle, numba all-instances NFA kernel (exact oracle parity; 206-227 specs/s @4 workers), TemporalGenome + DEAP/Optuna + MAP-Elites quality-diversity.
- Simulator guards: target_crossed_at_fill, space_below_min_at_fill (live executor MUST implement the same pre-fill checks).
- Multi-market: MarketSpec (5 markets, symbols resolved on the demo terminal, never guessed), data (terminal caps 100k bars/timeframe: M5 ~16 months,
  M1 ~2.5 months), per-market window/cost/sizing, local-clock minutes, forward-holdout guard, causal cross-market alignment.
- Research tools: raw edge scan, alpha families, formula alpha, probe + null calibration, pre-registered Pareto selection, call-once survival stage,
  purged folds, meta-labelling, confluence analysis, growth simulator.
- Independent Opus lookahead audit: NO leakage; fixed stale evaluator cache (fail-closed key), wrong-zone capture at ZONE_ENTER (0 violations), SHORT feature
  price-mirror, min_space_r at fill, worker frame materialisation.

## Findings that matter beyond the discovery result

- **500 EUR account:** at the 10x research cap the minimum lot needs 12.7x (GER40) and 10.7x (NAS100): every index trade is skipped `size_below_min`; Gold,
  SPX500 and EURUSD are feasible. Discovery ran on a normalised 10,000 EUR research account; 500 EUR feasibility is a separate annotation.
- **Growth arithmetic (synthetic, +0.15 R, 40% win, 1.2 trades/day, 250 days, 500 EUR start):** P(reach 5,000 EUR) ~0-6% at 1-3% risk per trade; ~34-53% only at
  5-10% risk, with 1.5-26% ruin and >=74-100% chance of a >=50% drawdown. Zero edge: decay and rising ruin at every risk level. No guarantee of 500->5,000.
- **Data limits:** 16 months = one macro regime; any positive result would be regime-conditional; the sealed forward holdout (>= 2026-09-01) is essential.
- **Incident:** 4 markets were probed on the wrong clock (Berlin minutes vs local windows) before the family builder spotted it; all four re-run, old runs kept
  under `_invalidated_pre_clock_fix`. GER40 was never affected.

## Not done / limits

- Not built: LLM hypothesis loop (spec section 48), RD-Agent/Qlib run, dynamic sizing, exit research (deliberately: no raw edge yet), Nautilus Stage F.
- 4 of 5 market calendars are PROVISIONAL (not broker-confirmed); commission/swap unmodelled (assumed 0 / placeholder); costs are calibrated fractions of spread.
- Meta-label and confluence were run on GER40 only. ML libraries (scikit-learn, LightGBM, SciPy) are NOT installed; installing needs owner approval.
- FormulaAlpha and the family smoke ran on the GER40 series only.
- Growth loader untested on real trade streams.

## Recommended next steps

1. Do NOT spend more search budget in this feature/grammar space; every route (static rules V1, state machines, families, formulas, raw time scan, ML filter,
   confluence) returns noise-level results after cost.
2. Pre-register the one descriptive lead (equity-index overnight drift, and the XAUUSD hints) as fixed hypotheses and test them on the sealed forward
   holdout once enough new data exists (September 2026 onward). Overnight holding needs swap/financing modelling and a RiskPolicy decision.
3. Widen the information space instead of the search budget: true order-flow/tick data (exchange feeds), longer history (tick reconstruction beyond the 100k-bar cap),
   an event calendar (NFP/CPI/ECB) with clean timestamps.
4. Decide whether to install scikit-learn/LightGBM/SciPy (owner approval) and whether to attempt RD-Agent in WSL/Docker.
5. Confirm broker session times and commissions for the four provisional markets.
