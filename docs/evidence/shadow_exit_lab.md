# Shadow exit lab (Lane W) - offline run

`swl-study-1` / lab `swl-1` / policies `eeq-policies-2` / baseline `fixed_1_5r`.

**hypotheses only; forward evidence decides promotion; no policy promoted from this table.** Hindsight diagnostics on development bars; no edge claim.

## Headline: median R per policy, family entries vs random-entry control

| market | n (clusters) | fixed_1_5r | TP1_only | TP1_plus_runner | TP1_TP2_runner | pure_structure_trail | break_even_plus_runner | momentum_failure | time_decay | failed_move_exit | struct_tp1_tp2 | breakeven_lock_fixed | eod_forced_flat |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BRENT | 2936 (1651) | -1.00 / -1.00 | 0.15 / 0.14 | -0.17 / -0.17 | -0.18 / -0.17 | -0.31 / -0.26 | -0.29 / -0.25 | -0.27 / -0.23 | -0.40 / -0.33 | -0.32 / - | -0.07 / -0.06 | -0.41 / -0.40 | -1.00 / -1.00 |
| BTCUSD | 1795 (1041) | -1.00 / -1.00 | 0.11 / 0.17 | -0.24 / -0.20 | -0.25 / -0.20 | -0.37 / -0.26 | -0.35 / -0.24 | -0.29 / -0.24 | -0.50 / -0.32 | -0.33 / - | -0.33 / -0.16 | -1.00 / -0.42 | -1.00 / -1.00 |
| EURUSD | 2867 (1562) | -1.00 / -1.00 | 0.23 / 0.19 | 0.09 / -0.14 | 0.11 / -0.12 | -0.82 / -0.71 | -0.64 / -0.58 | -0.46 / -0.41 | -1.00 / -1.00 | - / - | -0.31 / -0.32 | -1.00 / -1.00 | -1.00 / -1.00 |
| GER40 | 9644 (2970) | -1.00 / -1.00 | 0.16 / 0.15 | 0.08 / 0.02 | 0.09 / -0.01 | -0.71 / -0.65 | -0.52 / -0.51 | -0.39 / -0.40 | -1.00 / -1.00 | - / - | -0.26 / -0.32 | -1.00 / -1.00 | -1.00 / -1.00 |
| NAS100 | 5390 (1423) | -1.00 / -1.00 | 0.11 / 0.16 | 0.06 / 0.06 | 0.07 / 0.06 | -0.68 / -0.63 | -0.42 / -0.46 | -0.36 / -0.37 | -1.00 / -1.00 | - / - | -0.08 / -0.18 | -0.40 / -0.64 | -1.00 / -1.00 |
| SPX500 | 2990 (1341) | -1.00 / -1.00 | 0.15 / 0.17 | 0.07 / -0.12 | 0.07 / -0.13 | -0.72 / -0.67 | -0.42 / -0.52 | -0.36 / -0.37 | -1.00 / -1.00 | - / - | -0.19 / -0.30 | -0.62 / -1.00 | -1.00 / -1.00 |
| XAUUSD | 7180 (1782) | -1.00 / -1.00 | 0.19 / 0.19 | 0.10 / 0.07 | 0.11 / 0.07 | -0.84 / -0.66 | -0.62 / -0.51 | -0.48 / -0.36 | -1.00 / -1.00 | - / - | -0.29 / -0.24 | -1.00 / -1.00 | -1.00 / -1.00 |

Cell format: median R of the family entries / median R of the random-entry control (same policies, same bars source). A median is not an expectancy; compare means and the paired differences below before reading anything into a policy.

## All active markets pooled (event-cluster aware)

n 32802, event clusters 11770.

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 22386 | 0.32 | -0.053 | 0.079 | 0.26 | 0.846 | 0.012 | -0.074 | 0.013 (22386/9118) |
| TP1_only | 28929 | 0.12 | -0.066 | 0.161 | 0.46 | 0.647 | 0.168 | -0.087 | -0.009 (28929/10674) |
| TP1_plus_runner | 28929 | 0.12 | -0.048 | 0.072 | 0.24 | 0.849 | 0.016 | -0.069 | 0.010 (28929/10674) |
| break_even_plus_runner | 32802 | 0.00 | -0.053 | -0.451 | 0.15 | 1.282 | -0.424 | -0.067 | 0.006 (32802/11770) |
| breakeven_lock_fixed | 32802 | 0.00 | -0.058 | -1.000 | 0.33 | 0.970 | -1.000 | -0.078 | -0.000 (32802/11770) |
| eod_forced_flat | 32802 | 0.00 | -0.017 | -1.000 | 0.19 | 1.732 | -1.000 | -0.060 | 0.041 (32802/11770) |
| failed_move_exit | 4619 | 0.86 | -0.109 | -0.326 | 0.27 | 0.578 | - | - | 0.007 (4619/2633) |
| fixed_1_5r | 32802 | 0.00 | -0.058 | -1.000 | 0.38 | 1.005 | -1.000 | -0.080 | - |
| momentum_failure | 32802 | 0.00 | -0.056 | -0.368 | 0.30 | 0.839 | -0.336 | -0.080 | 0.003 (32802/11770) |
| pure_structure_trail | 32802 | 0.00 | -0.051 | -0.634 | 0.17 | 1.368 | -0.551 | -0.072 | 0.008 (32802/11770) |
| struct_tp1_tp2 | 22386 | 0.32 | -0.071 | -0.252 | 0.30 | 0.925 | -0.254 | -0.091 | -0.005 (22386/9118) |
| time_decay | 32802 | 0.00 | -0.056 | -1.000 | 0.38 | 0.986 | -1.000 | -0.080 | 0.002 (32802/11770) |

## Forward hook cost and default

Measured on 32802 real development-bar entries (12 policies, one process, uncontended-per-worker): mean 33.9 ms per entry, worst per-market p95 102 ms. The hook runs at outcome / label time on the runner thread (never in the order path, exception-contained, capped per cycle). Because up to 2 x the per-cycle cap of entries can be evaluated in one 15-minute label cycle on the same thread that manages open positions, the runner flag `shadow_exit_lab_enabled` (CLI `--shadow-exit-lab`) defaults to OFF; enable it deliberately once the live runner has been observed with it.

## Notes and caveats

- Hindsight diagnostics on a development window; entries of overlapping signals are not independent (event clusters; four STRUCT variants of one break are ONE observation); no edge claim, no promotion.
- Development end 2026-08-31 (Berlin date) is enforced by the existing loaders: no holdout bar was opened.
- Family generators run on Train-fitted frozen thresholds (fit_end 2026-06-30 for the five core markets): the evaluation window overlaps that fit window (in-sample for the thresholds) - another reason the numbers are descriptive only.
- BTCUSD M5 history lacks Oct-2025 and Mar-2026 (DST fold months); BTCUSD/BRENT have no frozen split and their STRUCT constants are discovery placeholders.
- The lab is CAUSAL: ORACLE / hindsight levels are not used; structural TP1/TP2/trail use only closed bars up to each decision time; absent levels -> NOT_APPLICABLE (never invented).
- Fills at the next bar open (long: ask), stop-first bar semantics, spread-adjusted gross R, no commission / slippage / swap; intrabar order of high/low unknown (conservative: adverse first).
- Policy parameters are the predeclared constants of POLICY_SET_VERSION; nothing was searched or tuned on these results; failed_move_exit applies to STRUCT entries only (the broken range edge); TP1_TP2_runner needs a structural TP2.
- Random-entry control: random direction, random operating-window decision bars, stop distance (ATR) bootstrapped from the real entries of the same market, fixed per-market seed; no family logic, so failed_move_exit is NOT_APPLICABLE there. It shows what a policy yields on noise given the same cost and window structure, not an edge benchmark.
- Rows are overlapping paths on the same bars: the pooled table adds no independent evidence beyond the event clusters shown.

## BRENT

Family entries: n 2936, independent event clusters 1651, same-entry assertion OK; random-entry control: n 2936 (seed 20262226). Lab cost per entry (12 policies, one process): mean 37.3 ms, median 33.1 ms, p95 82.2 ms (mean 115 bars).

### All family entries (real) vs random-entry control

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 1479 | 0.50 | -0.125 | -0.176 | 0.29 | 0.636 | -0.169 | -0.114 | 0.007 (1479/988) |
| TP1_only | 1945 | 0.34 | -0.111 | 0.151 | 0.56 | 0.548 | 0.144 | -0.122 | -0.001 (1945/1236) |
| TP1_plus_runner | 1945 | 0.34 | -0.112 | -0.166 | 0.27 | 0.600 | -0.170 | -0.116 | -0.001 (1945/1236) |
| break_even_plus_runner | 2936 | 0.00 | -0.104 | -0.291 | 0.20 | 0.827 | -0.248 | -0.121 | 0.010 (2936/1651) |
| breakeven_lock_fixed | 2936 | 0.00 | -0.118 | -0.414 | 0.34 | 0.864 | -0.400 | -0.149 | -0.003 (2936/1651) |
| eod_forced_flat | 2936 | 0.00 | -0.093 | -1.000 | 0.22 | 1.357 | -1.000 | -0.125 | 0.021 (2936/1651) |
| failed_move_exit | 2835 | 0.03 | -0.107 | -0.325 | 0.27 | 0.584 | - | - | -0.002 (2835/1598) |
| fixed_1_5r | 2936 | 0.00 | -0.115 | -1.000 | 0.38 | 0.889 | -1.000 | -0.149 | - |
| momentum_failure | 2936 | 0.00 | -0.120 | -0.267 | 0.24 | 0.582 | -0.233 | -0.143 | -0.006 (2936/1651) |
| pure_structure_trail | 2936 | 0.00 | -0.103 | -0.310 | 0.20 | 0.850 | -0.262 | -0.125 | 0.012 (2936/1651) |
| struct_tp1_tp2 | 1479 | 0.50 | -0.135 | -0.067 | 0.38 | 0.746 | -0.060 | -0.133 | -0.002 (1479/988) |
| time_decay | 2936 | 0.00 | -0.097 | -0.396 | 0.37 | 0.796 | -0.331 | -0.144 | 0.017 (2936/1651) |

### BRENT|STRUCT|breakout (n 961, clusters 961)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 401 | 0.58 | -0.127 | -0.147 | 0.29 | 0.486 | - | - | -0.018 (401/401) |
| TP1_only | 564 | 0.41 | -0.109 | 0.146 | 0.55 | 0.503 | - | - | -0.006 (564/564) |
| TP1_plus_runner | 564 | 0.41 | -0.119 | -0.122 | 0.27 | 0.496 | - | - | -0.017 (564/564) |
| break_even_plus_runner | 961 | 0.00 | -0.097 | -0.260 | 0.20 | 0.764 | - | - | 0.019 (961/961) |
| breakeven_lock_fixed | 961 | 0.00 | -0.121 | -0.336 | 0.33 | 0.855 | - | - | -0.005 (961/961) |
| eod_forced_flat | 961 | 0.00 | -0.107 | -1.000 | 0.23 | 1.285 | - | - | 0.009 (961/961) |
| failed_move_exit | 923 | 0.04 | -0.098 | -0.295 | 0.25 | 0.518 | - | - | 0.004 (923/923) |
| fixed_1_5r | 961 | 0.00 | -0.117 | -1.000 | 0.37 | 0.873 | - | - | - |
| momentum_failure | 961 | 0.00 | -0.117 | -0.260 | 0.22 | 0.532 | - | - | -0.000 (961/961) |
| pure_structure_trail | 961 | 0.00 | -0.102 | -0.266 | 0.20 | 0.768 | - | - | 0.015 (961/961) |
| struct_tp1_tp2 | 401 | 0.58 | -0.101 | 0.150 | 0.41 | 0.656 | - | - | 0.008 (401/401) |
| time_decay | 961 | 0.00 | -0.096 | -0.343 | 0.36 | 0.769 | - | - | 0.020 (961/961) |

### BRENT|STRUCT|confirmed (n 757, clusters 757)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 284 | 0.62 | -0.071 | -0.064 | 0.30 | 0.422 | - | - | -0.041 (284/284) |
| TP1_only | 420 | 0.45 | -0.054 | 0.169 | 0.58 | 0.433 | - | - | -0.023 (420/420) |
| TP1_plus_runner | 420 | 0.45 | -0.069 | -0.097 | 0.28 | 0.424 | - | - | -0.038 (420/420) |
| break_even_plus_runner | 757 | 0.00 | -0.058 | -0.245 | 0.21 | 0.702 | - | - | -0.008 (757/757) |
| breakeven_lock_fixed | 757 | 0.00 | -0.063 | -0.201 | 0.34 | 0.812 | - | - | -0.013 (757/757) |
| eod_forced_flat | 757 | 0.00 | -0.044 | -1.000 | 0.24 | 1.224 | - | - | 0.006 (757/757) |
| failed_move_exit | 744 | 0.02 | -0.072 | -0.321 | 0.28 | 0.546 | - | - | -0.021 (744/744) |
| fixed_1_5r | 757 | 0.00 | -0.051 | -0.429 | 0.38 | 0.822 | - | - | - |
| momentum_failure | 757 | 0.00 | -0.083 | -0.239 | 0.23 | 0.482 | - | - | -0.032 (757/757) |
| pure_structure_trail | 757 | 0.00 | -0.059 | -0.246 | 0.21 | 0.704 | - | - | -0.008 (757/757) |
| struct_tp1_tp2 | 284 | 0.62 | -0.064 | 0.183 | 0.41 | 0.601 | - | - | -0.034 (284/284) |
| time_decay | 757 | 0.00 | -0.030 | -0.220 | 0.38 | 0.702 | - | - | 0.020 (757/757) |

### BRENT|STRUCT|fade (n 592, clusters 592)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 533 | 0.10 | -0.179 | -0.447 | 0.29 | 0.964 | - | - | 0.050 (533/533) |
| TP1_only | 566 | 0.04 | -0.186 | -1.000 | 0.53 | 0.744 | - | - | 0.044 (566/566) |
| TP1_plus_runner | 566 | 0.04 | -0.162 | -0.446 | 0.28 | 0.956 | - | - | 0.068 (566/566) |
| break_even_plus_runner | 592 | 0.00 | -0.196 | -0.700 | 0.17 | 1.198 | - | - | 0.044 (592/592) |
| breakeven_lock_fixed | 592 | 0.00 | -0.230 | -1.000 | 0.32 | 0.985 | - | - | 0.010 (592/592) |
| eod_forced_flat | 592 | 0.00 | -0.154 | -1.000 | 0.16 | 1.742 | - | - | 0.086 (592/592) |
| failed_move_exit | 562 | 0.05 | -0.203 | -0.541 | 0.30 | 0.819 | - | - | 0.018 (562/562) |
| fixed_1_5r | 592 | 0.00 | -0.240 | -1.000 | 0.36 | 1.045 | - | - | - |
| momentum_failure | 592 | 0.00 | -0.204 | -0.562 | 0.29 | 0.897 | - | - | 0.036 (592/592) |
| pure_structure_trail | 592 | 0.00 | -0.179 | -0.793 | 0.17 | 1.297 | - | - | 0.061 (592/592) |
| struct_tp1_tp2 | 533 | 0.10 | -0.218 | -1.000 | 0.33 | 0.949 | - | - | 0.011 (533/533) |
| time_decay | 592 | 0.00 | -0.239 | -1.000 | 0.36 | 1.030 | - | - | 0.001 (592/592) |

### BRENT|STRUCT|retest (n 626, clusters 626)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 261 | 0.58 | -0.072 | -0.127 | 0.29 | 0.428 | - | - | 0.011 (261/261) |
| TP1_only | 395 | 0.37 | -0.069 | 0.183 | 0.58 | 0.455 | - | - | -0.032 (395/395) |
| TP1_plus_runner | 395 | 0.37 | -0.077 | -0.137 | 0.27 | 0.426 | - | - | -0.040 (395/395) |
| break_even_plus_runner | 626 | 0.00 | -0.085 | -0.249 | 0.20 | 0.724 | - | - | -0.014 (626/626) |
| breakeven_lock_fixed | 626 | 0.00 | -0.074 | -0.249 | 0.34 | 0.828 | - | - | -0.003 (626/626) |
| eod_forced_flat | 626 | 0.00 | -0.073 | -1.000 | 0.23 | 1.265 | - | - | -0.002 (626/626) |
| failed_move_exit | 606 | 0.03 | -0.076 | -0.293 | 0.27 | 0.513 | - | - | -0.007 (606/606) |
| fixed_1_5r | 626 | 0.00 | -0.070 | -0.598 | 0.38 | 0.845 | - | - | - |
| momentum_failure | 626 | 0.00 | -0.091 | -0.227 | 0.23 | 0.484 | - | - | -0.021 (626/626) |
| pure_structure_trail | 626 | 0.00 | -0.087 | -0.253 | 0.20 | 0.727 | - | - | -0.016 (626/626) |
| struct_tp1_tp2 | 261 | 0.58 | -0.094 | 0.106 | 0.40 | 0.626 | - | - | -0.010 (261/261) |
| time_decay | 626 | 0.00 | -0.046 | -0.255 | 0.38 | 0.729 | - | - | 0.024 (626/626) |


## BTCUSD

Family entries: n 1795, independent event clusters 1041, same-entry assertion OK; random-entry control: n 1795 (seed 20279131). Lab cost per entry (12 policies, one process): mean 37.9 ms, median 31.7 ms, p95 90.5 ms (mean 132 bars).

