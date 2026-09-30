# V2 probe: SPX500

Scope: TRAIN side of search fold 0 only; no Validation/fold-test number anywhere.

- unique specs 1500 (evaluations 1500, duplicates 0, invalid 0, behavioural twins 37)
- cumulative trials incl. v1 cumulative: 31810 (unique specs 31298)
- passed min 40 Train trades: 153; reject reasons {'zero_candidates': 660, 'too_few_trades': 677, 'invalid_stop': 10, 'pass': 153}
- zero-Train-decision share 0.44
- Train trades/day (passers): {'p05': 0.307143, 'p25': 0.414286, 'p50': 0.578571, 'p75': 0.828571, 'p95': 2.031429, 'min': 0.285714, 'max': 2.278571, 'mean': 0.75845, 'n': 153}
- families {'BREAK_RETEST_MOMENTUM': 137, 'COMPRESSION_EXPANSION': 218, 'FAILED_BREAKOUT': 155, 'PATTERN_CONFIRM_RETEST': 135, 'RANGE_SWEEP_CHOCH_RETEST': 130, 'SWING_BOS_RETEST': 140, 'TRENDLINE_BOUNCE_BREAK': 202, 'TREND_PULLBACK_RESUME': 254, 'ZONE_SWEEP_RECLAIM_BOS_RETEST': 129}
- TF sets 10, event signatures 218, niches 65
- sim skips {'no_next_bar': 1, 'gap_before_entry': 1243, 'outside_window': 114480, 'day_cap': 11403, 'spread_filter': 2059, 'stop_invalid': 0, 'risk_out_of_range': 5307, 'size_below_min': 2, 'entry_gap_stop': 0, 'target_crossed_at_fill': 83, 'space_below_min_at_fill': 1230}
- co-fire: {'n_strategies': 50, 'pairs': 1225, 'jaccard': {'p50': 0.089812, 'p90': 0.512492, 'p99': 0.943602, 'min': 0.0, 'max': 1.0, 'mean': 0.188525, 'n': 1225}, 'share_pairs_jaccard_gt_0.1': 0.4759, 'union_decision_bars': 13361, 'bars_with_ge2_same_direction': 6454, 'share_bars_with_ge2_same_direction': 0.483, 'max_concurrent_same_direction': 39, 'note': 'Jaccard on (decision bar, direction) sets of the top-K by Train trade count; Train bars only'}
- drift baselines (Train): {'stop_atr_mult': 1.0, 'target_r': 1.5, 'cost': 'COMBINED_ADVERSE', 'eligible_train_bars': 9135, 'train_days': 140, 'always_long': {'n_trades': 777, 'expectancy_r': -0.37833, 'trades_per_day': 5.55}, 'always_short': {'n_trades': 827, 'expectancy_r': -0.5999, 'trades_per_day': 5.9071}, 'random_long_short': {'draws': 20, 'expectancy_r_mean': -0.49991, 'expectancy_r_sd': 0.03086, 'trades_mean': 804.9}, 'same_session_random': {'draws': 20, 'expectancy_r_mean': -0.52898, 'expectancy_r_sd': 0.09575, 'trades_mean': 136.0, 'decisions_per_day': 1}}
- runtime {'search_deap_s': 7.14, 'search_random_s': 18.34, 'search_total_s': 25.51, 'cofire_s': 1.35, 'baselines_s': 0.06, 'account_feasibility_s': 2.29, 'total_s': 29.3}, peak RSS 856.1 MB
- WARNING: provisional calendar for this market
