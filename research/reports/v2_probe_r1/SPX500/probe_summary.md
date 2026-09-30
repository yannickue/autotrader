# V2 probe: SPX500

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 1500 (evaluations 1500, duplicates 0, invalid 0, behavioural twins 28)
- cumulative trials incl. v1 cumulative: 31810 (unique specs 31298)
- passed min 40 Train trades: 186; reject reasons {'zero_candidates': 654, 'too_few_trades': 656, 'pass': 186, 'invalid_stop': 4}
- zero-Train-decision share 0.436
- Train trades/day (passers): {'p05': 0.321429, 'p25': 0.435714, 'p50': 0.639286, 'p75': 0.973214, 'p95': 1.653571, 'min': 0.285714, 'max': 2.678571, 'mean': 0.769662, 'n': 186}
- families {'BREAK_RETEST_MOMENTUM': 133, 'COMPRESSION_EXPANSION': 139, 'FAILED_BREAKOUT': 217, 'PATTERN_CONFIRM_RETEST': 131, 'RANGE_SWEEP_CHOCH_RETEST': 136, 'SWING_BOS_RETEST': 153, 'TRENDLINE_BOUNCE_BREAK': 262, 'TREND_PULLBACK_RESUME': 198, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 131}
- TF sets 9, event signatures 222, niches 104
- sim skips {'no_next_bar': 0, 'gap_before_entry': 1994, 'outside_window': 292631, 'day_cap': 3076, 'spread_filter': 331, 'stop_invalid': 0, 'risk_out_of_range': 1084, 'size_below_min': 2, 'entry_gap_stop': 0, 'target_crossed_at_fill': 68, 'space_below_min_at_fill': 1310}
- co-fire: {'n_strategies': 50, 'pairs': 1225, 'jaccard': {'p50': 0.046708, 'p90': 0.313916, 'p99': 0.910337, 'min': 0.0, 'max': 1.0, 'mean': 0.121544, 'n': 1225}, 'share_pairs_jaccard_gt_0.1': 0.3037, 'union_decision_bars': 25299, 'bars_with_ge2_same_direction': 16953, 'share_bars_with_ge2_same_direction': 0.6701, 'max_concurrent_same_direction': 37, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 9057, 'train_days': 140, 'always_long': {'n_trades': 824, 'expectancy_r': -0.24951, 'trades_per_day': 5.8857}, 'always_short': {'n_trades': 834, 'expectancy_r': -0.32862, 'trades_per_day': 5.9571}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.28119, 'expectancy_r_sd': 0.04156, 'trades_mean': 828.0}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.26131, 'expectancy_r_sd': 0.12442, 'trades_mean': 137.1, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 7.94, 'search_random_s': 18.73, 'search_total_s': 26.67, 'stats_s': 0.27, 'cofire_s': 2.06, 'baselines_s': 0.06, 'account_feasibility_s': 3.53, 'total_s': 32.63}, peak RSS 863.5 MB
- WARNING: provisional calendar for this market
