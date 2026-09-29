# AR1 Phase 1 - GER40 research report (RESEARCH ONLY, nothing production-ready)

git: `a74e3516882633f7fb39ac90e3364d9fdd1f0be5+DIRTY`  fingerprint: `dc0a10c05297f88e`
seed 20260929, versions {'nautilus_trader': '1.231.0', 'numpy': '2.5.3', 'pandas': '3.0.6', 'pyarrow': '25.0.1'}

## Dataset

96219 M5 BID bars, 2025-02-04T00:15:00+00:00 .. 2026-08-31T19:55:00+00:00, ActivTrades DEMO Ger40. Bars per partition: {'total': 96219, 'train': 50705, 'validation': 25132, 'oos': 20382}. Splits: {'train': ['2025-02-01', '2025-11-30'], 'validation': ['2025-12-01', '2026-04-30'], 'oos': ['2026-05-01', '2026-08-31']}

Excluded months (failed validation / no data): 2024-01, 2024-02, 2024-03, 2024-04, 2024-05, 2024-06, 2024-07, 2024-08, 2024-09, 2024-10, 2024-11, 2024-12, 2025-01

| month | rows | validation_status | warning_summary | content_sha256 |
|---|---|---|---|---|
| 2025-02 | 4731 | PASSED | {} | e481a3b0799a57c6e2cd0465d54b3b168a1dfd84bdbf8b997ea5dd3cc1b4f610 |
| 2025-03 | 5209 | PASSED_WITH_WARNINGS | {'SUSPICIOUS_GAP': 1} | 54676f6eaec1697d2ab46014287373bd12ab9b3e7f15d386bd41a267fcb53849 |
| 2025-04 | 4739 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 24, 'SUSPICIOUS_GAP': 1} | 253cf0ec52ca99882ec584d423cf8ffa330b837216499fb5ffe350653b3b1ded |
| 2025-05 | 4977 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 3} | 1d34463a761d427bd0090fd3dd82374a64d8a0a8938875ccadfc466d78a756e8 |
| 2025-06 | 4977 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 37} | 464991f08308a8ff361f709dce5da96c3e2d3d1f30c4d912b7c876f221ed6f40 |
| 2025-07 | 5451 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 7} | 6eb37ba8968a2a68f54992f71a09c8a306c7473881f88fc540506e4028ae5f17 |
| 2025-08 | 4977 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 29} | 51886e76485631363b5b13238e0fb16e9b1ba313fa4b7e302ce7e4ebc222c0a5 |
| 2025-09 | 5214 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 42} | 5e30e17d33664a5d478ec3fb8664e19312eb70bdc30c2d8471036a7cbbee94ad |
| 2025-10 | 5511 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 3} | d1ff7b09deb96da8d44a5259c473fb33d618f5e14e04e450723a2dde04c98d5b |
| 2025-11 | 4919 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 1} | 3c8bebcce59035139a8dcb130fbad3f6b61a6bc7951e01b939954315752e3f4e |
| 2025-12 | 4731 | PASSED_WITH_WARNINGS | {'SUSPICIOUS_GAP': 1} | d2b2ea1caacdc7a2e61734f886d7a042a8265f7e1176a846ca209205fbd39a84 |
| 2026-01 | 5229 | PASSED | {} | ec21e32c7c70dd81362a0cc0c6e1126e57606d82f7830b2fef157d8572fd24b8 |
| 2026-02 | 4979 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 6} | 90dd409fa5a985dc308252e56ca9958e639ac7b1e8727ca9988fdb7eea7e8d7f |
| 2026-03 | 5453 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 8} | b8a4d8c3108c7ed1afdf2f00570228fcf8094365963883a06750dbef92f5e134 |
| 2026-04 | 4740 | PASSED_WITH_WARNINGS | {'SUSPICIOUS_GAP': 1} | d937f8e398141bb97a9309c3ad45e86ebedc2b6fc39755304dd8b8d7683f5670 |
| 2026-05 | 4740 | PASSED | {} | 1c0d686170101aa57ac65dd4949a4f565268e7304843a5dfeeaf91370064422e |
| 2026-06 | 5214 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 2} | 968f0bcfb1dc9ddcfa4a5533858f1bb77457182b410f889992305a7c0c658608 |
| 2026-07 | 5451 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 1} | 20afdfdcb8bf051f2c9fae10cb6f7d0ffe9d6449223d6c5ea2612fb971ef1148 |
| 2026-08 | 4977 | PASSED_WITH_WARNINGS | {'SPREAD_ANOMALY': 3} | 15fd1bae910585fe5c08cdd668f41e3c780f74b23b835709f824de33b6100009 |

Reviewed gaps admitted: {"2025-04-17T19:55:00+00:00..2025-04-22T00:15:00+00:00": "Easter closure", "2025-12-23T20:55:00+00:00..2025-12-29T00:15:00+00:00": "Christmas closure", "2026-04-02T19:55:00+00:00..2026-04-07T00:15:00+00:00": "Easter closure", "2025-03-03T20:05:00+00:00..2025-03-03T20:50:00+00:00": "45-minute hole at 21:05-21:50 Berlin: after the last entry (20:00) and the forced flat"}

