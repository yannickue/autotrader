# V2 probe: NAS100

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 1500 (evaluations 1500, duplicates 0, invalid 0, behavioural twins 24)
- cumulative trials incl. v1 cumulative: 31810 (unique specs 31298)
- passed min 40 Train trades: 193; reject reasons {'zero_candidates': 648, 'too_few_trades': 659, 'pass': 193}
- zero-Train-decision share 0.432
- Train trades/day (passers): {'p05': 0.307143, 'p25': 0.414286, 'p50': 0.614286, 'p75': 1.235714, 'p95': 2.647143, 'min': 0.285714, 'max': 3.928571, 'mean': 0.963434, 'n': 193}
- families {'BREAK_RETEST_MOMENTUM': 231, 'COMPRESSION_EXPANSION': 139, 'FAILED_BREAKOUT': 233, 'PATTERN_CONFIRM_RETEST': 130, 'RANGE_SWEEP_CHOCH_RETEST': 156, 'SWING_BOS_RETEST': 139, 'TRENDLINE_BOUNCE_BREAK': 179, 'TREND_PULLBACK_RESUME': 164, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 129}
- TF sets 9, event signatures 239, niches 106
- sim skips {'no_next_bar': 0, 'gap_before_entry': 2991, 'outside_window': 509466, 'day_cap': 9304, 'spread_filter': 202, 'stop_invalid': 0, 'risk_out_of_range': 1180, 'size_below_min': 0, 'entry_gap_stop': 2, 'target_crossed_at_fill': 3, 'space_below_min_at_fill': 1647}
- co-fire: {'n_strategies': 50, 'pairs': 1225, 'jaccard': {'p50': 0.092883, 'p90': 0.365252, 'p99': 0.936774, 'min': 0.0, 'max': 1.0, 'mean': 0.15945, 'n': 1225}, 'share_pairs_jaccard_gt_0.1': 0.471, 'union_decision_bars': 35433, 'bars_with_ge2_same_direction': 24009, 'share_bars_with_ge2_same_direction': 0.6776, 'max_concurrent_same_direction': 41, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 9057, 'train_days': 140, 'always_long': {'n_trades': 830, 'expectancy_r': -0.06688, 'trades_per_day': 5.9286}, 'always_short': {'n_trades': 833, 'expectancy_r': -0.1432, 'trades_per_day': 5.95}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.11123, 'expectancy_r_sd': 0.03497, 'trades_mean': 831.1}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.07309, 'expectancy_r_sd': 0.11145, 'trades_mean': 137.2, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 8.36, 'search_random_s': 18.82, 'search_total_s': 27.19, 'stats_s': 0.27, 'cofire_s': 3.01, 'baselines_s': 0.06, 'account_feasibility_s': 3.7, 'total_s': 34.26}, peak RSS 890.6 MB
- WARNING: provisional calendar for this market
