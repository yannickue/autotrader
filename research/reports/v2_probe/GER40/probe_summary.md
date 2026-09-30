# V2 probe: GER40

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 300 (evaluations 300, duplicates 0, invalid 0, behavioural twins 6)
- cumulative trials incl. v1 cumulative: 30610 (unique specs 30098)
- passed min 40 Train trades: 29; reject reasons {'zero_candidates': 132, 'pass': 29, 'too_few_trades': 139}
- zero-Train-decision share 0.44
- Train trades/day (passers): {'p05': 0.2625, 'p25': 0.29375, 'p50': 0.4375, 'p75': 0.575, 'p95': 0.985, 'min': 0.25, 'max': 1.29375, 'mean': 0.489009, 'n': 29}
- families {'BREAK_RETEST_MOMENTUM': 36, 'COMPRESSION_EXPANSION': 34, 'FAILED_BREAKOUT': 32, 'PATTERN_CONFIRM_RETEST': 30, 'RANGE_SWEEP_CHOCH_RETEST': 30, 'SWING_BOS_RETEST': 29, 'TRENDLINE_BOUNCE_BREAK': 36, 'TREND_PULLBACK_RESUME': 44, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 29}
- TF sets 8, event signatures 86, niches 24
- sim skips {'no_next_bar': 0, 'gap_before_entry': 76, 'outside_window': 5341, 'day_cap': 54, 'spread_filter': 0, 'stop_invalid': 0, 'risk_out_of_range': 171, 'size_below_min': 19, 'entry_gap_stop': 0, 'target_crossed_at_fill': 2}
- co-fire: {'n_strategies': 29, 'pairs': 406, 'jaccard': {'p50': 0.0, 'p90': 0.024185, 'p99': 0.827586, 'min': 0.0, 'max': 1.0, 'mean': 0.032425, 'n': 406}, 'share_pairs_jaccard_gt_0.1': 0.0394, 'union_decision_bars': 3513, 'bars_with_ge2_same_direction': 778, 'share_bars_with_ge2_same_direction': 0.2215, 'max_concurrent_same_direction': 8, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 21120, 'train_days': 160, 'always_long': {'n_trades': 960, 'expectancy_r': -0.21209, 'trades_per_day': 6.0}, 'always_short': {'n_trades': 960, 'expectancy_r': -0.1265, 'trades_per_day': 6.0}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.14542, 'expectancy_r_sd': 0.03439, 'trades_mean': 960.0}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.17151, 'expectancy_r_sd': 0.07939, 'trades_mean': 158.4, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 2.35, 'search_random_s': 3.28, 'search_total_s': 5.63, 'cofire_s': 0.74, 'baselines_s': 0.07, 'total_s': 6.45}, peak RSS 640.5 MB
