# V2 raw edge scan (structure-free, Train side only)

Trials: raw scan 48,324 (41,628 unique); V1 header 30,310 / 29,798 (header only, not added). Net = BASE costs, ATR units, one trade/day/cell, day-clustered t.

| dataset | days | cells | best net t | q_BH | best cell | signflip p95/p99 | boot p95/p99 | shift p95 (labelled; obs) | q<0.05 | survive gate |
|---|---|---|---|---|---|---|---|---|---|---|
| GER40 | 158 | 9722 | 3.76 | 1.000 | core_dow|dow3_long|17:00|3b n=31 mean=0.659 | 4.35/5.11 | 5.70/6.97 | 4.18 (obs 3.76) | 0 | 1/20 |
| GER40_ar1 | 160 | 9722 | 3.98 | 1.000 | core_dow|dow3_long|17:00|3b n=32 mean=0.724 | 4.38/4.69 | 6.08/7.64 | 4.74 (obs 3.98) | 0 | 0/20 |
| NAS100* | 139 | 4466 | 4.52 | 0.237 | first30_dow|dow0_long|09:35|48b n=26 mean=4.238 | 4.32/4.72 | 7.00/8.76 | 4.71 (obs 4.52) | 0 | 0/20 |
| SPX500* | 139 | 4466 | 3.44 | 1.000 | core_dow|dow1_long|12:10|6b n=28 mean=0.683 | 3.88/4.26 | 6.00/7.50 | 4.03 (obs 3.44) | 0 | 0/20 |
| XAUUSD* | 138 | 6626 | 3.15 | 0.841 | last60_dow|dow0_long|16:00|12b n=28 mean=0.734 | 4.08/4.51 | 5.55/6.56 | 3.29 (obs 3.15) | 0 | 1/20 |
| EURUSD* | 133 | 6626 | 3.03 | 1.000 | overlap_dow|dow2_long|14:30|24b n=27 mean=2.379 | 4.05/4.34 | 5.67/7.45 | 3.28 (obs 3.03) | 0 | 0/20 |

`*` provisional calendar. Bootstrap null = day-resampled, centred (all cells); day-shift null only speaks for labelled cells (DOW/conditioned).

Pooled (primary datasets, max over markets): observed best net t 4.52 vs signflip null p50/p95/p99 3.87/4.75/5.11 (bootstrap 5.25/7.52/9.50); pooled BH survivors (q<0.05): 0 of 31,906.

## Top 5 cells per dataset (net t, Train)

**GER40**: core_dow|dow3_long|17:00|3b t=3.76 m=0.659 comb=0.547 shock=0.590 chunks+=3/4; first30|short|09:05|3b t=3.63 m=0.695 comb=0.534 shock=0.582 chunks+=4/4; core_dow|dow0_long|15:45|flat t=3.35 m=2.501 comb=2.356 shock=2.403 chunks+=4/4; core_dow|dow0_long|15:40|flat t=3.27 m=2.610 comb=2.462 shock=2.511 chunks+=4/4; first30|short|09:05|6b t=3.27 m=0.789 comb=0.632 shock=0.685 chunks+=4/4

**GER40_ar1**: core_dow|dow3_long|17:00|3b t=3.98 m=0.724 comb=0.611 shock=0.655 chunks+=3/4; core_dow|dow0_long|15:45|flat t=3.54 m=2.562 comb=2.414 shock=2.462 chunks+=4/4; first30|short|09:05|3b t=3.51 m=0.658 comb=0.494 shock=0.542 chunks+=4/4; core_dow|dow0_long|15:40|flat t=3.46 m=2.668 comb=2.516 shock=2.567 chunks+=3/4; core|long|17:00|3b t=3.41 m=0.293 comb=0.174 shock=0.219 chunks+=4/4

