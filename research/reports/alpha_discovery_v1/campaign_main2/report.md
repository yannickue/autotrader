# AD1 survivors report (Train + Validation only)

OOS TOUCHED: NO

**ROBUST POSITIVE TRAIN+VALIDATION EDGE FOUND: INCONCLUSIVE**

## Finalists at a glance

| # | hash | origin | lineage family | Train n | Train E[R] | Val n | Val E[R] | Val t | Val Bonf. p | pooled t | null bound | trades/day | timing ok |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `0d7da45ac556` | deap | TREND_PULLBACK | 113 | 0.176 | 52 | 0.195 | 1.13 | 1.000 | 1.84 | 4.45 | 0.54 | yes |

Verdict rule: YES only if at least one overlap-cluster representative passes ALL stages (C, D, E) AND its pooled day-clustered t exceeds the selection null bound sqrt(2 ln N_total_trials); INCONCLUSIVE if some representative passes all stages but none exceeds the null bound; otherwise NO.

## Search accounting (from pool meta.ledger)

| total trials | unique specs | param | structural | duplicate rejects | invalid rejects | cache hits |
|---|---|---|---|---|---|---|
| 10113 | 9946 | 3200 | 6913 | 167 | 0 | 167 |

Selection null bound: E[max t] ~ sqrt(2 ln N) = 4.45 for N=20199 (rough extreme-value bound, NOT a proof); Bonferroni N_eff = 19871 unique canonical specs.

Cumulative N: this campaign 10113 trials / 9946 unique specs + prior campaigns 10086 trials / 9925 unique specs (`--prior-trials`, `--prior-unique-specs`) = 20199 / 19871.

Stage-E neighbour evaluations added 414 param trials (pipeline ledger delta, not part of the search N).

## Survivors per stage

| stage | count |
|---|---|
| pool (distinct) | 800 |
| Stage A pass (Train screen) | 800 |
| Train-positive (COMBINED_ADVERSE) | 800 |
| Validation-positive (COMBINED_ADVERSE) | 314 |
| Train- AND Validation-positive | 314 |
| Stage C (validation gate) | 15 |
| Stage D (cost stress, pooled) | 15 |
| Stage E (stability) | 1 |
| overlap clusters (Jaccard >= 0.6) | 1 |

Cluster sizes (best-fitness representative first): 1

## Finalist 1 (cluster representative) `0d7da45ac556`

origin deap / lineage TREND_PULLBACK>MUT2>X3 / cluster size 1 / furthest stage E / train fitness -0.058

- direction: LONG
- H1 logic:
  - regime_direction in {UP}  [classified H1 direction (UP/DOWN/NEUTRAL) as bias filter]
  - regime_vol_state in {EXPANSION}  [compression vs expansion volatility state]
- M15 logic:
  - (no M15 filter)
- M5 trigger:
  - bar_body_ratio > Train-quantile 0.60 (= 0.5665)  [body share of the bar: conviction]
  - bar_close_loc > Train-quantile 0.60 (= 0.632)  [where the bar closed within its range]
  - brk_up_20 > Train-quantile 0.95 (= 0.1617)  [ATR distance of close above the prior 20-bar high (>0 = fresh breakout)]
- stop: stop beyond the last confirmed swing pivot
- target: fixed target 2.75 R, next-bar-open fill, forced flat 21:30 Berlin
- window: all entry hours

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 112 | 0.247 | 1.571 | 0.108 | 6.8 | 7 |
| Train COMBINED_ADVERSE | 113 | 0.176 | 1.379 | 0.114 | 7.2 | 7 |
| Validation BASE | 52 | 0.232 | 1.545 | 0.241 | - | - |
| Validation COMBINED_ADVERSE | 52 | 0.195 | - | - | - | - |
| Pooled BASE | 164 | 0.242 | 1.563 | 0.075 | 6.8 | 7 |
| Pooled SPREAD_STRESS | 166 | 0.205 | 1.460 | 0.076 | 6.9 | 7 |
| Pooled SLIPPAGE_STRESS | 166 | 0.193 | 1.426 | 0.077 | 7.2 | 7 |
| Pooled COMBINED_ADVERSE | 165 | 0.182 | 1.397 | 0.078 | 7.2 | 7 |

