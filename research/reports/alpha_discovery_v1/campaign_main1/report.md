# AD1 survivors report (Train + Validation only)

OOS TOUCHED: NO

**ROBUST POSITIVE TRAIN+VALIDATION EDGE FOUND: INCONCLUSIVE**

Verdict rule: YES only if at least one overlap-cluster representative passes ALL stages (C, D, E) AND its pooled day-clustered t exceeds the selection null bound sqrt(2 ln N_total_trials); INCONCLUSIVE if some representative passes all stages but none exceeds the null bound; otherwise NO.

## Search accounting (from pool meta.ledger)

| total trials | unique specs | param | structural | duplicate rejects | invalid rejects | cache hits |
|---|---|---|---|---|---|---|
| 10086 | None | 3200 | 6886 | 161 | 0 | 161 |

Selection null bound: E[max t] ~ sqrt(2 ln N) = 4.29 for N=10086 (rough extreme-value bound, NOT a proof); Bonferroni N_eff = None unique canonical specs.

Stage-E neighbour evaluations added 7275 param trials (pipeline ledger delta, not part of the search N).

## Survivors per stage

| stage | count |
|---|---|
| pool (distinct) | 800 |
| Stage A pass (Train screen) | 800 |
| Train-positive (COMBINED_ADVERSE) | 800 |
| Validation-positive (COMBINED_ADVERSE) | 294 |
| Train- AND Validation-positive | 294 |
| Stage C (validation gate) | 234 |
| Stage D (cost stress, pooled) | 234 |
| Stage E (stability) | 28 |
| overlap clusters (Jaccard >= 0.6) | 21 |

Cluster sizes (best-fitness representative first): 6, 1, 1, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1

## Finalist 1 (cluster representative) `93ce2d52249d`

origin deap / lineage MUT:X:X:X:X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|BREAKOUT|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:BREAKOUT|X:BREAKOUT|COMPRESSION_EXPANSION|X:X:BREAKOUT|VOL_TRANSITION|BREAKOUT / cluster size 6 / furthest stage E / train fitness 0.603

- direction: LONG
- H1 logic:
  - h1_volatility_percentile < Train-quantile 0.20 (= 0.17)  [H1 volatility state gates breakout vs fade edges]
  - regime_trend_strength in {WEAK}  [classified trend strength: trending vs range-like]
- M15 logic:
  - (no M15 filter)
- M5 trigger:
  - brk_up_20 > Train-quantile 0.65 (= -1.321)  [ATR distance of close above the prior 20-bar high (>0 = fresh breakout)]
  - mom_6_atr > Train-quantile 0.55 (= 0.2553)  [6-bar momentum in ATR units]
  - OR( dist_pdl_atr < Train-quantile 0.60 (= 14.65)  [signed ATR distance to previous-day high/low (low side)]  |  sweep_lo_20 is set  [liquidity sweep below the 20-bar extreme then reclaim] )
- stop: stop 2.5 x M5 ATR from the fill
- target: fixed target 3 R, next-bar-open fill, forced flat 21:30 Berlin
- window: all entry hours

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 34 | 1.042 | 4.294 | 0.195 | 2.8 | 2 |
| Train COMBINED_ADVERSE | 34 | 0.993 | 4.082 | 0.201 | 2.9 | 2 |
| Validation BASE | 27 | 0.080 | 1.130 | 0.480 | - | - |
| Validation COMBINED_ADVERSE | 27 | 0.066 | - | - | - | - |
| Pooled BASE | 61 | 0.616 | 2.375 | 0.139 | 6.1 | 6 |
| Pooled SPREAD_STRESS | 61 | 0.597 | 2.333 | 0.141 | 6.1 | 6 |
| Pooled SLIPPAGE_STRESS | 61 | 0.593 | 2.300 | 0.141 | 6.2 | 6 |
| Pooled COMBINED_ADVERSE | 60 | 0.591 | 2.297 | 0.143 | 6.2 | 6 |

Cost stress: lower bound (adverse) 0.374 R; cost burden 0.084 R/trade; R lost BASE->COMBINED 0.025

Neighbours: n=35 (skipped 7), positive 1.00, worst 0.329 R, median 0.525 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | DOWN | 22 | 18.17 | 0.826 |
| regime_direction | NEUTRAL | 17 | 11.45 | 0.674 |
| regime_direction | UP | 21 | 5.85 | 0.278 |
| regime_vol_state | COMPRESSION | 8 | 9.53 | 1.191 |
| regime_vol_state | EXPANSION | 52 | 25.95 | 0.499 |
| phase | EUROPEAN_OPEN | 13 | 8.97 | 0.690 |
| phase | LATE | 6 | 5.40 | 0.899 |
| phase | MIDDAY | 18 | 5.05 | 0.280 |
| phase | MORNING | 8 | 11.85 | 1.482 |
| phase | US_CASH_OPEN_OVERLAP | 15 | 4.21 | 0.281 |
| hour | H08 | 5 | 6.42 | 1.284 |
| hour | H09 | 13 | 8.97 | 0.690 |
| hour | H10 | 5 | 6.89 | 1.377 |
| hour | H11 | 3 | 4.97 | 1.656 |
| hour | H12 | 6 | 0.84 | 0.140 |
| hour | H13 | 5 | 0.55 | 0.111 |
| hour | H14 | 6 | 3.09 | 0.514 |
| hour | H15 | 5 | 0.07 | 0.014 |
| hour | H16 | 7 | 3.48 | 0.498 |
| hour | H17 | 5 | 0.20 | 0.040 |
| month | 2025-02 | 2 | 6.00 | 3.000 |
| month | 2025-03 | 2 | 0.52 | 0.258 |
| month | 2025-04 | 10 | 11.89 | 1.189 |
| month | 2025-05 | 1 | 2.62 | 2.616 |
| month | 2025-06 | 4 | 1.09 | 0.273 |
| month | 2025-07 | 1 | -1.03 | -1.025 |
| month | 2025-08 | 3 | 4.38 | 1.459 |
| month | 2025-09 | 5 | 3.75 | 0.750 |
| month | 2025-10 | 2 | -1.73 | -0.867 |
| month | 2025-11 | 4 | 6.27 | 1.568 |
| month | 2025-12 | 3 | 0.24 | 0.081 |
| month | 2026-01 | 6 | -3.78 | -0.630 |
| month | 2026-02 | 5 | 7.04 | 1.408 |
| month | 2026-03 | 6 | 1.93 | 0.322 |
| month | 2026-04 | 6 | -3.71 | -0.618 |

Concentration: top-3 share 0.143, maxDD 6.2 R, loss streak 6, trades/day 0.195, zero-trade-day fraction 0.844

Selection-aware statistics: pooled day-clustered t 2.72 vs null bound 4.29 -> does NOT exceed null; Validation t 0.20, Bonferroni p 0.421; per-trade Sharpe 0.353, skew 0.37, kurtosis 1.48, deflated-Sharpe probability 0.110