**NAS100**: first30_dow|dow0_long|09:35|48b t=4.52 m=4.238 comb=4.139 shock=4.167 chunks+=4/4; first30_dow|dow0_long|09:40|48b t=4.29 m=3.257 comb=3.171 shock=3.201 chunks+=4/4; first30_dow|dow0_long|09:30|48b t=4.24 m=5.728 comb=5.607 shock=5.645 chunks+=4/4; first30_dow|dow0_long|09:30|flat t=3.92 m=5.807 comb=5.688 shock=5.728 chunks+=4/4; core_dow|dow1_long|12:10|3b t=3.75 m=0.457 comb=0.393 shock=0.414 chunks+=4/4

**SPX500**: core_dow|dow1_long|12:10|6b t=3.44 m=0.683 comb=0.493 shock=0.577 chunks+=4/4; first30_dow|dow0_long|09:35|48b t=3.01 m=3.390 comb=3.100 shock=3.226 chunks+=4/4; overnight_dow|dow2_long|16:00|ovn t=2.91 m=2.066 comb=1.853 shock=1.947 chunks+=4/4; core_dow|dow1_long|12:10|3b t=2.80 m=0.381 comb=0.191 shock=0.275 chunks+=4/4; first30_dow|dow0_long|09:45|48b t=2.66 m=2.625 comb=2.376 shock=2.489 chunks+=4/4

**XAUUSD**: last60_dow|dow0_long|16:00|12b t=3.15 m=0.734 comb=0.548 shock=0.592 chunks+=4/4; core_dow|dow0_long|10:35|24b t=3.08 m=1.307 comb=1.052 shock=1.112 chunks+=4/4; overlap_dow|dow0_long|14:40|48b t=2.96 m=1.561 comb=1.363 shock=1.410 chunks+=4/4; last60_dow|dow0_long|15:55|12b t=2.92 m=0.702 comb=0.520 shock=0.563 chunks+=4/4; last60_dow|dow4_long|16:15|3b t=2.86 m=0.315 comb=0.149 shock=0.189 chunks+=3/4

**EURUSD**: overlap_dow|dow2_long|14:30|24b t=3.03 m=2.379 comb=2.161 shock=2.176 chunks+=4/4; core_dow|dow3_short|11:30|24b t=2.89 m=1.985 comb=1.685 shock=1.693 chunks+=4/4; overlap_dow|dow2_long|14:25|24b t=2.88 m=2.138 comb=1.922 shock=1.937 chunks+=4/4; overlap_dow|dow2_long|14:30|12b t=2.87 m=1.111 comb=0.892 shock=0.905 chunks+=4/4; core_dow|dow3_short|11:30|12b t=2.84 m=1.238 comb=0.947 shock=0.964 chunks+=4/4

## Cross-market consistency (mean net ATR / mean t over the family's cells)

