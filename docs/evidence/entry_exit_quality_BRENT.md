# Lane X - BRENT

`eeq-1.0` hindsight diagnostics, no edge claim.

## BRENT

Data: 2025-06-01..2026-08-31 (82487 M5 bars; evaluated from 2025-07-01); 4 frozen family specs (STRUCT:breakout, STRUCT:confirmed, STRUCT:fade, STRUCT:retest); analysed entries n=2936.

Exclusions / counters: `{"analysed": 2936, "candidates": 2975, "policy_entry_runway_too_short": 2, "policy_flatten_window_active": 37}`

Entry quality (potential; R against the initial risk; MFE/MAE to the stop or the flat deadline):

| family:variant | dir | n | clusters | flag | MFE mean/med | MAE mean/med | P(MFE>=.25/.5/.75/1/1.5/2) | MFE-first / MAE-first | giveback | capture (floored) | useful / failure share | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | ALL | 961 | 961 |  | 1.18/0.56 | 0.91/1.02 | 0.66/0.53/0.42/0.35/0.25/0.19 | 0.38 / 0.58 | 1.29 | 0.27 | 0.53 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | long | 477 | 477 |  | 1.15/0.61 | 0.91/1.02 | 0.69/0.55/0.43/0.35/0.25/0.19 | 0.41 / 0.54 | 1.25 | 0.27 | 0.55 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | short | 484 | 484 |  | 1.20/0.52 | 0.91/1.02 | 0.63/0.51/0.42/0.36/0.26/0.20 | 0.36 / 0.61 | 1.34 | 0.26 | 0.51 / 0.31 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | ALL | 757 | 757 |  | 1.18/0.58 | 0.87/1.01 | 0.68/0.53/0.44/0.36/0.27/0.19 | 0.39 / 0.55 | 1.23 | 0.28 | 0.53 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | long | 380 | 380 |  | 1.20/0.62 | 0.86/1.00 | 0.71/0.55/0.45/0.37/0.28/0.19 | 0.42 / 0.51 | 1.19 | 0.30 | 0.55 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | short | 377 | 377 |  | 1.16/0.51 | 0.88/1.01 | 0.65/0.51/0.42/0.35/0.25/0.19 | 0.35 / 0.60 | 1.27 | 0.25 | 0.51 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | ALL | 592 | 592 |  | 1.59/0.55 | 1.18/1.14 | 0.62/0.52/0.44/0.37/0.28/0.22 | 0.28 / 0.72 | 1.83 | 0.22 | 0.52 / 0.38 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | long | 311 | 311 |  | 1.60/0.65 | 1.18/1.13 | 0.65/0.54/0.45/0.38/0.28/0.22 | 0.28 / 0.72 | 1.83 | 0.21 | 0.54 / 0.35 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | short | 281 | 281 |  | 1.57/0.51 | 1.18/1.15 | 0.59/0.50/0.43/0.37/0.29/0.22 | 0.27 / 0.73 | 1.82 | 0.23 | 0.50 / 0.41 | BOTH |
| STRUCT:retest | ALL | 626 | 626 |  | 1.19/0.58 | 0.90/1.02 | 0.67/0.53/0.45/0.37/0.27/0.19 | 0.38 / 0.56 | 1.26 | 0.27 | 0.53 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | long | 312 | 312 |  | 1.21/0.65 | 0.87/1.01 | 0.70/0.55/0.47/0.38/0.29/0.20 | 0.44 / 0.48 | 1.22 | 0.30 | 0.55 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | short | 314 | 314 |  | 1.17/0.50 | 0.92/1.02 | 0.64/0.50/0.42/0.36/0.25/0.18 | 0.32 / 0.63 | 1.30 | 0.25 | 0.50 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |

Exit-policy shadow comparison on the SAME entries (mean R, spread-adjusted gross; in brackets: entries the policy applies to; NA = no structural level):