Weaknesses:
- Validation adverse expectancy 0.066 R is < 25% of Train 0.993 R (decay / overfit signature)
- thin Validation sample (27 trades)
- small pooled sample (60 trades)
- high complexity (7)
- pooled t 2.716940405807529 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.421 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.110 < 0.95

## Finalist 2 (cluster representative) `d3a8b8ac2cc4`

origin deap / lineage MUT:X:X:X:X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:BREAKOUT|X:BREAKOUT|COMPRESSION_EXPANSION|X:X:BREAKOUT|VOL_TRANSITION|BREAKOUT|X:VOL_TRANSITION|PA_CONTINUATION|X:X:X:X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|BREAKOUT|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:BREAKOUT|X:BREAKOUT|COMPRESSION_EXPANSION|X:X:BREAKOUT|VOL_TRANSITION|BREAKOUT|X:X:X:X:VOL_TRANSITION|X:PA_REVERSAL|TREND_CONTINUATION|X:PA_REVERSAL|OPENING_DRIVE|X:PA_REVERSAL|X:PA_CONTINUATION|PA_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:VOL_TRANSITION|X:PA_REVERSAL|TREND_CONTINUATION|X:PA_REVERSAL|OPENING_DRIVE / cluster size 1 / furthest stage E / train fitness 0.546

- direction: LONG
- H1 logic:
  - h1_volatility_percentile < Train-quantile 0.35 (= 0.35)  [H1 volatility state gates breakout vs fade edges]
  - regime_trend_strength in {WEAK}  [classified trend strength: trending vs range-like]
- M15 logic:
  - (no M15 filter)
- M5 trigger:
  - brk_up_20 > Train-quantile 0.65 (= -1.321)  [ATR distance of close above the prior 20-bar high (>0 = fresh breakout)]
- stop: stop 2.5 x M5 ATR from the fill
- target: fixed target 3.5 R, next-bar-open fill, forced flat 21:30 Berlin
- window: entries 08:00-11:00 Berlin

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 38 | 0.998 | 3.480 | 0.197 | 2.8 | 3 |
| Train COMBINED_ADVERSE | 38 | 0.896 | 3.180 | 0.211 | 2.9 | 3 |
| Validation BASE | 15 | 0.846 | 2.799 | 0.532 | - | - |
| Validation COMBINED_ADVERSE | 15 | 0.790 | - | - | - | - |
| Pooled BASE | 53 | 0.955 | 3.265 | 0.144 | 3.0 | 3 |
| Pooled SPREAD_STRESS | 53 | 0.897 | 3.125 | 0.150 | 3.0 | 3 |
| Pooled SLIPPAGE_STRESS | 53 | 0.931 | 3.169 | 0.146 | 3.1 | 3 |
| Pooled COMBINED_ADVERSE | 53 | 0.866 | 3.016 | 0.153 | 3.1 | 3 |

Cost stress: lower bound (adverse) 0.618 R; cost burden 0.083 R/trade; R lost BASE->COMBINED 0.089

Neighbours: n=37 (skipped 5), positive 1.00, worst 0.474 R, median 0.713 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | DOWN | 15 | 23.12 | 1.541 |
| regime_direction | NEUTRAL | 21 | 6.33 | 0.301 |
| regime_direction | UP | 17 | 16.47 | 0.969 |
| regime_vol_state | COMPRESSION | 12 | 16.73 | 1.394 |
| regime_vol_state | EXPANSION | 41 | 29.18 | 0.712 |
| phase | EUROPEAN_OPEN | 24 | 21.66 | 0.903 |
| phase | LATE | 13 | 11.78 | 0.906 |
| phase | MORNING | 16 | 12.46 | 0.779 |
| hour | H08 | 13 | 11.78 | 0.906 |
| hour | H09 | 24 | 21.66 | 0.903 |
| hour | H10 | 16 | 12.46 | 0.779 |
| month | 2025-02 | 2 | 2.48 | 1.241 |
| month | 2025-03 | 4 | 1.16 | 0.289 |
| month | 2025-04 | 9 | 6.89 | 0.766 |
| month | 2025-05 | 3 | 7.80 | 2.599 |
| month | 2025-06 | 2 | 2.46 | 1.232 |
| month | 2025-07 | 3 | 1.44 | 0.479 |
| month | 2025-08 | 5 | 1.59 | 0.318 |
| month | 2025-09 | 4 | 5.86 | 1.464 |
| month | 2025-10 | 4 | 2.06 | 0.515 |
| month | 2025-11 | 2 | 2.32 | 1.159 |
| month | 2025-12 | 2 | 0.41 | 0.205 |
| month | 2026-01 | 1 | 1.37 | 1.369 |
| month | 2026-02 | 5 | 9.42 | 1.884 |
| month | 2026-03 | 3 | 1.46 | 0.488 |
| month | 2026-04 | 4 | -0.80 | -0.201 |

Concentration: top-3 share 0.153, maxDD 3.1 R, loss streak 3, trades/day 0.172, zero-trade-day fraction 0.838

Selection-aware statistics: pooled day-clustered t 3.48 vs null bound 4.29 -> does NOT exceed null; Validation t 1.62, Bonferroni p 0.053; per-trade Sharpe 0.467, skew 0.27, kurtosis 1.46, deflated-Sharpe probability 0.300

Weaknesses:
- thin Validation sample (15 trades)
- small pooled sample (53 trades)
- pooled t 3.484469988034182 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.053 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.300 < 0.95

## Finalist 3 (cluster representative) `1ae133d9eabd`

origin deap / lineage X:X:X:X:X:X:X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|BREAKOUT|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:BREAKOUT|X:BREAKOUT|COMPRESSION_EXPANSION|X:X:BREAKOUT|VOL_TRANSITION|BREAKOUT|X:X:X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:PA_CONTINUATION|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:VOL_TRANSITION|TREND_CONTINUATION|X:VOL_TRANSITION|PA_CONTINUATION|X:PA_REVERSAL|OPENING_DRIVE|X:X:VOL_TRANSITION|X:PA_REVERSAL|TREND_CONTINUATION|X:PA_REVERSAL|OPENING_DRIVE|X:X:X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:BREAKOUT|X:VOL_TRANSITION|X:X:TREND_CONTINUATION|MEAN_REVERSION|X:BREAKOUT|COMPRESSION_EXPANSION|X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|BREAKOUT|TREND_CONTINUATION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|BREAKOUT|TREND_CONTINUATION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|TREND_CONTINUATION|X:X:X:X:PA_REVERSAL|X:PA_CONTINUATION|PA_CONTINUATION|X:X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|X:PA_CONTINUATION|VOL_TRANSITION|X:COMPRESSION_EXPANSION|TREND_CONTINUATION|X:TREND_CONTINUATION|X:X:X:PA_REVERSAL|PA_CONTINUATION|X:COMPRESSION_EXPANSION|X:PA_CONTINUATION|VOL_TRANSITION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|X:PA_CONTINUATION|VOL_TRANSITION|X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:PA_CONTINUATION|VOL_TRANSITION|COMPRESSION_EXPANSION|X:X:X:VOL_TRANSITION|X:PA_REVERSAL|TREND_CONTINUATION|X:PA_REVERSAL|OPENING_DRIVE|X:X:X:VOL_TRANSITION|X:PA_REVERSAL|TREND_CONTINUATION|X:PA_REVERSAL|OPENING_DRIVE|X:PA_REVERSAL|X:PA_CONTINUATION|PA_CONTINUATION / cluster size 1 / furthest stage E / train fitness 0.370

