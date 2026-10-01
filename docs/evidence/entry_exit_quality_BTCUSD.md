# Lane X - BTCUSD

`eeq-1.0` hindsight diagnostics, no edge claim.

## BTCUSD

Data: 2025-09-24..2026-08-31 (75015 M5 bars; evaluated from 2025-10-24); 4 frozen family specs (STRUCT:breakout, STRUCT:confirmed, STRUCT:fade, STRUCT:retest); analysed entries n=1795.

Exclusions / counters: `{"analysed": 1795, "candidates": 2363, "policy_entry_runway_too_short": 8, "policy_flatten_window_active": 105, "policy_non_operating_day": 455}`

Entry quality (potential; R against the initial risk; MFE/MAE to the stop or the flat deadline):

| family:variant | dir | n | clusters | flag | MFE mean/med | MAE mean/med | P(MFE>=.25/.5/.75/1/1.5/2) | MFE-first / MAE-first | giveback | capture (floored) | useful / failure share | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | ALL | 580 | 580 |  | 1.34/0.49 | 0.95/1.04 | 0.65/0.49/0.41/0.34/0.26/0.19 | 0.33 / 0.64 | 1.52 | 0.24 | 0.49 / 0.31 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | long | 282 | 282 |  | 1.36/0.53 | 0.93/1.03 | 0.66/0.51/0.42/0.35/0.28/0.19 | 0.38 / 0.59 | 1.48 | 0.27 | 0.51 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | short | 298 | 298 |  | 1.32/0.47 | 0.97/1.04 | 0.63/0.48/0.41/0.34/0.24/0.19 | 0.29 / 0.68 | 1.56 | 0.22 | 0.48 / 0.33 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | ALL | 450 | 450 |  | 1.35/0.50 | 0.90/1.03 | 0.66/0.51/0.43/0.37/0.28/0.21 | 0.34 / 0.61 | 1.47 | 0.25 | 0.51 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | long | 228 | 228 |  | 1.34/0.50 | 0.90/1.04 | 0.68/0.51/0.43/0.35/0.29/0.20 | 0.36 / 0.60 | 1.41 | 0.28 | 0.51 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | short | 222 | 222 |  | 1.36/0.50 | 0.91/1.02 | 0.64/0.50/0.44/0.39/0.26/0.22 | 0.32 / 0.63 | 1.52 | 0.22 | 0.50 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | ALL | 392 | 392 |  | 1.83/0.63 | 1.15/1.14 | 0.62/0.52/0.47/0.41/0.32/0.25 | 0.25 / 0.74 | 1.94 | 0.26 | 0.52 / 0.37 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | long | 216 | 216 |  | 1.67/0.74 | 1.14/1.14 | 0.66/0.55/0.50/0.43/0.32/0.25 | 0.29 / 0.70 | 1.77 | 0.26 | 0.55 / 0.33 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | short | 176 | 176 |  | 2.03/0.44 | 1.16/1.13 | 0.58/0.48/0.43/0.38/0.31/0.25 | 0.21 / 0.78 | 2.15 | 0.26 | 0.48 / 0.41 | BOTH |
| STRUCT:retest | ALL | 373 | 373 |  | 1.49/0.51 | 0.92/1.04 | 0.65/0.51/0.43/0.37/0.29/0.24 | 0.36 / 0.61 | 1.60 | 0.24 | 0.51 / 0.31 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | long | 194 | 194 |  | 1.41/0.46 | 0.90/1.04 | 0.65/0.49/0.41/0.33/0.29/0.21 | 0.35 / 0.61 | 1.51 | 0.27 | 0.49 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | short | 179 | 179 |  | 1.57/0.57 | 0.94/1.03 | 0.65/0.53/0.45/0.41/0.30/0.26 | 0.36 / 0.61 | 1.70 | 0.22 | 0.53 / 0.32 | USEFUL ENTRIES + BAD CAPTURE |

Exit-policy shadow comparison on the SAME entries (mean R, spread-adjusted gross; in brackets: entries the policy applies to; NA = no structural level):

