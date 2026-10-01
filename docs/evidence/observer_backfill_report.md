# Observer backfill: Gate B report

Generated 2026-10-01 17:11 from code `70905f7a34037a81070a619e22efd33e994789d5`; backfill root `C:/Users/yanni/AppData/Local/Temp/observer_backfill` (outside git). OBSERVATION_ONLY_NOT_ALPHA_VALIDATED, offline, no edge claim.

## GATE B VERDICT: PASS

| market | events | controls | plausibility | leakage audit | live==batch | label sanity | matching | verdict |
|---|---|---|---|---|---|---|---|---|
| GER40 | 9546 | 27 | PASS | PASS | PASS | PASS | WARN | PASS |
| NAS100 | 5387 | 3 | PASS | PASS | PASS | PASS | WARN | PASS |
| SPX500 | 2991 | 44 | PASS | PASS | PASS | PASS | WARN | PASS |
| XAUUSD | 7181 | 0 | PASS | PASS | PASS | PASS | WARN | PASS |
| EURUSD | 2868 | 31 | PASS | PASS | PASS | PASS | WARN | PASS |
| BTCUSD | 2363 | 2303 | PASS | PASS | PASS | PASS | PASS | PASS |
| BRENT | 2975 | 2408 | PASS | PASS | PASS | PASS | PASS | PASS |

Blocking checks: plausibility, leakage audit, live-vs-batch parity, label sanity. Matching quality / warm-up / coverage are reported (WARN is non-blocking).

## GER40

Rows: 9546 events + 27 controls; frame fingerprint matches manifest: True. Gate B compute 8.5 s.

### (ii) Leakage audit on real data

* live-style incremental pass, full history, physically truncated views (prefix, bar 0..i): compared 327, pass 327, fail 0, skipped 0  -> PASS
* raw frame truncated exactly at the decision bar, history window 6636 bars, rebuilt through the batch adapter: compared 60, pass 60, fail 0, skipped 0  -> PASS
* same window but +500 FUTURE bars present in the frame: compared 30, pass 30, fail 0, skipped 0  -> PASS
* shallow histories (120/240/500 bars): 120: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 240: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 500: n=20, flagged not-warm 20, wrongly warm 0, still equal 0

### (i) Plausibility (83 feature columns, 0 flagged) -> PASS

| group | columns | constant | all-missing | missing >= 50% | flagged |
|---|---|---|---|---|---|
| acceptance | 15 | 0 | 0 | 14 | 0 |
| balance | 10 | 0 | 0 | 0 | 0 |
| levels | 18 | 0 | 0 | 1 | 0 |
| participation | 8 | 0 | 0 | 0 | 0 |
| swings | 32 | 0 | 0 | 4 | 0 |

### (iii) Warm-up accounting

* events: 0/9546 warmup_ok=false (0.0%); controls: 0/27 (0.0%)
* share of events below the documented history requirement by group: levels 0.0%, swings 0.0%, acceptance 0.0%, balance 0.0%, participation 0.0%, atr 0.0%
* events warmup_ok=false by family: EOD 0.0%, GAP 0.0%, LEADLAG 0.0%, ORB 0.0%, OVERNIGHT 0.0%, ROUND 0.0%, VOLREV 0.0%

### (iv) Label sanity -> PASS

| label | ref P (zero drift) | events rate [Wilson95] (n) | controls rate [Wilson95] (n) | censored ev/ctl |
|---|---|---|---|---|
| y_fav025_before_adv025 | 0.500 | 0.397 [0.387,0.408] (8138) | n/a | 14.7% / 100.0% |
| y_fav050_before_adv050 | 0.500 | 0.481 [0.470,0.492] (8053) | n/a | 15.6% / 100.0% |
| y_fav075_before_adv050 | 0.400 | 0.393 [0.382,0.404] (7956) | n/a | 16.7% / 100.0% |
| y_fav100_before_adv050 | 0.333 | 0.324 [0.314,0.334] (7876) | n/a | 17.5% / 100.0% |

* consistency violations: 0 over 9573 rows; horizon max 14400 s (bound 14400), full-48-bar share 48.8%
* no label issue

### (v) Matching quality -> WARN (matching revision: partition-aware (match_controls partition=auto, rank_mode=partition, exclude_idx = every generator opportunity))

* events 9546, matched 27 (rate 0.003), unmatched 9519, controls 27; SMD {'local_minute': -0.7814493697697315, 'atr_pct': 1.3764423786038718, 'spread_pct': -0.8071803206194293}; max session-share difference 0.000; controls in a different partition than their event: 0
* match rate by family: VOLREV 13/2199, ROUND 13/5757, ORB 0/681, EOD 0/190, GAP 0/179, OVERNIGHT 0/186, LEADLAG 1/354
* reasons: ['match rate 0.003 < 0.5', '|SMD(local_minute)|=-0.7814493697697315 > 0.1', '|SMD(atr_pct)|=1.3764423786038718 > 0.1', '|SMD(spread_pct)|=-0.8071803206194293 > 0.1']
* PRE-REVISION controls (observer-controls-1, whole-sample ranks): 35 controls, match rate 0.0037, SMD {'local_minute': -1.0771418648351208, 'atr_pct': 1.5613950496402011, 'spread_pct': -0.8216503622314812}; REVISED: 27 controls, match rate 0.0028284098051539913
* control label rates pre -> revised: fav025_before_adv025 n/a (n=0) -> n/a (n=0), fav050_before_adv050 n/a (n=0) -> n/a (n=0), fav075_before_adv050 n/a (n=0) -> n/a (n=0), fav100_before_adv050 n/a (n=0) -> n/a (n=0)

### (vii) Coverage

* bars 2025-02-10T17:55:00+00:00 .. 2026-08-31T19:55:00+00:00 (95011 bars); gaps 397 (intraday > 3 bars: 1, largest 123.33 h); missing UTC months: none; gaps > 3 days: [['2025-04-17T19:55:00+00:00', 100.3], ['2025-12-23T20:55:00+00:00', 123.3], ['2026-04-02T19:55:00+00:00', 100.3], ['2026-04-30T19:55:00+00:00', 76.3]]
* dev-end guard: {'dev_end_berlin_date': '2026-08-31', 'assert_no_forward_holdout': 'PASS', 'guard_dev_only': 'PASS', 'last_bar_utc': '2026-08-31T19:55:00+00:00'}
* partitions: {'FROZEN_OOS': 1106, 'TRAIN': 5929, 'VALIDATION': 2511} (plan {'train_start': '2025-02-10', 'train_end': '2026-02-23', 'validation_start': '2026-02-24', 'validation_end': '2026-06-30', 'oos_start': '2026-07-01', 'oos_end': '2026-08-31', 'forward_start': '2026-09-01', 'core_fit_end': '2026-06-30'}; frozen split exists: True). core thresholds were fitted up to the core fit end; the dev window 2026-07-01..2026-08-31 is a weaker OOS than a never-seen period
* events by family|variant|direction: {'EOD|continue|long': 54, 'EOD|continue|short': 41, 'EOD|reverse|long': 41, 'EOD|reverse|short': 54, 'GAP|fade|long': 42, 'GAP|fade|short': 50, 'GAP|go|long': 55, 'GAP|go|short': 32, 'LEADLAG||long': 161, 'LEADLAG||short': 193, 'ORB|breakout|long': 183, 'ORB|breakout|short': 187, 'ORB|fade|long': 166, 'ORB|fade|short': 145, 'OVERNIGHT|continue|long': 55, 'OVERNIGHT|continue|short': 38, 'OVERNIGHT|reverse|long': 38, 'OVERNIGHT|reverse|short': 55, 'ROUND|break|long': 874, 'ROUND|break|short': 859, 'ROUND|reject|long': 2034, 'ROUND|reject|short': 1990, 'VOLREV|expand|long': 905, 'VOLREV|expand|short': 783, 'VOLREV|fade|long': 270, 'VOLREV|fade|short': 241}
* candidate exclusions: {'candidates': 10214, 'before_eval_start': 569, 'duplicate_family_variant_direction': 99, 'emitted': 9546, 'emitted_before_limit': 9546}
* runtime s {'events_step': 462.5, 'controls_step': 98.7}, peak memory MB {'events_step': 268.2, 'controls_step': 211.1}

### Sample event record (features at the decision bar, non-null)