Cost stress: lower bound (adverse) 0.083 R; cost burden 0.072 R/trade; R lost BASE->COMBINED 0.060

Neighbours: n=22 (skipped 16), positive 1.00, worst 0.055 R, median 0.176 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | UP | 165 | 30.03 | 0.182 |
| regime_vol_state | EXPANSION | 165 | 30.03 | 0.182 |
| phase | EUROPEAN_OPEN | 17 | 6.97 | 0.410 |
| phase | LATE | 31 | 0.12 | 0.004 |
| phase | MIDDAY | 52 | 12.68 | 0.244 |
| phase | MORNING | 37 | 2.27 | 0.061 |
| phase | US_CASH_OPEN_OVERLAP | 28 | 7.99 | 0.285 |
| hour | H09 | 17 | 6.97 | 0.410 |
| hour | H10 | 19 | -3.26 | -0.172 |
| hour | H11 | 18 | 5.54 | 0.308 |
| hour | H12 | 14 | 13.55 | 0.968 |
| hour | H13 | 16 | 4.97 | 0.311 |
| hour | H14 | 18 | -3.68 | -0.204 |
| hour | H15 | 13 | -0.37 | -0.029 |
| hour | H16 | 15 | 3.97 | 0.264 |
| hour | H17 | 13 | 2.55 | 0.196 |
| hour | H18 | 11 | -0.53 | -0.048 |
| hour | H19 | 11 | 0.33 | 0.030 |
| month | 2025-02 | 18 | 6.18 | 0.343 |
| month | 2025-03 | 9 | 6.52 | 0.724 |
| month | 2025-04 | 11 | -5.99 | -0.544 |
| month | 2025-05 | 12 | 4.36 | 0.364 |
| month | 2025-06 | 11 | -0.74 | -0.068 |
| month | 2025-07 | 11 | 7.09 | 0.645 |
| month | 2025-08 | 8 | -1.39 | -0.174 |
| month | 2025-09 | 14 | -3.28 | -0.234 |
| month | 2025-10 | 6 | 4.97 | 0.828 |
| month | 2025-11 | 13 | 2.18 | 0.168 |
| month | 2025-12 | 10 | 3.16 | 0.316 |
| month | 2026-01 | 10 | 3.49 | 0.349 |
| month | 2026-02 | 13 | 0.79 | 0.061 |
| month | 2026-03 | 10 | -0.20 | -0.020 |
| month | 2026-04 | 9 | 2.89 | 0.321 |

Timing robustness (pooled COMBINED_ADVERSE, decisions delayed by k M5 bars; required > 0: k=1, k=2 and jitter):

| variant | trades | E[R] | t |
|---|---|---|---|
| undelayed | - | 0.182 | - |
| k=1 | 165 | 0.123 | 1.32 |
| k=2 | 164 | 0.101 | 1.10 |
| k=3 | 163 | 0.099 | 1.05 |
| jitter U{0..3} | 164 | 0.125 | 1.36 |
timing_ok = True

Concentration: top-3 share 0.078, maxDD 7.2 R, loss streak 7, trades/day 0.536, zero-trade-day fraction 0.571

Selection-aware statistics: pooled day-clustered t 1.84 vs null bound 4.45 -> does NOT exceed null; Validation t 1.13, Bonferroni p 1.000; per-trade Sharpe 0.139, skew 0.73, kurtosis 2.23, deflated-Sharpe probability 0.009

Weaknesses:
- high complexity (6)
- pooled t 1.835446675549743 does not exceed null bound 4.45 (N=20199)
- Validation Bonferroni p = 1.000 > 0.05 (N_eff=19871)
- deflated-Sharpe probability 0.009 < 0.95

---
OOS TOUCHED: NO
**ROBUST POSITIVE TRAIN+VALIDATION EDGE FOUND: INCONCLUSIVE**