| family | GER40 | GER40_ar1 | NAS100 | SPX500 | XAUUSD | EURUSD | #mkts>0 |
|---|---|---|---|---|---|---|---|
| core_long | +0.006/-0.3 | +0.027/-0.2 | -0.025/-0.2 | -0.052/-0.4 | +0.247/-0.2 | -0.147/-1.5 | 3/6 |
| core_short | -0.191/-0.8 | -0.214/-0.9 | -0.065/-0.4 | -0.197/-1.1 | -0.597/-1.5 | -0.302/-0.8 | 0/6 |
| first30_cont | +0.131/+0.2 | +0.123/+0.2 | +0.071/+0.3 | -0.034/-0.1 | -0.418/-1.0 | -0.330/-1.2 | 3/6 |
| first30_fade | -0.291/-0.9 | -0.286/-0.9 | -0.156/-0.6 | -0.221/-0.8 | +0.089/-0.3 | -0.114/-0.7 | 1/6 |
| first30_long | -0.311/-0.9 | -0.272/-0.9 | +0.070/+0.3 | +0.027/+0.0 | +0.138/-0.2 | -0.319/-1.3 | 3/6 |
| first30_short | +0.112/+0.3 | +0.069/+0.2 | -0.180/-0.6 | -0.342/-0.9 | -0.475/-1.3 | -0.149/-0.5 | 2/6 |
| gap_fade | -0.059/-0.0 | -0.003/+0.0 | +0.138/+0.0 | +0.480/+0.4 | -0.291/-1.6 | -0.081/-0.7 | 2/6 |
| gap_go | -0.185/-0.5 | -0.247/-0.6 | -0.287/-0.3 | -0.879/-1.2 | -0.058/+0.2 | -0.426/-1.0 | 0/6 |
| last60_long | -0.086/-1.2 | -0.104/-1.3 | -0.095/-0.8 | -0.190/-1.5 | -0.034/-0.5 | -0.117/-1.5 | 0/6 |
| last60_short | -0.249/-2.6 | -0.232/-2.5 | -0.019/-0.2 | -0.108/-0.9 | -0.199/-2.1 | -0.208/-1.8 | 0/6 |
| overlap_long | - | - | - | - | +0.152/+0.3 | +0.236/+0.3 | 2/2 |
| overlap_short | - | - | - | - | -0.419/-1.5 | -0.603/-2.0 | 0/2 |
| ovn_cont | +0.213/+0.4 | +0.160/+0.3 | +0.223/+0.4 | -0.179/-0.4 | -0.328/-0.5 | -0.028/-0.1 | 3/6 |
| ovn_long | +1.209/+2.3 | +1.219/+2.3 | +1.625/+2.9 | +1.212/+2.5 | +0.461/+0.8 | -0.338/-0.8 | 5/6 |
| ovn_rev | -0.374/-0.7 | -0.323/-0.6 | -0.330/-0.6 | -0.087/-0.2 | +0.062/+0.1 | -0.326/-0.8 | 1/6 |
| ovn_short | -1.370/-2.6 | -1.382/-2.7 | -1.732/-3.1 | -1.477/-3.0 | -0.727/-1.2 | -0.016/-0.0 | 0/6 |

## Spread-spike vs sweep-like bars (Train bars)

| dataset | spike bars | sweep rate spike/non | obs/exp by slot | z (slot-stratified) |
|---|---|---|---|---|
| GER40 | 3581 | 0.061/0.118 | 0.59 | -8.38 |
| GER40_ar1 | 3552 | 0.059/0.117 | 0.58 | -8.64 |
| NAS100 | 3820 | 0.105/0.127 | 0.85 | -3.56 |
| SPX500 | 3582 | 0.114/0.129 | 0.90 | -2.29 |
| XAUUSD | 816 | 0.098/0.124 | 0.81 | -2.00 |
| EURUSD | 2921 | 0.083/0.117 | 1.25 | 3.83 |

## Caveats

- Train side of the FIRST purged fold split only (~40% of dev days); small n per DOW cell. Fold TEST sides are read only in persistence_gate (once).
- Provisional calendars (NAS100, SPX500, XAUUSD, EURUSD; marked *): entry window/flat minutes and the London/NY overlap are unverified.
- Costs: half recorded spread at entry+exit plus one slippage per fill (calibrated fraction of median spread, not an observed fill); commission 0 in BASE; COMBINED_ADVERSE adds 1 EUR/lot round turn; swap/financing not modelled (overnight family holds through the night).
- Prices are BID OHLC; ATR(14) is the causal M5 ATR at the entry open; stop variant uses a fixed 1.0 ATR stop with a bar-extreme touch rule.
- Day-shift null is exactly invariant for unconditional (all-days) cells, so it is reported for labelled cells only; the centred day bootstrap covers the whole grid. Cells overlap heavily (adjacent slots/horizons, DOW subsets): BH is applied under positive dependence and is a screening tool.
- p-values use a normal approximation to Student-t (no scipy); one-sided upper tail = 'positive net edge'.
- Gate selection is by net t (positive edge); this equals |t| among edge-direction cells because the mirrored direction is its own cell.

GER40 v2 vs ar1 series: cell-t correlation 0.985 over 9722 cells; best cells v2 core_dow|dow3_long|17:00|3b / ar1 core_dow|dow3_long|17:00|3b.