```json
{
 "event_id": "64e9fe45270484ee37f640c697378748",
 "market": "GER40",
 "family": "ROUND",
 "variant": "reject",
 "direction": 1,
 "is_control": false,
 "decision_ts_ns": 1765290900000000000,
 "observer_version": "market-structure-observer-v1",
 "schema_version": "mso-schema-1",
 "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
 "v_acceptance": "mso-acceptance-1",
 "v_balance": "mso-balance-1",
 "v_levels": "mso-levels-1",
 "v_participation": "mso-participation-1",
 "v_swings": "mso-swings-1",
 "f_levels__nearest_level_distance_atr": 0.14009636257739722,
 "f_levels__nearest_level_source": "SWING_M5",
 "f_levels__nearest_level_age_bars": 45,
 "f_levels__touch_count": 12,
 "f_levels__clean_rejection_count": 6,
 "f_levels__penetration_count": 5,
 "f_levels__first_touch": false,
 "f_levels__break_count": 3,
 "f_levels__reclaim_count": 2,
 "f_levels__role": "FLIPPED_TO_RESISTANCE",
 "f_levels__previous_role": "ACCEPTED_BELOW",
 "f_levels__zone_source_count": 15,
 "f_levels__zone_independent_source_count": 5,
 "f_levels__zone_width_atr": 1.9328162227988326,
 "f_levels__n_levels_within_1atr": 14,
 "f_levels__last_touch_ts_ns": 1.7652903e+18,
 "f_levels__last_break_ts_ns": 1.7652876e+18,
 "f_levels__last_reclaim_ts_ns": 1.7652834e+18,
 "f_swings__m5_sequence": "MIXED_TRANSITION",
 "f_swings__m5_high_label": "LH",
 "f_swings__m5_low_label": "HL",
 "f_swings__m5_sequence_length": 0,
 "f_swings__m5_structure_age_bars": 3.0,
 "f_swings__m5_structure_age_minutes": 15.0,
 "f_swings__m5_high_delta_atr": -0.4469741091758265,
 "f_swings__m5_low_delta_atr": 0.11192883994270407,
 "f_swings__m5_confirmed_at_ts_ns": 1.76529e+18,
 "f_swings__m5_last_high_confirmed_at_ts_ns": 1.7652891e+18,
 "f_swings__m5_last_low_confirmed_at_ts_ns": 1.76529e+18,
 "f_swings__m5_close_beyond_last_swing_atr_high": 0.0,
 "f_swings__m5_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m5_bars_since_beyond_low": 1.0,
 "f_swings__m15_sequence": "MIXED_TRANSITION",
 "f_swings__m15_high_label": "LH",
 "f_swings__m15_low_label": "HL",
 "f_swings__m15_sequence_length": 0,
 "f_swings__m15_structure_age_bars": 1.0,
 "f_swings__m15_structure_age_minutes": 20.0,
 "f_swings__m15_high_delta_atr": -1.5914650288557133,
 "f_swings__m15_low_delta_atr": 0.2950177370680747,
 "f_swings__m15_confirmed_at_ts_ns": 1.7652897e+18,
 "f_swings__m15_last_high_confirmed_at_ts_ns": 1.7652879e+18,
 "f_swings__m15_last_low_confirmed_at_ts_ns": 1.7652897e+18,
 "f_swings__m15_close_beyond_last_swing_atr_high": 0.0,
 "f_swings__m15_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m15_bars_since_beyond_low": 1.0,
 "f_swings__ema_trend": "down",
 "f_swings__ema_swing_agreement": "UNDEFINED",
 "f_acceptance__break_found": false,
 "f_balance__range_width_atr_w24": 4.079843278445735,
 "f_balance__directional_efficiency_w24": 0.04605212577841917,
 "f_balance__bar_overlap_ratio_w24": 0.4804673255451453,
 "f_balance__close_occupancy_ratio_w24": 0.8333333333333334,
 "f_balance__midpoint_cross_count_w24": 3.0,
 "f_balance__range_width_atr_w48": 4.930057711653616,
 "f_balance__directional_efficiency_w48": 0.043153244988115395,
 "f_balance__bar_overlap_ratio_w48": 0.5048114888763975,
 "f_balance__close_occupancy_ratio_w48": 0.875,
 "f_balance__midpoint_cross_count_w48": 11.0,
 "f_participation__tick_activity_per_min": 127.2,
 "f_participation__tick_activity_percentile": 0.4,
 "f_participation__tick_activity_z": -0.39769308303208,
 "f_participation__activity_vs_same_tod": 0.8658951667801226,
 "f_participation__activity_vs_session_baseline": 5.530434782608696,
 "f_participation__activity_acceleration": 2.9308755760368665,
 "f_participation__tod_baseline_n": 20,
 "f_participation__tod_baseline_oldest_ts_ns": 1762871400000000000,
 "m_warmup_ok": true,
 "m_bars_available": 51163,
 "m_prev_days_available": 213,
 "m_atr_ok": true,
 "m_min_bars_levels": 603,
 "m_min_bars_swings": 480,
 "m_min_bars_acceptance": 98,
 "m_min_bars_balance": 48,
 "m_min_prev_days_participation": 21,
 "m_event_price": 24128.86,
 "m_reference_level_id": "77a7b1dda42e025a",
 "m_config_hash": "9baff67cbb414f2a",
 "m_hash_levels": "84a77f0a6266399f",
 "m_hash_swings": "eb1857c684e21d19",
 "m_hash_acceptance": "1608ec3fb935dc33ecc4874e1768289b3c0790e9bc3a0919f88e766e28f08060",
 "m_hash_balance": "ddac1af18d7032ca1ee3acf276d5f2ccd6101710a5bd8476158872b7ca8d3aec",
 "m_hash_participation": "452f5122fbfc26c12ed207281e1fcbd3152c3ecd265d25b5ce177c13529c92b3",
 "warmup_ok": true,
 "run_id": "fe32033bf7be",
 "decision_idx": 51162,
 "strategy_id": "ROUND-cada1b04da",
 "entry": 24128.86,
 "stop": 24108.62392857143,
 "risk": 20.23607142857145,
 "risk_atr": 1.5000000000000675,
 "partition": "TRAIN"
}
```

## NAS100

Rows: 5387 events + 3 controls; frame fingerprint matches manifest: True. Gate B compute 7.1 s.

### (ii) Leakage audit on real data

* live-style incremental pass, full history, physically truncated views (prefix, bar 0..i): compared 303, pass 303, fail 0, skipped 0  -> PASS
* raw frame truncated exactly at the decision bar, history window 7728 bars, rebuilt through the batch adapter: compared 60, pass 60, fail 0, skipped 0  -> PASS
* same window but +500 FUTURE bars present in the frame: compared 30, pass 30, fail 0, skipped 0  -> PASS
* shallow histories (120/240/500 bars): 120: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 240: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 500: n=20, flagged not-warm 20, wrongly warm 0, still equal 0

### (i) Plausibility (83 feature columns, 0 flagged) -> PASS

| group | columns | constant | all-missing | missing >= 50% | flagged |
|---|---|---|---|---|---|
| acceptance | 15 | 0 | 0 | 14 | 0 |
| balance | 10 | 0 | 0 | 0 | 0 |
| levels | 18 | 0 | 0 | 1 | 0 |
| participation | 8 | 0 | 0 | 0 | 0 |
| swings | 32 | 0 | 0 | 4 | 0 |

### (iii) Warm-up accounting

* events: 0/5387 warmup_ok=false (0.0%); controls: 0/3 (0.0%)
* share of events below the documented history requirement by group: levels 0.0%, swings 0.0%, acceptance 0.0%, balance 0.0%, participation 0.0%, atr 0.0%
* events warmup_ok=false by family: EOD 0.0%, GAP 0.0%, LEADLAG 0.0%, ORB 0.0%, OVERNIGHT 0.0%, ROUND 0.0%, VOLREV 0.0%

### (iv) Label sanity -> PASS

| label | ref P (zero drift) | events rate [Wilson95] (n) | controls rate [Wilson95] (n) | censored ev/ctl |
|---|---|---|---|---|
| y_fav025_before_adv025 | 0.500 | 0.392 [0.379,0.405] (5387) | 0.500 [0.095,0.905] (2) | 0.0% / 33.3% |
| y_fav050_before_adv050 | 0.500 | 0.469 [0.456,0.483] (5376) | n/a | 0.2% / 100.0% |
| y_fav075_before_adv050 | 0.400 | 0.381 [0.368,0.394] (5356) | n/a | 0.6% / 100.0% |
| y_fav100_before_adv050 | 0.333 | 0.322 [0.309,0.334] (5325) | n/a | 1.2% / 100.0% |

* consistency violations: 0 over 5390 rows; horizon max 14400 s (bound 14400), full-48-bar share 62.0%
* no label issue

### (v) Matching quality -> WARN (matching revision: partition-aware (match_controls partition=auto, rank_mode=partition, exclude_idx = every generator opportunity))

* events 5387, matched 3 (rate 0.001), unmatched 5384, controls 3; SMD {'local_minute': 4.32049379893858, 'atr_pct': -1.3942544967621457, 'spread_pct': -0.7395510652703426}; max session-share difference 0.000; controls in a different partition than their event: 0
* match rate by family: VOLREV 2/1034, GAP 0/169, ROUND 1/3104, ORB 0/582, LEADLAG 0/180, EOD 0/152, OVERNIGHT 0/166
* reasons: ['match rate 0.001 < 0.5', '|SMD(local_minute)|=4.32049379893858 > 0.1', '|SMD(atr_pct)|=-1.3942544967621457 > 0.1', '|SMD(spread_pct)|=-0.7395510652703426 > 0.1']
* PRE-REVISION controls (observer-controls-1, whole-sample ranks): 4 controls, match rate 0.0007, SMD {'local_minute': 4.929503017546495, 'atr_pct': -0.5525411993845394, 'spread_pct': -0.5261815881023961}; REVISED: 3 controls, match rate 0.0005568962316688324
* control label rates pre -> revised: fav025_before_adv025 0.667 (n=3) -> 0.500 (n=2), fav050_before_adv050 n/a (n=0) -> n/a (n=0), fav075_before_adv050 n/a (n=0) -> n/a (n=0), fav100_before_adv050 n/a (n=0) -> n/a (n=0)

### (vii) Coverage

* bars 2025-05-02T09:35:00+00:00 .. 2026-08-31T20:55:00+00:00 (94213 bars); gaps 346 (intraday > 3 bars: 272, largest 56.83 h); missing UTC months: none; gaps > 3 days: none
* dev-end guard: {'dev_end_berlin_date': '2026-08-31', 'assert_no_forward_holdout': 'PASS', 'guard_dev_only': 'PASS', 'last_bar_utc': '2026-08-31T20:55:00+00:00'}
* partitions: {'FROZEN_OOS': 814, 'TRAIN': 3200, 'VALIDATION': 1373} (plan {'train_start': '2025-05-02', 'train_end': '2026-03-16', 'validation_start': '2026-03-17', 'validation_end': '2026-06-30', 'oos_start': '2026-07-01', 'oos_end': '2026-08-31', 'forward_start': '2026-09-01', 'core_fit_end': '2026-06-30'}; frozen split exists: True). core thresholds were fitted up to the core fit end; the dev window 2026-07-01..2026-08-31 is a weaker OOS than a never-seen period
* events by family|variant|direction: {'EOD|continue|long': 56, 'EOD|continue|short': 20, 'EOD|reverse|long': 20, 'EOD|reverse|short': 56, 'GAP|fade|long': 30, 'GAP|fade|short': 43, 'GAP|go|long': 59, 'GAP|go|short': 37, 'LEADLAG||long': 93, 'LEADLAG||short': 87, 'ORB|breakout|long': 167, 'ORB|breakout|short': 155, 'ORB|fade|long': 138, 'ORB|fade|short': 122, 'OVERNIGHT|continue|long': 58, 'OVERNIGHT|continue|short': 25, 'OVERNIGHT|reverse|long': 25, 'OVERNIGHT|reverse|short': 58, 'ROUND|break|long': 491, 'ROUND|break|short': 459, 'ROUND|reject|long': 1143, 'ROUND|reject|short': 1011, 'VOLREV|expand|long': 455, 'VOLREV|expand|short': 314, 'VOLREV|fade|long': 146, 'VOLREV|fade|short': 119}
* candidate exclusions: {'candidates': 5734, 'before_eval_start': 341, 'duplicate_family_variant_direction': 6, 'emitted': 5387, 'emitted_before_limit': 5387}
* runtime s {'events_step': 365.6, 'controls_step': 76.5}, peak memory MB {'events_step': 268.2, 'controls_step': 217.6}

### Sample event record (features at the decision bar, non-null)