### All family entries (real) vs random-entry control

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 986 | 0.45 | -0.125 | -0.252 | 0.26 | 0.680 | -0.196 | -0.125 | 0.042 (986/659) |
| TP1_only | 1287 | 0.28 | -0.169 | 0.114 | 0.51 | 0.662 | 0.167 | -0.145 | -0.038 (1287/827) |
| TP1_plus_runner | 1287 | 0.28 | -0.113 | -0.243 | 0.26 | 0.638 | -0.199 | -0.125 | 0.018 (1287/827) |
| break_even_plus_runner | 1795 | 0.00 | -0.075 | -0.347 | 0.20 | 0.850 | -0.242 | -0.100 | 0.060 (1795/1041) |
| breakeven_lock_fixed | 1795 | 0.00 | -0.137 | -1.000 | 0.34 | 0.895 | -0.419 | -0.103 | -0.002 (1795/1041) |
| eod_forced_flat | 1795 | 0.00 | -0.047 | -1.000 | 0.21 | 1.526 | -1.000 | -0.061 | 0.088 (1795/1041) |
| failed_move_exit | 1784 | 0.01 | -0.111 | -0.328 | 0.27 | 0.569 | - | - | 0.022 (1784/1035) |
| fixed_1_5r | 1795 | 0.00 | -0.135 | -1.000 | 0.38 | 0.921 | -1.000 | -0.106 | - |
| momentum_failure | 1795 | 0.00 | -0.112 | -0.292 | 0.25 | 0.572 | -0.239 | -0.126 | 0.023 (1795/1041) |
| pure_structure_trail | 1795 | 0.00 | -0.070 | -0.367 | 0.20 | 0.878 | -0.256 | -0.098 | 0.065 (1795/1041) |
| struct_tp1_tp2 | 986 | 0.45 | -0.191 | -0.328 | 0.36 | 0.843 | -0.157 | -0.154 | -0.024 (986/659) |
| time_decay | 1795 | 0.00 | -0.130 | -0.500 | 0.35 | 0.814 | -0.324 | -0.119 | 0.004 (1795/1041) |

### BTCUSD|STRUCT|breakout (n 580, clusters 580)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 260 | 0.55 | -0.103 | -0.211 | 0.26 | 0.532 | - | - | 0.089 (260/260) |
| TP1_only | 357 | 0.38 | -0.161 | 0.160 | 0.52 | 0.608 | - | - | -0.026 (357/357) |
| TP1_plus_runner | 357 | 0.38 | -0.117 | -0.220 | 0.25 | 0.528 | - | - | 0.018 (357/357) |
| break_even_plus_runner | 580 | 0.00 | -0.111 | -0.338 | 0.19 | 0.787 | - | - | 0.071 (580/580) |
| breakeven_lock_fixed | 580 | 0.00 | -0.165 | -1.000 | 0.34 | 0.897 | - | - | 0.017 (580/580) |
| eod_forced_flat | 580 | 0.00 | -0.087 | -1.000 | 0.21 | 1.426 | - | - | 0.095 (580/580) |
| failed_move_exit | 573 | 0.01 | -0.112 | -0.304 | 0.24 | 0.504 | - | - | 0.069 (573/573) |
| fixed_1_5r | 580 | 0.00 | -0.182 | -1.000 | 0.36 | 0.930 | - | - | - |
| momentum_failure | 580 | 0.00 | -0.124 | -0.299 | 0.22 | 0.539 | - | - | 0.058 (580/580) |
| pure_structure_trail | 580 | 0.00 | -0.111 | -0.345 | 0.19 | 0.792 | - | - | 0.072 (580/580) |
| struct_tp1_tp2 | 260 | 0.55 | -0.210 | -0.253 | 0.38 | 0.763 | - | - | -0.017 (260/260) |
| time_decay | 580 | 0.00 | -0.175 | -0.478 | 0.33 | 0.804 | - | - | 0.007 (580/580) |

### BTCUSD|STRUCT|confirmed (n 450, clusters 450)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 196 | 0.56 | -0.098 | -0.199 | 0.26 | 0.486 | - | - | 0.093 (196/196) |
| TP1_only | 291 | 0.35 | -0.181 | 0.155 | 0.51 | 0.594 | - | - | -0.057 (291/291) |
| TP1_plus_runner | 291 | 0.35 | -0.096 | -0.203 | 0.26 | 0.481 | - | - | 0.028 (291/291) |
| break_even_plus_runner | 450 | 0.00 | -0.070 | -0.302 | 0.19 | 0.733 | - | - | 0.049 (450/450) |
| breakeven_lock_fixed | 450 | 0.00 | -0.133 | -0.644 | 0.32 | 0.883 | - | - | -0.014 (450/450) |
| eod_forced_flat | 450 | 0.00 | -0.036 | -1.000 | 0.21 | 1.383 | - | - | 0.084 (450/450) |
| failed_move_exit | 450 | 0.00 | -0.089 | -0.325 | 0.27 | 0.538 | - | - | 0.030 (450/450) |
| fixed_1_5r | 450 | 0.00 | -0.119 | -1.000 | 0.37 | 0.896 | - | - | - |
| momentum_failure | 450 | 0.00 | -0.091 | -0.277 | 0.23 | 0.481 | - | - | 0.028 (450/450) |
| pure_structure_trail | 450 | 0.00 | -0.069 | -0.311 | 0.20 | 0.739 | - | - | 0.051 (450/450) |
| struct_tp1_tp2 | 196 | 0.56 | -0.218 | -0.211 | 0.36 | 0.750 | - | - | -0.027 (196/196) |
| time_decay | 450 | 0.00 | -0.105 | -0.337 | 0.33 | 0.748 | - | - | 0.015 (450/450) |

### BTCUSD|STRUCT|fade (n 392, clusters 392)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 358 | 0.09 | -0.164 | -0.478 | 0.26 | 0.995 | - | - | -0.041 (358/358) |
| TP1_only | 380 | 0.03 | -0.133 | -1.000 | 0.49 | 0.775 | - | - | -0.015 (380/380) |
| TP1_plus_runner | 380 | 0.03 | -0.130 | -0.457 | 0.26 | 0.979 | - | - | -0.012 (380/380) |
| break_even_plus_runner | 392 | 0.00 | -0.059 | -0.587 | 0.21 | 1.165 | - | - | 0.047 (392/392) |
| breakeven_lock_fixed | 392 | 0.00 | -0.107 | -1.000 | 0.39 | 0.912 | - | - | -0.002 (392/392) |
| eod_forced_flat | 392 | 0.00 | -0.073 | -1.000 | 0.19 | 1.905 | - | - | 0.032 (392/392) |
| failed_move_exit | 389 | 0.01 | -0.158 | -0.514 | 0.34 | 0.774 | - | - | -0.059 (389/389) |
| fixed_1_5r | 392 | 0.00 | -0.105 | -1.000 | 0.42 | 0.957 | - | - | - |
| momentum_failure | 392 | 0.00 | -0.139 | -0.496 | 0.32 | 0.820 | - | - | -0.034 (392/392) |
| pure_structure_trail | 392 | 0.00 | -0.037 | -0.715 | 0.22 | 1.271 | - | - | 0.068 (392/392) |
| struct_tp1_tp2 | 358 | 0.09 | -0.146 | -1.000 | 0.35 | 0.975 | - | - | -0.023 (358/358) |
| time_decay | 392 | 0.00 | -0.125 | -1.000 | 0.40 | 0.947 | - | - | -0.019 (392/392) |

### BTCUSD|STRUCT|retest (n 373, clusters 373)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 172 | 0.54 | -0.106 | -0.203 | 0.25 | 0.470 | - | - | 0.084 (172/172) |
| TP1_only | 259 | 0.31 | -0.218 | 0.026 | 0.51 | 0.646 | - | - | -0.066 (259/259) |
| TP1_plus_runner | 259 | 0.31 | -0.101 | -0.207 | 0.26 | 0.467 | - | - | 0.051 (259/259) |
| break_even_plus_runner | 373 | 0.00 | -0.044 | -0.325 | 0.20 | 0.758 | - | - | 0.067 (373/373) |
| breakeven_lock_fixed | 373 | 0.00 | -0.130 | -0.837 | 0.34 | 0.889 | - | - | -0.019 (373/373) |
| eod_forced_flat | 373 | 0.00 | 0.030 | -1.000 | 0.21 | 1.457 | - | - | 0.142 (373/373) |
| failed_move_exit | 372 | 0.00 | -0.084 | -0.287 | 0.26 | 0.493 | - | - | 0.025 (372/372) |
| fixed_1_5r | 373 | 0.00 | -0.111 | -1.000 | 0.39 | 0.898 | - | - | - |
| momentum_failure | 373 | 0.00 | -0.090 | -0.256 | 0.24 | 0.473 | - | - | 0.022 (373/373) |
| pure_structure_trail | 373 | 0.00 | -0.043 | -0.340 | 0.20 | 0.765 | - | - | 0.068 (373/373) |
| struct_tp1_tp2 | 172 | 0.54 | -0.222 | -0.319 | 0.37 | 0.795 | - | - | -0.032 (172/172) |
| time_decay | 373 | 0.00 | -0.098 | -0.421 | 0.36 | 0.767 | - | - | 0.013 (373/373) |


## EURUSD

Family entries: n 2867, independent event clusters 1562, same-entry assertion OK; random-entry control: n 2867 (seed 20263469). Lab cost per entry (12 policies, one process): mean 22.2 ms, median 17.4 ms, p95 56.0 ms (mean 76 bars).

### All family entries (real) vs random-entry control

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 1834 | 0.36 | -0.087 | 0.107 | 0.27 | 0.880 | -0.117 | -0.105 | 0.008 (1834/1103) |
| TP1_only | 2345 | 0.18 | -0.107 | 0.228 | 0.48 | 0.722 | 0.187 | -0.132 | 0.013 (2345/1358) |
| TP1_plus_runner | 2345 | 0.18 | -0.104 | 0.091 | 0.25 | 0.889 | -0.136 | -0.112 | 0.016 (2345/1358) |
| break_even_plus_runner | 2867 | 0.00 | -0.131 | -0.641 | 0.14 | 1.358 | -0.583 | -0.138 | -0.023 (2867/1562) |
| breakeven_lock_fixed | 2867 | 0.00 | -0.115 | -1.000 | 0.31 | 1.011 | -1.000 | -0.144 | -0.007 (2867/1562) |
| eod_forced_flat | 2867 | 0.00 | -0.142 | -1.000 | 0.15 | 1.907 | -1.000 | -0.199 | -0.034 (2867/1562) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 2867 | 0.00 | -0.108 | -1.000 | 0.37 | 1.048 | -1.000 | -0.163 | - |
| momentum_failure | 2867 | 0.00 | -0.105 | -0.459 | 0.29 | 0.894 | -0.407 | -0.133 | 0.003 (2867/1562) |
| pure_structure_trail | 2867 | 0.00 | -0.139 | -0.820 | 0.14 | 1.469 | -0.708 | -0.160 | -0.031 (2867/1562) |
| struct_tp1_tp2 | 1834 | 0.36 | -0.109 | -0.315 | 0.30 | 0.995 | -0.317 | -0.146 | -0.015 (1834/1103) |
| time_decay | 2867 | 0.00 | -0.109 | -1.000 | 0.37 | 1.040 | -1.000 | -0.159 | -0.001 (2867/1562) |

### EURUSD|EOD|continue (n 77, clusters 77)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 27 | 0.65 | -0.004 | 0.126 | 0.31 | 0.879 | - | - | -0.038 (27/27, n too small) |
| TP1_only | 66 | 0.14 | -0.047 | 0.249 | 0.49 | 0.556 | - | - | -0.068 (66/66) |
| TP1_plus_runner | 66 | 0.14 | -0.044 | -0.032 | 0.24 | 0.761 | - | - | -0.066 (66/66) |
| break_even_plus_runner | 77 | 0.00 | -0.047 | -0.451 | 0.14 | 1.239 | - | - | -0.052 (77/77) |
| breakeven_lock_fixed | 77 | 0.00 | -0.008 | -0.486 | 0.33 | 0.904 | - | - | -0.014 (77/77) |
| eod_forced_flat | 77 | 0.00 | -0.065 | -1.000 | 0.20 | 1.409 | - | - | -0.070 (77/77) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 77 | 0.00 | 0.005 | -1.000 | 0.38 | 0.900 | - | - | - |
| momentum_failure | 77 | 0.00 | -0.003 | -0.349 | 0.28 | 0.709 | - | - | -0.009 (77/77) |
| pure_structure_trail | 77 | 0.00 | -0.066 | -0.468 | 0.15 | 1.270 | - | - | -0.071 (77/77) |
| struct_tp1_tp2 | 27 | 0.65 | -0.114 | 0.215 | 0.31 | 0.738 | - | - | -0.149 (27/27, n too small) |
| time_decay | 77 | 0.00 | 0.006 | -1.000 | 0.38 | 0.899 | - | - | 0.001 (77/77) |

### EURUSD|EOD|reverse (n 77, clusters 77)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 77 | 0.00 | -0.069 | -0.088 | 0.24 | 0.725 | - | - | -0.124 (77/77) |
| TP1_only | 77 | 0.00 | -0.026 | 0.227 | 0.49 | 0.691 | - | - | -0.080 (77/77) |
| TP1_plus_runner | 77 | 0.00 | -0.077 | -0.088 | 0.23 | 0.733 | - | - | -0.132 (77/77) |
| break_even_plus_runner | 77 | 0.00 | 0.010 | -0.346 | 0.19 | 1.124 | - | - | -0.045 (77/77) |
| breakeven_lock_fixed | 77 | 0.00 | -0.017 | 0.036 | 0.34 | 0.917 | - | - | -0.072 (77/77) |
| eod_forced_flat | 77 | 0.00 | 0.070 | -1.000 | 0.29 | 1.251 | - | - | 0.015 (77/77) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 77 | 0.00 | 0.055 | -1.000 | 0.43 | 0.892 | - | - | - |
| momentum_failure | 77 | 0.00 | -0.011 | -0.173 | 0.28 | 0.695 | - | - | -0.066 (77/77) |
| pure_structure_trail | 77 | 0.00 | -0.008 | -0.488 | 0.19 | 1.177 | - | - | -0.063 (77/77) |
| struct_tp1_tp2 | 77 | 0.00 | -0.011 | -0.092 | 0.35 | 0.894 | - | - | -0.066 (77/77) |
| time_decay | 77 | 0.00 | 0.055 | -1.000 | 0.43 | 0.882 | - | - | -0.000 (77/77) |

### EURUSD|GAP|fade (n 78, clusters 78)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 69 | 0.12 | -0.091 | 0.156 | 0.25 | 0.893 | - | - | 0.039 (69/69) |
| TP1_only | 71 | 0.09 | -0.098 | 0.233 | 0.47 | 0.708 | - | - | 0.057 (71/71) |
| TP1_plus_runner | 71 | 0.09 | -0.104 | 0.142 | 0.24 | 0.903 | - | - | 0.051 (71/71) |
| break_even_plus_runner | 78 | 0.00 | -0.196 | -0.699 | 0.13 | 1.324 | - | - | -0.029 (78/78) |
| breakeven_lock_fixed | 78 | 0.00 | -0.192 | -1.000 | 0.27 | 1.068 | - | - | -0.026 (78/78) |
| eod_forced_flat | 78 | 0.00 | -0.365 | -1.000 | 0.09 | 2.103 | - | - | -0.198 (78/78) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 78 | 0.00 | -0.167 | -1.000 | 0.34 | 1.085 | - | - | - |
| momentum_failure | 78 | 0.00 | -0.222 | -0.638 | 0.22 | 1.040 | - | - | -0.055 (78/78) |
| pure_structure_trail | 78 | 0.00 | -0.185 | -0.733 | 0.14 | 1.386 | - | - | -0.018 (78/78) |
| struct_tp1_tp2 | 69 | 0.12 | -0.158 | -0.317 | 0.25 | 1.019 | - | - | -0.027 (69/69) |
| time_decay | 78 | 0.00 | -0.162 | -1.000 | 0.34 | 1.080 | - | - | 0.005 (78/78) |

### EURUSD|GAP|go (n 72, clusters 72)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 31 | 0.57 | -0.152 | 0.168 | 0.27 | 0.934 | - | - | -0.120 (31/31) |
| TP1_only | 61 | 0.15 | -0.134 | 0.274 | 0.51 | 0.635 | - | - | 0.128 (61/61) |
| TP1_plus_runner | 61 | 0.15 | -0.185 | 0.149 | 0.27 | 0.893 | - | - | 0.078 (61/61) |
| break_even_plus_runner | 72 | 0.00 | -0.128 | -0.721 | 0.17 | 1.357 | - | - | -0.031 (72/72) |
| breakeven_lock_fixed | 72 | 0.00 | -0.121 | -1.000 | 0.33 | 1.003 | - | - | -0.024 (72/72) |
| eod_forced_flat | 72 | 0.00 | 0.228 | -1.000 | 0.17 | 2.028 | - | - | 0.325 (72/72) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 72 | 0.00 | -0.097 | -1.000 | 0.39 | 1.012 | - | - | - |
| momentum_failure | 72 | 0.00 | -0.123 | -0.620 | 0.31 | 0.948 | - | - | -0.026 (72/72) |
| pure_structure_trail | 72 | 0.00 | -0.141 | -0.989 | 0.18 | 1.475 | - | - | -0.043 (72/72) |
| struct_tp1_tp2 | 31 | 0.57 | -0.146 | -0.341 | 0.29 | 0.924 | - | - | -0.113 (31/31) |
| time_decay | 72 | 0.00 | -0.097 | -1.000 | 0.39 | 1.012 | - | - | 0.000 (72/72) |