| family:variant | dir | n | P1 FIXED_1_5R | P2 STRUCT_TP1 | P3 STRUCT_TP1_TP2 | P4 TP1_TP2_RUNNER | P5 STRUCT_TRAIL | P6 BREAKEVEN_LOCK | P7 MOMENTUM_EXIT | P8 TIME_ALPHA | P9 EOD_FORCED_FLAT | paired-structural n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | ALL | 961 | -0.12(961) | -0.11(564) | -0.10(401) | -0.12(564) | -0.10(961) | -0.12(961) | -0.12(961) | -0.10(961) | -0.11(961) | 401 |
| STRUCT:breakout | long | 477 | -0.09(477) | -0.09(257) | -0.07(176) | -0.09(257) | -0.07(477) | -0.11(477) | -0.07(477) | -0.07(477) | -0.07(477) | 176 |
| STRUCT:breakout | short | 484 | -0.14(484) | -0.12(307) | -0.12(225) | -0.14(307) | -0.13(484) | -0.14(484) | -0.16(484) | -0.12(484) | -0.14(484) | 225 |
| STRUCT:confirmed | ALL | 757 | -0.05(757) | -0.05(420) | -0.06(284) | -0.07(420) | -0.06(757) | -0.06(757) | -0.08(757) | -0.03(757) | -0.04(757) | 284 |
| STRUCT:confirmed | long | 380 | +0.01(380) | +0.00(185) | +0.03(121) | -0.02(185) | -0.01(380) | -0.01(380) | -0.03(380) | +0.05(380) | +0.03(380) | 121 |
| STRUCT:confirmed | short | 377 | -0.12(377) | -0.10(235) | -0.14(163) | -0.11(235) | -0.11(377) | -0.12(377) | -0.13(377) | -0.11(377) | -0.12(377) | 163 |
| STRUCT:fade | ALL | 592 | -0.24(592) | -0.19(566) | -0.22(533) | -0.17(566) | -0.18(592) | -0.23(592) | -0.20(592) | -0.24(592) | -0.15(592) | 533 |
| STRUCT:fade | long | 311 | -0.23(311) | -0.16(293) | -0.17(273) | -0.14(293) | -0.15(311) | -0.22(311) | -0.18(311) | -0.23(311) | -0.07(311) | 273 |
| STRUCT:fade | short | 281 | -0.25(281) | -0.22(273) | -0.27(260) | -0.20(273) | -0.21(281) | -0.24(281) | -0.23(281) | -0.25(281) | -0.25(281) | 260 |
| STRUCT:retest | ALL | 626 | -0.07(626) | -0.07(395) | -0.09(261) | -0.07(395) | -0.09(626) | -0.07(626) | -0.09(626) | -0.05(626) | -0.07(626) | 261 |
| STRUCT:retest | long | 312 | -0.01(312) | -0.00(181) | -0.01(114) | -0.01(181) | -0.00(312) | -0.01(312) | -0.03(312) | +0.04(312) | +0.01(312) | 114 |
| STRUCT:retest | short | 314 | -0.13(314) | -0.13(214) | -0.16(147) | -0.13(214) | -0.17(314) | -0.14(314) | -0.15(314) | -0.13(314) | -0.16(314) | 147 |

Timing / structure / cost state (medians; times dated by bar open): 

| family:variant | dir | n | t_MFE s | t_MAE s | t to 0.5R s | t to 1R s | bars above entry | max structure reached R | TP1 / TP2 reach | holding s | spread/1R | P1 exits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | ALL | 961 | 5700 | 4500 | 2700 | 6000 | 0.44 | 0.14 | 0.58 / 0.46 | 11700 | 0.088 | STOP 0.51, TP1 0.25, FORCED_FLAT 0.22 |
| STRUCT:confirmed | ALL | 757 | 6600 | 4800 | 3000 | 6900 | 0.46 | 0.14 | 0.64 / 0.49 | 13800 | 0.082 | STOP 0.47, TP1 0.27, FORCED_FLAT 0.25 |
| STRUCT:fade | ALL | 592 | 2400 | 1800 | 900 | 1800 | 0.46 | 0.29 | 0.46 / 0.33 | 3300 | 0.191 | STOP 0.64, TP1 0.28, FORCED_FLAT 0.06 |
| STRUCT:retest | ALL | 626 | 6600 | 4500 | 3000 | 6300 | 0.46 | 0.18 | 0.61 / 0.48 | 12450 | 0.088 | STOP 0.48, TP1 0.27, FORCED_FLAT 0.23 |

Profit capture per policy (mean floored capture ratio = share of the MFE kept; same entries):

| family:variant | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9 |
|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | 0.27 | 0.26 | 0.25 | 0.14 | 0.15 | 0.24 | 0.13 | 0.24 | 0.23 |
| STRUCT:confirmed | 0.28 | 0.28 | 0.23 | 0.14 | 0.15 | 0.25 | 0.13 | 0.24 | 0.24 |
| STRUCT:fade | 0.22 | 0.28 | 0.19 | 0.19 | 0.14 | 0.20 | 0.16 | 0.21 | 0.16 |
| STRUCT:retest | 0.27 | 0.28 | 0.22 | 0.13 | 0.15 | 0.25 | 0.12 | 0.24 | 0.23 |