```json
{
 "event_id": "66a33f829602d49d3de1a99a0da05c3a",
 "market": "NAS100",
 "family": "ORB",
 "variant": "fade",
 "direction": -1,
 "is_control": false,
 "decision_ts_ns": 1770303000000000000,
 "observer_version": "market-structure-observer-v1",
 "schema_version": "mso-schema-1",
 "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
 "v_acceptance": "mso-acceptance-1",
 "v_balance": "mso-balance-1",
 "v_levels": "mso-levels-1",
 "v_participation": "mso-participation-1",
 "v_swings": "mso-swings-1",
 "f_levels__nearest_level_distance_atr": -0.040026225561321106,
 "f_levels__nearest_level_source": "SWING_M5",
 "f_levels__nearest_level_age_bars": 129,
 "f_levels__touch_count": 3,
 "f_levels__clean_rejection_count": 0,
 "f_levels__penetration_count": 2,
 "f_levels__first_touch": false,
 "f_levels__break_count": 1,
 "f_levels__reclaim_count": 0,
 "f_levels__role": "ACCEPTED_BELOW",
 "f_levels__previous_role": "BROKEN_DOWN",
 "f_levels__zone_source_count": 46,
 "f_levels__zone_independent_source_count": 5,
 "f_levels__zone_width_atr": 2.340089639828421,
 "f_levels__n_levels_within_1atr": 22,
 "f_levels__last_touch_ts_ns": 1.7703027e+18,
 "f_levels__last_break_ts_ns": 1.7702943e+18,
 "f_swings__m5_sequence": "DOWN_SEQUENCE",
 "f_swings__m5_high_label": "LH",
 "f_swings__m5_low_label": "LL",
 "f_swings__m5_sequence_length": 3,
 "f_swings__m5_structure_age_bars": 1.0,
 "f_swings__m5_structure_age_minutes": 5.0,
 "f_swings__m5_high_delta_atr": -0.4165135050892634,
 "f_swings__m5_low_delta_atr": -1.255258547490797,
 "f_swings__m5_confirmed_at_ts_ns": 1.7703027e+18,
 "f_swings__m5_last_high_confirmed_at_ts_ns": 1.7703009e+18,
 "f_swings__m5_last_low_confirmed_at_ts_ns": 1.7703027e+18,
 "f_swings__m5_close_beyond_last_swing_atr_high": 1.3113855479959542,
 "f_swings__m5_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m5_bars_since_beyond_high": 2.0,
 "f_swings__m15_sequence": "DOWN_SEQUENCE",
 "f_swings__m15_high_label": "LH",
 "f_swings__m15_low_label": "LL",
 "f_swings__m15_sequence_length": 3,
 "f_swings__m15_structure_age_bars": 2.0,
 "f_swings__m15_structure_age_minutes": 35.0,
 "f_swings__m15_high_delta_atr": -2.8492352669310184,
 "f_swings__m15_low_delta_atr": -4.25120648330266,
 "f_swings__m15_confirmed_at_ts_ns": 1.7703009e+18,
 "f_swings__m15_last_high_confirmed_at_ts_ns": 1.7703009e+18,
 "f_swings__m15_last_low_confirmed_at_ts_ns": 1.7702982e+18,
 "f_swings__m15_close_beyond_last_swing_atr_high": 0.8948720429066909,
 "f_swings__m15_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m15_bars_since_beyond_high": 2.0,
 "f_swings__ema_trend": "up",
 "f_swings__ema_swing_agreement": "DISAGREE",
 "f_acceptance__break_found": false,
 "f_balance__range_width_atr_w24": 4.309891550854943,
 "f_balance__directional_efficiency_w24": 0.24291828340188906,
 "f_balance__bar_overlap_ratio_w24": 0.4748546307164919,
 "f_balance__close_occupancy_ratio_w24": 0.875,
 "f_balance__midpoint_cross_count_w24": 9.0,
 "f_balance__range_width_atr_w48": 6.212942959404042,
 "f_balance__directional_efficiency_w48": 0.08935933429445994,
 "f_balance__bar_overlap_ratio_w48": 0.47430055718681574,
 "f_balance__close_occupancy_ratio_w48": 0.5625,
 "f_balance__midpoint_cross_count_w48": 2.0,
 "f_participation__tick_activity_per_min": 2463.4,
 "f_participation__tick_activity_percentile": 1.0,
 "f_participation__tick_activity_z": 2.236989422365172,
 "f_participation__activity_vs_same_tod": 1.349216781684741,
 "f_participation__activity_vs_session_baseline": 3.7054753309265944,
 "f_participation__activity_acceleration": 0.8942209960795702,
 "f_participation__tod_baseline_n": 17,
 "f_participation__tod_baseline_oldest_ts_ns": 1768315500000000000,
 "m_warmup_ok": true,
 "m_bars_available": 53869,
 "m_prev_days_available": 239,
 "m_atr_ok": true,
 "m_min_bars_levels": 603,
 "m_min_bars_swings": 480,
 "m_min_bars_acceptance": 98,
 "m_min_bars_balance": 48,
 "m_min_prev_days_participation": 21,
 "m_event_price": 24841.26,
 "m_reference_level_id": "d2e75d357ad49764",
 "m_config_hash": "9baff67cbb414f2a",
 "m_hash_levels": "84a77f0a6266399f",
 "m_hash_swings": "eb1857c684e21d19",
 "m_hash_acceptance": "1608ec3fb935dc33ecc4874e1768289b3c0790e9bc3a0919f88e766e28f08060",
 "m_hash_balance": "ddac1af18d7032ca1ee3acf276d5f2ccd6101710a5bd8476158872b7ca8d3aec",
 "m_hash_participation": "452f5122fbfc26c12ed207281e1fcbd3152c3ecd265d25b5ce177c13529c92b3",
 "warmup_ok": true,
 "run_id": "11e7f0c949cd",
 "decision_idx": 53868,
 "strategy_id": "ORB-f881dee654",
 "entry": 24841.26,
 "stop": 24870.184107142857,
 "risk": 28.924107142858702,
 "risk_atr": 0.43523414912028,
 "partition": "TRAIN"
}
```

## SPX500

Rows: 2991 events + 44 controls; frame fingerprint matches manifest: True. Gate B compute 6.9 s.

### (ii) Leakage audit on real data

* live-style incremental pass, full history, physically truncated views (prefix, bar 0..i): compared 330, pass 330, fail 0, skipped 0  -> PASS
* raw frame truncated exactly at the decision bar, history window 7728 bars, rebuilt through the batch adapter: compared 60, pass 60, fail 0, skipped 0  -> PASS
* same window but +500 FUTURE bars present in the frame: compared 30, pass 30, fail 0, skipped 0  -> PASS
* shallow histories (120/240/500 bars): 120: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 240: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 500: n=20, flagged not-warm 20, wrongly warm 0, still equal 0

### (i) Plausibility (83 feature columns, 0 flagged) -> PASS

| group | columns | constant | all-missing | missing >= 50% | flagged |
|---|---|---|---|---|---|
| acceptance | 15 | 0 | 0 | 14 | 0 |
| balance | 10 | 0 | 0 | 0 | 0 |
| levels | 18 | 0 | 0 | 3 | 0 |
| participation | 8 | 0 | 0 | 0 | 0 |
| swings | 32 | 0 | 0 | 4 | 0 |

### (iii) Warm-up accounting

* events: 0/2991 warmup_ok=false (0.0%); controls: 0/44 (0.0%)
* share of events below the documented history requirement by group: levels 0.0%, swings 0.0%, acceptance 0.0%, balance 0.0%, participation 0.0%, atr 0.0%
* events warmup_ok=false by family: EOD 0.0%, GAP 0.0%, LEADLAG 0.0%, ORB 0.0%, OVERNIGHT 0.0%, ROUND 0.0%, VOLREV 0.0%

### (iv) Label sanity -> PASS

| label | ref P (zero drift) | events rate [Wilson95] (n) | controls rate [Wilson95] (n) | censored ev/ctl |
|---|---|---|---|---|
| y_fav025_before_adv025 | 0.500 | 0.380 [0.363,0.397] (2991) | 0.419 [0.284,0.567] (43) | 0.0% / 2.3% |
| y_fav050_before_adv050 | 0.500 | 0.476 [0.458,0.493] (2988) | 0.442 [0.304,0.589] (43) | 0.1% / 2.3% |
| y_fav075_before_adv050 | 0.400 | 0.402 [0.384,0.419] (2977) | 0.349 [0.224,0.498] (43) | 0.5% / 2.3% |
| y_fav100_before_adv050 | 0.333 | 0.345 [0.328,0.362] (2960) | 0.171 [0.085,0.313] (41) | 1.0% / 6.8% |

* consistency violations: 0 over 3035 rows; horizon max 14400 s (bound 14400), full-48-bar share 66.2%
* no label issue

### (v) Matching quality -> WARN (matching revision: partition-aware (match_controls partition=auto, rank_mode=partition, exclude_idx = every generator opportunity))

* events 2991, matched 44 (rate 0.015), unmatched 2947, controls 44; SMD {'local_minute': -0.15976762546407272, 'atr_pct': 0.11458240740781261, 'spread_pct': 0.07583974696975536}; max session-share difference 0.000; controls in a different partition than their event: 0
* match rate by family: VOLREV 26/1040, ROUND 13/708, GAP 0/164, ORB 0/587, LEADLAG 0/184, EOD 5/150, OVERNIGHT 0/158
* reasons: ['match rate 0.015 < 0.5', '|SMD(local_minute)|=-0.15976762546407272 > 0.1', '|SMD(atr_pct)|=0.11458240740781261 > 0.1']
* PRE-REVISION controls (observer-controls-1, whole-sample ranks): 47 controls, match rate 0.0157, SMD {'local_minute': -0.198959774256959, 'atr_pct': 0.0431533734292116, 'spread_pct': 0.09761269336425604}; REVISED: 44 controls, match rate 0.014710799063858242
* control label rates pre -> revised: fav025_before_adv025 0.489 (n=47) -> 0.419 (n=43), fav050_before_adv050 0.511 (n=45) -> 0.442 (n=43), fav075_before_adv050 0.333 (n=45) -> 0.349 (n=43), fav100_before_adv050 0.200 (n=45) -> 0.171 (n=41)

### (vii) Coverage

* bars 2025-05-02T09:40:00+00:00 .. 2026-08-31T20:55:00+00:00 (94212 bars); gaps 346 (intraday > 3 bars: 272, largest 56.83 h); missing UTC months: none; gaps > 3 days: none
* dev-end guard: {'dev_end_berlin_date': '2026-08-31', 'assert_no_forward_holdout': 'PASS', 'guard_dev_only': 'PASS', 'last_bar_utc': '2026-08-31T20:55:00+00:00'}
* partitions: {'FROZEN_OOS': 396, 'TRAIN': 1843, 'VALIDATION': 752} (plan {'train_start': '2025-05-02', 'train_end': '2026-03-16', 'validation_start': '2026-03-17', 'validation_end': '2026-06-30', 'oos_start': '2026-07-01', 'oos_end': '2026-08-31', 'forward_start': '2026-09-01', 'core_fit_end': '2026-06-30'}; frozen split exists: True). core thresholds were fitted up to the core fit end; the dev window 2026-07-01..2026-08-31 is a weaker OOS than a never-seen period
* events by family|variant|direction: {'EOD|continue|long': 55, 'EOD|continue|short': 20, 'EOD|reverse|long': 20, 'EOD|reverse|short': 55, 'GAP|fade|long': 29, 'GAP|fade|short': 45, 'GAP|go|long': 58, 'GAP|go|short': 32, 'LEADLAG||long': 80, 'LEADLAG||short': 104, 'ORB|breakout|long': 175, 'ORB|breakout|short': 147, 'ORB|fade|long': 135, 'ORB|fade|short': 130, 'OVERNIGHT|continue|long': 54, 'OVERNIGHT|continue|short': 25, 'OVERNIGHT|reverse|long': 25, 'OVERNIGHT|reverse|short': 54, 'ROUND|break|long': 104, 'ROUND|break|short': 114, 'ROUND|reject|long': 243, 'ROUND|reject|short': 247, 'VOLREV|expand|long': 459, 'VOLREV|expand|short': 340, 'VOLREV|fade|long': 140, 'VOLREV|fade|short': 101}
* candidate exclusions: {'candidates': 3209, 'before_eval_start': 218, 'emitted': 2991, 'emitted_before_limit': 2991}
* runtime s {'events_step': 236.7, 'controls_step': 96.1}, peak memory MB {'events_step': 268.2, 'controls_step': 221.4}