### EURUSD|ORB|breakout (n 306, clusters 306)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 166 | 0.46 | -0.117 | 0.110 | 0.28 | 0.699 | - | - | 0.029 (166/166) |
| TP1_only | 229 | 0.25 | -0.101 | 0.195 | 0.53 | 0.519 | - | - | 0.062 (229/229) |
| TP1_plus_runner | 229 | 0.25 | -0.100 | 0.107 | 0.26 | 0.716 | - | - | 0.064 (229/229) |
| break_even_plus_runner | 306 | 0.00 | -0.178 | -0.399 | 0.17 | 1.039 | - | - | -0.053 (306/306) |
| breakeven_lock_fixed | 306 | 0.00 | -0.118 | -1.000 | 0.31 | 0.969 | - | - | 0.007 (306/306) |
| eod_forced_flat | 306 | 0.00 | -0.024 | -1.000 | 0.18 | 1.645 | - | - | 0.101 (306/306) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 306 | 0.00 | -0.125 | -1.000 | 0.37 | 1.004 | - | - | - |
| momentum_failure | 306 | 0.00 | -0.122 | -0.363 | 0.23 | 0.766 | - | - | 0.003 (306/306) |
| pure_structure_trail | 306 | 0.00 | -0.176 | -0.423 | 0.17 | 1.057 | - | - | -0.051 (306/306) |
| struct_tp1_tp2 | 166 | 0.46 | -0.151 | -0.257 | 0.36 | 0.725 | - | - | -0.005 (166/166) |
| time_decay | 306 | 0.00 | -0.132 | -1.000 | 0.35 | 0.948 | - | - | -0.007 (306/306) |

### EURUSD|ORB|fade (n 262, clusters 262)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 233 | 0.11 | -0.215 | -0.465 | 0.26 | 1.009 | - | - | -0.081 (233/233) |
| TP1_only | 259 | 0.01 | -0.145 | -1.000 | 0.47 | 0.857 | - | - | -0.030 (259/259) |
| TP1_plus_runner | 259 | 0.01 | -0.195 | -0.449 | 0.25 | 1.035 | - | - | -0.080 (259/259) |
| break_even_plus_runner | 262 | 0.00 | -0.166 | -0.929 | 0.14 | 1.388 | - | - | -0.044 (262/262) |
| breakeven_lock_fixed | 262 | 0.00 | -0.164 | -1.000 | 0.33 | 0.999 | - | - | -0.043 (262/262) |
| eod_forced_flat | 262 | 0.00 | -0.182 | -1.000 | 0.12 | 2.293 | - | - | -0.061 (262/262) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 262 | 0.00 | -0.122 | -1.000 | 0.41 | 1.011 | - | - | - |
| momentum_failure | 262 | 0.00 | -0.132 | -0.625 | 0.34 | 0.930 | - | - | -0.010 (262/262) |
| pure_structure_trail | 262 | 0.00 | -0.127 | -1.000 | 0.15 | 1.672 | - | - | -0.006 (262/262) |
| struct_tp1_tp2 | 233 | 0.11 | -0.169 | -1.000 | 0.28 | 1.167 | - | - | -0.035 (233/233) |
| time_decay | 262 | 0.00 | -0.122 | -1.000 | 0.41 | 1.004 | - | - | -0.001 (262/262) |

### EURUSD|OVERNIGHT|continue (n 74, clusters 74)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 42 | 0.43 | -0.054 | 0.093 | 0.29 | 0.637 | - | - | 0.053 (42/42) |
| TP1_only | 69 | 0.07 | -0.154 | 0.244 | 0.48 | 0.693 | - | - | 0.121 (69/69) |
| TP1_plus_runner | 69 | 0.07 | -0.136 | 0.000 | 0.27 | 0.709 | - | - | 0.139 (69/69) |
| break_even_plus_runner | 74 | 0.00 | -0.304 | -0.811 | 0.11 | 1.382 | - | - | -0.115 (74/74) |
| breakeven_lock_fixed | 74 | 0.00 | -0.179 | -1.000 | 0.30 | 1.017 | - | - | 0.010 (74/74) |
| eod_forced_flat | 74 | 0.00 | -0.177 | -1.000 | 0.12 | 2.086 | - | - | 0.012 (74/74) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 74 | 0.00 | -0.189 | -1.000 | 0.35 | 1.062 | - | - | - |
| momentum_failure | 74 | 0.00 | -0.173 | -0.460 | 0.27 | 0.824 | - | - | 0.016 (74/74) |
| pure_structure_trail | 74 | 0.00 | -0.341 | -0.924 | 0.12 | 1.474 | - | - | -0.152 (74/74) |
| struct_tp1_tp2 | 42 | 0.43 | -0.090 | -0.316 | 0.28 | 0.928 | - | - | 0.017 (42/42) |
| time_decay | 74 | 0.00 | -0.189 | -1.000 | 0.35 | 1.062 | - | - | 0.000 (74/74) |

### EURUSD|OVERNIGHT|reverse (n 74, clusters 74)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 70 | 0.05 | -0.074 | 0.100 | 0.26 | 0.901 | - | - | -0.074 (70/70) |
| TP1_only | 71 | 0.04 | -0.058 | 0.289 | 0.47 | 0.771 | - | - | -0.044 (71/71) |
| TP1_plus_runner | 71 | 0.04 | -0.095 | 0.060 | 0.25 | 0.916 | - | - | -0.081 (71/71) |
| break_even_plus_runner | 74 | 0.00 | 0.035 | -0.364 | 0.16 | 1.387 | - | - | 0.022 (74/74) |
| breakeven_lock_fixed | 74 | 0.00 | -0.028 | 0.055 | 0.29 | 1.039 | - | - | -0.041 (74/74) |
| eod_forced_flat | 74 | 0.00 | -0.079 | -1.000 | 0.11 | 2.287 | - | - | -0.092 (74/74) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 74 | 0.00 | 0.014 | -1.000 | 0.38 | 1.073 | - | - | - |
| momentum_failure | 74 | 0.00 | -0.118 | -0.486 | 0.26 | 1.023 | - | - | -0.131 (74/74) |
| pure_structure_trail | 74 | 0.00 | 0.023 | -0.660 | 0.16 | 1.486 | - | - | 0.009 (74/74) |
| struct_tp1_tp2 | 70 | 0.05 | -0.065 | -0.233 | 0.28 | 1.073 | - | - | -0.065 (70/70) |
| time_decay | 74 | 0.00 | 0.014 | -1.000 | 0.38 | 1.073 | - | - | 0.000 (74/74) |

### EURUSD|ROUND|break (n 159, clusters 140)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 100 | 0.37 | -0.123 | 0.173 | 0.27 | 0.864 | - | - | -0.104 (100/92) |
| TP1_only | 145 | 0.09 | -0.081 | 0.272 | 0.51 | 0.654 | - | - | -0.044 (145/129) |
| TP1_plus_runner | 145 | 0.09 | -0.145 | 0.121 | 0.25 | 0.869 | - | - | -0.107 (145/129) |
| break_even_plus_runner | 159 | 0.00 | -0.191 | -0.653 | 0.13 | 1.438 | - | - | -0.148 (159/140) |
| breakeven_lock_fixed | 159 | 0.00 | -0.045 | -1.000 | 0.34 | 1.011 | - | - | -0.001 (159/140) |
| eod_forced_flat | 159 | 0.00 | -0.181 | -1.000 | 0.15 | 1.929 | - | - | -0.137 (159/140) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 159 | 0.00 | -0.044 | -1.000 | 0.38 | 1.047 | - | - | - |
| momentum_failure | 159 | 0.00 | -0.056 | -0.416 | 0.31 | 0.886 | - | - | -0.012 (159/140) |
| pure_structure_trail | 159 | 0.00 | -0.173 | -0.678 | 0.14 | 1.506 | - | - | -0.130 (159/140) |
| struct_tp1_tp2 | 100 | 0.37 | -0.081 | -0.111 | 0.32 | 0.949 | - | - | -0.061 (100/92) |
| time_decay | 159 | 0.00 | -0.044 | -1.000 | 0.38 | 1.047 | - | - | 0.000 (159/140) |

### EURUSD|ROUND|reject (n 406, clusters 302)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 324 | 0.20 | -0.024 | 0.134 | 0.28 | 0.943 | - | - | 0.109 (324/244) |
| TP1_only | 380 | 0.06 | -0.114 | 0.237 | 0.47 | 0.745 | - | - | -0.011 (380/286) |
| TP1_plus_runner | 380 | 0.06 | -0.026 | 0.128 | 0.26 | 0.911 | - | - | 0.077 (380/286) |
| break_even_plus_runner | 406 | 0.00 | 0.032 | -0.625 | 0.15 | 1.476 | - | - | 0.112 (406/302) |
| breakeven_lock_fixed | 406 | 0.00 | -0.054 | -1.000 | 0.32 | 1.056 | - | - | 0.026 (406/302) |
| eod_forced_flat | 406 | 0.00 | -0.141 | -1.000 | 0.14 | 2.003 | - | - | -0.060 (406/302) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 406 | 0.00 | -0.080 | -1.000 | 0.37 | 1.122 | - | - | - |
| momentum_failure | 406 | 0.00 | 0.008 | -0.351 | 0.32 | 0.887 | - | - | 0.088 (406/302) |
| pure_structure_trail | 406 | 0.00 | 0.029 | -0.804 | 0.16 | 1.558 | - | - | 0.109 (406/302) |
| struct_tp1_tp2 | 324 | 0.20 | -0.128 | -0.297 | 0.29 | 1.022 | - | - | 0.005 (324/244) |
| time_decay | 406 | 0.00 | -0.080 | -1.000 | 0.37 | 1.122 | - | - | 0.000 (406/302) |

### EURUSD|VOLREV|expand (n 973, clusters 911)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 386 | 0.60 | -0.117 | 0.180 | 0.27 | 1.007 | - | - | 0.014 (386/379) |
| TP1_only | 608 | 0.38 | -0.152 | 0.263 | 0.46 | 0.714 | - | - | 0.037 (608/591) |
| TP1_plus_runner | 608 | 0.38 | -0.150 | 0.151 | 0.25 | 1.012 | - | - | 0.039 (608/591) |
| break_even_plus_runner | 973 | 0.00 | -0.197 | -0.902 | 0.12 | 1.415 | - | - | -0.035 (973/911) |
| breakeven_lock_fixed | 973 | 0.00 | -0.176 | -1.000 | 0.29 | 1.027 | - | - | -0.013 (973/911) |
| eod_forced_flat | 973 | 0.00 | -0.244 | -1.000 | 0.12 | 1.937 | - | - | -0.081 (973/911) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 973 | 0.00 | -0.162 | -1.000 | 0.35 | 1.067 | - | - | - |
| momentum_failure | 973 | 0.00 | -0.160 | -0.724 | 0.28 | 0.992 | - | - | 0.003 (973/911) |
| pure_structure_trail | 973 | 0.00 | -0.221 | -1.000 | 0.12 | 1.549 | - | - | -0.059 (973/911) |
| struct_tp1_tp2 | 386 | 0.60 | -0.128 | -0.308 | 0.29 | 0.977 | - | - | 0.003 (386/379) |
| time_decay | 973 | 0.00 | -0.164 | -1.000 | 0.35 | 1.066 | - | - | -0.002 (973/911) |

### EURUSD|VOLREV|fade (n 309, clusters 301)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 309 | 0.00 | -0.002 | 0.000 | 0.29 | 0.719 | - | - | 0.041 (309/301) |
| TP1_only | 309 | 0.00 | -0.022 | 0.203 | 0.45 | 0.838 | - | - | 0.021 (309/301) |
| TP1_plus_runner | 309 | 0.00 | -0.012 | 0.000 | 0.28 | 0.729 | - | - | 0.031 (309/301) |
| break_even_plus_runner | 309 | 0.00 | -0.066 | -0.504 | 0.13 | 1.357 | - | - | -0.023 (309/301) |
| breakeven_lock_fixed | 309 | 0.00 | -0.028 | -1.000 | 0.33 | 0.979 | - | - | 0.015 (309/301) |
| eod_forced_flat | 309 | 0.00 | 0.006 | -1.000 | 0.19 | 1.679 | - | - | 0.049 (309/301) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 309 | 0.00 | -0.043 | -1.000 | 0.39 | 1.029 | - | - | - |
| momentum_failure | 309 | 0.00 | -0.058 | -0.223 | 0.28 | 0.726 | - | - | -0.015 (309/301) |
| pure_structure_trail | 309 | 0.00 | -0.082 | -0.640 | 0.14 | 1.454 | - | - | -0.040 (309/301) |
| struct_tp1_tp2 | 309 | 0.00 | -0.029 | -0.330 | 0.31 | 1.061 | - | - | 0.013 (309/301) |
| time_decay | 309 | 0.00 | -0.042 | -1.000 | 0.39 | 1.028 | - | - | 0.001 (309/301) |


## GER40

Family entries: n 9644, independent event clusters 2970, same-entry assertion OK; random-entry control: n 9644 (seed 20350256). Lab cost per entry (12 policies, one process): mean 34.8 ms, median 24.7 ms, p95 94.9 ms (mean 95 bars).

### All family entries (real) vs random-entry control

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 6987 | 0.28 | -0.039 | 0.086 | 0.25 | 0.849 | -0.005 | -0.085 | 0.011 (6987/2554) |
| TP1_only | 8992 | 0.07 | -0.051 | 0.163 | 0.46 | 0.644 | 0.147 | -0.090 | -0.011 (8992/2900) |
| TP1_plus_runner | 8992 | 0.07 | -0.037 | 0.082 | 0.23 | 0.863 | 0.016 | -0.076 | 0.003 (8992/2900) |
| break_even_plus_runner | 9644 | 0.00 | -0.043 | -0.518 | 0.15 | 1.345 | -0.510 | -0.057 | 0.000 (9644/2970) |
| breakeven_lock_fixed | 9644 | 0.00 | -0.042 | -1.000 | 0.32 | 0.986 | -1.000 | -0.068 | 0.001 (9644/2970) |
| eod_forced_flat | 9644 | 0.00 | 0.002 | -1.000 | 0.18 | 1.779 | -1.000 | -0.085 | 0.045 (9644/2970) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 9644 | 0.00 | -0.043 | -1.000 | 0.38 | 1.022 | -1.000 | -0.071 | - |
| momentum_failure | 9644 | 0.00 | -0.041 | -0.389 | 0.30 | 0.878 | -0.396 | -0.076 | 0.002 (9644/2970) |
| pure_structure_trail | 9644 | 0.00 | -0.039 | -0.706 | 0.17 | 1.429 | -0.650 | -0.064 | 0.004 (9644/2970) |
| struct_tp1_tp2 | 6987 | 0.28 | -0.051 | -0.261 | 0.29 | 0.940 | -0.317 | -0.091 | -0.002 (6987/2554) |
| time_decay | 9644 | 0.00 | -0.043 | -1.000 | 0.38 | 1.015 | -1.000 | -0.072 | 0.000 (9644/2970) |

### GER40|EOD|continue (n 95, clusters 95)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 38 | 0.60 | -0.037 | 0.036 | 0.20 | 0.704 | - | - | 0.031 (38/38) |
| TP1_only | 90 | 0.05 | -0.088 | 0.120 | 0.47 | 0.487 | - | - | -0.102 (90/90) |
| TP1_plus_runner | 90 | 0.05 | -0.071 | 0.034 | 0.20 | 0.686 | - | - | -0.086 (90/90) |
| break_even_plus_runner | 95 | 0.00 | 0.091 | -0.314 | 0.23 | 0.992 | - | - | -0.002 (95/95) |
| breakeven_lock_fixed | 95 | 0.00 | 0.115 | 0.007 | 0.40 | 0.800 | - | - | 0.023 (95/95) |
| eod_forced_flat | 95 | 0.00 | 0.120 | -0.595 | 0.28 | 1.153 | - | - | 0.027 (95/95) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 95 | 0.00 | 0.093 | -0.457 | 0.43 | 0.830 | - | - | - |
| momentum_failure | 95 | 0.00 | -0.033 | -0.372 | 0.28 | 0.720 | - | - | -0.126 (95/95) |
| pure_structure_trail | 95 | 0.00 | 0.064 | -0.376 | 0.23 | 1.019 | - | - | -0.029 (95/95) |
| struct_tp1_tp2 | 38 | 0.60 | -0.071 | 0.251 | 0.33 | 0.704 | - | - | -0.003 (38/38) |
| time_decay | 95 | 0.00 | 0.073 | -0.386 | 0.40 | 0.809 | - | - | -0.020 (95/95) |