- direction: LONG
- H1 logic:
  - h1_volatility_percentile < Train-quantile 0.35 (= 0.35)  [H1 volatility state gates breakout vs fade edges]
  - regime_trend_strength in {WEAK}  [classified trend strength: trending vs range-like]
- M15 logic:
  - m15_ema_slope > Train-quantile 0.20 (= -0.315)  [M15 EMA slope: setup-timeframe momentum]
- M5 trigger:
  - brk_up_20 > Train-quantile 0.65 (= -1.321)  [ATR distance of close above the prior 20-bar high (>0 = fresh breakout)]
  - m5_adx14 > Train-quantile 0.45 (= 21.37)  [M5 trend strength at entry]
  - mom_6_atr > Train-quantile 0.55 (= 0.2553)  [6-bar momentum in ATR units]
- stop: stop 3.0 x M5 ATR from the fill
- target: fixed target 3 R, next-bar-open fill, forced flat 21:30 Berlin
- window: all entry hours

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 55 | 0.791 | 3.074 | 0.140 | 3.8 | 4 |
| Train COMBINED_ADVERSE | 54 | 0.710 | 2.830 | 0.152 | 4.4 | 4 |
| Validation BASE | 21 | 0.567 | 2.476 | 0.451 | - | - |
| Validation COMBINED_ADVERSE | 21 | 0.451 | - | - | - | - |
| Pooled BASE | 76 | 0.729 | 2.908 | 0.107 | 4.0 | 4 |
| Pooled SPREAD_STRESS | 76 | 0.661 | 2.697 | 0.113 | 4.1 | 4 |
| Pooled SLIPPAGE_STRESS | 76 | 0.652 | 2.644 | 0.113 | 4.3 | 4 |
| Pooled COMBINED_ADVERSE | 74 | 0.640 | 2.677 | 0.119 | 4.4 | 4 |

Cost stress: lower bound (adverse) 0.454 R; cost burden 0.079 R/trade; R lost BASE->COMBINED 0.089

Neighbours: n=36 (skipped 8), positive 1.00, worst 0.447 R, median 0.665 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | DOWN | 24 | 26.14 | 1.089 |
| regime_direction | NEUTRAL | 18 | 5.91 | 0.328 |
| regime_direction | UP | 32 | 15.33 | 0.479 |
| regime_vol_state | COMPRESSION | 10 | 11.19 | 1.119 |
| regime_vol_state | EXPANSION | 64 | 36.18 | 0.565 |
| phase | EUROPEAN_OPEN | 15 | 21.04 | 1.403 |
| phase | LATE | 13 | 4.13 | 0.318 |
| phase | MIDDAY | 15 | -7.21 | -0.481 |
| phase | MORNING | 18 | 24.04 | 1.335 |
| phase | US_CASH_OPEN_OVERLAP | 13 | 5.37 | 0.413 |
| hour | H08 | 7 | 4.20 | 0.600 |
| hour | H09 | 15 | 21.04 | 1.403 |
| hour | H10 | 9 | 7.23 | 0.803 |
| hour | H11 | 9 | 16.81 | 1.868 |
| hour | H12 | 4 | -4.11 | -1.028 |
| hour | H13 | 6 | -5.25 | -0.875 |
| hour | H14 | 4 | 2.02 | 0.505 |
| hour | H15 | 2 | 0.18 | 0.091 |
| hour | H16 | 6 | 1.87 | 0.311 |
| hour | H17 | 7 | 2.67 | 0.382 |
| hour | H18 | 3 | 0.90 | 0.300 |
| hour | H19 | 2 | -0.18 | -0.091 |
| month | 2025-02 | 4 | 6.85 | 1.712 |
| month | 2025-03 | 4 | 3.31 | 0.828 |
| month | 2025-04 | 12 | 4.34 | 0.362 |
| month | 2025-05 | 3 | 6.11 | 2.038 |
| month | 2025-06 | 4 | 3.93 | 0.983 |
| month | 2025-07 | 4 | 2.82 | 0.706 |
| month | 2025-08 | 6 | -0.89 | -0.148 |
| month | 2025-09 | 5 | 0.23 | 0.047 |
| month | 2025-10 | 7 | 5.49 | 0.784 |
| month | 2025-11 | 5 | 6.15 | 1.230 |
| month | 2025-12 | 2 | 1.40 | 0.702 |
| month | 2026-01 | 4 | -4.10 | -1.026 |
| month | 2026-02 | 6 | 8.01 | 1.336 |
| month | 2026-03 | 3 | 1.89 | 0.629 |
| month | 2026-04 | 5 | 1.81 | 0.363 |

Concentration: top-3 share 0.119, maxDD 4.4 R, loss streak 4, trades/day 0.240, zero-trade-day fraction 0.782

Selection-aware statistics: pooled day-clustered t 3.43 vs null bound 4.29 -> does NOT exceed null; Validation t 1.33, Bonferroni p 0.092; per-trade Sharpe 0.410, skew 0.31, kurtosis 1.59, deflated-Sharpe probability 0.353

Weaknesses:
- thin Validation sample (21 trades)
- small pooled sample (74 trades)
- high complexity (6)
- pooled t 3.429561290726464 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.092 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.353 < 0.95

## Finalist 4 (cluster representative) `df86c7ec2f95`

origin deap / lineage MUT:X:X:X:X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|BREAKOUT|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:BREAKOUT|X:BREAKOUT|COMPRESSION_EXPANSION|X:X:BREAKOUT|VOL_TRANSITION|BREAKOUT / cluster size 3 / furthest stage E / train fitness 0.345

- direction: LONG
- H1 logic:
  - h1_volatility_percentile < Train-quantile 0.20 (= 0.17)  [H1 volatility state gates breakout vs fade edges]
  - regime_trend_strength in {WEAK}  [classified trend strength: trending vs range-like]
- M15 logic:
  - (no M15 filter)
- M5 trigger:
  - brk_up_20 > Train-quantile 0.65 (= -1.321)  [ATR distance of close above the prior 20-bar high (>0 = fresh breakout)]
  - mom_6_atr > Train-quantile 0.55 (= 0.2553)  [6-bar momentum in ATR units]
