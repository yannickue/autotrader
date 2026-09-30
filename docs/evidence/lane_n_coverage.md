# Lane N - retrospective coverage analysis (market move -> EXECUTED / REJECTED / NEAR_MISS / OUT_OF_WINDOW / NO_SETUP)

**HINDSIGHT DIAGNOSTICS: moves are found with future bars; never a gate, filter or model input.**

- move definition: pivot -> >= 3.0 ATR favourable excursion within 24 bars, max adverse 1.0 ATR, swing k=6; cover window = [start-3, start+12] bars
- near-miss: one family condition relaxed at a time by <= 0.15 of its scale, the family's own `generate` re-run (no re-implementation); actual = value effectively reached (<= 1.9% of scale resolution), required = frozen threshold, normalized gap = |required - actual| / scale (scale = |required|, ATR-distance parameters floored at 0.25 ATR); gap_abs in the condition's unit
- data: existing dev bars (load_dev_market_frame, holdout guard active: nothing after 2026-08-31); spans GER40 2026-07-02..2026-08-31; NAS100 2026-07-02..2026-08-31; SPX500 2026-07-02..2026-08-31; XAUUSD 2026-07-02..2026-08-31; EURUSD 2026-07-02..2026-08-31
- R2 data reused (read-only, copy): 7 opportunities read via DemoStore.funnel_rows/counterfactual_rows/get_outcome from a copy; funnel summary {'opportunities': 7, 'engine_accepted': 7, 'engine_rejected': 0, 'stack_rejected': 4, 'traded': 3, 'shadow_would_trade': 0, 'temporary_otherwise_valid_blocked': 0, 'trades_that_would_have_existed': {'actual': 3, 'without_temporary_limitations': 3, 'without_temporary_and_legacy_stack_gates': 3, 'engine_side_without_legacy_quality_temporary': 7}, 'engine_top_reasons': {}, 'stack_top_codes': {'margin_stop_too_close_to_liquidation': 3, 'spread_too_wide': 1}}
- sample: 3266 moves, 3558 replayed signals, 922 near-misses; seed 20260930; R2 opportunities in the analysed markets: 7

## Coverage per market

| market | bars | moves | EXECUTED | REJECTED | NEAR_MISS | OUT_OF_WINDOW | NO_SETUP | SIGNAL_NOT_IN_R2 | replay signals | near-misses |
|---|---|---|---|---|---|---|---|---|---|---|
| GER40 | 9955 | 575 | 0 | 0 | 20 | 227 | 168 | 160 | 1057 | 292 |
| NAS100 | 11545 | 672 | 0 | 0 | 4 | 501 | 58 | 109 | 776 | 191 |
| SPX500 | 11545 | 691 | 0 | 0 | 6 | 501 | 121 | 63 | 378 | 118 |
| XAUUSD | 11545 | 642 | 0 | 0 | 8 | 395 | 77 | 162 | 988 | 235 |
| EURUSD | 12092 | 686 | 0 | 0 | 6 | 439 | 173 | 68 | 359 | 86 |
| **all** | 56682 | 3266 | 0 | 0 | 44 | 2063 | 597 | 562 | 3558 | 922 |

EXECUTED / REJECTED need an R2 record around the move start. SIGNAL_NOT_IN_R2 is a data-availability bucket, not an outcome: the frozen generators re-run offline would have signalled, but R2 has no record for the period (bars pre-date the live runner).

## Top reasons for NO_SETUP (closest failing family condition at the move start)