## Research runs

{"development_simulations": 144, "final_simulations": 20, "grid_variants": 144, "signal_configs": 36}

## Development grid (BASE costs, train / validation) - family summary

| family | variants | train exp_R>0 | val exp_R>0 | both>0 | median train R | median val R | max trades/day (train) |
|---|---|---|---|---|---|---|---|
| BREAKOUT | 40 | 57% | 68% | 40% | 0.016 | 0.037 | 3.1 |
| MOMENTUM | 72 | 68% | 14% | 14% | 0.041 | -0.105 | 3.6 |
| PULLBACK | 32 | 94% | 3% | 3% | 0.023 | -0.086 | 3.4 |

Trade cadence: 48 of 144 variants reach >= 2 trades/day in train; 2 of them have positive expectancy in both train and validation. Full per-variant table: `grid_runs.csv` (parameter-stability map).

## CANONICAL - MOMENTUM|lookback=24,stop_mult=2.5,thr=1.5 exit {'kind': 'fixed_r', 'r': 1.5}

Verdict: **REJECTED**. pre-registered canonical config, single test
Failed checks: ['total_trades>=min', 'expectancy_r>0 in validation (BASE)', 'expectancy_r>0 in oos (SPREAD_STRESS)', 'oos_profit_factor>1']. Pooled PF (train+val+oos): 1.3324644062341104

### Metrics at BASE cost

| metric | train | validation | oos |
|---|---|---|---|
| trades | 94 | 47 | 42 |
| trades_per_day | 0.445498 | 0.460784 | 0.488372 |
| median_trades_per_day | 0.0 | 0.0 | 0.0 |
| zero_trade_days | 141 | 66 | 53 |
| net_pnl_eur | 1446.324768 | -242.755227 | -7.564844 |
| expectancy_eur | 15.386434 | -5.165005 | -0.180115 |
| expectancy_r | 0.359112 | -0.088629 | 0.00405 |
| expectancy_r_ci95_trade_iid | [0.12809, 0.585177] | [-0.40317, 0.225373] | [-0.350579, 0.36784] |
| expectancy_r_ci95_day_clustered | [0.10423, 0.618772] | [-0.418088, 0.250647] | [-0.347286, 0.336297] |
| profit_factor | 1.948425 | 0.781346 | 0.992138 |
| win_rate | 0.574468 | 0.382979 | 0.428571 |
| avg_winner_eur | 55.024065 | 48.192667 | 53.034938 |
| avg_loser_eur | -38.124369 | -38.283559 | -40.091405 |
| payoff_ratio | 1.443278 | 1.258835 | 1.322851 |
| max_drawdown_eur | 256.210825 | 540.088799 | 505.997194 |
| max_drawdown_pct | 2.290052 | 5.385432 | 4.968964 |
| max_consecutive_losses | 5 | 11 | 8 |
| worst_day_eur | -89.784364 | -88.238768 | -133.829419 |
| best_day_eur | 174.797549 | 122.456071 | 123.456225 |
| exposure_time_frac | 0.085382 | 0.068767 | 0.090775 |
| avg_holding_minutes | 143.723404 | 111.914894 | 139.404762 |
| avg_leverage | 1.714634 | 1.495899 | 1.540764 |
| max_leverage | 3.289814 | 3.1794 | 2.636456 |
| avg_entry_spread_pts | 1.015213 | 1.354681 | 1.496905 |
| est_execution_cost_eur | 123.1625 | 63.6725 | 59.5775 |
| cost_per_trade_eur | 1.310239 | 1.354734 | 1.418512 |

### Cost stress (expectancy R / profit factor / net EUR)

| scenario | train | validation | oos |
|---|---|---|---|
| GROSS_REFERENCE | 0.399657 / 2.098566 / 1594.928781 | -0.07859 / 0.800241 / -220.558039 | 0.016793 / 1.043098 / 40.100193 |
| BASE | 0.359112 / 1.948425 / 1446.324768 | -0.088629 / 0.781346 / -242.755227 | 0.00405 / 0.992138 / -7.564844 |
| SPREAD_STRESS | 0.289238 / 1.720933 / 1177.116694 | -0.0911 / 0.759901 / -270.877116 | -0.001599 / 0.999852 / -0.139172 |
| SLIPPAGE_STRESS | 0.318989 / 1.803906 / 1274.171264 | -0.101378 / 0.756195 / -276.839363 | 0.0157 / 1.045646 / 41.951364 |
| COMBINED_ADVERSE | 0.19622 / 1.451863 / 766.623744 | -0.103594 / 0.748305 / -290.378303 | -0.051348 / 0.912631 / -84.254679 |

### trend breakdown - development (train+validation)