- stop: stop beyond the last confirmed swing pivot
- target: fixed target 3 R, next-bar-open fill, forced flat 21:30 Berlin
- window: all entry hours

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 60 | 0.731 | 2.801 | 0.132 | 3.0 | 3 |
| Train COMBINED_ADVERSE | 59 | 0.660 | 2.559 | 0.141 | 3.1 | 3 |
| Validation BASE | 31 | 0.104 | 1.187 | 0.438 | - | - |
| Validation COMBINED_ADVERSE | 31 | 0.066 | - | - | - | - |
| Pooled BASE | 91 | 0.517 | 2.130 | 0.101 | 5.4 | 5 |
| Pooled SPREAD_STRESS | 90 | 0.517 | 2.116 | 0.102 | 5.6 | 5 |
| Pooled SLIPPAGE_STRESS | 90 | 0.495 | 2.043 | 0.103 | 5.8 | 5 |
| Pooled COMBINED_ADVERSE | 90 | 0.456 | 1.960 | 0.107 | 5.9 | 5 |

Cost stress: lower bound (adverse) 0.284 R; cost burden 0.106 R/trade; R lost BASE->COMBINED 0.062

Neighbours: n=22 (skipped 16), positive 1.00, worst 0.324 R, median 0.395 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | DOWN | 25 | 24.00 | 0.960 |
| regime_direction | NEUTRAL | 25 | 8.88 | 0.355 |
| regime_direction | UP | 40 | 8.14 | 0.203 |
| regime_vol_state | COMPRESSION | 10 | 9.01 | 0.901 |
| regime_vol_state | EXPANSION | 80 | 32.00 | 0.400 |
| phase | EUROPEAN_OPEN | 13 | 5.76 | 0.443 |
| phase | LATE | 18 | 8.48 | 0.471 |
| phase | MIDDAY | 28 | 6.81 | 0.243 |
| phase | MORNING | 12 | 9.49 | 0.791 |
| phase | US_CASH_OPEN_OVERLAP | 19 | 10.47 | 0.551 |
| hour | H08 | 9 | 8.75 | 0.972 |
| hour | H09 | 13 | 5.76 | 0.443 |
| hour | H10 | 5 | 5.77 | 1.154 |
| hour | H11 | 7 | 3.72 | 0.531 |
| hour | H12 | 9 | 0.44 | 0.048 |
| hour | H13 | 9 | 3.05 | 0.339 |
| hour | H14 | 8 | 3.90 | 0.487 |
| hour | H15 | 7 | 0.86 | 0.123 |
| hour | H16 | 6 | 5.48 | 0.913 |
| hour | H17 | 9 | 2.52 | 0.280 |
| hour | H18 | 5 | 1.77 | 0.354 |
| hour | H19 | 3 | -1.00 | -0.332 |
| month | 2025-02 | 3 | 8.03 | 2.678 |
| month | 2025-03 | 4 | 3.47 | 0.868 |
| month | 2025-04 | 11 | 11.16 | 1.014 |
| month | 2025-05 | 3 | -0.08 | -0.026 |
| month | 2025-06 | 6 | 3.20 | 0.534 |
| month | 2025-07 | 5 | -1.88 | -0.377 |
| month | 2025-08 | 6 | 0.47 | 0.078 |
| month | 2025-09 | 7 | 2.39 | 0.341 |
| month | 2025-10 | 5 | 0.93 | 0.186 |
| month | 2025-11 | 9 | 11.28 | 1.253 |
| month | 2025-12 | 4 | 0.09 | 0.022 |
| month | 2026-01 | 6 | -0.60 | -0.101 |
| month | 2026-02 | 5 | 5.42 | 1.084 |
| month | 2026-03 | 8 | 0.60 | 0.075 |
| month | 2026-04 | 8 | -3.46 | -0.433 |

Concentration: top-3 share 0.107, maxDD 5.9 R, loss streak 5, trades/day 0.292, zero-trade-day fraction 0.776

Selection-aware statistics: pooled day-clustered t 2.66 vs null bound 4.29 -> does NOT exceed null; Validation t 0.27, Bonferroni p 0.392; per-trade Sharpe 0.288, skew 0.47, kurtosis 1.65, deflated-Sharpe probability 0.111

Weaknesses:
- Validation adverse expectancy 0.066 R is < 25% of Train 0.660 R (decay / overfit signature)
- small pooled sample (90 trades)
- pooled t 2.6598781204690773 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.392 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.111 < 0.95

## Finalist 5 (cluster representative) `93c9afee322b`

origin deap / lineage MUT:X:X:X:X:X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|BREAKOUT|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:TREND_CONTINUATION|PA_CONTINUATION|X:X:BREAKOUT|X:BREAKOUT|COMPRESSION_EXPANSION|X:X:BREAKOUT|VOL_TRANSITION|BREAKOUT|X:X:X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:PA_CONTINUATION|VOL_TRANSITION|X:X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:VOL_TRANSITION|TREND_CONTINUATION|X:VOL_TRANSITION|PA_CONTINUATION|X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|BREAKOUT|X:VOL_TRANSITION|PA_CONTINUATION / cluster size 1 / furthest stage E / train fitness 0.325

- direction: LONG
- H1 logic:
  - h1_volatility_percentile < Train-quantile 0.20 (= 0.17)  [H1 volatility state gates breakout vs fade edges]
  - regime_trend_strength in {WEAK}  [classified trend strength: trending vs range-like]
- M15 logic:
  - (no M15 filter)
- M5 trigger:
  - bar_range_atr > Train-quantile 0.65 (= 1.058)  [entry-bar range/ATR: expansion bars]
  - lower_wick_ratio < Train-quantile 0.50 (= 0.2181)  [lower wick share: rejection of lower prices]
  - OR( dist_pdl_atr < Train-quantile 0.60 (= 14.65)  [signed ATR distance to previous-day high/low (low side)]  |  sweep_lo_20 is set  [liquidity sweep below the 20-bar extreme then reclaim] )
- stop: stop 2.5 x M5 ATR from the fill
- target: fixed target 3 R, next-bar-open fill, forced flat 21:30 Berlin
- window: all entry hours

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 60 | 0.657 | 2.394 | 0.133 | 5.6 | 3 |
| Train COMBINED_ADVERSE | 58 | 0.666 | 2.447 | 0.138 | 5.8 | 3 |
| Validation BASE | 39 | 0.219 | 1.431 | 0.318 | - | - |
| Validation COMBINED_ADVERSE | 39 | 0.204 | - | - | - | - |
| Pooled BASE | 99 | 0.484 | 1.997 | 0.094 | 5.6 | 5 |
| Pooled SPREAD_STRESS | 97 | 0.494 | 2.026 | 0.095 | 5.6 | 7 |
| Pooled SLIPPAGE_STRESS | 99 | 0.463 | 1.936 | 0.095 | 5.7 | 7 |
| Pooled COMBINED_ADVERSE | 96 | 0.483 | 1.997 | 0.097 | 5.8 | 7 |

Cost stress: lower bound (adverse) 0.316 R; cost burden 0.088 R/trade; R lost BASE->COMBINED 0.001