### GER40|EOD|reverse (n 95, clusters 95)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 95 | 0.00 | -0.010 | -0.016 | 0.22 | 0.726 | - | - | 0.137 (95/95) |
| TP1_only | 95 | 0.00 | -0.135 | 0.084 | 0.42 | 0.756 | - | - | 0.012 (95/95) |
| TP1_plus_runner | 95 | 0.00 | 0.001 | -0.016 | 0.21 | 0.715 | - | - | 0.148 (95/95) |
| break_even_plus_runner | 95 | 0.00 | -0.137 | -0.321 | 0.12 | 1.107 | - | - | 0.010 (95/95) |
| breakeven_lock_fixed | 95 | 0.00 | -0.146 | -0.555 | 0.26 | 1.010 | - | - | 0.001 (95/95) |
| eod_forced_flat | 95 | 0.00 | -0.227 | -1.000 | 0.19 | 1.323 | - | - | -0.080 (95/95) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 95 | 0.00 | -0.147 | -1.000 | 0.31 | 1.045 | - | - | - |
| momentum_failure | 95 | 0.00 | -0.120 | -0.264 | 0.19 | 0.758 | - | - | 0.027 (95/95) |
| pure_structure_trail | 95 | 0.00 | -0.153 | -0.331 | 0.12 | 1.153 | - | - | -0.006 (95/95) |
| struct_tp1_tp2 | 95 | 0.00 | -0.121 | -0.212 | 0.29 | 0.941 | - | - | 0.026 (95/95) |
| time_decay | 95 | 0.00 | -0.115 | -0.708 | 0.31 | 1.005 | - | - | 0.032 (95/95) |

### GER40|GAP|fade (n 92, clusters 92)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 86 | 0.07 | 0.053 | 0.107 | 0.25 | 0.901 | - | - | -0.051 (86/86) |
| TP1_only | 92 | 0.00 | 0.055 | 0.168 | 0.45 | 0.558 | - | - | -0.032 (92/92) |
| TP1_plus_runner | 92 | 0.00 | 0.049 | 0.093 | 0.24 | 0.883 | - | - | -0.038 (92/92) |
| break_even_plus_runner | 92 | 0.00 | 0.049 | -0.618 | 0.16 | 1.512 | - | - | -0.038 (92/92) |
| breakeven_lock_fixed | 92 | 0.00 | -0.002 | -1.000 | 0.34 | 0.988 | - | - | -0.089 (92/92) |
| eod_forced_flat | 92 | 0.00 | 0.272 | -1.000 | 0.15 | 2.223 | - | - | 0.185 (92/92) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 92 | 0.00 | 0.087 | -1.000 | 0.43 | 0.956 | - | - | - |
| momentum_failure | 92 | 0.00 | -0.006 | -0.523 | 0.32 | 0.972 | - | - | -0.093 (92/92) |
| pure_structure_trail | 92 | 0.00 | 0.162 | -0.954 | 0.19 | 1.584 | - | - | 0.075 (92/92) |
| struct_tp1_tp2 | 86 | 0.07 | 0.043 | 0.110 | 0.29 | 0.913 | - | - | -0.062 (86/86) |
| time_decay | 92 | 0.00 | 0.087 | -1.000 | 0.43 | 0.956 | - | - | 0.000 (92/92) |

### GER40|GAP|go (n 87, clusters 87)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 15 | 0.83 | -0.079 | 0.171 | 0.31 | 0.569 | - | - | -0.413 (15/15, n too small) |
| TP1_only | 75 | 0.14 | -0.057 | 0.228 | 0.43 | 0.655 | - | - | -0.191 (75/75) |
| TP1_plus_runner | 75 | 0.14 | -0.060 | 0.123 | 0.21 | 1.005 | - | - | -0.194 (75/75) |
| break_even_plus_runner | 87 | 0.00 | -0.001 | -0.446 | 0.14 | 1.497 | - | - | -0.150 (87/87) |
| breakeven_lock_fixed | 87 | 0.00 | 0.063 | 0.016 | 0.35 | 0.953 | - | - | -0.087 (87/87) |
| eod_forced_flat | 87 | 0.00 | 0.173 | -1.000 | 0.16 | 2.179 | - | - | 0.024 (87/87) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 87 | 0.00 | 0.149 | -1.000 | 0.46 | 0.941 | - | - | - |
| momentum_failure | 87 | 0.00 | 0.013 | -0.385 | 0.32 | 0.957 | - | - | -0.136 (87/87) |
| pure_structure_trail | 87 | 0.00 | 0.113 | -0.744 | 0.18 | 1.643 | - | - | -0.036 (87/87) |
| struct_tp1_tp2 | 15 | 0.83 | 0.147 | 0.352 | 0.37 | 0.713 | - | - | -0.186 (15/15, n too small) |
| time_decay | 87 | 0.00 | 0.149 | -1.000 | 0.46 | 0.941 | - | - | 0.000 (87/87) |

### GER40|LEADLAG|- (n 453, clusters 298)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 361 | 0.20 | -0.135 | 0.076 | 0.25 | 0.909 | - | - | 0.086 (361/242) |
| TP1_only | 450 | 0.01 | -0.143 | 0.165 | 0.44 | 0.694 | - | - | 0.057 (450/296) |
| TP1_plus_runner | 450 | 0.01 | -0.107 | 0.073 | 0.24 | 0.920 | - | - | 0.092 (450/296) |
| break_even_plus_runner | 453 | 0.00 | -0.127 | -1.000 | 0.14 | 1.371 | - | - | 0.072 (453/298) |
| breakeven_lock_fixed | 453 | 0.00 | -0.225 | -1.000 | 0.28 | 1.051 | - | - | -0.026 (453/298) |
| eod_forced_flat | 453 | 0.00 | -0.199 | -1.000 | 0.17 | 1.628 | - | - | 0.000 (453/298) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 453 | 0.00 | -0.199 | -1.000 | 0.34 | 1.084 | - | - | - |
| momentum_failure | 453 | 0.00 | -0.213 | -0.640 | 0.27 | 0.979 | - | - | -0.014 (453/298) |
| pure_structure_trail | 453 | 0.00 | -0.106 | -1.000 | 0.18 | 1.452 | - | - | 0.093 (453/298) |
| struct_tp1_tp2 | 361 | 0.20 | -0.171 | -0.387 | 0.28 | 0.948 | - | - | 0.050 (361/242) |
| time_decay | 453 | 0.00 | -0.198 | -1.000 | 0.34 | 1.081 | - | - | 0.001 (453/298) |

### GER40|ORB|breakout (n 370, clusters 370)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 159 | 0.57 | -0.012 | 0.050 | 0.25 | 0.448 | - | - | -0.029 (159/159) |
| TP1_only | 289 | 0.22 | -0.028 | 0.074 | 0.45 | 0.290 | - | - | -0.025 (289/289) |
| TP1_plus_runner | 289 | 0.22 | -0.019 | 0.042 | 0.23 | 0.442 | - | - | -0.017 (289/289) |
| break_even_plus_runner | 370 | 0.00 | -0.010 | -0.182 | 0.20 | 0.830 | - | - | 0.020 (370/370) |
| breakeven_lock_fixed | 370 | 0.00 | -0.034 | 0.006 | 0.31 | 0.945 | - | - | -0.004 (370/370) |
| eod_forced_flat | 370 | 0.00 | 0.023 | -1.000 | 0.24 | 1.305 | - | - | 0.053 (370/370) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 370 | 0.00 | -0.030 | -1.000 | 0.36 | 0.963 | - | - | - |
| momentum_failure | 370 | 0.00 | -0.017 | -0.207 | 0.23 | 0.600 | - | - | 0.014 (370/370) |
| pure_structure_trail | 370 | 0.00 | -0.005 | -0.192 | 0.21 | 0.831 | - | - | 0.026 (370/370) |
| struct_tp1_tp2 | 159 | 0.57 | -0.012 | 0.172 | 0.39 | 0.490 | - | - | -0.030 (159/159) |
| time_decay | 370 | 0.00 | -0.030 | -0.364 | 0.35 | 0.847 | - | - | 0.000 (370/370) |

### GER40|ORB|fade (n 311, clusters 311)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 296 | 0.05 | -0.032 | 0.055 | 0.25 | 1.103 | - | - | -0.062 (296/296) |
| TP1_only | 311 | 0.00 | -0.074 | 0.069 | 0.43 | 0.873 | - | - | -0.102 (311/311) |
| TP1_plus_runner | 311 | 0.00 | -0.016 | 0.051 | 0.24 | 1.117 | - | - | -0.044 (311/311) |
| break_even_plus_runner | 311 | 0.00 | 0.053 | -0.405 | 0.15 | 1.441 | - | - | 0.026 (311/311) |
| breakeven_lock_fixed | 311 | 0.00 | 0.022 | -1.000 | 0.38 | 0.930 | - | - | -0.006 (311/311) |
| eod_forced_flat | 311 | 0.00 | 0.193 | -1.000 | 0.14 | 2.205 | - | - | 0.165 (311/311) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 311 | 0.00 | 0.028 | -1.000 | 0.44 | 0.973 | - | - | - |
| momentum_failure | 311 | 0.00 | 0.050 | -0.370 | 0.38 | 0.851 | - | - | 0.022 (311/311) |
| pure_structure_trail | 311 | 0.00 | 0.041 | -1.000 | 0.18 | 1.659 | - | - | 0.014 (311/311) |
| struct_tp1_tp2 | 296 | 0.05 | -0.063 | -0.416 | 0.29 | 1.098 | - | - | -0.092 (296/296) |
| time_decay | 311 | 0.00 | 0.016 | -1.000 | 0.43 | 0.967 | - | - | -0.011 (311/311) |

### GER40|OVERNIGHT|continue (n 93, clusters 93)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 51 | 0.45 | 0.039 | 0.159 | 0.29 | 0.781 | - | - | -0.285 (51/51) |
| TP1_only | 87 | 0.06 | -0.032 | 0.267 | 0.44 | 0.614 | - | - | -0.297 (87/87) |
| TP1_plus_runner | 87 | 0.06 | 0.007 | 0.147 | 0.24 | 0.955 | - | - | -0.257 (87/87) |
| break_even_plus_runner | 93 | 0.00 | 0.015 | -0.536 | 0.13 | 1.557 | - | - | -0.221 (93/93) |
| breakeven_lock_fixed | 93 | 0.00 | 0.175 | 0.023 | 0.42 | 0.893 | - | - | -0.062 (93/93) |
| eod_forced_flat | 93 | 0.00 | 0.032 | -1.000 | 0.13 | 2.243 | - | - | -0.205 (93/93) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 93 | 0.00 | 0.237 | -1.000 | 0.48 | 0.864 | - | - | - |
| momentum_failure | 93 | 0.00 | 0.144 | -0.291 | 0.39 | 0.777 | - | - | -0.092 (93/93) |
| pure_structure_trail | 93 | 0.00 | 0.015 | -0.709 | 0.15 | 1.667 | - | - | -0.222 (93/93) |
| struct_tp1_tp2 | 51 | 0.45 | -0.015 | -0.204 | 0.26 | 0.989 | - | - | -0.339 (51/51) |
| time_decay | 93 | 0.00 | 0.237 | -1.000 | 0.48 | 0.864 | - | - | 0.000 (93/93) |

### GER40|OVERNIGHT|reverse (n 93, clusters 93)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 92 | 0.01 | -0.012 | 0.074 | 0.26 | 0.857 | - | - | -0.017 (92/92) |
| TP1_only | 93 | 0.00 | -0.055 | 0.107 | 0.41 | 0.802 | - | - | -0.050 (93/93) |
| TP1_plus_runner | 93 | 0.00 | -0.033 | 0.074 | 0.25 | 0.869 | - | - | -0.027 (93/93) |
| break_even_plus_runner | 93 | 0.00 | -0.146 | -0.915 | 0.12 | 1.488 | - | - | -0.140 (93/93) |
| breakeven_lock_fixed | 93 | 0.00 | -0.067 | -1.000 | 0.32 | 1.001 | - | - | -0.061 (93/93) |
| eod_forced_flat | 93 | 0.00 | -0.016 | -1.000 | 0.12 | 2.106 | - | - | -0.010 (93/93) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 93 | 0.00 | -0.005 | -1.000 | 0.40 | 0.991 | - | - | - |
| momentum_failure | 93 | 0.00 | -0.052 | -0.480 | 0.32 | 0.899 | - | - | -0.047 (93/93) |
| pure_structure_trail | 93 | 0.00 | -0.091 | -1.000 | 0.15 | 1.570 | - | - | -0.086 (93/93) |
| struct_tp1_tp2 | 92 | 0.01 | -0.060 | -0.440 | 0.27 | 1.091 | - | - | -0.066 (92/92) |
| time_decay | 93 | 0.00 | -0.005 | -1.000 | 0.40 | 0.991 | - | - | 0.000 (93/93) |

### GER40|ROUND|break (n 1733, clusters 1506)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 1185 | 0.32 | -0.044 | 0.097 | 0.26 | 0.825 | - | - | 0.009 (1185/1052) |
| TP1_only | 1669 | 0.04 | -0.036 | 0.183 | 0.49 | 0.582 | - | - | 0.006 (1669/1456) |
| TP1_plus_runner | 1669 | 0.04 | -0.034 | 0.092 | 0.24 | 0.850 | - | - | 0.008 (1669/1456) |
| break_even_plus_runner | 1733 | 0.00 | -0.059 | -0.535 | 0.14 | 1.351 | - | - | -0.006 (1733/1506) |
| breakeven_lock_fixed | 1733 | 0.00 | -0.052 | -1.000 | 0.32 | 0.988 | - | - | 0.001 (1733/1506) |
| eod_forced_flat | 1733 | 0.00 | 0.042 | -1.000 | 0.18 | 1.751 | - | - | 0.096 (1733/1506) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 1733 | 0.00 | -0.053 | -1.000 | 0.38 | 1.020 | - | - | - |
| momentum_failure | 1733 | 0.00 | -0.049 | -0.447 | 0.30 | 0.901 | - | - | 0.004 (1733/1506) |
| pure_structure_trail | 1733 | 0.00 | -0.031 | -0.679 | 0.16 | 1.412 | - | - | 0.022 (1733/1506) |
| struct_tp1_tp2 | 1185 | 0.32 | -0.056 | -0.237 | 0.29 | 0.890 | - | - | -0.003 (1185/1052) |
| time_decay | 1733 | 0.00 | -0.054 | -1.000 | 0.38 | 1.020 | - | - | -0.001 (1733/1506) |

### GER40|ROUND|reject (n 4023, clusters 2589)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 3307 | 0.18 | -0.033 | 0.084 | 0.24 | 0.860 | - | - | 0.019 (3307/2198) |
| TP1_only | 3911 | 0.03 | -0.039 | 0.175 | 0.46 | 0.658 | - | - | 0.009 (3911/2535) |
| TP1_plus_runner | 3911 | 0.03 | -0.036 | 0.079 | 0.23 | 0.873 | - | - | 0.011 (3911/2535) |
| break_even_plus_runner | 4023 | 0.00 | -0.061 | -0.571 | 0.14 | 1.357 | - | - | -0.013 (4023/2589) |
| breakeven_lock_fixed | 4023 | 0.00 | -0.047 | -1.000 | 0.32 | 0.991 | - | - | 0.001 (4023/2589) |
| eod_forced_flat | 4023 | 0.00 | -0.056 | -1.000 | 0.17 | 1.790 | - | - | -0.008 (4023/2589) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 4023 | 0.00 | -0.048 | -1.000 | 0.37 | 1.025 | - | - | - |
| momentum_failure | 4023 | 0.00 | -0.038 | -0.389 | 0.30 | 0.872 | - | - | 0.010 (4023/2589) |
| pure_structure_trail | 4023 | 0.00 | -0.071 | -0.723 | 0.16 | 1.438 | - | - | -0.023 (4023/2589) |
| struct_tp1_tp2 | 3307 | 0.18 | -0.040 | -0.245 | 0.29 | 0.945 | - | - | 0.012 (3307/2198) |
| time_decay | 4023 | 0.00 | -0.046 | -1.000 | 0.37 | 1.022 | - | - | 0.002 (4023/2589) |

### GER40|VOLREV|expand (n 1688, clusters 1581)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 791 | 0.53 | -0.063 | 0.108 | 0.26 | 0.920 | - | - | -0.008 (791/765) |
| TP1_only | 1319 | 0.22 | -0.073 | 0.168 | 0.44 | 0.631 | - | - | -0.048 (1319/1263) |
| TP1_plus_runner | 1319 | 0.22 | -0.048 | 0.102 | 0.24 | 0.933 | - | - | -0.023 (1319/1263) |
| break_even_plus_runner | 1688 | 0.00 | 0.003 | -0.501 | 0.15 | 1.403 | - | - | 0.037 (1688/1581) |
| breakeven_lock_fixed | 1688 | 0.00 | -0.018 | -1.000 | 0.33 | 0.997 | - | - | 0.016 (1688/1581) |
| eod_forced_flat | 1688 | 0.00 | 0.094 | -1.000 | 0.19 | 1.839 | - | - | 0.128 (1688/1581) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 1688 | 0.00 | -0.034 | -1.000 | 0.38 | 1.050 | - | - | - |
| momentum_failure | 1688 | 0.00 | -0.050 | -0.558 | 0.31 | 0.994 | - | - | -0.016 (1688/1581) |
| pure_structure_trail | 1688 | 0.00 | 0.014 | -0.870 | 0.17 | 1.519 | - | - | 0.049 (1688/1581) |
| struct_tp1_tp2 | 791 | 0.53 | -0.045 | -0.286 | 0.29 | 0.939 | - | - | 0.009 (791/765) |
| time_decay | 1688 | 0.00 | -0.033 | -1.000 | 0.38 | 1.048 | - | - | 0.001 (1688/1581) |