### Sample event record (features at the decision bar, non-null)

```json
{
 "event_id": "efaa92680b7efb4a0ee12ef3ea28f9f7",
 "market": "SPX500",
 "family": "LEADLAG",
 "direction": -1,
 "is_control": false,
 "decision_ts_ns": 1769179200000000000,
 "observer_version": "market-structure-observer-v1",
 "schema_version": "mso-schema-1",
 "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
 "v_acceptance": "mso-acceptance-1",
 "v_balance": "mso-balance-1",
 "v_levels": "mso-levels-1",
 "v_participation": "mso-participation-1",
 "v_swings": "mso-swings-1",
 "f_levels__nearest_level_distance_atr": -0.08969010727062468,
 "f_levels__nearest_level_source": "SWING_M5",
 "f_levels__nearest_level_age_bars": 11,
 "f_levels__touch_count": 3,
 "f_levels__clean_rejection_count": 1,
 "f_levels__penetration_count": 3,
 "f_levels__first_touch": false,
 "f_levels__break_count": 0,
 "f_levels__reclaim_count": 0,
 "f_levels__role": "SUPPORT",
 "f_levels__zone_source_count": 12,
 "f_levels__zone_independent_source_count": 4,
 "f_levels__zone_width_atr": 0.9490464839093042,
 "f_levels__n_levels_within_1atr": 17,
 "f_levels__last_touch_ts_ns": 1.7691792e+18,
 "f_swings__m5_sequence": "DOWN_SEQUENCE",
 "f_swings__m5_high_label": "LH",
 "f_swings__m5_low_label": "LL",
 "f_swings__m5_sequence_length": 3,
 "f_swings__m5_structure_age_bars": 5.0,
 "f_swings__m5_structure_age_minutes": 25.0,
 "f_swings__m5_high_delta_atr": -1.7750297973779512,
 "f_swings__m5_low_delta_atr": -0.6486889153754061,
 "f_swings__m5_confirmed_at_ts_ns": 1.7691777e+18,
 "f_swings__m5_last_high_confirmed_at_ts_ns": 1.7691765e+18,
 "f_swings__m5_last_low_confirmed_at_ts_ns": 1.7691777e+18,
 "f_swings__m5_close_beyond_last_swing_atr_high": 0.0,
 "f_swings__m5_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m5_bars_since_beyond_high": 2.0,
 "f_swings__m15_sequence": "DOWN_SEQUENCE",
 "f_swings__m15_high_label": "LH",
 "f_swings__m15_low_label": "LL",
 "f_swings__m15_sequence_length": 2,
 "f_swings__m15_structure_age_bars": 4.0,
 "f_swings__m15_structure_age_minutes": 70.0,
 "f_swings__m15_high_delta_atr": -0.25238379022647833,
 "f_swings__m15_low_delta_atr": -0.14183551847444095,
 "f_swings__m15_confirmed_at_ts_ns": 1.769175e+18,
 "f_swings__m15_last_high_confirmed_at_ts_ns": 1.769175e+18,
 "f_swings__m15_last_low_confirmed_at_ts_ns": 1.7691741e+18,
 "f_swings__m15_close_beyond_last_swing_atr_high": 0.0,
 "f_swings__m15_close_beyond_last_swing_atr_low": -0.7404648390942289,
 "f_swings__m15_bars_since_beyond_low": 14.0,
 "f_swings__ema_trend": "down",
 "f_swings__ema_swing_agreement": "AGREE_DOWN",
 "f_acceptance__break_found": false,
 "f_balance__range_width_atr_w24": 3.6585220500597417,
 "f_balance__directional_efficiency_w24": 0.23645220215142393,
 "f_balance__bar_overlap_ratio_w24": 0.4694855719740131,
 "f_balance__close_occupancy_ratio_w24": 0.5,
 "f_balance__midpoint_cross_count_w24": 3.0,
 "f_balance__range_width_atr_w48": 3.91090584028622,
 "f_balance__directional_efficiency_w48": 0.008765597607501662,
 "f_balance__bar_overlap_ratio_w48": 0.4516705465363459,
 "f_balance__close_occupancy_ratio_w48": 0.5833333333333334,
 "f_balance__midpoint_cross_count_w48": 10.0,
 "f_participation__tick_activity_per_min": 794.2,
 "f_participation__tick_activity_percentile": 0.625,
 "f_participation__tick_activity_z": 0.1491926444751944,
 "f_participation__activity_vs_same_tod": 1.1311778948867683,
 "f_participation__activity_vs_session_baseline": 8.466950959488273,
 "f_participation__activity_acceleration": 4.260729613733906,
 "f_participation__tod_baseline_n": 16,
 "f_participation__tod_baseline_oldest_ts_ns": 1767191700000000000,
 "m_warmup_ok": true,
 "m_bars_available": 51382,
 "m_prev_days_available": 228,
 "m_atr_ok": true,
 "m_min_bars_levels": 603,
 "m_min_bars_swings": 480,
 "m_min_bars_acceptance": 98,
 "m_min_bars_balance": 48,
 "m_min_prev_days_participation": 21,
 "m_event_price": 6900.48,
 "m_reference_level_id": "91e45fa758249c87",
 "m_config_hash": "9baff67cbb414f2a",
 "m_hash_levels": "84a77f0a6266399f",
 "m_hash_swings": "eb1857c684e21d19",
 "m_hash_acceptance": "1608ec3fb935dc33ecc4874e1768289b3c0790e9bc3a0919f88e766e28f08060",
 "m_hash_balance": "ddac1af18d7032ca1ee3acf276d5f2ccd6101710a5bd8476158872b7ca8d3aec",
 "m_hash_participation": "452f5122fbfc26c12ed207281e1fcbd3152c3ecd265d25b5ce177c13529c92b3",
 "warmup_ok": true,
 "run_id": "a7ccbdaa4a23",
 "decision_idx": 51381,
 "strategy_id": "LEADLAG-c3be0a92bd",
 "entry": 6900.48,
 "stop": 6907.671428571428,
 "risk": 7.191428571428332,
 "risk_atr": 1.5000000000000133,
 "partition": "TRAIN"
}
```

## XAUUSD

Rows: 7181 events + 0 controls; frame fingerprint matches manifest: True. Gate B compute 2.8 s.

### (ii) Leakage audit on real data

* live-style incremental pass, full history, physically truncated views (prefix, bar 0..i): compared 300, pass 300, fail 0, skipped 0  -> PASS
* raw frame truncated exactly at the decision bar, history window 7728 bars, rebuilt through the batch adapter: compared 60, pass 60, fail 0, skipped 0  -> PASS
* same window but +500 FUTURE bars present in the frame: compared 30, pass 30, fail 0, skipped 0  -> PASS
* shallow histories (120/240/500 bars): 120: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 240: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 500: n=20, flagged not-warm 20, wrongly warm 0, still equal 0

### (i) Plausibility (83 feature columns, 0 flagged) -> PASS

| group | columns | constant | all-missing | missing >= 50% | flagged |
|---|---|---|---|---|---|
| acceptance | 15 | 0 | 0 | 14 | 0 |
| balance | 10 | 0 | 0 | 0 | 0 |
| levels | 18 | 0 | 0 | 1 | 0 |
| participation | 8 | 0 | 0 | 0 | 0 |
| swings | 32 | 0 | 0 | 4 | 0 |

### (iii) Warm-up accounting

* events: 0/7181 warmup_ok=false (0.0%); controls: 0/0 (n/a)
* share of events below the documented history requirement by group: levels 0.0%, swings 0.0%, acceptance 0.0%, balance 0.0%, participation 0.0%, atr 0.0%
* events warmup_ok=false by family: EOD 0.0%, GAP 0.0%, ORB 0.0%, OVERNIGHT 0.0%, ROUND 0.0%, VOLREV 0.0%

### (iv) Label sanity -> PASS

| label | ref P (zero drift) | events rate [Wilson95] (n) | controls rate [Wilson95] (n) | censored ev/ctl |
|---|---|---|---|---|
| y_fav025_before_adv025 | 0.500 | 0.388 [0.376,0.399] (7181) | n/a | 0.0% / n/a |
| y_fav050_before_adv050 | 0.500 | 0.479 [0.467,0.491] (7180) | n/a | 0.0% / n/a |
| y_fav075_before_adv050 | 0.400 | 0.388 [0.377,0.400] (7178) | n/a | 0.0% / n/a |
| y_fav100_before_adv050 | 0.333 | 0.324 [0.314,0.335] (7176) | n/a | 0.1% / n/a |

* consistency violations: 0 over 7181 rows; horizon max 14400 s (bound 14400), full-48-bar share 70.3%
* no label issue

### (v) Matching quality -> WARN (matching revision: partition-aware (match_controls partition=auto, rank_mode=partition, exclude_idx = every generator opportunity))

* events 7181, matched 0 (rate 0.000), unmatched 7181, controls 0; SMD {'local_minute': None, 'atr_pct': None, 'spread_pct': None}; max session-share difference 0.000; controls in a different partition than their event: 0
* match rate by family: VOLREV 0/1265, ROUND 0/4844, ORB 0/601, GAP 0/157, OVERNIGHT 0/158, EOD 0/156
* reasons: ['match rate 0.000 < 0.5', '|SMD(local_minute)|=None > 0.1', '|SMD(atr_pct)|=None > 0.1', '|SMD(spread_pct)|=None > 0.1']
* PRE-REVISION controls (observer-controls-1, whole-sample ranks): 0 controls, match rate 0.0000, SMD {'local_minute': None, 'atr_pct': None, 'spread_pct': None}; REVISED: 0 controls, match rate 0.0
* control label rates pre -> revised: 

### (vii) Coverage