Neighbours: n=36 (skipped 6), positive 1.00, worst 0.255 R, median 0.388 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | DOWN | 28 | 22.30 | 0.797 |
| regime_direction | NEUTRAL | 29 | 10.16 | 0.350 |
| regime_direction | UP | 39 | 13.92 | 0.357 |
| regime_vol_state | COMPRESSION | 13 | 10.96 | 0.843 |
| regime_vol_state | EXPANSION | 83 | 35.43 | 0.427 |
| phase | EUROPEAN_OPEN | 25 | 12.86 | 0.514 |
| phase | LATE | 8 | 9.30 | 1.163 |
| phase | MIDDAY | 27 | 7.30 | 0.270 |
| phase | MORNING | 15 | 7.74 | 0.516 |
| phase | US_CASH_OPEN_OVERLAP | 21 | 9.19 | 0.438 |
| hour | H08 | 5 | 6.95 | 1.390 |
| hour | H09 | 25 | 12.86 | 0.514 |
| hour | H10 | 9 | 1.84 | 0.205 |
| hour | H11 | 6 | 5.90 | 0.983 |
| hour | H12 | 8 | 1.56 | 0.195 |
| hour | H13 | 4 | 3.96 | 0.991 |
| hour | H14 | 11 | 1.85 | 0.168 |
| hour | H15 | 11 | 5.58 | 0.507 |
| hour | H16 | 9 | 3.11 | 0.346 |
| hour | H17 | 6 | 0.80 | 0.134 |
| hour | H19 | 2 | 1.97 | 0.987 |
| month | 2025-02 | 3 | 8.59 | 2.863 |
| month | 2025-03 | 6 | 1.02 | 0.171 |
| month | 2025-04 | 12 | 15.12 | 1.260 |
| month | 2025-05 | 1 | 3.00 | 3.000 |
| month | 2025-06 | 9 | 0.13 | 0.014 |
| month | 2025-07 | 2 | -2.07 | -1.033 |
| month | 2025-08 | 5 | 4.31 | 0.863 |
| month | 2025-09 | 7 | 1.48 | 0.212 |
| month | 2025-10 | 5 | 1.83 | 0.366 |
| month | 2025-11 | 8 | 5.20 | 0.650 |
| month | 2025-12 | 3 | 0.46 | 0.153 |
| month | 2026-01 | 7 | -2.73 | -0.390 |
| month | 2026-02 | 6 | 8.63 | 1.439 |
| month | 2026-03 | 13 | 2.05 | 0.157 |
| month | 2026-04 | 9 | -0.64 | -0.071 |

Concentration: top-3 share 0.097, maxDD 5.8 R, loss streak 7, trades/day 0.312, zero-trade-day fraction 0.776

Selection-aware statistics: pooled day-clustered t 2.90 vs null bound 4.29 -> does NOT exceed null; Validation t 0.76, Bonferroni p 0.224; per-trade Sharpe 0.286, skew 0.54, kurtosis 1.58, deflated-Sharpe probability 0.123

Weaknesses:
- small pooled sample (96 trades)
- high complexity (7)
- pooled t 2.8964984031059298 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.224 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.123 < 0.95

## Finalist 6 (cluster representative) `7b836d34d595`

origin deap / lineage X:X:X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:PA_CONTINUATION|VOL_TRANSITION|X:X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:VOL_TRANSITION|TREND_CONTINUATION|X:VOL_TRANSITION|PA_CONTINUATION|X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|BREAKOUT|X:VOL_TRANSITION|PA_CONTINUATION / cluster size 1 / furthest stage E / train fitness 0.232

- direction: LONG
- H1 logic:
  - regime_direction in {UP}  [classified H1 direction (UP/DOWN/NEUTRAL) as bias filter]
  - regime_vol_state in {EXPANSION}  [compression vs expansion volatility state]
- M15 logic:
  - (no M15 filter)
- M5 trigger:
  - bar_body_ratio > Train-quantile 0.70 (= 0.6571)  [body share of the bar: conviction]
  - bar_close_loc > Train-quantile 0.90 (= 0.9413)  [where the bar closed within its range]
- stop: stop 2.3 x M5 ATR from the fill
- target: fixed target 3.25 R, next-bar-open fill, forced flat 21:30 Berlin
- window: entries 15:00-17:00 Berlin

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 53 | 0.605 | 2.509 | 0.183 | 3.0 | 3 |
| Train COMBINED_ADVERSE | 52 | 0.532 | 2.271 | 0.197 | 3.1 | 3 |
| Validation BASE | 30 | 0.228 | 1.422 | 0.423 | - | - |
| Validation COMBINED_ADVERSE | 30 | 0.150 | - | - | - | - |
| Pooled BASE | 83 | 0.468 | 2.039 | 0.128 | 6.1 | 8 |
| Pooled SPREAD_STRESS | 82 | 0.432 | 1.971 | 0.136 | 6.1 | 8 |
| Pooled SLIPPAGE_STRESS | 82 | 0.438 | 1.966 | 0.133 | 6.2 | 8 |
| Pooled COMBINED_ADVERSE | 81 | 0.395 | 1.859 | 0.141 | 6.2 | 8 |

Cost stress: lower bound (adverse) 0.220 R; cost burden 0.094 R/trade; R lost BASE->COMBINED 0.073

Neighbours: n=35 (skipped 7), positive 1.00, worst 0.103 R, median 0.254 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | UP | 81 | 32.03 | 0.395 |
| regime_vol_state | EXPANSION | 81 | 32.03 | 0.395 |
| phase | MIDDAY | 28 | 10.34 | 0.369 |
| phase | US_CASH_OPEN_OVERLAP | 53 | 21.69 | 0.409 |
| hour | H15 | 49 | 18.10 | 0.369 |
| hour | H16 | 32 | 13.93 | 0.435 |
| month | 2025-02 | 10 | 10.08 | 1.008 |
| month | 2025-03 | 6 | -0.14 | -0.024 |
| month | 2025-04 | 1 | 1.49 | 1.488 |
| month | 2025-05 | 6 | 9.40 | 1.567 |
| month | 2025-06 | 4 | 2.13 | 0.534 |
| month | 2025-07 | 2 | 2.22 | 1.109 |
| month | 2025-08 | 5 | -1.48 | -0.295 |
| month | 2025-09 | 5 | 2.82 | 0.564 |
| month | 2025-10 | 7 | 0.28 | 0.040 |
| month | 2025-11 | 6 | 0.85 | 0.142 |
| month | 2025-12 | 3 | 1.50 | 0.501 |
| month | 2026-01 | 6 | 2.92 | 0.486 |
| month | 2026-02 | 5 | 1.82 | 0.365 |
| month | 2026-03 | 11 | -2.48 | -0.225 |
| month | 2026-04 | 4 | 0.60 | 0.150 |

Concentration: top-3 share 0.141, maxDD 6.2 R, loss streak 8, trades/day 0.263, zero-trade-day fraction 0.760