### GER40|VOLREV|fade (n 511, clusters 500)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 511 | 0.00 | -0.004 | -0.008 | 0.27 | 0.703 | - | - | 0.028 (511/500) |
| TP1_only | 511 | 0.00 | -0.048 | 0.128 | 0.44 | 0.795 | - | - | -0.016 (511/500) |
| TP1_plus_runner | 511 | 0.00 | -0.006 | -0.008 | 0.26 | 0.705 | - | - | 0.026 (511/500) |
| break_even_plus_runner | 511 | 0.00 | -0.031 | -0.479 | 0.16 | 1.312 | - | - | 0.002 (511/500) |
| breakeven_lock_fixed | 511 | 0.00 | -0.005 | -1.000 | 0.34 | 0.967 | - | - | 0.027 (511/500) |
| eod_forced_flat | 511 | 0.00 | 0.009 | -1.000 | 0.19 | 1.714 | - | - | 0.042 (511/500) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 511 | 0.00 | -0.033 | -1.000 | 0.38 | 1.022 | - | - | - |
| momentum_failure | 511 | 0.00 | 0.031 | -0.183 | 0.33 | 0.623 | - | - | 0.063 (511/500) |
| pure_structure_trail | 511 | 0.00 | -0.061 | -0.612 | 0.17 | 1.384 | - | - | -0.029 (511/500) |
| struct_tp1_tp2 | 511 | 0.00 | -0.056 | -0.355 | 0.28 | 1.062 | - | - | -0.023 (511/500) |
| time_decay | 511 | 0.00 | -0.034 | -1.000 | 0.38 | 1.022 | - | - | -0.002 (511/500) |


## NAS100

Family entries: n 5390, independent event clusters 1423, same-entry assertion OK; random-entry control: n 5390 (seed 20344372). Lab cost per entry (12 policies, one process): mean 38.3 ms, median 28.9 ms, p95 102.0 ms (mean 53 bars).

### All family entries (real) vs random-entry control

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 3663 | 0.32 | -0.029 | 0.065 | 0.24 | 0.844 | 0.060 | -0.030 | -0.003 (3663/1253) |
| TP1_only | 5142 | 0.05 | -0.045 | 0.115 | 0.42 | 0.621 | 0.164 | -0.040 | -0.032 (5142/1411) |
| TP1_plus_runner | 5142 | 0.05 | -0.018 | 0.058 | 0.22 | 0.839 | 0.057 | -0.026 | -0.005 (5142/1411) |
| break_even_plus_runner | 5390 | 0.00 | -0.019 | -0.423 | 0.17 | 1.270 | -0.464 | -0.031 | -0.008 (5390/1423) |
| breakeven_lock_fixed | 5390 | 0.00 | -0.019 | -0.403 | 0.34 | 0.948 | -0.635 | -0.042 | -0.007 (5390/1423) |
| eod_forced_flat | 5390 | 0.00 | 0.006 | -1.000 | 0.23 | 1.495 | -1.000 | 0.046 | 0.018 (5390/1423) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 5390 | 0.00 | -0.012 | -1.000 | 0.39 | 0.968 | -1.000 | -0.029 | - |
| momentum_failure | 5390 | 0.00 | -0.014 | -0.357 | 0.32 | 0.851 | -0.368 | -0.051 | -0.002 (5390/1423) |
| pure_structure_trail | 5390 | 0.00 | -0.005 | -0.678 | 0.19 | 1.352 | -0.628 | -0.018 | 0.007 (5390/1423) |
| struct_tp1_tp2 | 3663 | 0.32 | -0.044 | -0.082 | 0.30 | 0.890 | -0.178 | -0.038 | -0.018 (3663/1253) |
| time_decay | 5390 | 0.00 | -0.009 | -1.000 | 0.39 | 0.962 | -1.000 | -0.031 | 0.003 (5390/1423) |

### NAS100|EOD|continue (n 76, clusters 76)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 33 | 0.57 | 0.089 | 0.104 | 0.29 | 0.729 | - | - | -0.319 (33/33) |
| TP1_only | 71 | 0.07 | -0.027 | 0.173 | 0.51 | 0.501 | - | - | -0.126 (71/71) |
| TP1_plus_runner | 71 | 0.07 | -0.016 | 0.058 | 0.23 | 0.695 | - | - | -0.115 (71/71) |
| break_even_plus_runner | 76 | 0.00 | -0.075 | -0.343 | 0.20 | 0.998 | - | - | -0.187 (76/76) |
| breakeven_lock_fixed | 76 | 0.00 | 0.040 | 0.009 | 0.37 | 0.873 | - | - | -0.071 (76/76) |
| eod_forced_flat | 76 | 0.00 | 0.112 | -0.397 | 0.37 | 0.989 | - | - | 0.000 (76/76) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 76 | 0.00 | 0.112 | -0.245 | 0.44 | 0.826 | - | - | - |
| momentum_failure | 76 | 0.00 | 0.024 | -0.191 | 0.30 | 0.750 | - | - | -0.088 (76/76) |
| pure_structure_trail | 76 | 0.00 | -0.048 | -0.381 | 0.22 | 1.004 | - | - | -0.160 (76/76) |
| struct_tp1_tp2 | 33 | 0.57 | 0.112 | 0.391 | 0.37 | 0.720 | - | - | -0.295 (33/33) |
| time_decay | 76 | 0.00 | 0.112 | -0.245 | 0.44 | 0.826 | - | - | 0.000 (76/76) |

### NAS100|EOD|reverse (n 76, clusters 76)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 76 | 0.00 | -0.010 | -0.284 | 0.24 | 0.779 | - | - | -0.021 (76/76) |
| TP1_only | 76 | 0.00 | 0.002 | 0.145 | 0.48 | 0.658 | - | - | -0.009 (76/76) |
| TP1_plus_runner | 76 | 0.00 | -0.018 | -0.284 | 0.24 | 0.787 | - | - | -0.029 (76/76) |
| break_even_plus_runner | 76 | 0.00 | -0.084 | -0.473 | 0.19 | 1.060 | - | - | -0.095 (76/76) |
| breakeven_lock_fixed | 76 | 0.00 | -0.037 | -0.532 | 0.36 | 0.899 | - | - | -0.048 (76/76) |
| eod_forced_flat | 76 | 0.00 | 0.034 | -0.891 | 0.31 | 1.090 | - | - | 0.023 (76/76) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 76 | 0.00 | 0.011 | -0.722 | 0.41 | 0.867 | - | - | - |
| momentum_failure | 76 | 0.00 | -0.010 | -0.428 | 0.32 | 0.723 | - | - | -0.021 (76/76) |
| pure_structure_trail | 76 | 0.00 | -0.060 | -0.473 | 0.21 | 1.049 | - | - | -0.071 (76/76) |
| struct_tp1_tp2 | 76 | 0.00 | -0.010 | -0.338 | 0.33 | 0.936 | - | - | -0.021 (76/76) |
| time_decay | 76 | 0.00 | 0.011 | -0.722 | 0.41 | 0.867 | - | - | 0.000 (76/76) |

### NAS100|GAP|fade (n 73, clusters 73)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 53 | 0.27 | -0.039 | 0.111 | 0.25 | 1.060 | - | - | -0.077 (53/53) |
| TP1_only | 72 | 0.01 | -0.003 | 0.157 | 0.42 | 0.747 | - | - | -0.114 (72/72) |
| TP1_plus_runner | 72 | 0.01 | -0.013 | 0.097 | 0.25 | 1.092 | - | - | -0.124 (72/72) |
| break_even_plus_runner | 73 | 0.00 | 0.008 | 0.006 | 0.14 | 1.731 | - | - | -0.088 (73/73) |
| breakeven_lock_fixed | 73 | 0.00 | 0.118 | 0.007 | 0.43 | 0.962 | - | - | 0.022 (73/73) |
| eod_forced_flat | 73 | 0.00 | -0.075 | -1.000 | 0.17 | 2.109 | - | - | -0.171 (73/73) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 73 | 0.00 | 0.096 | -1.000 | 0.48 | 1.000 | - | - | - |
| momentum_failure | 73 | 0.00 | 0.066 | -0.258 | 0.41 | 0.954 | - | - | -0.030 (73/73) |
| pure_structure_trail | 73 | 0.00 | -0.012 | -1.000 | 0.17 | 1.893 | - | - | -0.108 (73/73) |
| struct_tp1_tp2 | 53 | 0.27 | 0.013 | 0.273 | 0.36 | 0.905 | - | - | -0.025 (53/53) |
| time_decay | 73 | 0.00 | 0.096 | -1.000 | 0.48 | 1.000 | - | - | 0.000 (73/73) |

### NAS100|GAP|go (n 96, clusters 96)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 40 | 0.58 | -0.158 | 0.143 | 0.22 | 0.992 | - | - | 0.030 (40/40) |
| TP1_only | 87 | 0.09 | -0.092 | 0.149 | 0.33 | 0.826 | - | - | 0.046 (87/87) |
| TP1_plus_runner | 87 | 0.09 | -0.020 | 0.076 | 0.20 | 1.095 | - | - | 0.117 (87/87) |
| break_even_plus_runner | 96 | 0.00 | -0.163 | -0.905 | 0.11 | 1.466 | - | - | -0.049 (96/96) |
| breakeven_lock_fixed | 96 | 0.00 | -0.049 | -1.000 | 0.33 | 0.995 | - | - | 0.065 (96/96) |
| eod_forced_flat | 96 | 0.00 | 0.146 | -1.000 | 0.20 | 1.658 | - | - | 0.261 (96/96) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 96 | 0.00 | -0.115 | -1.000 | 0.37 | 1.088 | - | - | - |
| momentum_failure | 96 | 0.00 | -0.085 | -0.713 | 0.34 | 0.994 | - | - | 0.029 (96/96) |
| pure_structure_trail | 96 | 0.00 | -0.104 | -1.000 | 0.15 | 1.733 | - | - | 0.011 (96/96) |
| struct_tp1_tp2 | 40 | 0.58 | -0.117 | 0.265 | 0.31 | 0.881 | - | - | 0.070 (40/40) |
| time_decay | 96 | 0.00 | -0.115 | -1.000 | 0.37 | 1.088 | - | - | 0.000 (96/96) |

### NAS100|LEADLAG|- (n 186, clusters 171)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 136 | 0.27 | -0.007 | 0.122 | 0.24 | 1.129 | - | - | -0.096 (136/129) |
| TP1_only | 184 | 0.01 | -0.003 | 0.162 | 0.44 | 0.667 | - | - | -0.147 (184/170) |
| TP1_plus_runner | 184 | 0.01 | 0.064 | 0.100 | 0.24 | 1.047 | - | - | -0.080 (184/170) |
| break_even_plus_runner | 186 | 0.00 | 0.215 | 0.006 | 0.18 | 1.353 | - | - | 0.070 (186/171) |
| breakeven_lock_fixed | 186 | 0.00 | 0.122 | 0.012 | 0.39 | 0.940 | - | - | -0.023 (186/171) |
| eod_forced_flat | 186 | 0.00 | 0.319 | -1.000 | 0.26 | 1.684 | - | - | 0.174 (186/171) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 186 | 0.00 | 0.145 | -1.000 | 0.47 | 0.949 | - | - | - |
| momentum_failure | 186 | 0.00 | 0.094 | -0.286 | 0.39 | 0.887 | - | - | -0.051 (186/171) |
| pure_structure_trail | 186 | 0.00 | 0.295 | -0.722 | 0.23 | 1.463 | - | - | 0.150 (186/171) |
| struct_tp1_tp2 | 136 | 0.27 | -0.033 | 0.080 | 0.31 | 0.939 | - | - | -0.121 (136/129) |
| time_decay | 186 | 0.00 | 0.150 | -1.000 | 0.47 | 0.945 | - | - | 0.004 (186/171) |

### NAS100|ORB|breakout (n 322, clusters 322)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 118 | 0.63 | 0.037 | 0.052 | 0.23 | 0.408 | - | - | 0.107 (118/118) |
| TP1_only | 294 | 0.09 | -0.003 | 0.057 | 0.39 | 0.246 | - | - | -0.020 (294/294) |
| TP1_plus_runner | 294 | 0.09 | 0.009 | 0.030 | 0.19 | 0.395 | - | - | -0.008 (294/294) |
| break_even_plus_runner | 322 | 0.00 | -0.013 | -0.130 | 0.23 | 0.776 | - | - | -0.033 (322/322) |
| breakeven_lock_fixed | 322 | 0.00 | 0.029 | 0.003 | 0.36 | 0.760 | - | - | 0.009 (322/322) |
| eod_forced_flat | 322 | 0.00 | -0.014 | -0.172 | 0.30 | 0.944 | - | - | -0.034 (322/322) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 322 | 0.00 | 0.020 | -0.093 | 0.38 | 0.775 | - | - | - |
| momentum_failure | 322 | 0.00 | 0.008 | -0.206 | 0.25 | 0.577 | - | - | -0.012 (322/322) |
| pure_structure_trail | 322 | 0.00 | -0.020 | -0.174 | 0.23 | 0.785 | - | - | -0.040 (322/322) |
| struct_tp1_tp2 | 118 | 0.63 | -0.003 | 0.136 | 0.38 | 0.405 | - | - | 0.067 (118/118) |
| time_decay | 322 | 0.00 | 0.035 | -0.020 | 0.38 | 0.724 | - | - | 0.015 (322/322) |

### NAS100|ORB|fade (n 260, clusters 260)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 244 | 0.06 | -0.070 | 0.062 | 0.24 | 1.006 | - | - | -0.108 (244/244) |
| TP1_only | 260 | 0.00 | -0.079 | 0.093 | 0.39 | 0.810 | - | - | -0.102 (260/260) |
| TP1_plus_runner | 260 | 0.00 | -0.091 | 0.050 | 0.21 | 1.010 | - | - | -0.115 (260/260) |
| break_even_plus_runner | 260 | 0.00 | -0.034 | -0.578 | 0.16 | 1.408 | - | - | -0.058 (260/260) |
| breakeven_lock_fixed | 260 | 0.00 | -0.023 | -1.000 | 0.36 | 0.954 | - | - | -0.047 (260/260) |
| eod_forced_flat | 260 | 0.00 | 0.066 | -1.000 | 0.22 | 1.753 | - | - | 0.042 (260/260) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 260 | 0.00 | 0.023 | -1.000 | 0.43 | 0.980 | - | - | - |
| momentum_failure | 260 | 0.00 | 0.003 | -0.315 | 0.33 | 0.873 | - | - | -0.021 (260/260) |
| pure_structure_trail | 260 | 0.00 | 0.043 | -1.000 | 0.19 | 1.571 | - | - | 0.020 (260/260) |
| struct_tp1_tp2 | 244 | 0.06 | -0.016 | -0.271 | 0.34 | 0.960 | - | - | -0.054 (244/244) |
| time_decay | 260 | 0.00 | 0.017 | -1.000 | 0.43 | 0.983 | - | - | -0.006 (260/260) |

### NAS100|OVERNIGHT|continue (n 83, clusters 83)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 57 | 0.31 | -0.054 | 0.090 | 0.26 | 0.896 | - | - | 0.025 (57/57) |
| TP1_only | 79 | 0.05 | -0.035 | 0.149 | 0.39 | 0.776 | - | - | 0.016 (79/79) |
| TP1_plus_runner | 79 | 0.05 | 0.027 | 0.088 | 0.23 | 1.001 | - | - | 0.078 (79/79) |
| break_even_plus_runner | 83 | 0.00 | -0.141 | -1.000 | 0.11 | 1.439 | - | - | -0.075 (83/83) |
| breakeven_lock_fixed | 83 | 0.00 | -0.082 | -1.000 | 0.31 | 0.996 | - | - | -0.015 (83/83) |
| eod_forced_flat | 83 | 0.00 | 0.069 | -1.000 | 0.19 | 1.697 | - | - | 0.136 (83/83) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 83 | 0.00 | -0.066 | -1.000 | 0.40 | 1.040 | - | - | - |
| momentum_failure | 83 | 0.00 | -0.083 | -0.558 | 0.32 | 0.933 | - | - | -0.017 (83/83) |
| pure_structure_trail | 83 | 0.00 | -0.006 | -1.000 | 0.18 | 1.642 | - | - | 0.060 (83/83) |
| struct_tp1_tp2 | 57 | 0.31 | -0.043 | -0.191 | 0.31 | 0.965 | - | - | 0.036 (57/57) |
| time_decay | 83 | 0.00 | -0.066 | -1.000 | 0.40 | 1.040 | - | - | 0.000 (83/83) |

### NAS100|OVERNIGHT|reverse (n 83, clusters 83)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 70 | 0.16 | 0.068 | 0.183 | 0.31 | 1.229 | - | - | 0.149 (70/70) |
| TP1_only | 83 | 0.00 | 0.011 | 0.156 | 0.42 | 0.851 | - | - | 0.025 (83/83) |
| TP1_plus_runner | 83 | 0.00 | 0.118 | 0.190 | 0.30 | 1.190 | - | - | 0.132 (83/83) |
| break_even_plus_runner | 83 | 0.00 | 0.289 | -0.767 | 0.19 | 1.667 | - | - | 0.304 (83/83) |
| breakeven_lock_fixed | 83 | 0.00 | 0.056 | -1.000 | 0.40 | 0.972 | - | - | 0.071 (83/83) |
| eod_forced_flat | 83 | 0.00 | 0.265 | -1.000 | 0.22 | 1.926 | - | - | 0.279 (83/83) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 83 | 0.00 | -0.015 | -1.000 | 0.42 | 1.051 | - | - | - |
| momentum_failure | 83 | 0.00 | 0.067 | -0.265 | 0.43 | 0.918 | - | - | 0.081 (83/83) |
| pure_structure_trail | 83 | 0.00 | 0.223 | -1.000 | 0.20 | 1.813 | - | - | 0.237 (83/83) |
| struct_tp1_tp2 | 70 | 0.16 | 0.008 | -0.323 | 0.36 | 1.000 | - | - | 0.090 (70/70) |
| time_decay | 83 | 0.00 | -0.015 | -1.000 | 0.42 | 1.051 | - | - | 0.000 (83/83) |