* bars 2025-05-05T02:25:00+00:00 .. 2026-08-31T20:55:00+00:00 (94194 bars); gaps 342 (intraday > 3 bars: 271, largest 73.08 h); missing UTC months: none; gaps > 3 days: [['2026-04-02T20:55:00+00:00', 73.1]]
* dev-end guard: {'dev_end_berlin_date': '2026-08-31', 'assert_no_forward_holdout': 'PASS', 'guard_dev_only': 'PASS', 'last_bar_utc': '2026-08-31T20:55:00+00:00'}
* partitions: {'FROZEN_OOS': 1036, 'TRAIN': 4255, 'VALIDATION': 1890} (plan {'train_start': '2025-05-05', 'train_end': '2026-03-16', 'validation_start': '2026-03-17', 'validation_end': '2026-06-30', 'oos_start': '2026-07-01', 'oos_end': '2026-08-31', 'forward_start': '2026-09-01', 'core_fit_end': '2026-06-30'}; frozen split exists: True). core thresholds were fitted up to the core fit end; the dev window 2026-07-01..2026-08-31 is a weaker OOS than a never-seen period
* events by family|variant|direction: {'EOD|continue|long': 47, 'EOD|continue|short': 31, 'EOD|reverse|long': 31, 'EOD|reverse|short': 47, 'GAP|fade|long': 36, 'GAP|fade|short': 41, 'GAP|go|long': 51, 'GAP|go|short': 29, 'ORB|breakout|long': 169, 'ORB|breakout|short': 150, 'ORB|fade|long': 135, 'ORB|fade|short': 147, 'OVERNIGHT|continue|long': 49, 'OVERNIGHT|continue|short': 30, 'OVERNIGHT|reverse|long': 30, 'OVERNIGHT|reverse|short': 49, 'ROUND|break|long': 824, 'ROUND|break|short': 796, 'ROUND|reject|long': 1649, 'ROUND|reject|short': 1575, 'VOLREV|expand|long': 498, 'VOLREV|expand|short': 421, 'VOLREV|fade|long': 182, 'VOLREV|fade|short': 164}
* candidate exclusions: {'candidates': 7663, 'before_eval_start': 482, 'emitted': 7181, 'emitted_before_limit': 7181}
* runtime s {'events_step': 327.0, 'controls_step': 7.8}, peak memory MB {'events_step': 268.2, 'controls_step': 221.4}

### Sample event record (features at the decision bar, non-null)

```json
{
 "event_id": "753179baed5ee43e25934b0505b91728",
 "market": "XAUUSD",
 "family": "ORB",
 "variant": "breakout",
 "direction": 1.0,
 "is_control": false,
 "decision_ts_ns": 1.770366e+18,
 "observer_version": "market-structure-observer-v1",
 "schema_version": "mso-schema-1",
 "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
 "v_acceptance": "mso-acceptance-1",
 "v_balance": "mso-balance-1",
 "v_levels": "mso-levels-1",
 "v_participation": "mso-participation-1",
 "v_swings": "mso-swings-1",
 "f_levels__nearest_level_distance_atr": 0.007678595913883065,
 "f_levels__nearest_level_source": "SWING_M5",
 "f_levels__nearest_level_age_bars": 11.0,
 "f_levels__touch_count": 3.0,
 "f_levels__clean_rejection_count": 0.0,
 "f_levels__penetration_count": 2.0,
 "f_levels__first_touch": false,
 "f_levels__break_count": 0.0,
 "f_levels__reclaim_count": 0.0,
 "f_levels__role": "RESISTANCE",
 "f_levels__zone_source_count": 74.0,
 "f_levels__zone_independent_source_count": 6.0,
 "f_levels__zone_width_atr": 17.53948306595366,
 "f_levels__n_levels_within_1atr": 13.0,
 "f_levels__last_touch_ts_ns": 1.7703657e+18,
 "f_swings__m5_sequence": "MIXED_TRANSITION",
 "f_swings__m5_high_label": "LH",
 "f_swings__m5_low_label": "HL",
 "f_swings__m5_sequence_length": 0.0,
 "f_swings__m5_structure_age_bars": 1.0,
 "f_swings__m5_structure_age_minutes": 5.0,
 "f_swings__m5_high_delta_atr": -0.18332647744411093,
 "f_swings__m5_low_delta_atr": 0.3868092691621867,
 "f_swings__m5_confirmed_at_ts_ns": 1.7703657e+18,
 "f_swings__m5_last_high_confirmed_at_ts_ns": 1.7703645e+18,
 "f_swings__m5_last_low_confirmed_at_ts_ns": 1.7703657e+18,
 "f_swings__m5_close_beyond_last_swing_atr_high": 0.0,
 "f_swings__m5_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m15_sequence": "MIXED_TRANSITION",
 "f_swings__m15_high_label": "LH",
 "f_swings__m15_low_label": "HL",
 "f_swings__m15_sequence_length": 0.0,
 "f_swings__m15_structure_age_bars": 1.0,
 "f_swings__m15_structure_age_minutes": 20.0,
 "f_swings__m15_high_delta_atr": -3.9957493486905444,
 "f_swings__m15_low_delta_atr": 1.6710544357603079,
 "f_swings__m15_confirmed_at_ts_ns": 1.7703648e+18,
 "f_swings__m15_last_high_confirmed_at_ts_ns": 1.7703648e+18,
 "f_swings__m15_last_low_confirmed_at_ts_ns": 1.770363e+18,
 "f_swings__m15_close_beyond_last_swing_atr_high": 0.0,
 "f_swings__m15_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__ema_trend": "down",
 "f_swings__ema_swing_agreement": "UNDEFINED",
 "f_acceptance__break_found": false,
 "f_balance__range_width_atr_w24": 4.1733168791992865,
 "f_balance__directional_efficiency_w24": 0.012321943546271528,
 "f_balance__bar_overlap_ratio_w24": 0.44460035433264306,
 "f_balance__close_occupancy_ratio_w24": 0.9583333333333334,
 "f_balance__midpoint_cross_count_w24": 8.0,
 "f_balance__range_width_atr_w48": 9.237350884409794,
 "f_balance__directional_efficiency_w48": 0.145943966306539,
 "f_balance__bar_overlap_ratio_w48": 0.41646010372788134,
 "f_balance__close_occupancy_ratio_w48": 0.7083333333333334,
 "f_balance__midpoint_cross_count_w48": 6.0,
 "f_participation__tick_activity_per_min": 352.0,
 "f_participation__tick_activity_percentile": 0.47058823529411764,
 "f_participation__tick_activity_z": -0.3021239980164353,
 "f_participation__activity_vs_same_tod": 0.9482758620689655,
 "f_participation__activity_vs_session_baseline": 0.5615826419910658,
 "f_participation__activity_acceleration": 0.7227926078028748,
 "f_participation__tod_baseline_n": 17.0,
 "f_participation__tod_baseline_oldest_ts_ns": 1.7683785e+18,
 "m_warmup_ok": true,
 "m_bars_available": 54166.0,
 "m_prev_days_available": 238.0,
 "m_atr_ok": true,
 "m_min_bars_levels": 603.0,
 "m_min_bars_swings": 480.0,
 "m_min_bars_acceptance": 98.0,
 "m_min_bars_balance": 48.0,
 "m_min_prev_days_participation": 21.0,
 "m_event_price": 4854.79,
 "m_reference_level_id": "ed90cc212923fbfd",
 "m_config_hash": "75802aa12e9c221a",
 "m_hash_levels": "ea89adc947741019",
 "m_hash_swings": "eb1857c684e21d19",
 "m_hash_acceptance": "1608ec3fb935dc33ecc4874e1768289b3c0790e9bc3a0919f88e766e28f08060",
 "m_hash_balance": "ddac1af18d7032ca1ee3acf276d5f2ccd6101710a5bd8476158872b7ca8d3aec",
 "m_hash_participation": "452f5122fbfc26c12ed207281e1fcbd3152c3ecd265d25b5ce177c13529c92b3",
 "warmup_ok": true,
 "run_id": "146ff3fa85df",
 "decision_idx": 54165.0,
 "strategy_id": "ORB-c3663c7d30",
 "entry": 4854.79,
 "stop": 4829.15,
 "risk": 25.640000000000327,
 "risk_atr": 2.460989990401792,
 "partition": "TRAIN"
}
```

## EURUSD

Rows: 2868 events + 31 controls; frame fingerprint matches manifest: True. Gate B compute 2.8 s.

### (ii) Leakage audit on real data

* live-style incremental pass, full history, physically truncated views (prefix, bar 0..i): compared 330, pass 330, fail 0, skipped 0  -> PASS
* raw frame truncated exactly at the decision bar, history window 8064 bars, rebuilt through the batch adapter: compared 60, pass 60, fail 0, skipped 0  -> PASS
* same window but +500 FUTURE bars present in the frame: compared 30, pass 30, fail 0, skipped 0  -> PASS
* shallow histories (120/240/500 bars): 120: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 240: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 500: n=20, flagged not-warm 20, wrongly warm 0, still equal 0

### (i) Plausibility (83 feature columns, 0 flagged) -> PASS

| group | columns | constant | all-missing | missing >= 50% | flagged |
|---|---|---|---|---|---|
| acceptance | 15 | 0 | 0 | 14 | 0 |
| balance | 10 | 0 | 0 | 0 | 0 |
| levels | 18 | 0 | 0 | 1 | 0 |
| participation | 8 | 0 | 0 | 0 | 0 |
| swings | 32 | 0 | 0 | 4 | 0 |

### (iii) Warm-up accounting

* events: 0/2868 warmup_ok=false (0.0%); controls: 0/31 (0.0%)
* share of events below the documented history requirement by group: levels 0.0%, swings 0.0%, acceptance 0.0%, balance 0.0%, participation 0.0%, atr 0.0%
* events warmup_ok=false by family: EOD 0.0%, GAP 0.0%, ORB 0.0%, OVERNIGHT 0.0%, ROUND 0.0%, VOLREV 0.0%

### (iv) Label sanity -> PASS

| label | ref P (zero drift) | events rate [Wilson95] (n) | controls rate [Wilson95] (n) | censored ev/ctl |
|---|---|---|---|---|
| y_fav025_before_adv025 | 0.500 | 0.403 [0.385,0.421] (2868) | 0.484 [0.320,0.652] (31) | 0.0% / 0.0% |
| y_fav050_before_adv050 | 0.500 | 0.478 [0.460,0.496] (2865) | 0.484 [0.320,0.652] (31) | 0.1% / 0.0% |
| y_fav075_before_adv050 | 0.400 | 0.379 [0.362,0.397] (2862) | 0.452 [0.292,0.622] (31) | 0.2% / 0.0% |
| y_fav100_before_adv050 | 0.333 | 0.312 [0.295,0.329] (2853) | 0.290 [0.161,0.466] (31) | 0.5% / 0.0% |

* consistency violations: 0 over 2899 rows; horizon max 14400 s (bound 14400), full-48-bar share 74.0%
* no label issue

### (v) Matching quality -> WARN (matching revision: partition-aware (match_controls partition=auto, rank_mode=partition, exclude_idx = every generator opportunity))

* events 2868, matched 31 (rate 0.011), unmatched 2837, controls 31; SMD {'local_minute': -0.8001313827447635, 'atr_pct': -0.38618400415438464, 'spread_pct': 0.4826593294882545}; max session-share difference 0.000; controls in a different partition than their event: 0
* match rate by family: ROUND 13/566, GAP 0/150, OVERNIGHT 0/148, ORB 0/568, VOLREV 16/1282, EOD 2/154
* reasons: ['match rate 0.011 < 0.5', '|SMD(local_minute)|=-0.8001313827447635 > 0.1', '|SMD(atr_pct)|=-0.38618400415438464 > 0.1', '|SMD(spread_pct)|=0.4826593294882545 > 0.1']
* PRE-REVISION controls (observer-controls-1, whole-sample ranks): 31 controls, match rate 0.0108, SMD {'local_minute': -0.868197421981565, 'atr_pct': -0.4340936143474357, 'spread_pct': 0.5318267367628551}; REVISED: 31 controls, match rate 0.010808926080892609
* control label rates pre -> revised: fav025_before_adv025 0.323 (n=31) -> 0.484 (n=31), fav050_before_adv050 0.452 (n=31) -> 0.484 (n=31), fav075_before_adv050 0.323 (n=31) -> 0.452 (n=31), fav100_before_adv050 0.258 (n=31) -> 0.290 (n=31)

