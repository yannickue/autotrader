# V2 probe: EURUSD

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 1500 (evaluations 1500, duplicates 0, invalid 0, behavioural twins 51)
- cumulative trials incl. v1 cumulative: 31810 (unique specs 31298)
- passed min 40 Train trades: 174; reject reasons {'zero_candidates': 648, 'too_few_trades': 674, 'pass': 174, 'invalid_stop': 4}
- zero-Train-decision share 0.432
- Train trades/day (passers): {'p05': 0.282595, 'p25': 0.348101, 'p50': 0.518987, 'p75': 1.162975, 'p95': 1.803797, 'min': 0.253165, 'max': 2.177215, 'mean': 0.747454, 'n': 174}
- families {'BREAK_RETEST_MOMENTUM': 170, 'COMPRESSION_EXPANSION': 177, 'FAILED_BREAKOUT': 229, 'PATTERN_CONFIRM_RETEST': 128, 'RANGE_SWEEP_CHOCH_RETEST': 131, 'SWING_BOS_RETEST': 136, 'TRENDLINE_BOUNCE_BREAK': 145, 'TREND_PULLBACK_RESUME': 249, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 135}
- TF sets 9, event signatures 216, niches 86
- sim skips {'no_next_bar': 2, 'gap_before_entry': 913, 'outside_window': 152603, 'day_cap': 6770, 'spread_filter': 0, 'stop_invalid': 0, 'risk_out_of_range': 4399, 'size_below_min': 0, 'entry_gap_stop': 10, 'target_crossed_at_fill': 37, 'space_below_min_at_fill': 5285}
- co-fire: {'n_strategies': 50, 'pairs': 1225, 'jaccard': {'p50': 0.058213, 'p90': 0.519682, 'p99': 1.0, 'min': 0.0, 'max': 1.0, 'mean': 0.185212, 'n': 1225}, 'share_pairs_jaccard_gt_0.1': 0.4188, 'union_decision_bars': 16313, 'bars_with_ge2_same_direction': 9637, 'share_bars_with_ge2_same_direction': 0.5908, 'max_concurrent_same_direction': 33, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 11088, 'train_days': 158, 'always_long': {'n_trades': 603, 'expectancy_r': -0.38855, 'trades_per_day': 3.8165}, 'always_short': {'n_trades': 612, 'expectancy_r': -0.34952, 'trades_per_day': 3.8734}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.27982, 'expectancy_r_sd': 0.08151, 'trades_mean': 598.8}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.32532, 'expectancy_r_sd': 0.23775, 'trades_mean': 130.5, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 6.2, 'search_random_s': 16.88, 'search_total_s': 23.09, 'cofire_s': 1.41, 'baselines_s': 0.06, 'account_feasibility_s': 2.07, 'total_s': 26.66}, peak RSS 870.8 MB
- WARNING: provisional calendar for this market