### NAS100|ROUND|break (n 950, clusters 792)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 598 | 0.37 | 0.006 | 0.073 | 0.23 | 0.770 | - | - | 0.042 (598/536) |
| TP1_only | 929 | 0.02 | -0.005 | 0.133 | 0.44 | 0.529 | - | - | 0.017 (929/776) |
| TP1_plus_runner | 929 | 0.02 | 0.012 | 0.067 | 0.22 | 0.784 | - | - | 0.034 (929/776) |
| break_even_plus_runner | 950 | 0.00 | -0.039 | -0.418 | 0.16 | 1.261 | - | - | -0.012 (950/792) |
| breakeven_lock_fixed | 950 | 0.00 | -0.017 | 0.004 | 0.32 | 0.969 | - | - | 0.010 (950/792) |
| eod_forced_flat | 950 | 0.00 | -0.022 | -1.000 | 0.22 | 1.486 | - | - | 0.005 (950/792) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 950 | 0.00 | -0.027 | -1.000 | 0.37 | 1.011 | - | - | - |
| momentum_failure | 950 | 0.00 | 0.015 | -0.346 | 0.31 | 0.872 | - | - | 0.042 (950/792) |
| pure_structure_trail | 950 | 0.00 | -0.034 | -0.618 | 0.18 | 1.343 | - | - | -0.007 (950/792) |
| struct_tp1_tp2 | 598 | 0.37 | -0.014 | 0.253 | 0.31 | 0.809 | - | - | 0.023 (598/536) |
| time_decay | 950 | 0.00 | -0.025 | -1.000 | 0.37 | 1.007 | - | - | 0.002 (950/792) |

### NAS100|ROUND|reject (n 2151, clusters 1319)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 1673 | 0.22 | -0.033 | 0.063 | 0.23 | 0.838 | - | - | -0.003 (1673/1080) |
| TP1_only | 2113 | 0.02 | -0.055 | 0.124 | 0.44 | 0.639 | - | - | -0.028 (2113/1301) |
| TP1_plus_runner | 2113 | 0.02 | -0.033 | 0.061 | 0.22 | 0.843 | - | - | -0.006 (2113/1301) |
| break_even_plus_runner | 2151 | 0.00 | -0.057 | -0.524 | 0.17 | 1.278 | - | - | -0.035 (2151/1319) |
| breakeven_lock_fixed | 2151 | 0.00 | -0.035 | -1.000 | 0.33 | 0.961 | - | - | -0.012 (2151/1319) |
| eod_forced_flat | 2151 | 0.00 | -0.054 | -1.000 | 0.22 | 1.482 | - | - | -0.032 (2151/1319) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 2151 | 0.00 | -0.022 | -1.000 | 0.39 | 0.970 | - | - | - |
| momentum_failure | 2151 | 0.00 | -0.033 | -0.412 | 0.31 | 0.861 | - | - | -0.010 (2151/1319) |
| pure_structure_trail | 2151 | 0.00 | -0.051 | -0.690 | 0.18 | 1.337 | - | - | -0.028 (2151/1319) |
| struct_tp1_tp2 | 1673 | 0.22 | -0.058 | -0.189 | 0.29 | 0.915 | - | - | -0.027 (1673/1080) |
| time_decay | 2151 | 0.00 | -0.020 | -1.000 | 0.39 | 0.967 | - | - | 0.002 (2151/1319) |

### NAS100|VOLREV|expand (n 769, clusters 730)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 300 | 0.61 | -0.088 | 0.065 | 0.22 | 0.896 | - | - | 0.006 (300/294) |
| TP1_only | 629 | 0.18 | -0.115 | 0.109 | 0.38 | 0.606 | - | - | -0.082 (629/611) |
| TP1_plus_runner | 629 | 0.18 | -0.038 | 0.065 | 0.22 | 0.917 | - | - | -0.005 (629/611) |
| break_even_plus_runner | 769 | 0.00 | 0.044 | -0.522 | 0.18 | 1.316 | - | - | 0.076 (769/730) |
| breakeven_lock_fixed | 769 | 0.00 | -0.042 | -1.000 | 0.34 | 0.957 | - | - | -0.010 (769/730) |
| eod_forced_flat | 769 | 0.00 | 0.105 | -1.000 | 0.23 | 1.548 | - | - | 0.137 (769/730) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 769 | 0.00 | -0.032 | -1.000 | 0.40 | 0.976 | - | - | - |
| momentum_failure | 769 | 0.00 | -0.028 | -0.628 | 0.35 | 0.929 | - | - | 0.003 (769/730) |
| pure_structure_trail | 769 | 0.00 | 0.062 | -0.997 | 0.20 | 1.437 | - | - | 0.094 (769/730) |
| struct_tp1_tp2 | 300 | 0.61 | -0.127 | -0.334 | 0.26 | 0.865 | - | - | -0.033 (300/294) |
| time_decay | 769 | 0.00 | -0.029 | -1.000 | 0.40 | 0.973 | - | - | 0.003 (769/730) |

### NAS100|VOLREV|fade (n 265, clusters 261)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 265 | 0.00 | -0.029 | -0.077 | 0.24 | 0.742 | - | - | -0.016 (265/261) |
| TP1_only | 265 | 0.00 | -0.011 | 0.080 | 0.43 | 0.826 | - | - | 0.002 (265/261) |
| TP1_plus_runner | 265 | 0.00 | -0.031 | -0.077 | 0.24 | 0.744 | - | - | -0.018 (265/261) |
| break_even_plus_runner | 265 | 0.00 | 0.034 | -0.349 | 0.19 | 1.276 | - | - | 0.046 (265/261) |
| breakeven_lock_fixed | 265 | 0.00 | -0.030 | -1.000 | 0.33 | 0.962 | - | - | -0.017 (265/261) |
| eod_forced_flat | 265 | 0.00 | -0.119 | -1.000 | 0.19 | 1.593 | - | - | -0.106 (265/261) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 265 | 0.00 | -0.012 | -1.000 | 0.39 | 0.976 | - | - | - |
| momentum_failure | 265 | 0.00 | -0.060 | -0.248 | 0.29 | 0.683 | - | - | -0.048 (265/261) |
| pure_structure_trail | 265 | 0.00 | 0.034 | -0.599 | 0.21 | 1.329 | - | - | 0.046 (265/261) |
| struct_tp1_tp2 | 265 | 0.00 | -0.020 | -0.380 | 0.31 | 1.028 | - | - | -0.007 (265/261) |
| time_decay | 265 | 0.00 | -0.009 | -1.000 | 0.39 | 0.971 | - | - | 0.004 (265/261) |


## SPX500

Family entries: n 2990, independent event clusters 1341, same-entry assertion OK; random-entry control: n 2990 (seed 20336122). Lab cost per entry (12 policies, one process): mean 36.1 ms, median 26.3 ms, p95 99.7 ms (mean 56 bars).

### All family entries (real) vs random-entry control

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 1753 | 0.41 | -0.049 | 0.074 | 0.26 | 0.901 | -0.129 | -0.120 | -0.024 (1753/939) |
| TP1_only | 2438 | 0.18 | -0.080 | 0.146 | 0.46 | 0.694 | 0.169 | -0.117 | -0.052 (2438/1192) |
| TP1_plus_runner | 2438 | 0.18 | -0.038 | 0.075 | 0.25 | 0.896 | -0.120 | -0.113 | -0.010 (2438/1192) |
| break_even_plus_runner | 2990 | 0.00 | -0.002 | -0.422 | 0.18 | 1.270 | -0.525 | -0.107 | 0.010 (2990/1341) |
| breakeven_lock_fixed | 2990 | 0.00 | -0.014 | -0.617 | 0.36 | 0.930 | -1.000 | -0.100 | -0.002 (2990/1341) |
| eod_forced_flat | 2990 | 0.00 | 0.015 | -1.000 | 0.23 | 1.565 | -1.000 | -0.108 | 0.027 (2990/1341) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 2990 | 0.00 | -0.012 | -1.000 | 0.41 | 0.959 | -1.000 | -0.101 | - |
| momentum_failure | 2990 | 0.00 | 0.003 | -0.356 | 0.35 | 0.823 | -0.374 | -0.099 | 0.015 (2990/1341) |
| pure_structure_trail | 2990 | 0.00 | 0.024 | -0.720 | 0.21 | 1.366 | -0.665 | -0.110 | 0.036 (2990/1341) |
| struct_tp1_tp2 | 1753 | 0.41 | -0.063 | -0.191 | 0.33 | 0.928 | -0.304 | -0.126 | -0.038 (1753/939) |
| time_decay | 2990 | 0.00 | -0.013 | -1.000 | 0.41 | 0.953 | -1.000 | -0.102 | -0.001 (2990/1341) |

### SPX500|EOD|continue (n 75, clusters 75)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 24 | 0.68 | 0.123 | 0.129 | 0.33 | 0.691 | - | - | -0.014 (24/24, n too small) |
| TP1_only | 56 | 0.25 | 0.070 | 0.262 | 0.57 | 0.483 | - | - | -0.060 (56/56) |
| TP1_plus_runner | 56 | 0.25 | 0.097 | 0.129 | 0.28 | 0.673 | - | - | -0.034 (56/56) |
| break_even_plus_runner | 75 | 0.00 | 0.144 | 0.046 | 0.28 | 0.913 | - | - | -0.012 (75/75) |
| breakeven_lock_fixed | 75 | 0.00 | 0.119 | 0.053 | 0.40 | 0.840 | - | - | -0.037 (75/75) |
| eod_forced_flat | 75 | 0.00 | 0.155 | -0.157 | 0.37 | 1.006 | - | - | -0.001 (75/75) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 75 | 0.00 | 0.156 | 0.018 | 0.46 | 0.818 | - | - | - |
| momentum_failure | 75 | 0.00 | 0.159 | -0.003 | 0.36 | 0.657 | - | - | 0.003 (75/75) |
| pure_structure_trail | 75 | 0.00 | 0.173 | -0.089 | 0.30 | 0.904 | - | - | 0.016 (75/75) |
| struct_tp1_tp2 | 24 | 0.68 | 0.076 | 0.443 | 0.40 | 0.687 | - | - | -0.062 (24/24, n too small) |
| time_decay | 75 | 0.00 | 0.156 | 0.018 | 0.46 | 0.818 | - | - | 0.000 (75/75) |

### SPX500|EOD|reverse (n 75, clusters 75)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 75 | 0.00 | -0.186 | -0.420 | 0.20 | 0.856 | - | - | -0.037 (75/75) |
| TP1_only | 75 | 0.00 | -0.223 | -1.000 | 0.45 | 0.777 | - | - | -0.074 (75/75) |
| TP1_plus_runner | 75 | 0.00 | -0.170 | -0.420 | 0.19 | 0.841 | - | - | -0.022 (75/75) |
| break_even_plus_runner | 75 | 0.00 | -0.189 | -0.654 | 0.21 | 1.041 | - | - | -0.040 (75/75) |
| breakeven_lock_fixed | 75 | 0.00 | -0.184 | -1.000 | 0.39 | 0.899 | - | - | -0.036 (75/75) |
| eod_forced_flat | 75 | 0.00 | -0.171 | -1.000 | 0.27 | 1.135 | - | - | -0.022 (75/75) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 75 | 0.00 | -0.149 | -1.000 | 0.41 | 0.894 | - | - | - |
| momentum_failure | 75 | 0.00 | -0.100 | -0.491 | 0.34 | 0.704 | - | - | 0.048 (75/75) |
| pure_structure_trail | 75 | 0.00 | -0.147 | -0.654 | 0.23 | 1.021 | - | - | 0.002 (75/75) |
| struct_tp1_tp2 | 75 | 0.00 | -0.174 | -1.000 | 0.35 | 0.931 | - | - | -0.026 (75/75) |
| time_decay | 75 | 0.00 | -0.149 | -1.000 | 0.41 | 0.894 | - | - | 0.000 (75/75) |

### SPX500|GAP|fade (n 74, clusters 74)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 51 | 0.31 | 0.011 | 0.231 | 0.30 | 1.108 | - | - | -0.165 (51/51) |
| TP1_only | 70 | 0.05 | -0.126 | 0.193 | 0.43 | 0.773 | - | - | -0.162 (70/70) |
| TP1_plus_runner | 70 | 0.05 | -0.061 | 0.129 | 0.28 | 1.115 | - | - | -0.097 (70/70) |
| break_even_plus_runner | 74 | 0.00 | -0.064 | 0.023 | 0.13 | 1.594 | - | - | -0.112 (74/74) |
| breakeven_lock_fixed | 74 | 0.00 | 0.096 | 0.028 | 0.44 | 0.859 | - | - | 0.049 (74/74) |
| eod_forced_flat | 74 | 0.00 | 0.308 | -1.000 | 0.19 | 2.159 | - | - | 0.260 (74/74) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 74 | 0.00 | 0.047 | -1.000 | 0.49 | 0.960 | - | - | - |
| momentum_failure | 74 | 0.00 | 0.085 | -0.343 | 0.45 | 0.889 | - | - | 0.038 (74/74) |
| pure_structure_trail | 74 | 0.00 | 0.236 | -1.000 | 0.20 | 1.796 | - | - | 0.189 (74/74) |
| struct_tp1_tp2 | 51 | 0.31 | -0.007 | 0.336 | 0.32 | 0.923 | - | - | -0.183 (51/51) |
| time_decay | 74 | 0.00 | 0.047 | -1.000 | 0.49 | 0.960 | - | - | 0.000 (74/74) |

### SPX500|GAP|go (n 90, clusters 90)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 26 | 0.71 | -0.165 | 0.088 | 0.28 | 0.955 | - | - | 0.066 (26/26, n too small) |
| TP1_only | 65 | 0.28 | -0.188 | 0.139 | 0.41 | 0.760 | - | - | -0.034 (65/65) |
| TP1_plus_runner | 65 | 0.28 | 0.030 | 0.090 | 0.28 | 1.123 | - | - | 0.184 (65/65) |
| break_even_plus_runner | 90 | 0.00 | 0.049 | -1.000 | 0.17 | 1.423 | - | - | 0.160 (90/90) |
| breakeven_lock_fixed | 90 | 0.00 | -0.080 | -1.000 | 0.38 | 0.944 | - | - | 0.032 (90/90) |
| eod_forced_flat | 90 | 0.00 | 0.130 | -1.000 | 0.22 | 1.692 | - | - | 0.242 (90/90) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 90 | 0.00 | -0.111 | -1.000 | 0.42 | 0.990 | - | - | - |
| momentum_failure | 90 | 0.00 | -0.058 | -0.850 | 0.41 | 0.921 | - | - | 0.053 (90/90) |
| pure_structure_trail | 90 | 0.00 | 0.055 | -1.000 | 0.20 | 1.565 | - | - | 0.166 (90/90) |
| struct_tp1_tp2 | 26 | 0.71 | -0.136 | 0.310 | 0.41 | 0.798 | - | - | 0.094 (26/26, n too small) |
| time_decay | 90 | 0.00 | -0.111 | -1.000 | 0.42 | 0.990 | - | - | 0.000 (90/90) |

### SPX500|LEADLAG|- (n 184, clusters 173)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 125 | 0.32 | -0.011 | 0.165 | 0.29 | 1.114 | - | - | -0.031 (125/118) |
| TP1_only | 171 | 0.07 | -0.049 | 0.238 | 0.52 | 0.697 | - | - | 0.001 (171/160) |
| TP1_plus_runner | 171 | 0.07 | -0.020 | 0.132 | 0.29 | 1.058 | - | - | 0.030 (171/160) |
| break_even_plus_runner | 184 | 0.00 | 0.070 | -0.503 | 0.16 | 1.359 | - | - | 0.119 (184/173) |
| breakeven_lock_fixed | 184 | 0.00 | -0.038 | -1.000 | 0.36 | 0.973 | - | - | 0.011 (184/173) |
| eod_forced_flat | 184 | 0.00 | 0.077 | -1.000 | 0.19 | 1.900 | - | - | 0.126 (184/173) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 184 | 0.00 | -0.049 | -1.000 | 0.43 | 1.061 | - | - | - |
| momentum_failure | 184 | 0.00 | 0.000 | -0.474 | 0.38 | 0.916 | - | - | 0.049 (184/173) |
| pure_structure_trail | 184 | 0.00 | 0.113 | -1.000 | 0.19 | 1.581 | - | - | 0.162 (184/173) |
| struct_tp1_tp2 | 125 | 0.32 | -0.019 | 0.299 | 0.37 | 0.921 | - | - | -0.039 (125/118) |
| time_decay | 184 | 0.00 | -0.049 | -1.000 | 0.43 | 1.061 | - | - | 0.000 (184/173) |