| family:variant | dir | n | P1 FIXED_1_5R | P2 STRUCT_TP1 | P3 STRUCT_TP1_TP2 | P4 TP1_TP2_RUNNER | P5 STRUCT_TRAIL | P6 BREAKEVEN_LOCK | P7 MOMENTUM_EXIT | P8 TIME_ALPHA | P9 EOD_FORCED_FLAT | paired-structural n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | ALL | 580 | -0.18(580) | -0.16(357) | -0.21(260) | -0.12(357) | -0.11(580) | -0.17(580) | -0.12(580) | -0.18(580) | -0.09(580) | 260 |
| STRUCT:breakout | long | 282 | -0.13(282) | -0.13(170) | -0.17(129) | -0.05(170) | -0.06(282) | -0.12(282) | -0.08(282) | -0.10(282) | -0.03(282) | 129 |
| STRUCT:breakout | short | 298 | -0.23(298) | -0.19(187) | -0.25(131) | -0.18(187) | -0.16(298) | -0.21(298) | -0.17(298) | -0.25(298) | -0.14(298) | 131 |
| STRUCT:confirmed | ALL | 450 | -0.12(450) | -0.18(291) | -0.22(196) | -0.10(291) | -0.07(450) | -0.13(450) | -0.09(450) | -0.10(450) | -0.04(450) | 196 |
| STRUCT:confirmed | long | 228 | -0.08(228) | -0.19(140) | -0.23(102) | -0.07(140) | -0.03(228) | -0.11(228) | -0.06(228) | -0.06(228) | -0.01(228) | 102 |
| STRUCT:confirmed | short | 222 | -0.16(222) | -0.17(151) | -0.21(94) | -0.13(151) | -0.10(222) | -0.15(222) | -0.13(222) | -0.15(222) | -0.07(222) | 94 |
| STRUCT:fade | ALL | 392 | -0.11(392) | -0.13(380) | -0.15(358) | -0.14(380) | -0.04(392) | -0.11(392) | -0.14(392) | -0.12(392) | -0.07(392) | 358 |
| STRUCT:fade | long | 216 | -0.09(216) | -0.11(208) | -0.13(192) | -0.09(208) | -0.05(216) | -0.07(216) | -0.10(216) | -0.10(216) | -0.16(216) | 192 |
| STRUCT:fade | short | 176 | -0.12(176) | -0.16(172) | -0.16(166) | -0.20(172) | -0.02(176) | -0.15(176) | -0.19(176) | -0.15(176) | +0.04(176) | 166 |
| STRUCT:retest | ALL | 373 | -0.11(373) | -0.22(259) | -0.22(172) | -0.10(259) | -0.04(373) | -0.13(373) | -0.09(373) | -0.10(373) | +0.03(373) | 172 |
| STRUCT:retest | long | 194 | -0.10(194) | -0.24(132) | -0.24(89) | -0.08(132) | -0.01(194) | -0.13(194) | -0.06(194) | -0.06(194) | +0.05(194) | 89 |
| STRUCT:retest | short | 179 | -0.13(179) | -0.19(127) | -0.21(83) | -0.13(127) | -0.08(179) | -0.13(179) | -0.12(179) | -0.13(179) | +0.01(179) | 83 |

Timing / structure / cost state (medians; times dated by bar open): 

| family:variant | dir | n | t_MFE s | t_MAE s | t to 0.5R s | t to 1R s | bars above entry | max structure reached R | TP1 / TP2 reach | holding s | spread/1R | P1 exits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | ALL | 580 | 4500 | 5100 | 2850 | 5400 | 0.41 | 0.12 | 0.52 / 0.38 | 9600 | 0.101 | STOP 0.57, TP1 0.26, FORCED_FLAT 0.17 |
| STRUCT:confirmed | ALL | 450 | 4650 | 5400 | 3000 | 5700 | 0.44 | 0.13 | 0.53 / 0.39 | 13500 | 0.092 | STOP 0.54, TP1 0.28, FORCED_FLAT 0.19 |
| STRUCT:fade | ALL | 392 | 3000 | 1500 | 900 | 2100 | 0.48 | 0.27 | 0.45 / 0.34 | 3900 | 0.208 | STOP 0.60, TP1 0.32, FORCED_FLAT 0.08 |
| STRUCT:retest | ALL | 373 | 4950 | 5100 | 2850 | 5400 | 0.44 | 0.16 | 0.49 / 0.38 | 12300 | 0.099 | STOP 0.54, TP1 0.29, FORCED_FLAT 0.16 |

Profit capture per policy (mean floored capture ratio = share of the MFE kept; same entries):