- GER40: NO_TRIGGER_STRUCTURE (no relaxable condition reaches a trigger within 0.75 of its scale) x111; LEADLAG/:leader_move_quantile x24; VOLREV/fade:rel_atr_quantile x21; ROUND/reject:prox_atr x7; ROUND/reject:rej_atr x2
- NAS100: NO_TRIGGER_STRUCTURE (no relaxable condition reaches a trigger within 0.75 of its scale) x30; LEADLAG/:leader_move_quantile x16; EOD/continue:eod_trend_quantile x3; ROUND/reject:rej_atr x3; EOD/reverse:eod_trend_quantile x3
- SPX500: NO_TRIGGER_STRUCTURE (no relaxable condition reaches a trigger within 0.75 of its scale) x60; LEADLAG/:leader_move_quantile x37; VOLREV/fade:rel_atr_quantile x12; EOD/continue:eod_trend_quantile x8; EOD/reverse:eod_trend_quantile x3
- XAUUSD: NO_TRIGGER_STRUCTURE (no relaxable condition reaches a trigger within 0.75 of its scale) x46; VOLREV/fade:rel_atr_quantile x11; ROUND/reject:prox_atr x6; ROUND/reject:rej_atr x4; ROUND/break:break_beyond_level_atr x3
- EURUSD: NO_TRIGGER_STRUCTURE (no relaxable condition reaches a trigger within 0.75 of its scale) x126; VOLREV/fade:rel_atr_quantile x38; EOD/continue:eod_trend_quantile x3; EOD/reverse:eod_trend_quantile x3; ORB/breakout:breakout_buffer_atr x1

## NEAR_MISS moves (first 15 per market)