### SPX500|ORB|breakout (n 322, clusters 322)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 131 | 0.59 | 0.004 | 0.083 | 0.28 | 0.567 | - | - | 0.109 (131/131) |
| TP1_only | 236 | 0.27 | -0.037 | 0.098 | 0.46 | 0.347 | - | - | 0.007 (236/236) |
| TP1_plus_runner | 236 | 0.27 | 0.002 | 0.061 | 0.25 | 0.525 | - | - | 0.046 (236/236) |
| break_even_plus_runner | 322 | 0.00 | 0.023 | -0.155 | 0.24 | 0.876 | - | - | 0.003 (322/322) |
| breakeven_lock_fixed | 322 | 0.00 | 0.018 | -0.078 | 0.37 | 0.833 | - | - | -0.002 (322/322) |
| eod_forced_flat | 322 | 0.00 | 0.030 | -0.503 | 0.30 | 1.092 | - | - | 0.009 (322/322) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 322 | 0.00 | 0.020 | -0.308 | 0.40 | 0.840 | - | - | - |
| momentum_failure | 322 | 0.00 | 0.028 | -0.203 | 0.28 | 0.625 | - | - | 0.008 (322/322) |
| pure_structure_trail | 322 | 0.00 | 0.033 | -0.199 | 0.25 | 0.879 | - | - | 0.013 (322/322) |
| struct_tp1_tp2 | 131 | 0.59 | -0.057 | 0.191 | 0.39 | 0.524 | - | - | 0.047 (131/131) |
| time_decay | 322 | 0.00 | 0.016 | -0.202 | 0.40 | 0.796 | - | - | -0.004 (322/322) |

### SPX500|ORB|fade (n 265, clusters 265)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 246 | 0.07 | 0.017 | 0.084 | 0.27 | 1.049 | - | - | 0.009 (246/246) |
| TP1_only | 263 | 0.01 | -0.023 | 0.144 | 0.44 | 0.828 | - | - | -0.025 (263/263) |
| TP1_plus_runner | 263 | 0.01 | 0.011 | 0.068 | 0.25 | 1.051 | - | - | 0.009 (263/263) |
| break_even_plus_runner | 265 | 0.00 | 0.108 | -0.529 | 0.18 | 1.474 | - | - | 0.104 (265/265) |
| breakeven_lock_fixed | 265 | 0.00 | 0.009 | -1.000 | 0.37 | 1.010 | - | - | 0.005 (265/265) |
| eod_forced_flat | 265 | 0.00 | 0.017 | -1.000 | 0.20 | 1.878 | - | - | 0.013 (265/265) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 265 | 0.00 | 0.004 | -1.000 | 0.41 | 1.064 | - | - | - |
| momentum_failure | 265 | 0.00 | 0.013 | -0.412 | 0.33 | 0.913 | - | - | 0.009 (265/265) |
| pure_structure_trail | 265 | 0.00 | 0.021 | -1.000 | 0.19 | 1.641 | - | - | 0.017 (265/265) |
| struct_tp1_tp2 | 246 | 0.07 | 0.030 | -0.201 | 0.36 | 1.007 | - | - | 0.023 (246/246) |
| time_decay | 265 | 0.00 | -0.001 | -1.000 | 0.40 | 1.065 | - | - | -0.006 (265/265) |

### SPX500|OVERNIGHT|continue (n 79, clusters 79)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 48 | 0.39 | -0.163 | 0.086 | 0.29 | 0.770 | - | - | 0.056 (48/48) |
| TP1_only | 69 | 0.13 | -0.156 | 0.139 | 0.39 | 0.826 | - | - | -0.026 (69/69) |
| TP1_plus_runner | 69 | 0.13 | -0.026 | 0.088 | 0.26 | 1.007 | - | - | 0.104 (69/69) |
| break_even_plus_runner | 79 | 0.00 | -0.029 | -0.677 | 0.12 | 1.366 | - | - | 0.084 (79/79) |
| breakeven_lock_fixed | 79 | 0.00 | -0.060 | -1.000 | 0.34 | 0.955 | - | - | 0.054 (79/79) |
| eod_forced_flat | 79 | 0.00 | 0.133 | -1.000 | 0.22 | 1.653 | - | - | 0.247 (79/79) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 79 | 0.00 | -0.114 | -1.000 | 0.41 | 1.037 | - | - | - |
| momentum_failure | 79 | 0.00 | -0.009 | -0.414 | 0.40 | 0.829 | - | - | 0.104 (79/79) |
| pure_structure_trail | 79 | 0.00 | -0.034 | -1.000 | 0.18 | 1.575 | - | - | 0.080 (79/79) |
| struct_tp1_tp2 | 48 | 0.39 | -0.147 | -0.421 | 0.34 | 0.953 | - | - | 0.072 (48/48) |
| time_decay | 79 | 0.00 | -0.114 | -1.000 | 0.41 | 1.037 | - | - | 0.000 (79/79) |

### SPX500|OVERNIGHT|reverse (n 79, clusters 79)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 65 | 0.18 | 0.115 | 0.201 | 0.31 | 1.240 | - | - | -0.000 (65/65) |
| TP1_only | 78 | 0.01 | 0.054 | 0.207 | 0.46 | 0.826 | - | - | -0.004 (78/78) |
| TP1_plus_runner | 78 | 0.01 | 0.107 | 0.187 | 0.31 | 1.174 | - | - | 0.049 (78/78) |
| break_even_plus_runner | 79 | 0.00 | 0.269 | 0.018 | 0.20 | 1.651 | - | - | 0.193 (79/79) |
| breakeven_lock_fixed | 79 | 0.00 | 0.149 | 0.023 | 0.48 | 0.866 | - | - | 0.073 (79/79) |
| eod_forced_flat | 79 | 0.00 | 0.326 | -1.000 | 0.22 | 2.094 | - | - | 0.250 (79/79) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 79 | 0.00 | 0.076 | -1.000 | 0.49 | 0.956 | - | - | - |
| momentum_failure | 79 | 0.00 | 0.066 | -0.405 | 0.43 | 0.864 | - | - | -0.010 (79/79) |
| pure_structure_trail | 79 | 0.00 | 0.443 | -1.000 | 0.26 | 1.775 | - | - | 0.367 (79/79) |
| struct_tp1_tp2 | 65 | 0.18 | 0.149 | -0.109 | 0.37 | 0.981 | - | - | 0.034 (65/65) |
| time_decay | 79 | 0.00 | 0.076 | -1.000 | 0.49 | 0.956 | - | - | 0.000 (79/79) |

### SPX500|ROUND|break (n 218, clusters 208)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 109 | 0.50 | -0.061 | 0.076 | 0.23 | 0.901 | - | - | -0.065 (109/105) |
| TP1_only | 194 | 0.11 | -0.061 | 0.190 | 0.48 | 0.612 | - | - | -0.132 (194/184) |
| TP1_plus_runner | 194 | 0.11 | -0.011 | 0.093 | 0.24 | 0.891 | - | - | -0.081 (194/184) |
| break_even_plus_runner | 218 | 0.00 | 0.028 | -0.423 | 0.18 | 1.293 | - | - | -0.062 (218/208) |
| breakeven_lock_fixed | 218 | 0.00 | 0.042 | -0.905 | 0.39 | 0.912 | - | - | -0.049 (218/208) |
| eod_forced_flat | 218 | 0.00 | 0.065 | -1.000 | 0.24 | 1.518 | - | - | -0.026 (218/208) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 218 | 0.00 | 0.091 | -1.000 | 0.44 | 0.898 | - | - | - |
| momentum_failure | 218 | 0.00 | 0.093 | -0.317 | 0.38 | 0.809 | - | - | 0.002 (218/208) |
| pure_structure_trail | 218 | 0.00 | 0.026 | -0.618 | 0.19 | 1.365 | - | - | -0.065 (218/208) |
| struct_tp1_tp2 | 109 | 0.50 | -0.124 | -0.243 | 0.27 | 0.911 | - | - | -0.127 (109/105) |
| time_decay | 218 | 0.00 | 0.091 | -1.000 | 0.44 | 0.898 | - | - | 0.000 (218/208) |

### SPX500|ROUND|reject (n 490, clusters 362)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 362 | 0.26 | -0.089 | 0.069 | 0.25 | 0.828 | - | - | -0.071 (362/267) |
| TP1_only | 459 | 0.06 | -0.118 | 0.156 | 0.45 | 0.701 | - | - | -0.064 (459/338) |
| TP1_plus_runner | 459 | 0.06 | -0.100 | 0.064 | 0.23 | 0.847 | - | - | -0.045 (459/338) |
| break_even_plus_runner | 490 | 0.00 | -0.143 | -0.557 | 0.16 | 1.260 | - | - | -0.081 (490/362) |
| breakeven_lock_fixed | 490 | 0.00 | -0.031 | -0.644 | 0.34 | 0.937 | - | - | 0.030 (490/362) |
| eod_forced_flat | 490 | 0.00 | -0.223 | -1.000 | 0.17 | 1.576 | - | - | -0.161 (490/362) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 490 | 0.00 | -0.062 | -1.000 | 0.38 | 0.991 | - | - | - |
| momentum_failure | 490 | 0.00 | -0.036 | -0.398 | 0.33 | 0.820 | - | - | 0.026 (490/362) |
| pure_structure_trail | 490 | 0.00 | -0.126 | -0.783 | 0.18 | 1.355 | - | - | -0.065 (490/362) |
| struct_tp1_tp2 | 362 | 0.26 | -0.085 | -0.190 | 0.32 | 0.925 | - | - | -0.067 (362/267) |
| time_decay | 490 | 0.00 | -0.061 | -1.000 | 0.38 | 0.984 | - | - | 0.001 (490/362) |

### SPX500|VOLREV|expand (n 798, clusters 750)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 251 | 0.69 | -0.091 | 0.114 | 0.26 | 0.974 | - | - | -0.044 (251/246) |
| TP1_only | 461 | 0.42 | -0.123 | 0.168 | 0.45 | 0.683 | - | - | -0.091 (461/451) |
| TP1_plus_runner | 461 | 0.42 | -0.075 | 0.109 | 0.25 | 0.962 | - | - | -0.043 (461/451) |
| break_even_plus_runner | 798 | 0.00 | -0.015 | -0.565 | 0.18 | 1.302 | - | - | -0.005 (798/750) |
| breakeven_lock_fixed | 798 | 0.00 | -0.035 | -1.000 | 0.36 | 0.943 | - | - | -0.026 (798/750) |
| eod_forced_flat | 798 | 0.00 | 0.054 | -1.000 | 0.24 | 1.544 | - | - | 0.063 (798/750) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 798 | 0.00 | -0.009 | -1.000 | 0.42 | 0.948 | - | - | - |
| momentum_failure | 798 | 0.00 | -0.009 | -0.574 | 0.37 | 0.906 | - | - | 0.000 (798/750) |
| pure_structure_trail | 798 | 0.00 | 0.022 | -0.809 | 0.21 | 1.392 | - | - | 0.032 (798/750) |
| struct_tp1_tp2 | 251 | 0.69 | -0.146 | -0.312 | 0.27 | 0.973 | - | - | -0.100 (251/246) |
| time_decay | 798 | 0.00 | -0.007 | -1.000 | 0.42 | 0.944 | - | - | 0.002 (798/750) |

### SPX500|VOLREV|fade (n 241, clusters 236)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 240 | 0.00 | -0.049 | -0.145 | 0.24 | 0.771 | - | - | -0.012 (240/235) |
| TP1_only | 241 | 0.00 | -0.034 | -0.070 | 0.43 | 0.859 | - | - | -0.004 (241/236) |
| TP1_plus_runner | 241 | 0.00 | -0.033 | -0.143 | 0.23 | 0.768 | - | - | -0.002 (241/236) |
| break_even_plus_runner | 241 | 0.00 | 0.028 | -0.459 | 0.16 | 1.272 | - | - | 0.059 (241/236) |
| breakeven_lock_fixed | 241 | 0.00 | -0.041 | -0.070 | 0.31 | 0.958 | - | - | -0.010 (241/236) |
| eod_forced_flat | 241 | 0.00 | -0.006 | -1.000 | 0.21 | 1.560 | - | - | 0.025 (241/236) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 241 | 0.00 | -0.030 | -1.000 | 0.39 | 0.985 | - | - | - |
| momentum_failure | 241 | 0.00 | -0.036 | -0.228 | 0.30 | 0.678 | - | - | -0.005 (241/236) |
| pure_structure_trail | 241 | 0.00 | 0.073 | -0.552 | 0.19 | 1.329 | - | - | 0.103 (241/236) |
| struct_tp1_tp2 | 240 | 0.00 | -0.061 | -0.423 | 0.30 | 1.053 | - | - | -0.024 (240/235) |
| time_decay | 241 | 0.00 | -0.038 | -1.000 | 0.39 | 0.983 | - | - | -0.007 (241/236) |


## XAUUSD

Family entries: n 7180, independent event clusters 1782, same-entry assertion OK; random-entry control: n 7180 (seed 20289911). Lab cost per entry (12 policies, one process): mean 30.8 ms, median 24.0 ms, p95 77.1 ms (mean 69 bars).

### All family entries (real) vs random-entry control

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 5684 | 0.21 | -0.045 | 0.112 | 0.26 | 0.900 | 0.074 | -0.036 | 0.037 (5684/1622) |
| TP1_only | 6780 | 0.06 | -0.051 | 0.194 | 0.47 | 0.652 | 0.188 | -0.063 | 0.023 (6780/1750) |
| TP1_plus_runner | 6780 | 0.06 | -0.037 | 0.104 | 0.24 | 0.917 | 0.073 | -0.029 | 0.036 (6780/1750) |
| break_even_plus_runner | 7180 | 0.00 | -0.053 | -0.618 | 0.13 | 1.474 | -0.506 | -0.034 | 0.018 (7180/1782) |
| breakeven_lock_fixed | 7180 | 0.00 | -0.062 | -1.000 | 0.31 | 1.028 | -1.000 | -0.049 | 0.010 (7180/1782) |
| eod_forced_flat | 7180 | 0.00 | 0.014 | -1.000 | 0.14 | 2.050 | -1.000 | -0.005 | 0.085 (7180/1782) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 7180 | 0.00 | -0.071 | -1.000 | 0.36 | 1.081 | -1.000 | -0.052 | - |
| momentum_failure | 7180 | 0.00 | -0.071 | -0.480 | 0.29 | 0.934 | -0.356 | -0.043 | 0.001 (7180/1782) |
| pure_structure_trail | 7180 | 0.00 | -0.072 | -0.838 | 0.14 | 1.591 | -0.662 | -0.044 | -0.000 (7180/1782) |
| struct_tp1_tp2 | 5684 | 0.21 | -0.064 | -0.286 | 0.29 | 0.966 | -0.237 | -0.066 | 0.017 (5684/1622) |
| time_decay | 7180 | 0.00 | -0.070 | -1.000 | 0.36 | 1.076 | -1.000 | -0.051 | 0.001 (7180/1782) |

### XAUUSD|EOD|continue (n 78, clusters 78)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 22 | 0.72 | -0.072 | 0.105 | 0.26 | 0.624 | - | - | -0.095 (22/22, n too small) |
| TP1_only | 66 | 0.15 | -0.022 | 0.176 | 0.44 | 0.483 | - | - | 0.107 (66/66) |
| TP1_plus_runner | 66 | 0.15 | -0.013 | 0.094 | 0.22 | 0.700 | - | - | 0.116 (66/66) |
| break_even_plus_runner | 78 | 0.00 | -0.016 | -0.466 | 0.15 | 1.298 | - | - | 0.049 (78/78) |
| breakeven_lock_fixed | 78 | 0.00 | 0.045 | 0.011 | 0.32 | 0.992 | - | - | 0.110 (78/78) |
| eod_forced_flat | 78 | 0.00 | 0.005 | -1.000 | 0.22 | 1.531 | - | - | 0.069 (78/78) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 78 | 0.00 | -0.065 | -1.000 | 0.33 | 1.113 | - | - | - |
| momentum_failure | 78 | 0.00 | -0.207 | -0.489 | 0.18 | 0.926 | - | - | -0.143 (78/78) |
| pure_structure_trail | 78 | 0.00 | -0.062 | -0.635 | 0.15 | 1.344 | - | - | 0.003 (78/78) |
| struct_tp1_tp2 | 22 | 0.72 | 0.074 | 0.388 | 0.37 | 0.745 | - | - | 0.052 (22/22, n too small) |
| time_decay | 78 | 0.00 | -0.065 | -1.000 | 0.33 | 1.113 | - | - | 0.000 (78/78) |