| family:variant | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9 |
|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | 0.24 | 0.25 | 0.20 | 0.14 | 0.14 | 0.23 | 0.13 | 0.20 | 0.21 |
| STRUCT:confirmed | 0.25 | 0.22 | 0.18 | 0.13 | 0.14 | 0.22 | 0.12 | 0.20 | 0.21 |
| STRUCT:fade | 0.26 | 0.26 | 0.21 | 0.18 | 0.18 | 0.24 | 0.18 | 0.25 | 0.19 |
| STRUCT:retest | 0.24 | 0.22 | 0.18 | 0.12 | 0.14 | 0.21 | 0.12 | 0.21 | 0.21 |

Session / regime cells with n >= 30:

| family:variant | cell | n | clusters | MFE med | MAE med | useful / failure | capture | baseline R | verdict |
|---|---|---|---|---|---|---|---|---|---|
| STRUCT:breakout | session=AFTERNOON_12_18 | 220 | 220 | 0.61 | 1.03 | 0.53 / 0.30 | 0.25 | -0.15 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | session=EVENING_18_24 | 72 | 72 | 0.17 | 0.65 | 0.24 / 0.26 | 0.34 | -0.24 | NO CLEAR DEFICIT |
| STRUCT:breakout | session=MORNING_06_12 | 141 | 141 | 0.71 | 1.06 | 0.56 / 0.29 | 0.26 | -0.03 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | session=NIGHT_00_06 | 147 | 147 | 0.48 | 1.09 | 0.50 / 0.35 | 0.17 | -0.35 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | regime=HIGH_VOL | 445 | 445 | 0.49 | 1.03 | 0.49 / 0.30 | 0.27 | -0.15 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:breakout | regime=MID_VOL | 128 | 128 | 0.57 | 1.08 | 0.51 / 0.33 | 0.17 | -0.28 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | session=AFTERNOON_12_18 | 183 | 183 | 0.66 | 1.00 | 0.56 / 0.31 | 0.29 | -0.03 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | session=EVENING_18_24 | 47 | 47 | 0.15 | 0.56 | 0.15 / 0.28 | 0.28 | -0.25 | NO CLEAR DEFICIT |
| STRUCT:confirmed | session=MORNING_06_12 | 107 | 107 | 0.78 | 1.08 | 0.56 / 0.27 | 0.27 | -0.03 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | session=NIGHT_00_06 | 113 | 113 | 0.51 | 1.09 | 0.51 / 0.29 | 0.17 | -0.30 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | regime=HIGH_VOL | 352 | 352 | 0.50 | 1.01 | 0.51 / 0.30 | 0.28 | -0.05 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:confirmed | regime=MID_VOL | 94 | 94 | 0.53 | 1.09 | 0.51 / 0.26 | 0.15 | -0.36 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | session=AFTERNOON_12_18 | 146 | 146 | 0.81 | 1.12 | 0.57 / 0.34 | 0.23 | -0.10 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | session=EVENING_18_24 | 54 | 54 | 0.60 | 0.87 | 0.52 / 0.26 | 0.36 | -0.01 | NO CLEAR DEFICIT |
| STRUCT:fade | session=MORNING_06_12 | 87 | 87 | 0.60 | 1.21 | 0.51 / 0.41 | 0.29 | -0.14 | BOTH |
| STRUCT:fade | session=NIGHT_00_06 | 105 | 105 | 0.38 | 1.17 | 0.46 / 0.44 | 0.24 | -0.14 | BOTH |
| STRUCT:fade | regime=HIGH_VOL | 315 | 315 | 0.72 | 1.12 | 0.55 / 0.33 | 0.27 | -0.05 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:fade | regime=MID_VOL | 75 | 75 | 0.13 | 1.21 | 0.39 / 0.53 | 0.19 | -0.38 | BOTH |
| STRUCT:retest | session=AFTERNOON_12_18 | 152 | 152 | 0.73 | 1.01 | 0.59 / 0.30 | 0.29 | -0.01 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | session=EVENING_18_24 | 37 | 37 | 0.06 | 0.68 | 0.16 / 0.32 | 0.20 | -0.32 | NO CLEAR DEFICIT |
| STRUCT:retest | session=MORNING_06_12 | 94 | 94 | 0.76 | 1.08 | 0.56 / 0.29 | 0.27 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | session=NIGHT_00_06 | 90 | 90 | 0.43 | 1.10 | 0.47 / 0.34 | 0.16 | -0.32 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | regime=HIGH_VOL | 286 | 286 | 0.50 | 1.01 | 0.50 / 0.32 | 0.27 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| STRUCT:retest | regime=MID_VOL | 84 | 84 | 0.61 | 1.10 | 0.54 / 0.29 | 0.17 | -0.28 | USEFUL ENTRIES + BAD CAPTURE |
