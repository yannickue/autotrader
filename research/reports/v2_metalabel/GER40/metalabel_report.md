# V2 meta-label + confluence: GER40

Scope: TRAIN side of search fold 0 only; purged rolling-origin CV inside it; no fold-test / validation / holdout data.

- trades 47754 from 500 specs over 160 Train days, 89 features, 31143 OOF rows (base hit-rate R>0 0.34556, mean R -0.11895)
- META-LABEL VERDICT: **INCONCLUSIVE**  (per model: {'logit_y1': 'INCONCLUSIVE', 'ridge_y2': 'NOT USEFUL', 'gbdt_y1': 'INCONCLUSIVE'})
- CONFLUENCE VERDICT: **INCONCLUSIVE** (diff=-0.0171, t=-0.69, p_upper=0.637, p_lower=0.368)

## Models (out-of-fold, purged rolling-origin)

| model | AUC [95% day-cluster CI] | top-decile mean R | all mean R | diff t | calib slope | Brier skill | p_perm(AUC/R) | p_shift(AUC/R) | verdict |
|---|---|---|---|---|---|---|---|---|---|
| logit_y1 | 0.54548 [0.51198, 0.58156] | -0.13839 | -0.12953 | -0.08091 | 0.13122 | -0.01234 | 0.00498/1.0 | 0.00498/0.57711 | INCONCLUSIVE |
| ridge_y2 | 0.52107 [0.48663, 0.55306] | -0.20765 | -0.12953 | -0.86999 | None | None | 0.00498/1.0 | 0.0199/0.69154 | NOT USEFUL |
| gbdt_y1 | 0.58281 [0.55451, 0.60931] | -0.03441 | -0.12953 | 0.98003 | 0.59292 | 0.00588 | 0.00498/0.1393 | 0.00498/0.20398 | INCONCLUSIVE |
| logit_y3 | 0.53057 [0.49589, 0.56205] | -0.19942 | -0.12953 | -0.95335 | 0.76369 | 0.01575 | None/None | None/None | - |

### gbdt_y1 decile lift (OOF; decile 10 = highest score)

| decile | n | mean R | hit rate |
|---|---|---|---|
| 1 | 3115 | -0.34387 | 0.18716 |
| 2 | 3115 | -0.23906 | 0.24944 |
| 3 | 3115 | -0.08327 | 0.32937 |
| 4 | 3114 | -0.03699 | 0.37733 |
| 5 | 3114 | -0.16185 | 0.33622 |
| 6 | 3114 | -0.08913 | 0.3738 |
| 7 | 3114 | -0.1181 | 0.36962 |
| 8 | 3114 | -0.14586 | 0.36063 |
| 9 | 3114 | -0.04263 | 0.42293 |
| 10 | 3114 | -0.03441 | 0.44412 |

### gbdt_y1 top OOF permutation importances (AUC drop)

- risk_atr: 0.03904
- plan_rr: 0.02819
- lvl_m15_swing_low: 0.00512
- d1_sma_dist_atr: 0.0041
- lvl_m15_swing_high: 0.00378
- ahead_dist_pdc_cash_atr: 0.0026
- dir_m15_ema_slope: 0.00214
- ahead_dist_pdl_cash_atr: 0.00176
- spread_rel_med: 0.00175
- tod: 0.00165

### logit_y1 decile lift (OOF; decile 10 = highest score)

| decile | n | mean R | hit rate |
|---|---|---|---|
| 1 | 3115 | -0.19004 | 0.2504 |
| 2 | 3115 | -0.10833 | 0.32648 |
| 3 | 3115 | -0.08833 | 0.34735 |
| 4 | 3114 | -0.21708 | 0.307 |
| 5 | 3114 | -0.143 | 0.33879 |
| 6 | 3114 | -0.13454 | 0.35389 |
| 7 | 3114 | -0.12158 | 0.37155 |
| 8 | 3114 | -0.12916 | 0.36962 |
| 9 | 3114 | -0.02481 | 0.40944 |
| 10 | 3114 | -0.13839 | 0.37604 |

### logit_y1 top OOF permutation importances (AUC drop)

- plan_rr: 0.02352
- dir_dist_vwap_atr: 0.0231
- risk_atr: 0.02093
- ahead_dist_pdc_atr: 0.02001
- ahead_dist_sess_high_cash_atr: 0.01574
- ahead_dist_sess_low_cash_atr: 0.01314
- ahead_dist_pdh_cash_atr: 0.01142
- dir_rsi: 0.00976
- ahead_dist_pdl_cash_atr: 0.00912
- lvl_pdl: 0.00788

## Confluence

clusters: {'n_specs': 500, 'n_clusters': 295, 'jaccard_gt_thr_pairs': 544, 'corr_only_links': 23, 'pairs': 124750, 'largest_cluster': 19}

| k (independent clusters) | groups | mean R | hit rate | day-clustered t |
|---|---|---|---|---|
| 1 | 6259 | -0.1586 | 0.35373 | -9.09766 |
| 2 | 2851 | -0.17082 | 0.35075 | -5.68505 |
| 3+ | 5030 | -0.17841 | 0.34791 | -6.19095 |

k>=2 vs k=1: {'n_a': 7881, 'n_b': 6259, 'mean_a': -0.17567, 'mean_b': -0.1586, 'diff': -0.01707, 'se': 0.02467, 't': -0.69201}
day-shift null: {'n_draws': 200, 'diff_mean': -0.0119, 'diff_sd': 0.01563, 'diff_q95': 0.01326, 'obs_diff': -0.01707, 'p_upper': 0.63682, 'p_lower': 0.36816}
by families: {'n_a': 4800, 'n_b': 9340, 'mean_a': -0.17394, 'mean_b': -0.16511, 'diff': -0.00883, 'se': 0.02824, 't': -0.31274}

## Ledger / caveats

{'v1_cumulative': {'trials': 30310, 'unique_specs': 29798, 'label': 'v1 cumulative'}, 'v2_probe_this_market_evaluations': 1500, 'metalabel_structure_evaluations': 7138, 'metalabel_model_configurations': 4, 'null_pipeline_refits_not_counted_as_trials': True, 'cumulative_trials_incl_this_run': 38952, 'selection_caveat': "pool = top-100 by Train fitness (profit-selected) + extra min-trade passers (not profit-selected); specs, ~89 features, 4 model configurations and the verdict thresholds were all chosen on this Train side; the nulls do not re-run spec selection, so p-values are optimistic by the spec-selection effect (see 'unselected_specs_only' for the profit-unselected subset)"}
