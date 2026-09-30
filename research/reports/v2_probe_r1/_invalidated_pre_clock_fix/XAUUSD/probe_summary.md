# V2 probe: XAUUSD

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 1500 (evaluations 1500, duplicates 0, invalid 0, behavioural twins 26)
- cumulative trials incl. v1 cumulative: 31810 (unique specs 31298)
- passed min 40 Train trades: 199; reject reasons {'zero_candidates': 672, 'too_few_trades': 623, 'pass': 199, 'invalid_stop': 6}
- zero-Train-decision share 0.448
- Train trades/day (passers): {'p05': 0.309353, 'p25': 0.446043, 'p50': 0.57554, 'p75': 0.877698, 'p95': 1.527338, 'min': 0.294964, 'max': 2.345324, 'mean': 0.717508, 'n': 199}
- families {'BREAK_RETEST_MOMENTUM': 139, 'COMPRESSION_EXPANSION': 143, 'FAILED_BREAKOUT': 213, 'PATTERN_CONFIRM_RETEST': 130, 'RANGE_SWEEP_CHOCH_RETEST': 130, 'SWING_BOS_RETEST': 144, 'TRENDLINE_BOUNCE_BREAK': 272, 'TREND_PULLBACK_RESUME': 199, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 130}
- TF sets 9, event signatures 224, niches 103
- sim skips {'no_next_bar': 0, 'gap_before_entry': 2255, 'outside_window': 314819, 'day_cap': 4317, 'spread_filter': 2527, 'stop_invalid': 0, 'risk_out_of_range': 807, 'size_below_min': 146, 'entry_gap_stop': 1, 'target_crossed_at_fill': 13, 'space_below_min_at_fill': 2486}
- co-fire: {'n_strategies': 50, 'pairs': 1225, 'jaccard': {'p50': 0.050334, 'p90': 0.401351, 'p99': 1.0, 'min': 0.0, 'max': 1.0, 'mean': 0.143524, 'n': 1225}, 'share_pairs_jaccard_gt_0.1': 0.3616, 'union_decision_bars': 18262, 'bars_with_ge2_same_direction': 14895, 'share_bars_with_ge2_same_direction': 0.8156, 'max_concurrent_same_direction': 35, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 11592, 'train_days': 139, 'always_long': {'n_trades': 828, 'expectancy_r': -0.30601, 'trades_per_day': 5.9568}, 'always_short': {'n_trades': 828, 'expectancy_r': -0.45612, 'trades_per_day': 5.9568}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.39487, 'expectancy_r_sd': 0.02529, 'trades_mean': 827.9}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.35109, 'expectancy_r_sd': 0.08669, 'trades_mean': 135.8, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 9.32, 'search_random_s': 18.63, 'search_total_s': 27.97, 'cofire_s': 1.96, 'baselines_s': 0.07, 'account_feasibility_s': 4.6, 'total_s': 34.73}, peak RSS 877.3 MB
- WARNING: provisional calendar for this market
