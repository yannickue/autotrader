# V2 probe: EURUSD

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 1500 (evaluations 1500, duplicates 0, invalid 0, behavioural twins 51)
- cumulative trials incl. v1 cumulative: 31810 (unique specs 31298)
- passed min 40 Train trades: 193; reject reasons {'zero_candidates': 664, 'too_few_trades': 639, 'pass': 193, 'invalid_stop': 4}
- zero-Train-decision share 0.4427
- Train trades/day (passers): {'p05': 0.28481, 'p25': 0.392405, 'p50': 0.607595, 'p75': 0.905063, 'p95': 1.739241, 'min': 0.253165, 'max': 2.575949, 'mean': 0.715977, 'n': 193}
- families {'BREAK_RETEST_MOMENTUM': 132, 'COMPRESSION_EXPANSION': 172, 'FAILED_BREAKOUT': 287, 'PATTERN_CONFIRM_RETEST': 127, 'RANGE_SWEEP_CHOCH_RETEST': 132, 'SWING_BOS_RETEST': 151, 'TRENDLINE_BOUNCE_BREAK': 194, 'TREND_PULLBACK_RESUME': 168, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 137}
- TF sets 9, event signatures 229, niches 110
- sim skips {'no_next_bar': 19, 'gap_before_entry': 1641, 'outside_window': 236196, 'day_cap': 1593, 'spread_filter': 0, 'stop_invalid': 0, 'risk_out_of_range': 1899, 'size_below_min': 0, 'entry_gap_stop': 5, 'target_crossed_at_fill': 70, 'space_below_min_at_fill': 6190}
- co-fire: {'n_strategies': 50, 'pairs': 1225, 'jaccard': {'p50': 0.011207, 'p90': 0.187651, 'p99': 0.914311, 'min': 0.0, 'max': 1.0, 'mean': 0.074916, 'n': 1225}, 'share_pairs_jaccard_gt_0.1': 0.1461, 'union_decision_bars': 20508, 'bars_with_ge2_same_direction': 14744, 'share_bars_with_ge2_same_direction': 0.7189, 'max_concurrent_same_direction': 27, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 11088, 'train_days': 158, 'always_long': {'n_trades': 562, 'expectancy_r': -0.36388, 'trades_per_day': 3.557}, 'always_short': {'n_trades': 548, 'expectancy_r': -0.2913, 'trades_per_day': 3.4684}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.28943, 'expectancy_r_sd': 0.05896, 'trades_mean': 555.8}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.39181, 'expectancy_r_sd': 0.19893, 'trades_mean': 130.4, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 7.14, 'search_random_s': 17.02, 'search_total_s': 24.17, 'stats_s': 0.26, 'cofire_s': 1.5, 'baselines_s': 0.05, 'account_feasibility_s': 2.59, 'total_s': 28.62}, peak RSS 858.9 MB
- WARNING: provisional calendar for this market