Session / regime cells with n >= 30:

| family:variant | cell | n | clusters | MFE med | MAE med | useful / failure | capture | baseline R | verdict |
|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | session=AFTERNOON_12_18 | 340 | 340 | 0.63 | 1.00 | 0.57 / 0.27 | 0.28 | -0.10 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | session=EVENING_18_24 | 124 | 124 | 0.27 | 0.46 | 0.33 / 0.13 | 0.36 | 0.06 | NO CLEAR DEFICIT |
| STRUCT:breakout | session=MORNING_06_12 | 426 | 426 | 0.68 | 1.06 | 0.57 / 0.33 | 0.24 | -0.16 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | session=NIGHT_00_06 | 71 | 71 | 0.50 | 1.08 | 0.49 / 0.35 | 0.21 | -0.25 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | regime=HIGH_VOL | 280 | 280 | 0.72 | 0.82 | 0.62 / 0.19 | 0.31 | 0.06 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | regime=LOW_VOL | 278 | 278 | 0.35 | 1.06 | 0.44 / 0.40 | 0.22 | -0.27 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | regime=MID_VOL | 403 | 403 | 0.52 | 1.01 | 0.53 / 0.27 | 0.26 | -0.13 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | session=AFTERNOON_12_18 | 272 | 272 | 0.58 | 0.89 | 0.53 / 0.29 | 0.30 | -0.07 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | session=EVENING_18_24 | 104 | 104 | 0.29 | 0.43 | 0.35 / 0.10 | 0.33 | 0.03 | NO CLEAR DEFICIT |
| STRUCT:confirmed | session=MORNING_06_12 | 329 | 329 | 0.78 | 1.04 | 0.58 / 0.28 | 0.25 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | session=NIGHT_00_06 | 52 | 52 | 0.61 | 1.09 | 0.58 / 0.31 | 0.23 | -0.15 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | regime=HIGH_VOL | 230 | 230 | 0.66 | 0.77 | 0.59 / 0.19 | 0.30 | 0.08 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | regime=LOW_VOL | 201 | 201 | 0.43 | 1.05 | 0.47 / 0.32 | 0.24 | -0.20 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | regime=MID_VOL | 326 | 326 | 0.57 | 1.01 | 0.53 / 0.28 | 0.28 | -0.05 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | session=AFTERNOON_12_18 | 194 | 194 | 0.73 | 1.14 | 0.56 / 0.32 | 0.24 | -0.14 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | session=EVENING_18_24 | 76 | 76 | 0.38 | 1.05 | 0.42 / 0.38 | 0.20 | -0.45 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | session=MORNING_06_12 | 278 | 278 | 0.65 | 1.17 | 0.54 / 0.38 | 0.21 | -0.20 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | session=NIGHT_00_06 | 44 | 44 | 0.19 | 1.15 | 0.36 / 0.55 | 0.18 | -0.55 | BOTH |
| STRUCT:fade | regime=HIGH_VOL | 175 | 175 | 0.59 | 1.16 | 0.55 / 0.29 | 0.22 | -0.21 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | regime=LOW_VOL | 151 | 151 | 0.17 | 1.17 | 0.38 / 0.55 | 0.21 | -0.48 | BOTH |
| STRUCT:fade | regime=MID_VOL | 266 | 266 | 0.75 | 1.09 | 0.58 / 0.33 | 0.22 | -0.12 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | session=AFTERNOON_12_18 | 223 | 223 | 0.58 | 0.92 | 0.52 / 0.30 | 0.31 | -0.06 | NO CLEAR DEFICIT |
| STRUCT:retest | session=EVENING_18_24 | 84 | 84 | 0.25 | 0.44 | 0.32 / 0.13 | 0.33 | 0.01 | NO CLEAR DEFICIT |
| STRUCT:retest | session=MORNING_06_12 | 278 | 278 | 0.77 | 1.05 | 0.58 / 0.29 | 0.23 | -0.08 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | session=NIGHT_00_06 | 41 | 41 | 0.64 | 1.10 | 0.61 / 0.27 | 0.22 | -0.22 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | regime=HIGH_VOL | 189 | 189 | 0.65 | 0.76 | 0.58 / 0.19 | 0.28 | 0.10 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | regime=LOW_VOL | 163 | 163 | 0.47 | 1.05 | 0.48 / 0.31 | 0.24 | -0.20 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | regime=MID_VOL | 274 | 274 | 0.54 | 1.02 | 0.51 / 0.30 | 0.28 | -0.11 | USEFUL ENTRIES + BAD CAPTURE |