Selection-aware statistics: pooled day-clustered t 2.26 vs null bound 4.29 -> does NOT exceed null; Validation t 0.53, Bonferroni p 0.297; per-trade Sharpe 0.258, skew 0.61, kurtosis 1.98, deflated-Sharpe probability 0.047

Weaknesses:
- small pooled sample (81 trades)
- pooled t 2.260308150907095 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.297 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.047 < 0.95

## Finalist 7 (cluster representative) `6836c6a485bb`

origin deap / lineage X:X:X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:PA_CONTINUATION|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:BREAKOUT|OPENING_DRIVE|VOL_TRANSITION|X:X:VOL_TRANSITION|TREND_CONTINUATION|X:VOL_TRANSITION|PA_CONTINUATION|X:PA_REVERSAL|OPENING_DRIVE|X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|BREAKOUT|TREND_CONTINUATION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|TREND_CONTINUATION / cluster size 1 / furthest stage E / train fitness 0.210

- direction: LONG
- H1 logic:
  - h1_efficiency_ratio > Train-quantile 0.30 (= 0.1812)  [H1 directional efficiency separates trend from chop]
  - regime_direction in {UP}  [classified H1 direction (UP/DOWN/NEUTRAL) as bias filter]
- M15 logic:
  - (no M15 filter)
- M5 trigger:
  - bar_range_atr > Train-quantile 0.75 (= 1.195)  [entry-bar range/ATR: expansion bars]
  - mom_6_atr > Train-quantile 0.65 (= 0.6594)  [6-bar momentum in ATR units]
- stop: stop beyond the last confirmed swing pivot
- target: fixed target 2.75 R, next-bar-open fill, forced flat 21:30 Berlin
- window: entries 15:30-19:30 Berlin

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 73 | 0.531 | 2.935 | 0.140 | 2.1 | 2 |
| Train COMBINED_ADVERSE | 71 | 0.441 | 2.501 | 0.158 | 2.4 | 2 |
| Validation BASE | 36 | 0.136 | 1.344 | 0.349 | - | - |
| Validation COMBINED_ADVERSE | 36 | 0.083 | - | - | - | - |
| Pooled BASE | 109 | 0.401 | 2.275 | 0.106 | 6.2 | 5 |
| Pooled SPREAD_STRESS | 108 | 0.358 | 2.124 | 0.113 | 6.3 | 5 |
| Pooled SLIPPAGE_STRESS | 108 | 0.343 | 2.056 | 0.114 | 6.4 | 5 |
| Pooled COMBINED_ADVERSE | 107 | 0.320 | 1.967 | 0.118 | 6.5 | 5 |

Cost stress: lower bound (adverse) 0.211 R; cost burden 0.089 R/trade; R lost BASE->COMBINED 0.080

Neighbours: n=35 (skipped 7), positive 1.00, worst 0.177 R, median 0.261 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | UP | 107 | 34.29 | 0.320 |
| regime_vol_state | COMPRESSION | 11 | 8.30 | 0.755 |
| regime_vol_state | EXPANSION | 96 | 25.98 | 0.271 |
| phase | LATE | 27 | 8.54 | 0.316 |
| phase | US_CASH_OPEN_OVERLAP | 80 | 25.75 | 0.322 |
| hour | H15 | 47 | 16.99 | 0.361 |
| hour | H16 | 26 | 4.71 | 0.181 |
| hour | H17 | 19 | 7.43 | 0.391 |
| hour | H18 | 8 | -0.16 | -0.020 |
| hour | H19 | 7 | 5.32 | 0.760 |
| month | 2025-02 | 10 | 6.99 | 0.699 |
| month | 2025-03 | 7 | 2.13 | 0.305 |
| month | 2025-04 | 9 | 1.70 | 0.189 |
| month | 2025-05 | 6 | 5.34 | 0.890 |
| month | 2025-06 | 3 | 3.62 | 1.206 |
| month | 2025-07 | 8 | 4.58 | 0.572 |
| month | 2025-08 | 5 | -1.07 | -0.213 |
| month | 2025-09 | 8 | 1.86 | 0.232 |
| month | 2025-10 | 5 | 3.09 | 0.619 |
| month | 2025-11 | 10 | 3.07 | 0.307 |
| month | 2025-12 | 5 | 3.42 | 0.685 |
| month | 2026-01 | 5 | -0.16 | -0.032 |
| month | 2026-02 | 11 | 1.02 | 0.093 |
| month | 2026-03 | 7 | -0.36 | -0.052 |
| month | 2026-04 | 8 | -0.94 | -0.118 |

Concentration: top-3 share 0.118, maxDD 6.5 R, loss streak 5, trades/day 0.347, zero-trade-day fraction 0.685

Selection-aware statistics: pooled day-clustered t 2.93 vs null bound 4.29 -> does NOT exceed null; Validation t 0.47, Bonferroni p 0.318; per-trade Sharpe 0.282, skew 0.36, kurtosis 2.21, deflated-Sharpe probability 0.160

Weaknesses:
- Validation adverse expectancy 0.083 R is < 25% of Train 0.441 R (decay / overfit signature)
- pooled t 2.9326041676740706 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.318 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.160 < 0.95

## Finalist 8 (cluster representative) `eca21ba33ffb`

origin deap / lineage X:X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:X:X:OPENING_DRIVE|BREAKOUT|TREND_CONTINUATION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|TREND_CONTINUATION|X:X:X:PA_REVERSAL|X:PA_CONTINUATION|PA_CONTINUATION|X:X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|X:PA_CONTINUATION|VOL_TRANSITION|X:COMPRESSION_EXPANSION|TREND_CONTINUATION|X:TREND_CONTINUATION|X:X:X:PA_REVERSAL|PA_CONTINUATION|X:COMPRESSION_EXPANSION|X:PA_CONTINUATION|VOL_TRANSITION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|X:PA_CONTINUATION|VOL_TRANSITION / cluster size 1 / furthest stage E / train fitness 0.204

- direction: LONG
- H1 logic:
  - regime_direction in {UP}  [classified H1 direction (UP/DOWN/NEUTRAL) as bias filter]
  - regime_vol_state in {EXPANSION}  [compression vs expansion volatility state]
- M15 logic:
  - context_trend_continuation is set  [M15 context flag: established trend still intact]
- M5 trigger:
  - bar_range_atr > Train-quantile 0.75 (= 1.195)  [entry-bar range/ATR: expansion bars]
  - mom_6_atr > Train-quantile 0.65 (= 0.6594)  [6-bar momentum in ATR units]
- stop: stop 2.3 x M5 ATR from the fill
- target: fixed target 3 R, next-bar-open fill, forced flat 21:30 Berlin
- window: entries 15:30-19:30 Berlin

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 54 | 0.549 | 2.450 | 0.180 | 4.2 | 4 |
| Train COMBINED_ADVERSE | 54 | 0.472 | 2.217 | 0.194 | 4.5 | 4 |
| Validation BASE | 26 | 0.259 | 1.594 | 0.460 | - | - |
| Validation COMBINED_ADVERSE | 26 | 0.203 | - | - | - | - |
| Pooled BASE | 80 | 0.455 | 2.145 | 0.132 | 4.2 | 5 |
| Pooled SPREAD_STRESS | 80 | 0.424 | 2.066 | 0.137 | 4.3 | 5 |
| Pooled SLIPPAGE_STRESS | 80 | 0.407 | 2.002 | 0.138 | 4.5 | 5 |
| Pooled COMBINED_ADVERSE | 80 | 0.384 | 1.945 | 0.142 | 4.5 | 5 |