| trend | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| RANGING | 58 | 0.381193 | 0.152933 | 0.603448 | 2.00681 | 951.498721 |
| TRENDING | 83 | 0.090142 | 0.127169 | 0.445783 | 1.149142 | 252.07082 |

material difference: `{"material": false, "best": ["RANGING", 0.381193, 58], "worst": ["TRENDING", 0.090142, 83], "diff_r": 0.2911, "z": 1.46, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### vol breakdown - development (train+validation)

| vol | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| HIGH_VOL | 46 | 0.28073 | 0.171639 | 0.521739 | 1.753572 | 562.696611 |
| LOW_VOL | 14 | 0.193749 | 0.337293 | 0.5 | 1.315072 | 92.610661 |
| NA | 17 | 0.646454 | 0.272505 | 0.705882 | 3.431907 | 476.111907 |
| NORMAL_VOL | 64 | 0.046487 | 0.143659 | 0.453125 | 1.051581 | 72.150361 |

material difference: `{"material": false, "best": ["HIGH_VOL", 0.28073, 46], "worst": ["NORMAL_VOL", 0.046487, 64], "diff_r": 0.2342, "z": 1.05, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### session breakdown - development (train+validation)

| session | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| LATE_1730_2130 | 23 | 0.478408 | 0.162666 | 0.73913 | 4.515965 | 441.466684 |
| MIDDAY_12_1530 | 29 | 0.249235 | 0.233085 | 0.517241 | 1.412237 | 244.450658 |
| MORNING_10_12 | 44 | 0.416769 | 0.189488 | 0.568182 | 2.078729 | 835.391858 |
| OPEN_09_10 | 18 | -0.590964 | 0.226797 | 0.166667 | 0.256173 | -473.532875 |
| US_OVERLAP_1530_1730 | 27 | 0.135528 | 0.219504 | 0.444444 | 1.308128 | 155.793215 |

material difference: `{"material": null, "reason": "fewer than two groups with enough trades"}`

### trend breakdown - OOS

| trend | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| RANGING | 19 | -0.248238 | 0.248414 | 0.315789 | 0.572463 | -223.661531 |
| TRENDING | 23 | 0.212462 | 0.256447 | 0.521739 | 1.492187 | 216.096687 |

material difference: `{"material": null, "reason": "fewer than two groups with enough trades"}`

### vol breakdown - OOS

| vol | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| HIGH_VOL | 1 | 1.5 | nan | 1.0 | nan | 73.489504 |
| LOW_VOL | 14 | -0.431605 | 0.233187 | 0.285714 | 0.348889 | -243.614632 |
| NORMAL_VOL | 27 | 0.17454 | 0.241711 | 0.481481 | 1.276444 | 162.560283 |

material difference: `{"material": null, "reason": "fewer than two groups with enough trades"}`

### session breakdown - OOS

| session | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| LATE_1730_2130 | 7 | 0.356843 | 0.406271 | 0.571429 | 2.203123 | 101.652226 |
| MIDDAY_12_1530 | 7 | -0.291038 | 0.462445 | 0.285714 | 0.583008 | -88.301069 |
| MORNING_10_12 | 15 | -0.171145 | 0.315817 | 0.333333 | 0.752296 | -99.725058 |
| OPEN_09_10 | 9 | 0.514557 | 0.407234 | 0.666667 | 2.264602 | 175.768949 |
| US_OVERLAP_1530_1730 | 4 | -0.588592 | 0.418146 | 0.25 | 0.220313 | -96.959892 |

material difference: `{"material": null, "reason": "fewer than two groups with enough trades"}`

### MFE/MAE

dev: `{"p_mfe_ge_1r": 0.4752, "p_mfe_ge_2r": 0.0, "p_mfe_ge_3r": 0.0, "winners_mae_r_median": 0.404, "winners_mae_r_q90": 0.8332, "losers_mfe_r_median": 0.3449, "losers_reaching_1r_before_stop": 0.0725, "median_bars_to_mfe": 11.0, "note": "MAE/MFE on executable side incl. costs at fill; input for later ExitPolicy work"}`

oos: `{"p_mfe_ge_1r": 0.4762, "p_mfe_ge_2r": 0.0, "p_mfe_ge_3r": 0.0, "winners_mae_r_median": 0.2668, "winners_mae_r_q90": 0.8715, "losers_mfe_r_median": 0.3725, "losers_reaching_1r_before_stop": 0.125, "median_bars_to_mfe": 7.0, "note": "MAE/MFE on executable side incl. costs at fill; input for later ExitPolicy work"}`

## CANONICAL - BREAKOUT|buffer_atr=0.25,kind=orb,or_bars=6,stop_mode=or_mid exit {'kind': 'fixed_r', 'r': 1.5}

Verdict: **REJECTED**. pre-registered canonical config, single test
Failed checks: ['expectancy_r>0 in dev (SLIPPAGE_STRESS)', 'pooled_profit_factor>min']. Pooled PF (train+val+oos): 1.0420603400157633

### Metrics at BASE cost

| metric | train | validation | oos |
|---|---|---|---|
| trades | 204 | 100 | 87 |
| trades_per_day | 0.966825 | 0.980392 | 1.011628 |
| median_trades_per_day | 1.0 | 1.0 | 1.0 |
| zero_trade_days | 31 | 11 | 7 |
| net_pnl_eur | 51.874375 | 55.475625 | 284.6775 |
| expectancy_eur | 0.254286 | 0.554756 | 3.272155 |
| expectancy_r | 0.015294 | 0.00875 | 0.077849 |
| expectancy_r_ci95_trade_iid | [-0.137604, 0.178392] | [-0.226233, 0.248444] | [-0.165084, 0.333191] |
| expectancy_r_ci95_day_clustered | [-0.153604, 0.181587] | [-0.226507, 0.243178] | [-0.166041, 0.333536] |
| profit_factor | 1.010524 | 1.023124 | 1.142878 |
| win_rate | 0.421569 | 0.41 | 0.448276 |
| avg_winner_eur | 57.917914 | 59.867119 | 58.388013 |
| avg_loser_eur | -41.771748 | -40.662309 | -41.509479 |
| payoff_ratio | 1.386533 | 1.4723 | 1.406619 |
| max_drawdown_eur | 477.45125 | 492.32375 | 533.986875 |
| max_drawdown_pct | 4.564421 | 4.724699 | 4.993665 |
| max_consecutive_losses | 7 | 5 | 10 |
| worst_day_eur | -96.6375 | -96.175 | -92.52625 |
| best_day_eur | 74.9325 | 74.9475 | 73.8225 |
| exposure_time_frac | 0.20644 | 0.199634 | 0.227674 |
| avg_holding_minutes | 160.122549 | 152.7 | 168.793103 |
| avg_leverage | 1.743664 | 1.673743 | 1.778662 |
| max_leverage | 4.313876 | 4.43232 | 3.312764 |
| avg_entry_spread_pts | 1.02848 | 1.2887 | 1.276552 |
| est_execution_cost_eur | 268.9975 | 139.2875 | 126.0425 |
| cost_per_trade_eur | 1.318615 | 1.392875 | 1.448764 |

### Cost stress (expectancy R / profit factor / net EUR)

| scenario | train | validation | oos |
|---|---|---|---|
| GROSS_REFERENCE | 0.047602 / 1.049894 / 244.1425 | 0.039555 / 1.053649 / 127.00625 | 0.093916 / 1.136125 / 275.845625 |
| BASE | 0.015294 / 1.010524 / 51.874375 | 0.00875 / 1.023124 / 55.475625 | 0.077849 / 1.142878 / 284.6775 |
| SPREAD_STRESS | 0.006668 / 0.996904 / -15.40375 | 0.008261 / 1.029767 / 71.410625 | 0.067092 / 1.112663 / 227.534375 |
| SLIPPAGE_STRESS | -0.008553 / 0.968749 / -157.535625 | -0.001459 / 0.985939 / -34.5225 | 0.067406 / 1.119133 / 245.6475 |
| COMBINED_ADVERSE | -0.049526 / 0.904716 / -490.955 | -0.001827 / 0.983828 / -39.68625 | 0.027533 / 1.046315 / 96.468125 |

### trend breakdown - development (train+validation)

| trend | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| RANGING | 253 | 0.002969 | 0.076073 | 0.411067 | 1.011763 | 72.194375 |
| TRENDING | 51 | 0.063606 | 0.170416 | 0.45098 | 1.029524 | 35.155625 |

material difference: `{"material": false, "best": ["TRENDING", 0.063606, 51], "worst": ["RANGING", 0.002969, 253], "diff_r": 0.0606, "z": 0.32, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### vol breakdown - development (train+validation)

| vol | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| HIGH_VOL | 94 | -0.042866 | 0.124909 | 0.393617 | 0.935218 | -145.658125 |
| LOW_VOL | 33 | 0.401299 | 0.216658 | 0.575758 | 1.940317 | 548.82875 |
| NA | 22 | -0.096322 | 0.263325 | 0.363636 | 0.854252 | -87.971875 |
| NORMAL_VOL | 155 | -0.019996 | 0.095889 | 0.406452 | 0.946602 | -207.84875 |

material difference: `{"material": false, "best": ["LOW_VOL", 0.401299, 33], "worst": ["HIGH_VOL", -0.042866, 94], "diff_r": 0.4442, "z": 1.78, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### session breakdown - development (train+validation)

| session | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| MIDDAY_12_1530 | 2 | 0.244038 | 1.255962 | 0.5 | 1.276688 | 11.74125 |
| MORNING_10_12 | 159 | 0.006535 | 0.096587 | 0.408805 | 1.011521 | 44.568125 |
| OPEN_09_10 | 143 | 0.017258 | 0.100608 | 0.426573 | 1.014936 | 51.040625 |

material difference: `{"material": false, "best": ["OPEN_09_10", 0.017258, 143], "worst": ["MORNING_10_12", 0.006535, 159], "diff_r": 0.0107, "z": 0.08, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### trend breakdown - OOS

| trend | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| RANGING | 69 | 0.051355 | 0.146165 | 0.434783 | 1.109374 | 175.55875 |
| TRENDING | 18 | 0.17941 | 0.295093 | 0.5 | 1.281719 | 109.11875 |

material difference: `{"material": null, "reason": "fewer than two groups with enough trades"}`

### vol breakdown - OOS

| vol | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| HIGH_VOL | 6 | -0.590509 | 0.418104 | 0.166667 | 0.317432 | -143.6925 |
| LOW_VOL | 28 | -0.082577 | 0.229036 | 0.357143 | 0.847019 | -112.5025 |
| NORMAL_VOL | 53 | 0.238265 | 0.167643 | 0.528302 | 1.516821 | 540.8725 |

material difference: `{"material": null, "reason": "fewer than two groups with enough trades"}`

### session breakdown - OOS

| session | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| MORNING_10_12 | 54 | 0.014331 | 0.163159 | 0.425926 | 1.044146 | 55.105625 |
| OPEN_09_10 | 33 | 0.181788 | 0.218377 | 0.484848 | 1.308477 | 229.571875 |

material difference: `{"material": false, "best": ["OPEN_09_10", 0.181788, 33], "worst": ["MORNING_10_12", 0.014331, 54], "diff_r": 0.1675, "z": 0.61, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### MFE/MAE

dev: `{"p_mfe_ge_1r": 0.5066, "p_mfe_ge_2r": 0.0, "p_mfe_ge_3r": 0.0, "winners_mae_r_median": 0.4382, "winners_mae_r_q90": 0.7901, "losers_mfe_r_median": 0.3924, "losers_reaching_1r_before_stop": 0.1525, "median_bars_to_mfe": 8.0, "note": "MAE/MFE on executable side incl. costs at fill; input for later ExitPolicy work"}`

oos: `{"p_mfe_ge_1r": 0.5632, "p_mfe_ge_2r": 0.0, "p_mfe_ge_3r": 0.0, "winners_mae_r_median": 0.3823, "winners_mae_r_q90": 0.8317, "losers_mfe_r_median": 0.4818, "losers_reaching_1r_before_stop": 0.2083, "median_bars_to_mfe": 11.0, "note": "MAE/MFE on executable side incl. costs at fill; input for later ExitPolicy work"}`

## SELECTED - BREAKOUT|buffer_atr=0.25,kind=orb,or_bars=6,stop_mode=or_mid exit {'kind': 'fixed_r', 'r': 2.5}

Verdict: **RESEARCH_CANDIDATE**. best of 40 variants of this family on train/validation; its OOS is one confirmation, not a selection-corrected estimate
Failed checks: none. Pooled PF (train+val+oos): 1.1441803295507242

### Metrics at BASE cost

| metric | train | validation | oos |
|---|---|---|---|
| trades | 204 | 100 | 87 |
| trades_per_day | 0.966825 | 0.980392 | 1.011628 |
| median_trades_per_day | 1.0 | 1.0 | 1.0 |
| zero_trade_days | 31 | 11 | 7 |
| net_pnl_eur | 431.585 | 1041.184375 | 29.19875 |
| expectancy_eur | 2.115613 | 10.411844 | 0.335618 |
| expectancy_r | 0.072797 | 0.247998 | 0.017624 |
| expectancy_r_ci95_trade_iid | [-0.125174, 0.279342] | [-0.05507, 0.573369] | [-0.287192, 0.339745] |
| expectancy_r_ci95_day_clustered | [-0.129601, 0.289734] | [-0.062488, 0.560744] | [-0.283722, 0.339458] |
| profit_factor | 1.078495 | 1.41157 | 1.012221 |
| win_rate | 0.348039 | 0.38 | 0.333333 |
| avg_winner_eur | 83.518433 | 93.972911 | 83.395776 |
| avg_loser_eur | -41.340028 | -40.803004 | -41.194461 |
| payoff_ratio | 2.02028 | 2.303088 | 2.024441 |
| max_drawdown_eur | 650.43 | 360.43 | 568.67125 |
| max_drawdown_pct | 6.232787 | 3.229929 | 5.473831 |
| max_consecutive_losses | 10 | 7 | 13 |
| worst_day_eur | -96.6375 | -96.175 | -92.52625 |
| best_day_eur | 124.8875 | 124.9125 | 123.0375 |
| exposure_time_frac | 0.287682 | 0.287227 | 0.317364 |
| avg_holding_minutes | 223.137255 | 219.7 | 235.287356 |
| avg_leverage | 1.743664 | 1.673743 | 1.778662 |
| max_leverage | 4.313876 | 4.43232 | 3.312764 |
| avg_entry_spread_pts | 1.02848 | 1.2887 | 1.276552 |
| est_execution_cost_eur | 278.4 | 141.63 | 134.76 |
| cost_per_trade_eur | 1.364706 | 1.4163 | 1.548966 |

### Cost stress (expectancy R / profit factor / net EUR)

| scenario | train | validation | oos |
|---|---|---|---|
| GROSS_REFERENCE | 0.082913 / 1.092782 / 512.165 | 0.261905 / 1.404495 / 1018.583125 | 0.026441 / 0.994428 / -13.498125 |
| BASE | 0.072797 / 1.078495 / 431.585 | 0.247998 / 1.41157 / 1041.184375 | 0.017624 / 1.012221 / 29.19875 |
| SPREAD_STRESS | 0.047921 / 1.047343 / 262.290625 | 0.246514 / 1.418128 / 1058.343125 | 0.01572 / 0.988976 / -26.70375 |
| SLIPPAGE_STRESS | 0.042262 / 1.033956 / 189.40875 | 0.207697 / 1.306053 / 796.715625 | 0.004364 / 0.99102 / -22.20625 |
| COMBINED_ADVERSE | 0.006927 / 0.985673 / -80.764375 | 0.115069 / 1.148821 / 399.438125 | 0.002631 / 0.975886 / -59.034375 |

### trend breakdown - development (train+validation)

| trend | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| RANGING | 253 | 0.113887 | 0.09843 | 0.347826 | 1.172994 | 1166.7375 |
| TRENDING | 51 | 0.21249 | 0.21799 | 0.411765 | 1.238414 | 306.031875 |

material difference: `{"material": false, "best": ["TRENDING", 0.21249, 51], "worst": ["RANGING", 0.113887, 253], "diff_r": 0.0986, "z": 0.41, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### vol breakdown - development (train+validation)

| vol | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| HIGH_VOL | 94 | 0.125336 | 0.165497 | 0.340426 | 1.168108 | 412.168125 |
| LOW_VOL | 33 | 0.217232 | 0.270789 | 0.424242 | 1.30823 | 248.569375 |
| NA | 22 | -0.001154 | 0.337997 | 0.318182 | 1.004651 | 3.01375 |
| NORMAL_VOL | 155 | 0.133713 | 0.124334 | 0.36129 | 1.196278 | 809.018125 |

material difference: `{"material": false, "best": ["LOW_VOL", 0.217232, 33], "worst": ["HIGH_VOL", 0.125336, 94], "diff_r": 0.0919, "z": 0.29, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### session breakdown - development (train+validation)

| session | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| MIDDAY_12_1530 | 2 | 0.744038 | 1.755962 | 0.5 | 2.127813 | 47.85875 |
| MORNING_10_12 | 159 | 0.135742 | 0.123518 | 0.358491 | 1.220716 | 908.8025 |
| OPEN_09_10 | 143 | 0.11594 | 0.13111 | 0.356643 | 1.133429 | 516.108125 |

material difference: `{"material": false, "best": ["MORNING_10_12", 0.135742, 159], "worst": ["OPEN_09_10", 0.11594, 143], "diff_r": 0.0198, "z": 0.11, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### trend breakdown - OOS

| trend | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| RANGING | 69 | 0.014537 | 0.18426 | 0.318841 | 1.004305 | 8.269375 |
| TRENDING | 18 | 0.029459 | 0.34829 | 0.388889 | 1.044679 | 20.929375 |

material difference: `{"material": null, "reason": "fewer than two groups with enough trades"}`

### vol breakdown - OOS

| vol | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| HIGH_VOL | 6 | -0.423842 | 0.58477 | 0.166667 | 0.529053 | -99.1425 |
| LOW_VOL | 28 | -0.16809 | 0.275842 | 0.25 | 0.760693 | -205.80375 |
| NORMAL_VOL | 53 | 0.165715 | 0.213262 | 0.396226 | 1.253378 | 334.145 |

material difference: `{"material": null, "reason": "fewer than two groups with enough trades"}`

### session breakdown - OOS

| session | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| MORNING_10_12 | 54 | -0.022136 | 0.203042 | 0.314815 | 0.992691 | -10.678125 |
| OPEN_09_10 | 33 | 0.082687 | 0.272145 | 0.363636 | 1.042958 | 39.876875 |

material difference: `{"material": false, "best": ["OPEN_09_10", 0.082687, 33], "worst": ["MORNING_10_12", -0.022136, 54], "diff_r": 0.1048, "z": 0.31, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### MFE/MAE

dev: `{"p_mfe_ge_1r": 0.5066, "p_mfe_ge_2r": 0.3224, "p_mfe_ge_3r": 0.0, "winners_mae_r_median": 0.5007, "winners_mae_r_q90": 0.7824, "losers_mfe_r_median": 0.4589, "losers_reaching_1r_before_stop": 0.2308, "median_bars_to_mfe": 11.0, "note": "MAE/MFE on executable side incl. costs at fill; input for later ExitPolicy work"}`

oos: `{"p_mfe_ge_1r": 0.5632, "p_mfe_ge_2r": 0.3103, "p_mfe_ge_3r": 0.0, "winners_mae_r_median": 0.4315, "winners_mae_r_q90": 0.7205, "losers_mfe_r_median": 0.6279, "losers_reaching_1r_before_stop": 0.3448, "median_bars_to_mfe": 17.0, "note": "MAE/MFE on executable side incl. costs at fill; input for later ExitPolicy work"}`

## CANONICAL - PULLBACK|depth=1.0,ema_fast=21,stop_mode=swing exit {'kind': 'fixed_r', 'r': 1.5}

Verdict: **REJECTED**. pre-registered canonical config, single test
Failed checks: ['expectancy_r>0 in validation (BASE)', 'expectancy_r>0 in oos (BASE)', 'expectancy_r>0 in dev (BASE)', 'expectancy_r>0 in dev (SPREAD_STRESS)', 'expectancy_r>0 in oos (SPREAD_STRESS)', 'expectancy_r>0 in dev (SLIPPAGE_STRESS)', 'expectancy_r>0 in oos (SLIPPAGE_STRESS)', 'pooled_profit_factor>min', 'oos_profit_factor>1', 'max_drawdown_pct<=limit']. Pooled PF (train+val+oos): 0.8913625198628756

### Metrics at BASE cost

| metric | train | validation | oos |
|---|---|---|---|
| trades | 624 | 321 | 236 |
| trades_per_day | 2.957346 | 3.147059 | 2.744186 |
| median_trades_per_day | 3.0 | 3.0 | 3.0 |
| zero_trade_days | 6 | 1 | 3 |
| net_pnl_eur | 383.242066 | -1779.870932 | -1960.134911 |
| expectancy_eur | 0.61417 | -5.544769 | -8.305656 |
| expectancy_r | 0.011524 | -0.122476 | -0.172572 |
| expectancy_r_ci95_trade_iid | [-0.086567, 0.102724] | [-0.249894, 0.000135] | [-0.317802, -0.026826] |
| expectancy_r_ci95_day_clustered | [-0.081965, 0.104208] | [-0.253281, 0.003082] | [-0.298907, -0.038129] |
| profit_factor | 1.024624 | 0.792898 | 0.709208 |
| win_rate | 0.429487 | 0.364486 | 0.347458 |
| avg_winner_eur | 59.504212 | 58.24204 | 58.299415 |
| avg_loser_eur | -43.718783 | -42.12838 | -43.770694 |
| payoff_ratio | 1.361067 | 1.382489 | 1.331928 |
| max_drawdown_eur | 1031.642313 | 2306.441513 | 2212.476717 |
| max_drawdown_pct | 9.747303 | 22.174699 | 21.812064 |
| max_consecutive_losses | 10 | 14 | 11 |
| worst_day_eur | -276.387799 | -231.615971 | -230.777421 |
| best_day_eur | 273.416494 | 224.783731 | 138.090693 |
| exposure_time_frac | 0.322031 | 0.359459 | 0.32845 |
| avg_holding_minutes | 81.658654 | 85.654206 | 89.766949 |
| avg_leverage | 3.193523 | 3.041935 | 3.136059 |
| max_leverage | 9.795328 | 9.95948 | 9.916 |
| avg_entry_spread_pts | 1.140881 | 1.380841 | 1.422585 |
| est_execution_cost_eur | 1593.9925 | 876.11 | 650.0875 |
| cost_per_trade_eur | 2.554475 | 2.729315 | 2.754608 |

### Cost stress (expectancy R / profit factor / net EUR)

| scenario | train | validation | oos |
|---|---|---|---|
| GROSS_REFERENCE | 0.056705 / 1.096927 / 1469.196869 | -0.055989 / 0.899254 / -837.153731 | -0.116836 / 0.810458 / -1232.502498 |
| BASE | 0.011524 / 1.024624 / 383.242066 | -0.122476 / 0.792898 / -1779.870932 | -0.172572 / 0.709208 / -1960.134911 |
| SPREAD_STRESS | -0.005053 / 0.992887 / -111.739156 | -0.146853 / 0.759145 / -2080.987984 | -0.218919 / 0.648115 / -2463.126148 |
| SLIPPAGE_STRESS | -0.023041 / 0.964628 / -563.964496 | -0.168932 / 0.734399 / -2341.876489 | -0.230387 / 0.62855 / -2619.806793 |
| COMBINED_ADVERSE | -0.059411 / 0.913021 / -1395.496168 | -0.183454 / 0.705655 / -2578.118748 | -0.280204 / 0.569616 / -3056.286455 |

### trend breakdown - development (train+validation)

| trend | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| RANGING | 897 | -0.035904 | 0.039215 | 0.405797 | 0.937214 | -1445.006043 |
| TRENDING | 48 | 0.001702 | 0.167855 | 0.4375 | 1.04232 | 48.377176 |

material difference: `{"material": false, "best": ["TRENDING", 0.001702, 48], "worst": ["RANGING", -0.035904, 897], "diff_r": 0.0376, "z": 0.22, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### vol breakdown - development (train+validation)

| vol | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| HIGH_VOL | 280 | -0.002211 | 0.071169 | 0.421429 | 1.021647 | 147.189272 |
| LOW_VOL | 107 | -0.105361 | 0.112183 | 0.364486 | 0.82052 | -527.368018 |
| NA | 55 | -0.066494 | 0.160688 | 0.4 | 0.875753 | -190.012915 |
| NORMAL_VOL | 503 | -0.032951 | 0.05203 | 0.409543 | 0.935889 | -826.437206 |

material difference: `{"material": false, "best": ["HIGH_VOL", -0.002211, 280], "worst": ["LOW_VOL", -0.105361, 107], "diff_r": 0.1031, "z": 0.78, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### session breakdown - development (train+validation)

| session | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| LATE_1730_2130 | 175 | 0.057176 | 0.083617 | 0.468571 | 1.133411 | 503.595498 |
| MIDDAY_12_1530 | 319 | -0.06076 | 0.067561 | 0.388715 | 0.914662 | -745.261941 |
| MORNING_10_12 | 154 | -0.289682 | 0.091699 | 0.285714 | 0.587236 | -2024.06707 |
| OPEN_09_10 | 141 | 0.030898 | 0.103241 | 0.41844 | 1.026488 | 93.877673 |
| US_OVERLAP_1530_1730 | 156 | 0.112224 | 0.090815 | 0.487179 | 1.242075 | 775.226973 |

material difference: `{"material": true, "best": ["US_OVERLAP_1530_1730", 0.112224, 156], "worst": ["MORNING_10_12", -0.289682, 154], "diff_r": 0.4019, "z": 3.11, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### trend breakdown - OOS

| trend | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| RANGING | 218 | -0.114395 | 0.078376 | 0.37156 | 0.788098 | -1267.112006 |
| TRENDING | 18 | -0.877152 | 0.139871 | 0.055556 | 0.089304 | -693.022905 |

material difference: `{"material": null, "reason": "fewer than two groups with enough trades"}`

### vol breakdown - OOS

| vol | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| HIGH_VOL | 17 | -0.021343 | 0.299637 | 0.411765 | 0.941829 | -26.570474 |
| LOW_VOL | 84 | -0.188221 | 0.125358 | 0.333333 | 0.678351 | -796.984386 |
| NORMAL_VOL | 135 | -0.181878 | 0.097465 | 0.348148 | 0.70138 | -1136.580051 |

material difference: `{"material": false, "best": ["NORMAL_VOL", -0.181878, 135], "worst": ["LOW_VOL", -0.188221, 84], "diff_r": 0.0063, "z": 0.04, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### session breakdown - OOS

| session | n | expectancy_r | se_r | win_rate | profit_factor | net_pnl_eur |
|---|---|---|---|---|---|---|
| LATE_1730_2130 | 49 | -0.371764 | 0.136917 | 0.285714 | 0.425078 | -854.05229 |
| MIDDAY_12_1530 | 71 | -0.057789 | 0.145855 | 0.380282 | 0.891306 | -216.994969 |
| MORNING_10_12 | 35 | -0.392816 | 0.182549 | 0.257143 | 0.476532 | -609.424471 |
| OPEN_09_10 | 38 | 0.116803 | 0.199073 | 0.473684 | 1.150355 | 130.412917 |
| US_OVERLAP_1530_1730 | 43 | -0.21157 | 0.171679 | 0.325581 | 0.665847 | -410.076098 |

material difference: `{"material": false, "best": ["OPEN_09_10", 0.116803, 38], "worst": ["MORNING_10_12", -0.392816, 35], "diff_r": 0.5096, "z": 1.89, "note": "descriptive only: many groups are compared, no multiplicity correction"}`

### MFE/MAE

dev: `{"p_mfe_ge_1r": 0.4656, "p_mfe_ge_2r": 0.0, "p_mfe_ge_3r": 0.0, "winners_mae_r_median": 0.3789, "winners_mae_r_q90": 0.7824, "losers_mfe_r_median": 0.3568, "losers_reaching_1r_before_stop": 0.1429, "median_bars_to_mfe": 3.0, "note": "MAE/MFE on executable side incl. costs at fill; input for later ExitPolicy work"}`

oos: `{"p_mfe_ge_1r": 0.3856, "p_mfe_ge_2r": 0.0, "p_mfe_ge_3r": 0.0, "winners_mae_r_median": 0.3324, "winners_mae_r_q90": 0.768, "losers_mfe_r_median": 0.3472, "losers_reaching_1r_before_stop": 0.0974, "median_bars_to_mfe": 3.0, "note": "MAE/MFE on executable side incl. costs at fill; input for later ExitPolicy work"}`