### (vii) Coverage

* bars 2025-05-26T22:00:00+00:00 .. 2026-08-31T21:55:00+00:00 (94458 bars); gaps 72 (intraday > 3 bars: 0, largest 49.08 h); missing UTC months: none; gaps > 3 days: none
* dev-end guard: {'dev_end_berlin_date': '2026-08-31', 'assert_no_forward_holdout': 'PASS', 'guard_dev_only': 'PASS', 'last_bar_utc': '2026-08-31T21:55:00+00:00'}
* partitions: {'FROZEN_OOS': 390, 'TRAIN': 1719, 'VALIDATION': 759} (plan {'train_start': '2025-05-27', 'train_end': '2026-03-22', 'validation_start': '2026-03-23', 'validation_end': '2026-06-30', 'oos_start': '2026-07-01', 'oos_end': '2026-08-31', 'forward_start': '2026-09-01', 'core_fit_end': '2026-06-30'}; frozen split exists: True). core thresholds were fitted up to the core fit end; the dev window 2026-07-01..2026-08-31 is a weaker OOS than a never-seen period
* events by family|variant|direction: {'EOD|continue|long': 40, 'EOD|continue|short': 37, 'EOD|reverse|long': 37, 'EOD|reverse|short': 40, 'GAP|fade|long': 40, 'GAP|fade|short': 38, 'GAP|go|long': 33, 'GAP|go|short': 39, 'ORB|breakout|long': 147, 'ORB|breakout|short': 159, 'ORB|fade|long': 136, 'ORB|fade|short': 126, 'OVERNIGHT|continue|long': 39, 'OVERNIGHT|continue|short': 35, 'OVERNIGHT|reverse|long': 35, 'OVERNIGHT|reverse|short': 39, 'ROUND|break|long': 74, 'ROUND|break|short': 85, 'ROUND|reject|long': 212, 'ROUND|reject|short': 195, 'VOLREV|expand|long': 491, 'VOLREV|expand|short': 482, 'VOLREV|fade|long': 161, 'VOLREV|fade|short': 148}
* candidate exclusions: {'candidates': 3104, 'before_eval_start': 236, 'emitted': 2868, 'emitted_before_limit': 2868}
* runtime s {'events_step': 297.6, 'controls_step': 83.6}, peak memory MB {'events_step': 268.2, 'controls_step': 221.4}

### Sample event record (features at the decision bar, non-null)

```json
{
 "event_id": "083730934bfdec8f14486f4276681e43",
 "market": "EURUSD",
 "family": "VOLREV",
 "variant": "expand",
 "direction": 1,
 "is_control": false,
 "decision_ts_ns": 1770806700000000000,
 "observer_version": "market-structure-observer-v1",
 "schema_version": "mso-schema-1",
 "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
 "v_acceptance": "mso-acceptance-1",
 "v_balance": "mso-balance-1",
 "v_levels": "mso-levels-1",
 "v_participation": "mso-participation-1",
 "v_swings": "mso-swings-1",
 "f_levels__nearest_level_distance_atr": -0.24623115577895335,
 "f_levels__nearest_level_source": "SWING_M15",
 "f_levels__nearest_level_age_bars": 459,
 "f_levels__touch_count": 4,
 "f_levels__clean_rejection_count": 1,
 "f_levels__penetration_count": 3,
 "f_levels__first_touch": false,
 "f_levels__break_count": 2,
 "f_levels__reclaim_count": 2,
 "f_levels__role": "RECLAIMED_FROM_ABOVE",
 "f_levels__previous_role": "BROKEN_UP",
 "f_levels__zone_source_count": 190,
 "f_levels__zone_independent_source_count": 6,
 "f_levels__zone_width_atr": 15.20452261306636,
 "f_levels__n_levels_within_1atr": 6,
 "f_levels__last_touch_ts_ns": 1.7708067e+18,
 "f_levels__last_break_ts_ns": 1.7707902e+18,
 "f_levels__last_reclaim_ts_ns": 1.7707911e+18,
 "f_swings__m5_sequence": "RANGE_OR_UNDEFINED",
 "f_swings__m5_high_label": "HH",
 "f_swings__m5_low_label": "EQ",
 "f_swings__m5_sequence_length": 0,
 "f_swings__m5_structure_age_bars": 2,
 "f_swings__m5_structure_age_minutes": 10.0,
 "f_swings__m5_high_delta_atr": 0.28140703517616983,
 "f_swings__m5_low_delta_atr": 0.035175879397216496,
 "f_swings__m5_confirmed_at_ts_ns": 1770806100000000000,
 "f_swings__m5_last_high_confirmed_at_ts_ns": 1770804900000000000,
 "f_swings__m5_last_low_confirmed_at_ts_ns": 1770806100000000000,
 "f_swings__m5_close_beyond_last_swing_atr_high": 1.0904522613066818,
 "f_swings__m5_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m5_bars_since_beyond_high": 0.0,
 "f_swings__m15_sequence": "DOWN_SEQUENCE",
 "f_swings__m15_high_label": "LH",
 "f_swings__m15_low_label": "LL",
 "f_swings__m15_sequence_length": 3,
 "f_swings__m15_structure_age_bars": 4,
 "f_swings__m15_structure_age_minutes": 60.0,
 "f_swings__m15_high_delta_atr": -0.14070351758808491,
 "f_swings__m15_low_delta_atr": -0.42211055276425474,
 "f_swings__m15_confirmed_at_ts_ns": 1770803100000000000,
 "f_swings__m15_last_high_confirmed_at_ts_ns": 1770803100000000000,
 "f_swings__m15_last_low_confirmed_at_ts_ns": 1770803100000000000,
 "f_swings__m15_close_beyond_last_swing_atr_high": 1.1608040201011147,
 "f_swings__m15_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m15_bars_since_beyond_high": 0.0,
 "f_swings__ema_trend": "up",
 "f_swings__ema_swing_agreement": "UNDEFINED",
 "f_acceptance__break_found": false,
 "f_balance__range_width_atr_w24": 5.0301507537691545,
 "f_balance__directional_efficiency_w24": 0.233226837060818,
 "f_balance__bar_overlap_ratio_w24": 0.5584010201993739,
 "f_balance__close_occupancy_ratio_w24": 0.8333333333333334,
 "f_balance__midpoint_cross_count_w24": 5,
 "f_balance__range_width_atr_w48": 5.0301507537691545,
 "f_balance__directional_efficiency_w48": 0.14044943820229328,
 "f_balance__bar_overlap_ratio_w48": 0.5504552687825003,
 "f_balance__close_occupancy_ratio_w48": 0.7916666666666666,
 "f_balance__midpoint_cross_count_w48": 9,
 "f_participation__tick_activity_per_min": 98.8,
 "f_participation__tick_activity_percentile": 0.5294117647058824,
 "f_participation__tick_activity_z": -0.01629889992814761,
 "f_participation__activity_vs_same_tod": 1.1076233183856503,
 "f_participation__activity_vs_session_baseline": 1.5608214849921012,
 "f_participation__activity_acceleration": 1.54375,
 "f_participation__tod_baseline_n": 17,
 "f_participation__tod_baseline_oldest_ts_ns": 1768819200000000000,
 "m_warmup_ok": true,
 "m_bars_available": 53133,
 "m_prev_days_available": 224,
 "m_atr_ok": true,
 "m_min_bars_levels": 603,
 "m_min_bars_swings": 480,
 "m_min_bars_acceptance": 98,
 "m_min_bars_balance": 48,
 "m_min_prev_days_participation": 21,
 "m_event_price": 1.19226,
 "m_reference_level_id": "6a8908d5f98459a8",
 "m_config_hash": "3880e131817c8caf",
 "m_hash_levels": "304b356dae8d39d4",
 "m_hash_swings": "eb1857c684e21d19",
 "m_hash_acceptance": "1608ec3fb935dc33ecc4874e1768289b3c0790e9bc3a0919f88e766e28f08060",
 "m_hash_balance": "ddac1af18d7032ca1ee3acf276d5f2ccd6101710a5bd8476158872b7ca8d3aec",
 "m_hash_participation": "452f5122fbfc26c12ed207281e1fcbd3152c3ecd265d25b5ce177c13529c92b3",
 "warmup_ok": true,
 "run_id": "42b3c47f7d89",
 "decision_idx": 53132,
 "strategy_id": "VOLREV-18ead2008e",
 "entry": 1.19226,
 "stop": 1.1918335714285715,
 "risk": 0.00042642857142860535,
 "risk_atr": 1.5000000000001674,
 "partition": "TRAIN"
}
```

## BTCUSD

Rows: 2363 events + 2303 controls; frame fingerprint matches manifest: True. Gate B compute 2.2 s.

### (ii) Leakage audit on real data

* live-style incremental pass, full history, physically truncated views (prefix, bar 0..i): compared 330, pass 330, fail 0, skipped 0  -> PASS
* raw frame truncated exactly at the decision bar, history window 8064 bars, rebuilt through the batch adapter: compared 60, pass 60, fail 0, skipped 0  -> PASS
* same window but +500 FUTURE bars present in the frame: compared 30, pass 30, fail 0, skipped 0  -> PASS
* shallow histories (120/240/500 bars): 120: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 240: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 500: n=20, flagged not-warm 20, wrongly warm 0, still equal 1

### (i) Plausibility (83 feature columns, 0 flagged) -> PASS

| group | columns | constant | all-missing | missing >= 50% | flagged |
|---|---|---|---|---|---|
| acceptance | 15 | 0 | 0 | 14 | 0 |
| balance | 10 | 0 | 0 | 0 | 0 |
| levels | 18 | 0 | 0 | 1 | 0 |
| participation | 8 | 0 | 0 | 0 | 0 |
| swings | 32 | 0 | 0 | 4 | 0 |

### (iii) Warm-up accounting

* events: 97/2363 warmup_ok=false (4.1%); controls: 191/2303 (8.3%)
* share of events below the documented history requirement by group: levels 0.0%, swings 0.0%, acceptance 0.0%, balance 0.0%, participation 4.1%, atr 0.0%
* events warmup_ok=false by family: STRUCT 4.1%

### (iv) Label sanity -> PASS

| label | ref P (zero drift) | events rate [Wilson95] (n) | controls rate [Wilson95] (n) | censored ev/ctl |
|---|---|---|---|---|
| y_fav025_before_adv025 | 0.500 | 0.478 [0.458,0.499] (2350) | 0.471 [0.451,0.492] (2263) | 0.6% / 1.7% |
| y_fav050_before_adv050 | 0.500 | 0.490 [0.469,0.511] (2231) | 0.492 [0.471,0.513] (2153) | 5.6% / 6.5% |
| y_fav075_before_adv050 | 0.400 | 0.386 [0.366,0.407] (2166) | 0.396 [0.375,0.417] (2070) | 8.3% / 10.1% |
| y_fav100_before_adv050 | 0.333 | 0.313 [0.293,0.333] (2089) | 0.322 [0.302,0.343] (1982) | 11.6% / 13.9% |