| market | dir | start | family/mode | failed condition | actual | required | normalized gap | gap (abs, unit) |
|---|---|---|---|---|---|---|---|---|
| GER40 | +1 | 2026-07-08T11:25:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001187 | 0.001258 | 0.056 | 7.075e-05 rel_ATR |
| GER40 | +1 | 2026-07-09T14:25:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.00114 | 0.001258 | 0.094 | 0.0001179 rel_ATR |
| GER40 | +1 | 2026-07-10T14:35:00+00:00 | VOLREV/fade | anchor_ext_k_atr | 1.738 | 2.000 | 0.131 | 0.262 ATR |
| GER40 | -1 | 2026-07-20T11:45:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001234 | 0.001258 | 0.019 | 2.358e-05 rel_ATR |
| GER40 | -1 | 2026-07-20T13:35:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001164 | 0.001258 | 0.075 | 9.434e-05 rel_ATR |
| GER40 | -1 | 2026-07-20T16:15:00+00:00 | ROUND/reject | prox_atr | 0.334 | 0.300 | 0.113 | 0.034 ATR |
| GER40 | -1 | 2026-07-22T09:25:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001116 | 0.001258 | 0.113 | 0.0001415 rel_ATR |
| GER40 | +1 | 2026-07-23T13:30:00+00:00 | LEADLAG/ | leader_move_quantile | 2.133 | 2.306 | 0.075 | 0.173 ATR |
| GER40 | +1 | 2026-07-27T08:05:00+00:00 | VOLREV/fade | anchor_ext_k_atr | 1.738 | 2.000 | 0.131 | 0.262 ATR |
| GER40 | -1 | 2026-07-28T17:00:00+00:00 | LEADLAG/ | leader_move_quantile | 2.133 | 2.306 | 0.075 | 0.173 ATR |
| GER40 | +1 | 2026-07-29T16:20:00+00:00 | ROUND/reject | prox_atr | 0.345 | 0.300 | 0.150 | 0.045 ATR |
| GER40 | +1 | 2026-07-31T15:35:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001069 | 0.001258 | 0.150 | 0.0001887 rel_ATR |
| GER40 | -1 | 2026-08-04T08:00:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001187 | 0.001258 | 0.056 | 7.075e-05 rel_ATR |
| GER40 | +1 | 2026-08-10T14:00:00+00:00 | LEADLAG/ | leader_move_quantile | 2.038 | 2.345 | 0.131 | 0.308 ATR |
| GER40 | -1 | 2026-08-21T17:50:00+00:00 | ROUND/break | break_beyond_level_atr | 0.283 | 0.300 | 0.056 | 0.017 ATR |
| NAS100 | -1 | 2026-07-15T15:15:00+00:00 | VOLREV/expand | compression_rel_atr_quantile | 0.001899 | 0.001737 | 0.094 | 0.0001628 rel_ATR |
| NAS100 | +1 | 2026-07-30T17:40:00+00:00 | EOD/continue | eod_trend_quantile | 8.627 | 8.964 | 0.037 | 0.336 ATR |
| NAS100 | -1 | 2026-08-19T16:20:00+00:00 | ROUND/reject | prox_atr | 0.334 | 0.300 | 0.113 | 0.034 ATR |
| NAS100 | -1 | 2026-08-27T15:20:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001509 | 0.001737 | 0.131 | 0.0002279 rel_ATR |
| SPX500 | -1 | 2026-07-21T17:00:00+00:00 | EOD/reverse | eod_trend_quantile | 7.422 | 8.024 | 0.075 | 0.602 ATR |
| SPX500 | +1 | 2026-07-29T16:20:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001167 | 0.001262 | 0.075 | 9.465e-05 rel_ATR |
| SPX500 | -1 | 2026-08-05T18:10:00+00:00 | EOD/continue | eod_trend_quantile | 7.873 | 8.024 | 0.019 | 0.150 ATR |
| SPX500 | +1 | 2026-08-12T18:00:00+00:00 | EOD/reverse | eod_trend_quantile | 6.820 | 8.024 | 0.150 | 1.204 ATR |
| SPX500 | -1 | 2026-08-19T15:05:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001073 | 0.001262 | 0.150 | 0.0001893 rel_ATR |
| SPX500 | -1 | 2026-08-20T15:05:00+00:00 | ROUND/reject | rej_atr | 0.289 | 0.300 | 0.037 | 0.011 ATR |
| XAUUSD | +1 | 2026-07-06T12:45:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001167 | 0.001373 | 0.150 | 0.0002059 rel_ATR |
| XAUUSD | +1 | 2026-07-16T11:30:00+00:00 | ROUND/reject | prox_atr | 0.334 | 0.300 | 0.113 | 0.034 ATR |
| XAUUSD | -1 | 2026-07-16T12:10:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001347 | 0.001373 | 0.019 | 2.573e-05 rel_ATR |
| XAUUSD | -1 | 2026-07-28T12:50:00+00:00 | EOD/continue | eod_trend_quantile | 3.930 | 4.624 | 0.150 | 0.694 ATR |
| XAUUSD | +1 | 2026-08-03T09:35:00+00:00 | ROUND/reject | prox_atr | 0.345 | 0.300 | 0.150 | 0.045 ATR |
| XAUUSD | -1 | 2026-08-04T11:55:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001347 | 0.001373 | 0.019 | 2.573e-05 rel_ATR |
| XAUUSD | -1 | 2026-08-06T12:40:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001321 | 0.001373 | 0.037 | 5.147e-05 rel_ATR |
| XAUUSD | -1 | 2026-08-14T12:35:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.001167 | 0.001373 | 0.150 | 0.0002059 rel_ATR |
| EURUSD | +1 | 2026-07-08T11:45:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.0004108 | 0.0004442 | 0.075 | 3.331e-05 rel_ATR |
| EURUSD | +1 | 2026-07-29T12:20:00+00:00 | EOD/reverse | eod_trend_quantile | 4.459 | 5.245 | 0.150 | 0.787 ATR |
| EURUSD | +1 | 2026-07-31T08:40:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.0003775 | 0.0004442 | 0.150 | 6.662e-05 rel_ATR |
| EURUSD | -1 | 2026-08-14T12:35:00+00:00 | VOLREV/fade | rel_atr_quantile | 0.0004025 | 0.0004442 | 0.094 | 4.164e-05 rel_ATR |
| EURUSD | +1 | 2026-08-14T13:05:00+00:00 | EOD/continue | eod_trend_quantile | 4.655 | 5.245 | 0.113 | 0.590 ATR |
| EURUSD | -1 | 2026-08-25T13:15:00+00:00 | EOD/reverse | eod_trend_quantile | 4.950 | 5.245 | 0.056 | 0.295 ATR |

## R2 records attached to moves (EXECUTED / REJECTED)