### XAUUSD|EOD|reverse (n 78, clusters 78)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 78 | 0.00 | 0.054 | 0.045 | 0.26 | 0.719 | - | - | 0.127 (78/78) |
| TP1_only | 78 | 0.00 | 0.028 | 0.225 | 0.44 | 0.785 | - | - | 0.101 (78/78) |
| TP1_plus_runner | 78 | 0.00 | 0.045 | 0.044 | 0.25 | 0.728 | - | - | 0.118 (78/78) |
| break_even_plus_runner | 78 | 0.00 | -0.149 | -0.499 | 0.12 | 1.280 | - | - | -0.076 (78/78) |
| breakeven_lock_fixed | 78 | 0.00 | -0.014 | -1.000 | 0.33 | 1.021 | - | - | 0.058 (78/78) |
| eod_forced_flat | 78 | 0.00 | -0.118 | -1.000 | 0.21 | 1.492 | - | - | -0.046 (78/78) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 78 | 0.00 | -0.072 | -1.000 | 0.34 | 1.088 | - | - | - |
| momentum_failure | 78 | 0.00 | 0.033 | -0.216 | 0.28 | 0.692 | - | - | 0.105 (78/78) |
| pure_structure_trail | 78 | 0.00 | -0.133 | -0.513 | 0.14 | 1.303 | - | - | -0.060 (78/78) |
| struct_tp1_tp2 | 78 | 0.00 | -0.006 | -0.161 | 0.28 | 1.058 | - | - | 0.067 (78/78) |
| time_decay | 78 | 0.00 | -0.072 | -1.000 | 0.34 | 1.088 | - | - | 0.000 (78/78) |

### XAUUSD|GAP|fade (n 77, clusters 77)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 75 | 0.03 | -0.012 | 0.179 | 0.26 | 1.029 | - | - | -0.112 (75/75) |
| TP1_only | 76 | 0.01 | -0.076 | 0.230 | 0.44 | 0.701 | - | - | -0.162 (76/76) |
| TP1_plus_runner | 76 | 0.01 | 0.002 | 0.151 | 0.25 | 1.015 | - | - | -0.084 (76/76) |
| break_even_plus_runner | 77 | 0.00 | 0.120 | 0.034 | 0.18 | 1.433 | - | - | 0.049 (77/77) |
| breakeven_lock_fixed | 77 | 0.00 | 0.123 | 0.034 | 0.38 | 0.954 | - | - | 0.052 (77/77) |
| eod_forced_flat | 77 | 0.00 | 0.659 | -1.000 | 0.17 | 2.464 | - | - | 0.587 (77/77) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 77 | 0.00 | 0.071 | -1.000 | 0.42 | 1.019 | - | - | - |
| momentum_failure | 77 | 0.00 | 0.130 | -0.363 | 0.38 | 0.892 | - | - | 0.058 (77/77) |
| pure_structure_trail | 77 | 0.00 | 0.153 | -0.436 | 0.20 | 1.483 | - | - | 0.081 (77/77) |
| struct_tp1_tp2 | 75 | 0.03 | -0.047 | -0.274 | 0.29 | 1.051 | - | - | -0.147 (75/75) |
| time_decay | 77 | 0.00 | 0.071 | -1.000 | 0.42 | 1.019 | - | - | 0.000 (77/77) |

### XAUUSD|GAP|go (n 80, clusters 80)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 47 | 0.41 | -0.007 | 0.132 | 0.24 | 1.020 | - | - | 0.248 (47/47) |
| TP1_only | 70 | 0.12 | -0.062 | 0.202 | 0.46 | 0.648 | - | - | 0.045 (70/70) |
| TP1_plus_runner | 70 | 0.12 | -0.059 | 0.110 | 0.22 | 0.985 | - | - | 0.048 (70/70) |
| break_even_plus_runner | 80 | 0.00 | 0.044 | -0.474 | 0.15 | 1.358 | - | - | 0.076 (80/80) |
| breakeven_lock_fixed | 80 | 0.00 | -0.072 | -1.000 | 0.29 | 1.011 | - | - | -0.041 (80/80) |
| eod_forced_flat | 80 | 0.00 | 0.211 | -1.000 | 0.14 | 2.087 | - | - | 0.243 (80/80) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 80 | 0.00 | -0.031 | -1.000 | 0.39 | 1.023 | - | - | - |
| momentum_failure | 80 | 0.00 | -0.125 | -0.496 | 0.27 | 0.980 | - | - | -0.094 (80/80) |
| pure_structure_trail | 80 | 0.00 | 0.088 | -0.597 | 0.15 | 1.467 | - | - | 0.119 (80/80) |
| struct_tp1_tp2 | 47 | 0.41 | -0.104 | -0.180 | 0.29 | 0.932 | - | - | 0.152 (47/47) |
| time_decay | 80 | 0.00 | -0.031 | -1.000 | 0.39 | 1.023 | - | - | 0.000 (80/80) |

### XAUUSD|ORB|breakout (n 319, clusters 319)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 220 | 0.31 | -0.051 | 0.081 | 0.26 | 0.663 | - | - | 0.063 (220/220) |
| TP1_only | 280 | 0.12 | -0.085 | 0.139 | 0.49 | 0.508 | - | - | 0.015 (280/280) |
| TP1_plus_runner | 280 | 0.12 | -0.064 | 0.071 | 0.24 | 0.670 | - | - | 0.036 (280/280) |
| break_even_plus_runner | 319 | 0.00 | -0.108 | -0.353 | 0.15 | 1.120 | - | - | 0.014 (319/319) |
| breakeven_lock_fixed | 319 | 0.00 | -0.158 | -1.000 | 0.29 | 1.028 | - | - | -0.037 (319/319) |
| eod_forced_flat | 319 | 0.00 | 0.123 | -1.000 | 0.17 | 1.846 | - | - | 0.245 (319/319) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 319 | 0.00 | -0.121 | -1.000 | 0.37 | 1.040 | - | - | - |
| momentum_failure | 319 | 0.00 | -0.113 | -0.371 | 0.22 | 0.802 | - | - | 0.008 (319/319) |
| pure_structure_trail | 319 | 0.00 | -0.106 | -0.384 | 0.15 | 1.152 | - | - | 0.015 (319/319) |
| struct_tp1_tp2 | 220 | 0.31 | -0.077 | 0.155 | 0.32 | 0.786 | - | - | 0.037 (220/220) |
| time_decay | 319 | 0.00 | -0.097 | -1.000 | 0.35 | 0.968 | - | - | 0.025 (319/319) |

### XAUUSD|ORB|fade (n 282, clusters 282)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 270 | 0.04 | 0.023 | 0.086 | 0.26 | 1.269 | - | - | 0.090 (270/270) |
| TP1_only | 282 | 0.00 | -0.017 | 0.147 | 0.43 | 0.885 | - | - | 0.037 (282/282) |
| TP1_plus_runner | 282 | 0.00 | 0.038 | 0.067 | 0.24 | 1.263 | - | - | 0.092 (282/282) |
| break_even_plus_runner | 282 | 0.00 | 0.096 | -0.656 | 0.14 | 1.600 | - | - | 0.149 (282/282) |
| breakeven_lock_fixed | 282 | 0.00 | -0.063 | -1.000 | 0.33 | 1.024 | - | - | -0.009 (282/282) |
| eod_forced_flat | 282 | 0.00 | 0.324 | -1.000 | 0.12 | 2.574 | - | - | 0.378 (282/282) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 282 | 0.00 | -0.054 | -1.000 | 0.39 | 1.052 | - | - | - |
| momentum_failure | 282 | 0.00 | -0.072 | -0.680 | 0.33 | 0.988 | - | - | -0.018 (282/282) |
| pure_structure_trail | 282 | 0.00 | 0.012 | -1.000 | 0.14 | 1.884 | - | - | 0.065 (282/282) |
| struct_tp1_tp2 | 270 | 0.04 | 0.031 | -0.344 | 0.30 | 1.150 | - | - | 0.098 (270/270) |
| time_decay | 282 | 0.00 | -0.055 | -1.000 | 0.39 | 1.051 | - | - | -0.002 (282/282) |

### XAUUSD|OVERNIGHT|continue (n 79, clusters 79)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 56 | 0.29 | -0.071 | 0.020 | 0.22 | 0.947 | - | - | 0.126 (56/56) |
| TP1_only | 76 | 0.04 | -0.140 | 0.194 | 0.45 | 0.684 | - | - | 0.037 (76/76) |
| TP1_plus_runner | 76 | 0.04 | -0.058 | 0.020 | 0.22 | 0.914 | - | - | 0.119 (76/76) |
| break_even_plus_runner | 79 | 0.00 | -0.007 | -0.589 | 0.14 | 1.381 | - | - | 0.107 (79/79) |
| breakeven_lock_fixed | 79 | 0.00 | -0.158 | -1.000 | 0.28 | 1.033 | - | - | -0.044 (79/79) |
| eod_forced_flat | 79 | 0.00 | 0.439 | -1.000 | 0.19 | 1.826 | - | - | 0.553 (79/79) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 79 | 0.00 | -0.114 | -1.000 | 0.36 | 1.029 | - | - | - |
| momentum_failure | 79 | 0.00 | -0.080 | -0.341 | 0.26 | 0.840 | - | - | 0.034 (79/79) |
| pure_structure_trail | 79 | 0.00 | -0.042 | -0.643 | 0.14 | 1.431 | - | - | 0.072 (79/79) |
| struct_tp1_tp2 | 56 | 0.29 | -0.164 | -0.332 | 0.25 | 0.947 | - | - | 0.032 (56/56) |
| time_decay | 79 | 0.00 | -0.114 | -1.000 | 0.36 | 1.029 | - | - | 0.000 (79/79) |

### XAUUSD|OVERNIGHT|reverse (n 79, clusters 79)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 78 | 0.01 | -0.023 | 0.124 | 0.23 | 1.070 | - | - | -0.081 (78/78) |
| TP1_only | 79 | 0.00 | -0.153 | 0.161 | 0.38 | 0.851 | - | - | -0.197 (79/79) |
| TP1_plus_runner | 79 | 0.00 | -0.002 | 0.126 | 0.22 | 1.048 | - | - | -0.046 (79/79) |
| break_even_plus_runner | 79 | 0.00 | -0.056 | -0.436 | 0.14 | 1.458 | - | - | -0.100 (79/79) |
| breakeven_lock_fixed | 79 | 0.00 | 0.049 | -1.000 | 0.33 | 1.027 | - | - | 0.005 (79/79) |
| eod_forced_flat | 79 | 0.00 | 0.357 | -1.000 | 0.13 | 2.417 | - | - | 0.312 (79/79) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 79 | 0.00 | 0.044 | -1.000 | 0.38 | 1.052 | - | - | - |
| momentum_failure | 79 | 0.00 | 0.072 | -0.349 | 0.32 | 0.915 | - | - | 0.027 (79/79) |
| pure_structure_trail | 79 | 0.00 | -0.019 | -0.489 | 0.16 | 1.514 | - | - | -0.064 (79/79) |
| struct_tp1_tp2 | 78 | 0.01 | -0.113 | -0.362 | 0.23 | 1.219 | - | - | -0.171 (78/78) |
| time_decay | 79 | 0.00 | 0.044 | -1.000 | 0.38 | 1.052 | - | - | 0.000 (79/79) |

### XAUUSD|ROUND|break (n 1620, clusters 1278)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 1203 | 0.26 | -0.054 | 0.117 | 0.26 | 0.852 | - | - | 0.087 (1203/1004) |
| TP1_only | 1550 | 0.04 | -0.048 | 0.201 | 0.47 | 0.606 | - | - | 0.067 (1550/1233) |
| TP1_plus_runner | 1550 | 0.04 | -0.037 | 0.108 | 0.24 | 0.897 | - | - | 0.077 (1550/1233) |
| break_even_plus_runner | 1620 | 0.00 | -0.074 | -0.632 | 0.12 | 1.481 | - | - | 0.032 (1620/1278) |
| breakeven_lock_fixed | 1620 | 0.00 | -0.082 | -1.000 | 0.29 | 1.049 | - | - | 0.024 (1620/1278) |
| eod_forced_flat | 1620 | 0.00 | -0.027 | -1.000 | 0.14 | 1.991 | - | - | 0.078 (1620/1278) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 1620 | 0.00 | -0.105 | -1.000 | 0.34 | 1.119 | - | - | - |
| momentum_failure | 1620 | 0.00 | -0.079 | -0.519 | 0.29 | 0.958 | - | - | 0.027 (1620/1278) |
| pure_structure_trail | 1620 | 0.00 | -0.073 | -0.803 | 0.13 | 1.598 | - | - | 0.032 (1620/1278) |
| struct_tp1_tp2 | 1203 | 0.26 | -0.074 | -0.250 | 0.28 | 0.922 | - | - | 0.068 (1203/1004) |
| time_decay | 1620 | 0.00 | -0.107 | -1.000 | 0.34 | 1.116 | - | - | -0.001 (1620/1278) |

### XAUUSD|ROUND|reject (n 3223, clusters 1803)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 2771 | 0.14 | -0.054 | 0.113 | 0.26 | 0.916 | - | - | 0.000 (2771/1613) |
| TP1_only | 3151 | 0.02 | -0.052 | 0.200 | 0.48 | 0.642 | - | - | 0.001 (3151/1770) |
| TP1_plus_runner | 3151 | 0.02 | -0.048 | 0.104 | 0.24 | 0.927 | - | - | 0.005 (3151/1770) |
| break_even_plus_runner | 3223 | 0.00 | -0.068 | -0.634 | 0.12 | 1.503 | - | - | -0.015 (3223/1803) |
| breakeven_lock_fixed | 3223 | 0.00 | -0.044 | -1.000 | 0.32 | 1.011 | - | - | 0.008 (3223/1803) |
| eod_forced_flat | 3223 | 0.00 | -0.039 | -1.000 | 0.13 | 2.073 | - | - | 0.013 (3223/1803) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 3223 | 0.00 | -0.053 | -1.000 | 0.37 | 1.061 | - | - | - |
| momentum_failure | 3223 | 0.00 | -0.063 | -0.489 | 0.30 | 0.935 | - | - | -0.010 (3223/1803) |
| pure_structure_trail | 3223 | 0.00 | -0.094 | -0.846 | 0.13 | 1.612 | - | - | -0.042 (3223/1803) |
| struct_tp1_tp2 | 2771 | 0.14 | -0.063 | -0.271 | 0.30 | 0.949 | - | - | -0.008 (2771/1613) |
| time_decay | 3223 | 0.00 | -0.052 | -1.000 | 0.37 | 1.060 | - | - | 0.000 (3223/1803) |

### XAUUSD|VOLREV|expand (n 919, clusters 882)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 518 | 0.44 | -0.035 | 0.141 | 0.28 | 0.932 | - | - | 0.075 (518/512) |
| TP1_only | 726 | 0.21 | -0.048 | 0.207 | 0.46 | 0.645 | - | - | 0.034 (726/710) |
| TP1_plus_runner | 726 | 0.21 | -0.032 | 0.130 | 0.25 | 0.981 | - | - | 0.050 (726/710) |
| break_even_plus_runner | 919 | 0.00 | -0.016 | -0.785 | 0.12 | 1.532 | - | - | 0.071 (919/882) |
| breakeven_lock_fixed | 919 | 0.00 | -0.074 | -1.000 | 0.30 | 1.056 | - | - | 0.013 (919/882) |
| eod_forced_flat | 919 | 0.00 | 0.018 | -1.000 | 0.13 | 2.114 | - | - | 0.105 (919/882) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 919 | 0.00 | -0.087 | -1.000 | 0.35 | 1.124 | - | - | - |
| momentum_failure | 919 | 0.00 | -0.081 | -0.707 | 0.30 | 1.036 | - | - | 0.006 (919/882) |
| pure_structure_trail | 919 | 0.00 | -0.043 | -1.000 | 0.14 | 1.708 | - | - | 0.044 (919/882) |
| struct_tp1_tp2 | 518 | 0.44 | -0.098 | -0.333 | 0.24 | 1.018 | - | - | 0.012 (518/512) |
| time_decay | 919 | 0.00 | -0.085 | -1.000 | 0.35 | 1.122 | - | - | 0.002 (919/882) |

### XAUUSD|VOLREV|fade (n 346, clusters 341)

| policy | n appl. | NA share | mean R | median R | capture | giveback | control median R | control mean R | paired dR vs fixed_1_5r (pairs/clusters) |
|---|---|---|---|---|---|---|---|---|---|
| TP1_TP2_runner | 346 | 0.00 | -0.030 | 0.017 | 0.27 | 0.723 | - | - | 0.042 (346/341) |
| TP1_only | 346 | 0.00 | -0.025 | 0.127 | 0.44 | 0.820 | - | - | 0.047 (346/341) |
| TP1_plus_runner | 346 | 0.00 | -0.022 | 0.017 | 0.26 | 0.715 | - | - | 0.051 (346/341) |
| break_even_plus_runner | 346 | 0.00 | -0.054 | -0.642 | 0.15 | 1.377 | - | - | 0.019 (346/341) |
| breakeven_lock_fixed | 346 | 0.00 | -0.084 | -1.000 | 0.31 | 1.035 | - | - | -0.012 (346/341) |
| eod_forced_flat | 346 | 0.00 | 0.000 | -1.000 | 0.18 | 1.819 | - | - | 0.073 (346/341) |
| failed_move_exit | 0 | 1.00 | - | - | - | - | - | - | - (0/0, n too small) |
| fixed_1_5r | 346 | 0.00 | -0.072 | -1.000 | 0.37 | 1.063 | - | - | - |
| momentum_failure | 346 | 0.00 | -0.096 | -0.224 | 0.27 | 0.706 | - | - | -0.023 (346/341) |
| pure_structure_trail | 346 | 0.00 | -0.064 | -0.769 | 0.17 | 1.443 | - | - | 0.008 (346/341) |
| struct_tp1_tp2 | 346 | 0.00 | -0.049 | -0.388 | 0.28 | 1.070 | - | - | 0.024 (346/341) |
| time_decay | 346 | 0.00 | -0.069 | -1.000 | 0.37 | 1.059 | - | - | 0.003 (346/341) |