* consistency violations: 0 over 4666 rows; horizon max 14400 s (bound 14400), full-48-bar share 84.5%
* no label issue

### (v) Matching quality -> PASS (matching revision: partition-aware (match_controls partition=auto, rank_mode=partition, exclude_idx = every generator opportunity))

* events 2363, matched 2303 (rate 0.975), unmatched 60, controls 2303; SMD {'local_minute': 0.00018258497080635033, 'atr_pct': -0.015385589876532423, 'spread_pct': 0.015284728639857878}; max session-share difference 0.000; controls in a different partition than their event: 0
* match rate by family: STRUCT 2303/2363

### (vii) Coverage

* bars 2025-09-24T10:45:00+00:00 .. 2026-08-31T21:55:00+00:00 (75015 bars); gaps 44 (intraday > 3 bars: 0, largest 752.08 h); missing UTC months: ['2025-10', '2026-03']; gaps > 3 days: [['2025-09-30T23:55:00+00:00', 752.1], ['2026-02-28T23:55:00+00:00', 744.1]]
* dev-end guard: {'dev_end_berlin_date': '2026-08-31', 'assert_no_forward_holdout': 'PASS', 'guard_dev_only': 'PASS', 'last_bar_utc': '2026-08-31T21:55:00+00:00'}
* partitions: {'EMBARGO': 4, 'FROZEN_OOS': 525, 'PURGED': 8, 'TRAIN': 1347, 'VALIDATION': 479} (plan {'train_start': '2025-09-24', 'train_end': '2026-05-06', 'validation_start': '2026-05-07', 'validation_end': '2026-06-30', 'oos_start': '2026-07-01', 'oos_end': '2026-08-31', 'forward_start': '2026-09-01', 'core_fit_end': '2026-06-30'}; frozen split exists: False). BTCUSD/BRENT: NO frozen split exists (short history, placeholder STRUCT constants); the boundaries above are the repo's generic dev dates, TRAIN/VALIDATION here are NOT a frozen train/validation of any fitted parameter
* events by family|variant|direction: {'STRUCT|breakout|long': 373, 'STRUCT|breakout|short': 402, 'STRUCT|confirmed|long': 299, 'STRUCT|confirmed|short': 302, 'STRUCT|fade|long': 282, 'STRUCT|fade|short': 224, 'STRUCT|retest|long': 242, 'STRUCT|retest|short': 239}
* candidate exclusions: {'candidates': 2423, 'before_eval_start': 60, 'emitted': 2363, 'emitted_before_limit': 2363}
* runtime s {'events_step': 228.6, 'controls_step': 210.4}, peak memory MB {'events_step': 212.0, 'controls_step': 221.5}

### Sample event record (features at the decision bar, non-null)

```json
{
 "event_id": "4432224951f6084941aa8a3ce1f76bb0",
 "market": "BTCUSD",
 "family": "STRUCT",
 "variant": "retest",
 "direction": 1,
 "is_control": false,
 "decision_ts_ns": 1776956400000000000,
 "observer_version": "market-structure-observer-v1",
 "schema_version": "mso-schema-1",
 "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
 "v_acceptance": "mso-acceptance-1",
 "v_balance": "mso-balance-1",
 "v_levels": "mso-levels-1",
 "v_participation": "mso-participation-1",
 "v_swings": "mso-swings-1",
 "f_levels__nearest_level_distance_atr": 0.01725386365130051,
 "f_levels__nearest_level_source": "PREV_DAY_CLOSE",
 "f_levels__nearest_level_age_bars": 180,
 "f_levels__touch_count": 14,
 "f_levels__clean_rejection_count": 4,
 "f_levels__penetration_count": 6,
 "f_levels__first_touch": false,
 "f_levels__break_count": 4,
 "f_levels__reclaim_count": 3,
 "f_levels__role": "FLIPPED_TO_RESISTANCE",
 "f_levels__previous_role": "ACCEPTED_BELOW",
 "f_levels__zone_source_count": 142,
 "f_levels__zone_independent_source_count": 4,
 "f_levels__zone_width_atr": 8.358959355777902,
 "f_levels__n_levels_within_1atr": 45,
 "f_levels__last_touch_ts_ns": 1.7769564e+18,
 "f_levels__last_break_ts_ns": 1.776927e+18,
 "f_levels__last_reclaim_ts_ns": 1.7769246e+18,
 "f_swings__m5_sequence": "DOWN_SEQUENCE",
 "f_swings__m5_high_label": "LH",
 "f_swings__m5_low_label": "LL",
 "f_swings__m5_sequence_length": 3,
 "f_swings__m5_structure_age_bars": 8.0,
 "f_swings__m5_structure_age_minutes": 40.0,
 "f_swings__m5_high_delta_atr": -0.25906394681212513,
 "f_swings__m5_low_delta_atr": -0.845029731647221,
 "f_swings__m5_confirmed_at_ts_ns": 1.776954e+18,
 "f_swings__m5_last_high_confirmed_at_ts_ns": 1.776954e+18,
 "f_swings__m5_last_low_confirmed_at_ts_ns": 1.7769531e+18,
 "f_swings__m5_close_beyond_last_swing_atr_high": 2.7082934107648327,
 "f_swings__m5_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m5_bars_since_beyond_high": 7.0,
 "f_swings__m15_sequence": "DOWN_SEQUENCE",
 "f_swings__m15_high_label": "LH",
 "f_swings__m15_low_label": "LL",
 "f_swings__m15_sequence_length": 2,
 "f_swings__m15_structure_age_bars": 2.0,
 "f_swings__m15_structure_age_minutes": 30.0,
 "f_swings__m15_high_delta_atr": -2.1643615192762455,
 "f_swings__m15_low_delta_atr": -0.845029731647221,
 "f_swings__m15_confirmed_at_ts_ns": 1.7769546e+18,
 "f_swings__m15_last_high_confirmed_at_ts_ns": 1.7769519e+18,
 "f_swings__m15_last_low_confirmed_at_ts_ns": 1.7769546e+18,
 "f_swings__m15_close_beyond_last_swing_atr_high": 1.3797459096129052,
 "f_swings__m15_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m15_bars_since_beyond_high": 1.0,
 "f_swings__ema_trend": "up",
 "f_swings__ema_swing_agreement": "DISAGREE",
 "f_acceptance__break_found": false,
 "f_balance__range_width_atr_w24": 4.252386211537266,
 "f_balance__directional_efficiency_w24": 0.21374961089269964,
 "f_balance__bar_overlap_ratio_w24": 0.5427672690552824,
 "f_balance__close_occupancy_ratio_w24": 0.625,
 "f_balance__midpoint_cross_count_w24": 2.0,
 "f_balance__range_width_atr_w48": 4.324166380199399,
 "f_balance__directional_efficiency_w48": 0.230830135000936,
 "f_balance__bar_overlap_ratio_w48": 0.5142653662136133,
 "f_balance__close_occupancy_ratio_w48": 0.6458333333333334,
 "f_balance__midpoint_cross_count_w48": 3.0,
 "f_participation__tick_activity_per_min": 637.0,
 "f_participation__tick_activity_percentile": 1.0,
 "f_participation__tick_activity_z": 1.9959692046101662,
 "f_participation__activity_vs_same_tod": 2.155668358714044,
 "f_participation__activity_vs_session_baseline": 1.4174454828660437,
 "f_participation__activity_acceleration": 1.0814940577249577,
 "f_participation__tod_baseline_n": 20.0,
 "f_participation__tod_baseline_oldest_ts_ns": 1.7752281e+18,
 "m_warmup_ok": true,
 "m_bars_available": 39771,
 "m_prev_days_available": 149,
 "m_atr_ok": true,
 "m_min_bars_levels": 603,
 "m_min_bars_swings": 480,
 "m_min_bars_acceptance": 98,
 "m_min_bars_balance": 48,
 "m_min_prev_days_participation": 21,
 "m_event_price": 78193.42,
 "m_reference_level_id": "93c8f69ef0789995",
 "m_config_hash": "a88e7e30087b202c",
 "m_structure_event_id": "BTCUSD:1:2026-04-23T14:50:00+00:00",
 "m_hash_levels": "8fe16129e7aa61e0",
 "m_hash_swings": "eb1857c684e21d19",
 "m_hash_acceptance": "1608ec3fb935dc33ecc4874e1768289b3c0790e9bc3a0919f88e766e28f08060",
 "m_hash_balance": "ddac1af18d7032ca1ee3acf276d5f2ccd6101710a5bd8476158872b7ca8d3aec",
 "m_hash_participation": "452f5122fbfc26c12ed207281e1fcbd3152c3ecd265d25b5ce177c13529c92b3",
 "warmup_ok": true,
 "run_id": "a4f806fbafff",
 "decision_idx": 39770,
 "strategy_id": "STRUCT-8f4ac39e81",
 "structure_event_id": "BTCUSD:1:2026-04-23T14:50:00+00:00",
 "entry": 78193.42,
 "stop": 77349.35035714284,
 "risk": 844.0696428571537,
 "risk_atr": 4.321502234444917,
 "partition": "TRAIN"
}
```

## BRENT

Rows: 2975 events + 2408 controls; frame fingerprint matches manifest: True. Gate B compute 2.5 s.

### (ii) Leakage audit on real data

* live-style incremental pass, full history, physically truncated views (prefix, bar 0..i): compared 330, pass 330, fail 0, skipped 0  -> PASS
* raw frame truncated exactly at the decision bar, history window 7056 bars, rebuilt through the batch adapter: compared 60, pass 60, fail 0, skipped 0  -> PASS
* same window but +500 FUTURE bars present in the frame: compared 30, pass 30, fail 0, skipped 0  -> PASS
* shallow histories (120/240/500 bars): 120: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 240: n=20, flagged not-warm 20, wrongly warm 0, still equal 0; 500: n=20, flagged not-warm 20, wrongly warm 0, still equal 0

### (i) Plausibility (83 feature columns, 0 flagged) -> PASS

| group | columns | constant | all-missing | missing >= 50% | flagged |
|---|---|---|---|---|---|
| acceptance | 15 | 0 | 0 | 14 | 0 |
| balance | 10 | 0 | 0 | 0 | 0 |
| levels | 18 | 0 | 0 | 2 | 0 |
| participation | 8 | 0 | 0 | 0 | 0 |
| swings | 32 | 0 | 0 | 4 | 0 |

### (iii) Warm-up accounting

* events: 0/2975 warmup_ok=false (0.0%); controls: 0/2408 (0.0%)
* share of events below the documented history requirement by group: levels 0.0%, swings 0.0%, acceptance 0.0%, balance 0.0%, participation 0.0%, atr 0.0%
* events warmup_ok=false by family: STRUCT 0.0%