Cost stress: lower bound (adverse) 0.247 R; cost burden 0.099 R/trade; R lost BASE->COMBINED 0.070

Neighbours: n=32 (skipped 10), positive 1.00, worst 0.222 R, median 0.294 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | UP | 80 | 30.75 | 0.384 |
| regime_vol_state | EXPANSION | 80 | 30.75 | 0.384 |
| phase | LATE | 22 | 6.22 | 0.283 |
| phase | US_CASH_OPEN_OVERLAP | 58 | 24.53 | 0.423 |
| hour | H15 | 27 | 13.32 | 0.493 |
| hour | H16 | 21 | 6.96 | 0.332 |
| hour | H17 | 22 | 6.57 | 0.298 |
| hour | H18 | 4 | -1.25 | -0.313 |
| hour | H19 | 6 | 5.15 | 0.859 |
| month | 2025-02 | 7 | 10.03 | 1.432 |
| month | 2025-03 | 2 | -0.07 | -0.034 |
| month | 2025-04 | 9 | -1.55 | -0.172 |
| month | 2025-05 | 5 | 7.12 | 1.424 |
| month | 2025-06 | 4 | 1.88 | 0.471 |
| month | 2025-07 | 8 | 2.06 | 0.258 |
| month | 2025-08 | 3 | -0.17 | -0.057 |
| month | 2025-09 | 4 | -2.05 | -0.512 |
| month | 2025-10 | 4 | 6.33 | 1.582 |
| month | 2025-11 | 8 | 1.90 | 0.238 |
| month | 2025-12 | 6 | 3.58 | 0.596 |
| month | 2026-01 | 5 | -1.45 | -0.289 |
| month | 2026-02 | 5 | 2.38 | 0.476 |
| month | 2026-03 | 4 | 0.88 | 0.219 |
| month | 2026-04 | 6 | -0.12 | -0.020 |

Concentration: top-3 share 0.142, maxDD 4.5 R, loss streak 5, trades/day 0.260, zero-trade-day fraction 0.766

Selection-aware statistics: pooled day-clustered t 2.79 vs null bound 4.29 -> does NOT exceed null; Validation t 0.88, Bonferroni p 0.190; per-trade Sharpe 0.283, skew 0.43, kurtosis 1.96, deflated-Sharpe probability 0.077

Weaknesses:
- thin Validation sample (26 trades)
- small pooled sample (80 trades)
- pooled t 2.793931718865321 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.190 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.077 < 0.95

## Finalist 9 (cluster representative) `1a3baeaa60f7`

origin deap / lineage MUT:X:X:X:PA_REVERSAL|X:PA_CONTINUATION|PA_CONTINUATION|X:X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION|X:PA_CONTINUATION|VOL_TRANSITION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:X:OPENING_DRIVE|X:PA_REVERSAL|VOL_TRANSITION|X:TREND_CONTINUATION|TREND_CONTINUATION / cluster size 1 / furthest stage E / train fitness 0.195

- direction: LONG
- H1 logic:
  - h1_volatility_percentile < Train-quantile 0.35 (= 0.35)  [H1 volatility state gates breakout vs fade edges]
  - regime_trend_strength in {WEAK}  [classified trend strength: trending vs range-like]
- M15 logic:
  - m15_range_position > Train-quantile 0.35 (= 0.3926)  [location inside the M15 range (fade highs, buy lows)]
  - m15_volatility_percentile > Train-quantile 0.10 (= 0.06)  [M15 volatility state for the setup]
- M5 trigger:
  - brk_up_20 > Train-quantile 0.65 (= -1.321)  [ATR distance of close above the prior 20-bar high (>0 = fresh breakout)]
  - mom_6_atr > Train-quantile 0.55 (= 0.2553)  [6-bar momentum in ATR units]
- stop: stop 2.9 x M5 ATR from the fill
- target: fixed target 3 R, next-bar-open fill, forced flat 21:30 Berlin
- window: all entry hours

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 90 | 0.541 | 2.294 | 0.104 | 7.6 | 5 |
| Train COMBINED_ADVERSE | 89 | 0.478 | 2.133 | 0.112 | 7.9 | 5 |
| Validation BASE | 40 | 0.248 | 1.544 | 0.320 | - | - |
| Validation COMBINED_ADVERSE | 40 | 0.203 | - | - | - | - |
| Pooled BASE | 130 | 0.451 | 2.049 | 0.079 | 7.6 | 6 |
| Pooled SPREAD_STRESS | 130 | 0.436 | 2.017 | 0.080 | 7.6 | 6 |
| Pooled SLIPPAGE_STRESS | 130 | 0.424 | 1.966 | 0.080 | 7.8 | 6 |
| Pooled COMBINED_ADVERSE | 129 | 0.392 | 1.902 | 0.084 | 7.9 | 6 |

Cost stress: lower bound (adverse) 0.259 R; cost burden 0.084 R/trade; R lost BASE->COMBINED 0.058

Neighbours: n=37 (skipped 7), positive 1.00, worst 0.281 R, median 0.377 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | DOWN | 44 | 29.70 | 0.675 |
| regime_direction | NEUTRAL | 31 | 2.42 | 0.078 |
| regime_direction | UP | 54 | 18.49 | 0.342 |
| regime_vol_state | COMPRESSION | 13 | 14.55 | 1.120 |
| regime_vol_state | EXPANSION | 116 | 36.06 | 0.311 |
| phase | EUROPEAN_OPEN | 18 | 18.87 | 1.049 |
| phase | LATE | 27 | 3.20 | 0.118 |
| phase | MIDDAY | 29 | -0.88 | -0.030 |
| phase | MORNING | 28 | 25.79 | 0.921 |
| phase | US_CASH_OPEN_OVERLAP | 27 | 3.64 | 0.135 |
| hour | H08 | 8 | 7.57 | 0.946 |
| hour | H09 | 18 | 18.87 | 1.049 |
| hour | H10 | 16 | 11.19 | 0.699 |
| hour | H11 | 12 | 14.61 | 1.217 |
| hour | H12 | 11 | -2.47 | -0.224 |
| hour | H13 | 6 | 3.42 | 0.570 |
| hour | H14 | 8 | -0.57 | -0.071 |
| hour | H15 | 9 | -2.16 | -0.240 |
| hour | H16 | 14 | 2.52 | 0.180 |
| hour | H17 | 15 | -2.78 | -0.185 |
| hour | H18 | 6 | 2.84 | 0.474 |
| hour | H19 | 6 | -2.43 | -0.405 |
| month | 2025-02 | 5 | 8.38 | 1.676 |
| month | 2025-03 | 6 | 1.71 | 0.285 |
| month | 2025-04 | 16 | 10.00 | 0.625 |
| month | 2025-05 | 5 | 4.02 | 0.804 |
| month | 2025-06 | 6 | 5.39 | 0.899 |
| month | 2025-07 | 7 | -0.00 | -0.000 |
| month | 2025-08 | 11 | -3.44 | -0.313 |
| month | 2025-09 | 11 | 3.09 | 0.281 |
| month | 2025-10 | 10 | 6.02 | 0.602 |
| month | 2025-11 | 12 | 7.34 | 0.612 |
| month | 2025-12 | 4 | 1.28 | 0.319 |
| month | 2026-01 | 10 | -0.52 | -0.052 |
| month | 2026-02 | 10 | 8.15 | 0.815 |
| month | 2026-03 | 8 | 1.30 | 0.162 |
| month | 2026-04 | 8 | -2.10 | -0.262 |

