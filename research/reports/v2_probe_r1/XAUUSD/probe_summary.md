# V2 probe: XAUUSD

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 1500 (evaluations 1500, duplicates 0, invalid 0, behavioural twins 35)
- cumulative trials incl. v1 cumulative: 31810 (unique specs 31298)
- passed min 40 Train trades: 197; reject reasons {'zero_candidates': 665, 'too_few_trades': 632, 'pass': 197, 'invalid_stop': 6}
- zero-Train-decision share 0.4433
- Train trades/day (passers): {'p05': 0.323741, 'p25': 0.417266, 'p50': 0.539568, 'p75': 0.748201, 'p95': 1.88777, 'min': 0.28777, 'max': 2.496403, 'mean': 0.712851, 'n': 197}
- families {'BREAK_RETEST_MOMENTUM': 135, 'COMPRESSION_EXPANSION': 209, 'FAILED_BREAKOUT': 255, 'PATTERN_CONFIRM_RETEST': 129, 'RANGE_SWEEP_CHOCH_RETEST': 129, 'SWING_BOS_RETEST': 137, 'TRENDLINE_BOUNCE_BREAK': 211, 'TREND_PULLBACK_RESUME': 165, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 130}
- TF sets 10, event signatures 228, niches 108
- sim skips {'no_next_bar': 0, 'gap_before_entry': 1006, 'outside_window': 168683, 'day_cap': 5756, 'spread_filter': 1144, 'stop_invalid': 0, 'risk_out_of_range': 654, 'size_below_min': 176, 'entry_gap_stop': 0, 'target_crossed_at_fill': 16, 'space_below_min_at_fill': 3289}
- co-fire: {'n_strategies': 50, 'pairs': 1225, 'jaccard': {'p50': 0.009749, 'p90': 0.105368, 'p99': 0.711617, 'min': 0.0, 'max': 1.0, 'mean': 0.048345, 'n': 1225}, 'share_pairs_jaccard_gt_0.1': 0.1053, 'union_decision_bars': 17611, 'bars_with_ge2_same_direction': 12995, 'share_bars_with_ge2_same_direction': 0.7379, 'max_concurrent_same_direction': 25, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 11592, 'train_days': 139, 'always_long': {'n_trades': 828, 'expectancy_r': -0.32117, 'trades_per_day': 5.9568}, 'always_short': {'n_trades': 828, 'expectancy_r': -0.45849, 'trades_per_day': 5.9568}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.3934, 'expectancy_r_sd': 0.04301, 'trades_mean': 827.9}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.42998, 'expectancy_r_sd': 0.09544, 'trades_mean': 135.8, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 7.48, 'search_random_s': 18.87, 'search_total_s': 26.36, 'stats_s': 0.3, 'cofire_s': 1.66, 'baselines_s': 0.07, 'account_feasibility_s': 3.14, 'total_s': 31.58}, peak RSS 799.0 MB
- WARNING: provisional calendar for this market