- none (no R2 record overlaps the analysed bars)

## MANDATORY CONTROL: outcome mix after the trigger bar vs random / matched / time-shifted bars

For every trigger (real signal or NEAR_MISS) the entry is the bar close, same direction. STRONG = >= N ATR favourable within M bars before an adverse move of 1.0 ATR; ADVERSE = the adverse move comes first; NOISE = neither within M bars. Controls: `base_all` = exact share over all eligible bars (trigger direction mix); `uniform` / `matched` (same local hour + direction) = seeded random bars, K=20 per trigger; `shifted` = trigger bars moved +-1/2 days. Neighbouring bars are autocorrelated: 95% Wilson intervals are optimistic. n < 30 triggers = no conclusion.

| market | group | n | STRONG [95% CI] | ADVERSE | NOISE | base STRONG / ADV / NOISE | uniform STRONG | matched STRONG | shifted STRONG | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| GER40 | ALL/SIGNAL | 1057 | 0.171 [0.150, 0.195] | 0.734 | 0.095 | 0.198 / 0.684 / 0.118 | 0.202 | 0.203 | 0.190 | strong-move share BELOW the base rate of random bars |
| GER40 | EOD/SIGNAL | 24 | 0.250 [0.120, 0.449] | 0.667 | 0.083 | 0.199 / 0.683 / 0.118 | 0.202 | 0.208 | 0.208 | n=24 < 30: too small for any conclusion |
| GER40 | GAP/SIGNAL | 19 | 0.211 [0.085, 0.433] | 0.737 | 0.053 | 0.198 / 0.684 / 0.119 | 0.197 | 0.208 | 0.257 | n=19 < 30: too small for any conclusion |
| GER40 | LEADLAG/SIGNAL | 63 | 0.159 [0.089, 0.268] | 0.762 | 0.079 | 0.203 / 0.683 / 0.115 | 0.207 | 0.233 | 0.242 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| GER40 | ORB/SIGNAL | 78 | 0.179 [0.110, 0.279] | 0.718 | 0.103 | 0.199 / 0.683 / 0.117 | 0.211 | 0.200 | 0.172 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| GER40 | OVERNIGHT/SIGNAL | 22 | 0.136 [0.047, 0.333] | 0.773 | 0.091 | 0.199 / 0.683 / 0.118 | 0.211 | 0.195 | 0.211 | n=22 < 30: too small for any conclusion |
| GER40 | ROUND/SIGNAL | 586 | 0.166 [0.138, 0.198] | 0.741 | 0.094 | 0.197 / 0.684 / 0.119 | 0.198 | 0.199 | 0.195 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| GER40 | VOLREV/SIGNAL | 265 | 0.177 [0.136, 0.228] | 0.721 | 0.102 | 0.198 / 0.684 / 0.119 | 0.196 | 0.209 | 0.164 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| GER40 | ALL/NEAR | 292 | 0.199 [0.157, 0.248] | 0.719 | 0.082 | 0.199 / 0.683 / 0.118 | 0.196 | 0.207 | 0.195 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| GER40 | EOD/NEAR | 2 | 0.000 [0.000, 0.658] | 1.000 | 0.000 | 0.199 / 0.683 / 0.118 | 0.250 | 0.250 | 0.500 | n=2 < 30: too small for any conclusion |
| GER40 | GAP/NEAR | 3 | 0.000 [0.000, 0.562] | 1.000 | 0.000 | 0.207 / 0.682 / 0.112 | 0.167 | 0.217 | 0.167 | n=3 < 30: too small for any conclusion |
| GER40 | LEADLAG/NEAR | 45 | 0.222 [0.125, 0.363] | 0.689 | 0.089 | 0.201 / 0.683 / 0.116 | 0.210 | 0.199 | 0.167 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| GER40 | ORB/NEAR | 6 | 0.000 [0.000, 0.390] | 1.000 | 0.000 | 0.191 / 0.685 / 0.124 | 0.242 | 0.200 | 0.100 | n=6 < 30: too small for any conclusion |
| GER40 | OVERNIGHT/NEAR | 8 | 0.250 [0.071, 0.591] | 0.750 | 0.000 | 0.199 / 0.683 / 0.118 | 0.212 | 0.175 | 0.125 | n=8 < 30: too small for any conclusion |
| GER40 | ROUND/NEAR | 73 | 0.137 [0.076, 0.234] | 0.726 | 0.137 | 0.192 / 0.685 / 0.123 | 0.197 | 0.199 | 0.214 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| GER40 | VOLREV/NEAR | 155 | 0.232 [0.173, 0.305] | 0.703 | 0.065 | 0.202 / 0.683 / 0.115 | 0.199 | 0.211 | 0.201 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| NAS100 | ALL/SIGNAL | 773 | 0.207 [0.180, 0.237] | 0.673 | 0.120 | 0.171 / 0.667 / 0.163 | 0.172 | 0.185 | 0.160 | strong-move share ABOVE the base rate of random bars (95% CI excludes it; CI optimistic, clustered bars) |
| NAS100 | EOD/SIGNAL | 16 | 0.125 [0.035, 0.360] | 0.812 | 0.062 | 0.171 / 0.666 / 0.162 | 0.172 | 0.191 | 0.188 | n=16 < 30: too small for any conclusion |
| NAS100 | GAP/SIGNAL | 29 | 0.241 [0.122, 0.421] | 0.759 | 0.000 | 0.171 / 0.667 / 0.163 | 0.200 | 0.224 | 0.105 | n=29 < 30: too small for any conclusion |
| NAS100 | LEADLAG/SIGNAL | 25 | 0.320 [0.172, 0.516] | 0.600 | 0.080 | 0.169 / 0.668 / 0.163 | 0.178 | 0.194 | 0.060 | n=25 < 30: too small for any conclusion |
| NAS100 | ORB/SIGNAL | 75 | 0.200 [0.125, 0.304] | 0.733 | 0.067 | 0.173 / 0.665 / 0.162 | 0.167 | 0.191 | 0.150 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| NAS100 | OVERNIGHT/SIGNAL | 32 | 0.312 [0.180, 0.486] | 0.656 | 0.031 | 0.171 / 0.666 / 0.162 | 0.169 | 0.212 | 0.097 | strong-move share ABOVE the base rate of random bars (95% CI excludes it; CI optimistic, clustered bars) |
| NAS100 | ROUND/SIGNAL | 456 | 0.197 [0.163, 0.236] | 0.656 | 0.147 | 0.171 / 0.667 / 0.163 | 0.174 | 0.161 | 0.174 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| NAS100 | VOLREV/SIGNAL | 140 | 0.200 [0.142, 0.274] | 0.679 | 0.121 | 0.170 / 0.668 / 0.163 | 0.170 | 0.196 | 0.147 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| NAS100 | ALL/NEAR | 191 | 0.199 [0.149, 0.261] | 0.618 | 0.183 | 0.169 / 0.668 / 0.163 | 0.167 | 0.179 | 0.134 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| NAS100 | EOD/NEAR | 2 | 0.500 [0.095, 0.905] | 0.500 | 0.000 | 0.171 / 0.666 / 0.162 | 0.225 | 0.225 | 0.250 | n=2 < 30: too small for any conclusion |
| NAS100 | GAP/NEAR | 2 | 0.000 [0.000, 0.658] | 1.000 | 0.000 | 0.171 / 0.666 / 0.162 | 0.225 | 0.250 | 0.000 | n=2 < 30: too small for any conclusion |
| NAS100 | LEADLAG/NEAR | 18 | 0.167 [0.058, 0.392] | 0.500 | 0.333 | 0.173 / 0.665 / 0.162 | 0.172 | 0.172 | 0.163 | n=18 < 30: too small for any conclusion |
| NAS100 | ORB/NEAR | 2 | 0.000 [0.000, 0.658] | 1.000 | 0.000 | 0.171 / 0.666 / 0.162 | 0.150 | 0.275 | 0.000 | n=2 < 30: too small for any conclusion |
| NAS100 | OVERNIGHT/NEAR | 4 | 0.000 [0.000, 0.490] | 1.000 | 0.000 | 0.171 / 0.666 / 0.162 | 0.212 | 0.125 | 0.125 | n=4 < 30: too small for any conclusion |
| NAS100 | ROUND/NEAR | 63 | 0.159 [0.089, 0.268] | 0.619 | 0.222 | 0.168 / 0.668 / 0.163 | 0.160 | 0.167 | 0.142 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| NAS100 | VOLREV/NEAR | 100 | 0.240 [0.167, 0.332] | 0.610 | 0.150 | 0.168 / 0.669 / 0.163 | 0.167 | 0.166 | 0.127 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| SPX500 | ALL/SIGNAL | 378 | 0.220 [0.181, 0.264] | 0.693 | 0.087 | 0.196 / 0.674 / 0.130 | 0.203 | 0.203 | 0.183 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| SPX500 | EOD/SIGNAL | 14 | 0.286 [0.117, 0.546] | 0.643 | 0.071 | 0.197 / 0.674 / 0.129 | 0.236 | 0.254 | 0.179 | n=14 < 30: too small for any conclusion |
| SPX500 | GAP/SIGNAL | 24 | 0.208 [0.092, 0.405] | 0.792 | 0.000 | 0.194 / 0.674 / 0.132 | 0.196 | 0.260 | 0.111 | n=24 < 30: too small for any conclusion |
| SPX500 | LEADLAG/SIGNAL | 29 | 0.103 [0.036, 0.264] | 0.828 | 0.069 | 0.203 / 0.674 / 0.123 | 0.191 | 0.247 | 0.111 | n=29 < 30: too small for any conclusion |
| SPX500 | ORB/SIGNAL | 75 | 0.227 [0.147, 0.333] | 0.747 | 0.027 | 0.197 / 0.674 / 0.129 | 0.179 | 0.222 | 0.149 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| SPX500 | OVERNIGHT/SIGNAL | 22 | 0.364 [0.197, 0.570] | 0.636 | 0.000 | 0.197 / 0.674 / 0.129 | 0.223 | 0.250 | 0.167 | n=22 < 30: too small for any conclusion |
| SPX500 | ROUND/SIGNAL | 75 | 0.200 [0.125, 0.304] | 0.600 | 0.200 | 0.190 / 0.674 / 0.136 | 0.189 | 0.181 | 0.176 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| SPX500 | VOLREV/SIGNAL | 139 | 0.223 [0.162, 0.299] | 0.683 | 0.094 | 0.196 / 0.674 / 0.130 | 0.192 | 0.204 | 0.224 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| SPX500 | ALL/NEAR | 118 | 0.161 [0.106, 0.238] | 0.729 | 0.110 | 0.194 / 0.674 / 0.132 | 0.188 | 0.205 | 0.218 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| SPX500 | EOD/NEAR | 10 | 0.100 [0.018, 0.404] | 0.800 | 0.100 | 0.197 / 0.674 / 0.129 | 0.215 | 0.185 | 0.400 | n=10 < 30: too small for any conclusion |
| SPX500 | GAP/NEAR | 3 | 0.000 [0.000, 0.562] | 1.000 | 0.000 | 0.203 / 0.674 / 0.123 | 0.217 | 0.267 | 0.167 | n=3 < 30: too small for any conclusion |
| SPX500 | LEADLAG/NEAR | 17 | 0.294 [0.133, 0.531] | 0.588 | 0.118 | 0.198 / 0.674 / 0.128 | 0.176 | 0.232 | 0.297 | n=17 < 30: too small for any conclusion |
| SPX500 | ORB/NEAR | 1 | 0.000 [0.000, 0.793] | 1.000 | 0.000 | 0.179 / 0.674 / 0.146 | 0.150 | 0.300 | 0.000 | n=1 < 30: too small for any conclusion |
| SPX500 | OVERNIGHT/NEAR | 12 | 0.167 [0.047, 0.448] | 0.833 | 0.000 | 0.197 / 0.674 / 0.129 | 0.179 | 0.258 | 0.125 | n=12 < 30: too small for any conclusion |
| SPX500 | ROUND/NEAR | 6 | 0.333 [0.097, 0.700] | 0.500 | 0.167 | 0.203 / 0.674 / 0.123 | 0.183 | 0.192 | 0.267 | n=6 < 30: too small for any conclusion |
| SPX500 | VOLREV/NEAR | 69 | 0.130 [0.070, 0.230] | 0.739 | 0.130 | 0.191 / 0.674 / 0.135 | 0.184 | 0.191 | 0.195 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| XAUUSD | ALL/SIGNAL | 988 | 0.205 [0.181, 0.232] | 0.737 | 0.058 | 0.215 / 0.722 / 0.063 | 0.215 | 0.223 | 0.212 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| XAUUSD | EOD/SIGNAL | 26 | 0.115 [0.040, 0.290] | 0.846 | 0.038 | 0.215 / 0.722 / 0.063 | 0.185 | 0.244 | 0.231 | n=26 < 30: too small for any conclusion |
| XAUUSD | GAP/SIGNAL | 18 | 0.222 [0.090, 0.452] | 0.722 | 0.056 | 0.218 / 0.719 / 0.063 | 0.217 | 0.228 | 0.147 | n=18 < 30: too small for any conclusion |
| XAUUSD | ORB/SIGNAL | 82 | 0.159 [0.095, 0.253] | 0.756 | 0.085 | 0.214 / 0.722 / 0.063 | 0.221 | 0.203 | 0.188 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| XAUUSD | OVERNIGHT/SIGNAL | 18 | 0.222 [0.090, 0.452] | 0.778 | 0.000 | 0.215 / 0.722 / 0.063 | 0.194 | 0.200 | 0.188 | n=18 < 30: too small for any conclusion |
| XAUUSD | ROUND/SIGNAL | 658 | 0.210 [0.180, 0.242] | 0.736 | 0.055 | 0.215 / 0.722 / 0.063 | 0.219 | 0.216 | 0.214 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| XAUUSD | VOLREV/SIGNAL | 186 | 0.220 [0.167, 0.285] | 0.715 | 0.065 | 0.215 / 0.722 / 0.063 | 0.215 | 0.232 | 0.216 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| XAUUSD | ALL/NEAR | 235 | 0.174 [0.131, 0.228] | 0.762 | 0.064 | 0.215 / 0.722 / 0.063 | 0.214 | 0.216 | 0.194 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| XAUUSD | EOD/NEAR | 10 | 0.100 [0.018, 0.404] | 0.800 | 0.100 | 0.215 / 0.722 / 0.063 | 0.165 | 0.245 | 0.050 | n=10 < 30: too small for any conclusion |
| XAUUSD | GAP/NEAR | 1 | 0.000 [0.000, 0.793] | 1.000 | 0.000 | 0.209 / 0.728 / 0.063 | 0.250 | 0.250 | 0.000 | n=1 < 30: too small for any conclusion |
| XAUUSD | ORB/NEAR | 7 | 0.286 [0.082, 0.641] | 0.571 | 0.143 | 0.214 / 0.723 / 0.063 | 0.229 | 0.200 | 0.133 | n=7 < 30: too small for any conclusion |
| XAUUSD | OVERNIGHT/NEAR | 10 | 0.200 [0.057, 0.510] | 0.700 | 0.100 | 0.215 / 0.722 / 0.063 | 0.190 | 0.215 | 0.150 | n=10 < 30: too small for any conclusion |
| XAUUSD | ROUND/NEAR | 72 | 0.167 [0.098, 0.269] | 0.778 | 0.056 | 0.215 / 0.722 / 0.063 | 0.205 | 0.210 | 0.211 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| XAUUSD | VOLREV/NEAR | 135 | 0.178 [0.122, 0.251] | 0.763 | 0.059 | 0.215 / 0.722 / 0.063 | 0.216 | 0.224 | 0.198 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| EURUSD | ALL/SIGNAL | 359 | 0.203 [0.165, 0.248] | 0.724 | 0.072 | 0.204 / 0.726 / 0.070 | 0.198 | 0.208 | 0.210 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| EURUSD | EOD/SIGNAL | 20 | 0.250 [0.112, 0.469] | 0.650 | 0.100 | 0.204 / 0.725 / 0.071 | 0.200 | 0.195 | 0.237 | n=20 < 30: too small for any conclusion |
| EURUSD | GAP/SIGNAL | 19 | 0.263 [0.118, 0.488] | 0.737 | 0.000 | 0.203 / 0.727 / 0.070 | 0.195 | 0.216 | 0.171 | n=19 < 30: too small for any conclusion |
| EURUSD | ORB/SIGNAL | 80 | 0.138 [0.079, 0.230] | 0.775 | 0.087 | 0.202 / 0.730 / 0.068 | 0.192 | 0.166 | 0.216 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| EURUSD | OVERNIGHT/SIGNAL | 16 | 0.250 [0.102, 0.495] | 0.750 | 0.000 | 0.204 / 0.725 / 0.071 | 0.216 | 0.200 | 0.188 | n=16 < 30: too small for any conclusion |
| EURUSD | ROUND/SIGNAL | 63 | 0.302 [0.202, 0.424] | 0.571 | 0.127 | 0.207 / 0.719 / 0.075 | 0.210 | 0.195 | 0.193 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| EURUSD | VOLREV/SIGNAL | 161 | 0.180 [0.128, 0.247] | 0.764 | 0.056 | 0.203 / 0.727 / 0.070 | 0.193 | 0.204 | 0.218 | indistinguishable from random bars (base rate inside the trigger 95% CI) |
| EURUSD | ALL/NEAR | 86 | 0.116 [0.064, 0.201] | 0.814 | 0.070 | 0.203 / 0.728 / 0.069 | 0.205 | 0.219 | 0.264 | strong-move share BELOW the base rate of random bars |
| EURUSD | EOD/NEAR | 6 | 0.500 [0.188, 0.812] | 0.500 | 0.000 | 0.204 / 0.725 / 0.071 | 0.225 | 0.242 | 0.292 | n=6 < 30: too small for any conclusion |
| EURUSD | GAP/NEAR | 4 | 0.000 [0.000, 0.490] | 0.750 | 0.250 | 0.195 / 0.746 / 0.058 | 0.212 | 0.163 | 0.308 | n=4 < 30: too small for any conclusion |
| EURUSD | ORB/NEAR | 9 | 0.111 [0.020, 0.435] | 0.889 | 0.000 | 0.202 / 0.730 / 0.068 | 0.239 | 0.172 | 0.206 | n=9 < 30: too small for any conclusion |
| EURUSD | OVERNIGHT/NEAR | 6 | 0.167 [0.030, 0.564] | 0.667 | 0.167 | 0.204 / 0.725 / 0.071 | 0.158 | 0.208 | 0.250 | n=6 < 30: too small for any conclusion |
| EURUSD | ROUND/NEAR | 9 | 0.000 [0.000, 0.299] | 0.889 | 0.111 | 0.202 / 0.730 / 0.068 | 0.161 | 0.178 | 0.265 | n=9 < 30: too small for any conclusion |
| EURUSD | VOLREV/NEAR | 52 | 0.096 [0.042, 0.206] | 0.846 | 0.058 | 0.204 / 0.726 / 0.070 | 0.194 | 0.198 | 0.269 | indistinguishable from random bars (base rate inside the trigger 95% CI) |

Base rate of STRONG moves on all eligible bars (per direction): GER40 long 0.175 / short 0.222; NAS100 long 0.152 / short 0.190; SPX500 long 0.179 / short 0.215; XAUUSD long 0.209 / short 0.221; EURUSD long 0.187 / short 0.222

