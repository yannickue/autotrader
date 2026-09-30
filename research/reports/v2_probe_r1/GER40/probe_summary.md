# V2 probe: GER40

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 1500 (evaluations 1500, duplicates 0, invalid 0, behavioural twins 37)
- cumulative trials incl. v1 cumulative: 31810 (unique specs 31298)
- passed min 40 Train trades: 250; reject reasons {'zero_candidates': 609, 'pass': 250, 'too_few_trades': 640, 'invalid_stop': 1}
- zero-Train-decision share 0.406
- Train trades/day (passers): {'p05': 0.271562, 'p25': 0.375, 'p50': 0.546875, 'p75': 0.910937, 'p95': 2.478437, 'min': 0.25, 'max': 3.7125, 'mean': 0.815875, 'n': 250}
- families {'BREAK_RETEST_MOMENTUM': 135, 'COMPRESSION_EXPANSION': 211, 'FAILED_BREAKOUT': 185, 'PATTERN_CONFIRM_RETEST': 130, 'RANGE_SWEEP_CHOCH_RETEST': 130, 'SWING_BOS_RETEST': 145, 'TRENDLINE_BOUNCE_BREAK': 234, 'TREND_PULLBACK_RESUME': 202, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 128}
- TF sets 10, event signatures 229, niches 145
- sim skips {'no_next_bar': 0, 'gap_before_entry': 1255, 'outside_window': 92208, 'day_cap': 9933, 'spread_filter': 0, 'stop_invalid': 0, 'risk_out_of_range': 1404, 'size_below_min': 667, 'entry_gap_stop': 4, 'target_crossed_at_fill': 17, 'space_below_min_at_fill': 1653}
- co-fire: {'n_strategies': 50, 'pairs': 1225, 'jaccard': {'p50': 0.015617, 'p90': 0.247421, 'p99': 0.848667, 'min': 0.0, 'max': 1.0, 'mean': 0.07805, 'n': 1225}, 'share_pairs_jaccard_gt_0.1': 0.2082, 'union_decision_bars': 18794, 'bars_with_ge2_same_direction': 12145, 'share_bars_with_ge2_same_direction': 0.6462, 'max_concurrent_same_direction': 28, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 21120, 'train_days': 160, 'always_long': {'n_trades': 960, 'expectancy_r': -0.21209, 'trades_per_day': 6.0}, 'always_short': {'n_trades': 960, 'expectancy_r': -0.1265, 'trades_per_day': 6.0}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.14542, 'expectancy_r_sd': 0.03439, 'trades_mean': 960.0}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.17151, 'expectancy_r_sd': 0.07939, 'trades_mean': 158.4, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 6.45, 'search_random_s': 18.14, 'search_total_s': 24.6, 'cofire_s': 1.41, 'baselines_s': 0.07, 'account_feasibility_s': 2.25, 'total_s': 28.37}, peak RSS 868.6 MB
