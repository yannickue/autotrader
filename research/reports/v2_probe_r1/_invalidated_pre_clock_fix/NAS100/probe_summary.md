# V2 probe: NAS100

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 1500 (evaluations 1500, duplicates 0, invalid 0, behavioural twins 30)
- cumulative trials incl. v1 cumulative: 31810 (unique specs 31298)
- passed min 40 Train trades: 162; reject reasons {'zero_candidates': 669, 'too_few_trades': 669, 'pass': 162}
- zero-Train-decision share 0.446
- Train trades/day (passers): {'p05': 0.321429, 'p25': 0.473214, 'p50': 0.732143, 'p75': 1.1625, 'p95': 2.52, 'min': 0.285714, 'max': 4.421429, 'mean': 0.986772, 'n': 162}
- families {'BREAK_RETEST_MOMENTUM': 137, 'COMPRESSION_EXPANSION': 180, 'FAILED_BREAKOUT': 175, 'PATTERN_CONFIRM_RETEST': 128, 'RANGE_SWEEP_CHOCH_RETEST': 136, 'SWING_BOS_RETEST': 171, 'TRENDLINE_BOUNCE_BREAK': 196, 'TREND_PULLBACK_RESUME': 245, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 132}
- TF sets 10, event signatures 240, niches 92
- sim skips {'no_next_bar': 2, 'gap_before_entry': 3195, 'outside_window': 480606, 'day_cap': 11886, 'spread_filter': 4605, 'stop_invalid': 0, 'risk_out_of_range': 500, 'size_below_min': 0, 'entry_gap_stop': 1, 'target_crossed_at_fill': 185, 'space_below_min_at_fill': 3638}
- co-fire: {'n_strategies': 50, 'pairs': 1225, 'jaccard': {'p50': 0.098104, 'p90': 0.297065, 'p99': 0.86419, 'min': 0.000403, 'max': 1.0, 'mean': 0.140892, 'n': 1225}, 'share_pairs_jaccard_gt_0.1': 0.4873, 'union_decision_bars': 27237, 'bars_with_ge2_same_direction': 19441, 'share_bars_with_ge2_same_direction': 0.7138, 'max_concurrent_same_direction': 41, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 9136, 'train_days': 140, 'always_long': {'n_trades': 821, 'expectancy_r': -0.18053, 'trades_per_day': 5.8643}, 'always_short': {'n_trades': 834, 'expectancy_r': -0.26582, 'trades_per_day': 5.9571}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.22037, 'expectancy_r_sd': 0.03468, 'trades_mean': 826.9}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.25906, 'expectancy_r_sd': 0.09007, 'trades_mean': 136.7, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 7.77, 'search_random_s': 19.51, 'search_total_s': 27.29, 'cofire_s': 2.21, 'baselines_s': 0.06, 'account_feasibility_s': 2.64, 'total_s': 32.24}, peak RSS 686.6 MB
- WARNING: provisional calendar for this market