### (iv) Label sanity -> PASS

| label | ref P (zero drift) | events rate [Wilson95] (n) | controls rate [Wilson95] (n) | censored ev/ctl |
|---|---|---|---|---|
| y_fav025_before_adv025 | 0.500 | 0.471 [0.452,0.490] (2626) | 0.485 [0.463,0.507] (2046) | 11.7% / 15.0% |
| y_fav050_before_adv050 | 0.500 | 0.504 [0.484,0.523] (2520) | 0.491 [0.469,0.514] (1927) | 15.3% / 20.0% |
| y_fav075_before_adv050 | 0.400 | 0.404 [0.385,0.424] (2455) | 0.396 [0.374,0.418] (1852) | 17.5% / 23.1% |
| y_fav100_before_adv050 | 0.333 | 0.327 [0.309,0.347] (2358) | 0.328 [0.307,0.351] (1787) | 20.7% / 25.8% |

* consistency violations: 0 over 5383 rows; horizon max 14400 s (bound 14400), full-48-bar share 63.7%
* no label issue

### (v) Matching quality -> PASS (matching revision: partition-aware (match_controls partition=auto, rank_mode=partition, exclude_idx = every generator opportunity))

* events 2975, matched 2408 (rate 0.809), unmatched 567, controls 2408; SMD {'local_minute': 0.005693940738369814, 'atr_pct': -0.015634117362142097, 'spread_pct': -0.008041219700057129}; max session-share difference 0.000; controls in a different partition than their event: 0
* match rate by family: STRUCT 2408/2975

### (vii) Coverage

* bars 2025-06-01T22:00:00+00:00 .. 2026-08-31T20:55:00+00:00 (82487 bars); gaps 332 (intraday > 3 bars: 3, largest 73.08 h); missing UTC months: none; gaps > 3 days: [['2026-04-02T20:55:00+00:00', 73.1]]
* dev-end guard: {'dev_end_berlin_date': '2026-08-31', 'assert_no_forward_holdout': 'PASS', 'guard_dev_only': 'PASS', 'last_bar_utc': '2026-08-31T20:55:00+00:00'}
* partitions: {'EMBARGO': 4, 'FROZEN_OOS': 392, 'TRAIN': 2008, 'VALIDATION': 571} (plan {'train_start': '2025-06-02', 'train_end': '2026-03-22', 'validation_start': '2026-03-23', 'validation_end': '2026-06-30', 'oos_start': '2026-07-01', 'oos_end': '2026-08-31', 'forward_start': '2026-09-01', 'core_fit_end': '2026-06-30'}; frozen split exists: False). BTCUSD/BRENT: NO frozen split exists (short history, placeholder STRUCT constants); the boundaries above are the repo's generic dev dates, TRAIN/VALIDATION here are NOT a frozen train/validation of any fitted parameter
* events by family|variant|direction: {'STRUCT|breakout|long': 488, 'STRUCT|breakout|short': 490, 'STRUCT|confirmed|long': 388, 'STRUCT|confirmed|short': 380, 'STRUCT|fade|long': 313, 'STRUCT|fade|short': 286, 'STRUCT|retest|long': 315, 'STRUCT|retest|short': 315}
* candidate exclusions: {'candidates': 3143, 'before_eval_start': 168, 'emitted': 2975, 'emitted_before_limit': 2975}
* runtime s {'events_step': 147.4, 'controls_step': 141.1}, peak memory MB {'events_step': 229.7, 'controls_step': 233.4}

### Sample event record (features at the decision bar, non-null)

```json
{
 "event_id": "0e99d4f399528b5cdf6f697ef54f5763",
 "market": "BRENT",
 "family": "STRUCT",
 "variant": "breakout",
 "direction": 1,
 "is_control": false,
 "decision_ts_ns": 1767781500000000000,
 "observer_version": "market-structure-observer-v1",
 "schema_version": "mso-schema-1",
 "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
 "v_acceptance": "mso-acceptance-1",
 "v_balance": "mso-balance-1",
 "v_levels": "mso-levels-1",
 "v_participation": "mso-participation-1",
 "v_swings": "mso-swings-1",
 "f_levels__nearest_level_distance_atr": 0.0,
 "f_levels__nearest_level_source": "PREV_DAY_LOW",
 "f_levels__nearest_level_age_bars": 113,
 "f_levels__touch_count": 3,
 "f_levels__clean_rejection_count": 0,
 "f_levels__penetration_count": 0,
 "f_levels__first_touch": false,
 "f_levels__break_count": 1,
 "f_levels__reclaim_count": 0,
 "f_levels__role": "ACCEPTED_BELOW",
 "f_levels__previous_role": "BROKEN_DOWN",
 "f_levels__zone_source_count": 48,
 "f_levels__zone_independent_source_count": 6,
 "f_levels__zone_width_atr": 5.411042944785252,
 "f_levels__n_levels_within_1atr": 7,
 "f_levels__last_touch_ts_ns": 1.7677815e+18,
 "f_levels__last_break_ts_ns": 1.7677479e+18,
 "f_swings__m5_sequence": "DOWN_SEQUENCE",
 "f_swings__m5_high_label": "LH",
 "f_swings__m5_low_label": "LL",
 "f_swings__m5_sequence_length": 2,
 "f_swings__m5_structure_age_bars": 2.0,
 "f_swings__m5_structure_age_minutes": 10.0,
 "f_swings__m5_high_delta_atr": -1.5460122699386523,
 "f_swings__m5_low_delta_atr": -1.2024539877300695,
 "f_swings__m5_confirmed_at_ts_ns": 1.7677809e+18,
 "f_swings__m5_last_high_confirmed_at_ts_ns": 1.7677809e+18,
 "f_swings__m5_last_low_confirmed_at_ts_ns": 1.7677806e+18,
 "f_swings__m5_close_beyond_last_swing_atr_high": 1.717791411042974,
 "f_swings__m5_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m5_bars_since_beyond_high": 1.0,
 "f_swings__m15_sequence": "UP_SEQUENCE",
 "f_swings__m15_high_label": "HH",
 "f_swings__m15_low_label": "HL",
 "f_swings__m15_sequence_length": 3,
 "f_swings__m15_structure_age_bars": 1.0,
 "f_swings__m15_structure_age_minutes": 25.0,
 "f_swings__m15_high_delta_atr": 2.147239263803687,
 "f_swings__m15_low_delta_atr": 1.717791411042974,
 "f_swings__m15_confirmed_at_ts_ns": 1.76778e+18,
 "f_swings__m15_last_high_confirmed_at_ts_ns": 1.76778e+18,
 "f_swings__m15_last_low_confirmed_at_ts_ns": 1.7677791e+18,
 "f_swings__m15_close_beyond_last_swing_atr_high": 0.1717791411043218,
 "f_swings__m15_close_beyond_last_swing_atr_low": 0.0,
 "f_swings__m15_bars_since_beyond_high": 0.0,
 "f_swings__m15_bars_since_beyond_low": 7.0,
 "f_swings__ema_trend": "up",
 "f_swings__ema_swing_agreement": "DISAGREE",
 "f_acceptance__break_found": false,
 "f_balance__range_width_atr_w24": 3.5214723926380174,
 "f_balance__directional_efficiency_w24": 0.15702479338842867,
 "f_balance__bar_overlap_ratio_w24": 0.5097389080610233,
 "f_balance__close_occupancy_ratio_w24": 0.7916666666666666,
 "f_balance__midpoint_cross_count_w24": 6.0,
 "f_balance__range_width_atr_w48": 4.638036809815956,
 "f_balance__directional_efficiency_w48": 0.15639810426540313,
 "f_balance__bar_overlap_ratio_w48": 0.45909940572097785,
 "f_balance__close_occupancy_ratio_w48": 0.7083333333333334,
 "f_balance__midpoint_cross_count_w48": 3.0,
 "f_participation__tick_activity_per_min": 22.0,
 "f_participation__tick_activity_percentile": 0.9375,
 "f_participation__tick_activity_z": 1.9152416279126687,
 "f_participation__activity_vs_same_tod": 1.5277777777777777,
 "f_participation__activity_vs_session_baseline": 1.6541353383458646,
 "f_participation__activity_acceleration": 1.1,
 "f_participation__tod_baseline_n": 16,
 "f_participation__tod_baseline_oldest_ts_ns": 1765534800000000000,
 "m_warmup_ok": true,
 "m_bars_available": 39641,
 "m_prev_days_available": 187,
 "m_atr_ok": true,
 "m_min_bars_levels": 603,
 "m_min_bars_swings": 480,
 "m_min_bars_acceptance": 98,
 "m_min_bars_balance": 48,
 "m_min_prev_days_participation": 21,
 "m_event_price": 60.21,
 "m_reference_level_id": "b44026715d6930f5",
 "m_config_hash": "9f2a4b3a080201a1",
 "m_structure_event_id": "BRENT:1:2026-01-07T10:20:00+00:00",
 "m_hash_levels": "2e831b19f634d411",
 "m_hash_swings": "eb1857c684e21d19",
 "m_hash_acceptance": "1608ec3fb935dc33ecc4874e1768289b3c0790e9bc3a0919f88e766e28f08060",
 "m_hash_balance": "ddac1af18d7032ca1ee3acf276d5f2ccd6101710a5bd8476158872b7ca8d3aec",
 "m_hash_participation": "452f5122fbfc26c12ed207281e1fcbd3152c3ecd265d25b5ce177c13529c92b3",
 "warmup_ok": true,
 "run_id": "5c876abdf7d5",
 "decision_idx": 39640,
 "strategy_id": "STRUCT-2004a99997",
 "structure_event_id": "BRENT:1:2026-01-07T10:20:00+00:00",
 "entry": 60.21,
 "stop": 59.78089285714286,
 "risk": 0.42910714285714135,
 "risk_atr": 3.685582822085887,
 "partition": "TRAIN"
}
```

## Honest caveats

* BTCUSD and BRENT have NO frozen split, a short history (BTC from 2025-09-24, BRENT from 2025-06) with missing months (BTC lacks Oct-2025 and Mar-2026) and provisional STRUCT constants.
* The core thresholds (GER40 NAS100 SPX500 XAUUSD EURUSD) were partly fitted up to 2026-06-30: events before that date are in-sample for the family thresholds.
* The dev window 2026-07-01..2026-08-31 is NOT a clean holdout for the core (other lanes validated on it); the forward period (>= 2026-09-01) is untouched and never read.
* Bars are M5 bid bars: first-passage labels are bar-resolution, stop-first, cost-free; they are not P&L. Events are generator opportunities without the live operating policy filters.
* Controls use the partition-aware matching (observer-controls-2): inside the event's partition, partition-internal percentile ranks, +-48 bars away from every generator opportunity. BTCUSD/BRENT have no frozen split, so their partitions are generic dev-date tags only. The pre-revision controls (whole-sample ranks) are kept under _prerevision/ for comparison.
* Nothing here is an edge claim; OBSERVATION_ONLY_NOT_ALPHA_VALIDATED.