Concentration: top-3 share 0.084, maxDD 7.9 R, loss streak 6, trades/day 0.419, zero-trade-day fraction 0.653

Selection-aware statistics: pooled day-clustered t 2.93 vs null bound 4.29 -> does NOT exceed null; Validation t 0.94, Bonferroni p 0.173; per-trade Sharpe 0.268, skew 0.56, kurtosis 1.91, deflated-Sharpe probability 0.185

Weaknesses:
- high complexity (6)
- pooled t 2.93111308325237 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.173 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.185 < 0.95

## Finalist 10 (cluster representative) `d74aa11a7ff8`

origin deap / lineage X:X:X:PA_REVERSAL|X:PA_CONTINUATION|PA_CONTINUATION|X:X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|X:PA_CONTINUATION|VOL_TRANSITION|X:COMPRESSION_EXPANSION|TREND_CONTINUATION|X:TREND_CONTINUATION|X:X:X:PA_REVERSAL|PA_CONTINUATION|X:COMPRESSION_EXPANSION|X:PA_CONTINUATION|VOL_TRANSITION|X:X:TREND_CONTINUATION|TREND_CONTINUATION|X:COMPRESSION_EXPANSION|X:PA_CONTINUATION|VOL_TRANSITION / cluster size 1 / furthest stage E / train fitness 0.176

- direction: LONG
- H1 logic:
  - regime_direction in {UP}  [classified H1 direction (UP/DOWN/NEUTRAL) as bias filter]
  - regime_vol_state in {EXPANSION}  [compression vs expansion volatility state]
- M15 logic:
  - context_trend_continuation is set  [M15 context flag: established trend still intact]
- M5 trigger:
  - mom_6_atr > Train-quantile 0.75 (= 1.123)  [6-bar momentum in ATR units]
- stop: stop 2.8 x M5 ATR from the fill
- target: fixed target 3 R, next-bar-open fill, forced flat 21:30 Berlin
- window: entries 15:30-19:30 Berlin

| partition / cost | trades | E[R] | PF | top3 | maxDD R | loss streak |
|---|---|---|---|---|---|---|
| Train BASE | 80 | 0.445 | 2.546 | 0.154 | 3.0 | 3 |
| Train COMBINED_ADVERSE | 80 | 0.374 | 2.264 | 0.168 | 3.5 | 3 |
| Validation BASE | 36 | 0.053 | 1.138 | 0.426 | - | - |
| Validation COMBINED_ADVERSE | 36 | 0.010 | - | - | - | - |
| Pooled BASE | 116 | 0.323 | 2.020 | 0.121 | 4.5 | 6 |
| Pooled SPREAD_STRESS | 116 | 0.301 | 1.946 | 0.125 | 4.6 | 6 |
| Pooled SLIPPAGE_STRESS | 116 | 0.288 | 1.888 | 0.127 | 4.7 | 6 |
| Pooled COMBINED_ADVERSE | 116 | 0.261 | 1.801 | 0.132 | 4.8 | 6 |

Cost stress: lower bound (adverse) 0.160 R; cost burden 0.082 R/trade; R lost BASE->COMBINED 0.062

Neighbours: n=30 (skipped 10), positive 1.00, worst 0.112 R, median 0.217 R

Regime / session / month (pooled, COMBINED_ADVERSE):

| dimension | bucket | trades | sum R | mean R |
|---|---|---|---|---|
| regime_direction | UP | 116 | 30.26 | 0.261 |
| regime_vol_state | EXPANSION | 116 | 30.26 | 0.261 |
| phase | LATE | 34 | 12.94 | 0.381 |
| phase | US_CASH_OPEN_OVERLAP | 82 | 17.32 | 0.211 |
| hour | H15 | 39 | 14.69 | 0.377 |
| hour | H16 | 32 | 3.44 | 0.108 |
| hour | H17 | 25 | 2.21 | 0.088 |
| hour | H18 | 15 | 7.72 | 0.515 |
| hour | H19 | 5 | 2.21 | 0.441 |
| month | 2025-02 | 10 | 6.78 | 0.678 |
| month | 2025-03 | 7 | 0.35 | 0.050 |
| month | 2025-04 | 8 | 1.91 | 0.238 |
| month | 2025-05 | 11 | 7.57 | 0.688 |
| month | 2025-06 | 7 | 2.68 | 0.383 |
| month | 2025-07 | 11 | 3.89 | 0.354 |
| month | 2025-08 | 4 | -0.21 | -0.053 |
| month | 2025-09 | 6 | -1.21 | -0.202 |
| month | 2025-10 | 5 | 5.35 | 1.070 |
| month | 2025-11 | 11 | 2.82 | 0.256 |
| month | 2025-12 | 6 | 1.00 | 0.166 |
| month | 2026-01 | 7 | -2.54 | -0.362 |
| month | 2026-02 | 7 | 3.23 | 0.462 |
| month | 2026-03 | 9 | -1.28 | -0.142 |
| month | 2026-04 | 7 | -0.07 | -0.010 |

Concentration: top-3 share 0.132, maxDD 4.8 R, loss streak 6, trades/day 0.377, zero-trade-day fraction 0.653

Selection-aware statistics: pooled day-clustered t 2.58 vs null bound 4.29 -> does NOT exceed null; Validation t 0.06, Bonferroni p 0.477; per-trade Sharpe 0.237, skew 0.56, kurtosis 2.65, deflated-Sharpe probability 0.081

Weaknesses:
- Validation adverse expectancy 0.010 R is < 25% of Train 0.374 R (decay / overfit signature)
- pooled t 2.584454076860171 does not exceed null bound 4.29 (N=10086)
- Validation Bonferroni p = 0.477 > 0.05 (N_eff=0)
- deflated-Sharpe probability 0.081 < 0.95

---
OOS TOUCHED: NO
**ROBUST POSITIVE TRAIN+VALIDATION EDGE FOUND: INCONCLUSIVE**
