# Lane X - EURUSD

`eeq-1.0` hindsight diagnostics, no edge claim.

## EURUSD

Data: 2025-05-26..2026-08-31 (94458 M5 bars; evaluated from 2025-06-25); 12 frozen family specs (EOD:continue, EOD:reverse, GAP:fade, GAP:go, ORB:breakout, ORB:fade, OVERNIGHT:continue, OVERNIGHT:reverse, ROUND:break, ROUND:reject, VOLREV:expand, VOLREV:fade); analysed entries n=2867.

Exclusions / counters: `{"analysed": 2867, "candidates": 2868, "entry_gap_stop": 1}`

Entry quality (potential; R against the initial risk; MFE/MAE to the stop or the flat deadline):

| family:variant | dir | n | clusters | flag | MFE mean/med | MAE mean/med | P(MFE>=.25/.5/.75/1/1.5/2) | MFE-first / MAE-first | giveback | capture (floored) | useful / failure share | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 77 | 77 |  | 1.34/0.80 | 0.98/1.09 | 0.75/0.58/0.52/0.43/0.35/0.22 | 0.35 / 0.65 | 1.34 | 0.27 | 0.58 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:continue | long | 40 | 40 |  | 1.12/0.59 | 0.98/1.07 | 0.72/0.55/0.47/0.40/0.28/0.20 | 0.38 / 0.62 | 1.23 | 0.24 | 0.55 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:continue | short | 37 | 37 |  | 1.58/0.88 | 0.99/1.10 | 0.78/0.62/0.57/0.46/0.43/0.24 | 0.32 / 0.68 | 1.45 | 0.29 | 0.62 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | ALL | 77 | 77 |  | 1.32/0.89 | 0.95/1.03 | 0.77/0.68/0.55/0.45/0.34/0.25 | 0.49 / 0.51 | 1.27 | 0.31 | 0.68 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | long | 37 | 37 |  | 1.46/0.89 | 1.04/1.05 | 0.78/0.68/0.51/0.49/0.38/0.30 | 0.43 / 0.57 | 1.38 | 0.27 | 0.68 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | short | 40 | 40 |  | 1.20/0.88 | 0.86/1.01 | 0.75/0.68/0.57/0.42/0.30/0.20 | 0.55 / 0.45 | 1.16 | 0.35 | 0.68 / 0.25 | NO CLEAR DEFICIT |
| GAP:fade | ALL | 78 | 78 |  | 1.74/0.77 | 1.21/1.12 | 0.74/0.65/0.51/0.41/0.33/0.27 | 0.37 / 0.63 | 1.90 | 0.19 | 0.65 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | long | 40 | 40 |  | 1.79/0.80 | 1.24/1.14 | 0.70/0.65/0.53/0.38/0.33/0.30 | 0.45 / 0.55 | 1.97 | 0.18 | 0.65 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | short | 38 | 38 |  | 1.69/0.75 | 1.18/1.08 | 0.79/0.66/0.50/0.45/0.34/0.24 | 0.29 / 0.71 | 1.83 | 0.21 | 0.66 / 0.21 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | ALL | 72 | 72 |  | 2.26/0.73 | 1.11/1.09 | 0.71/0.58/0.49/0.43/0.36/0.33 | 0.35 / 0.65 | 2.35 | 0.17 | 0.58 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | long | 33 | 33 |  | 2.88/0.92 | 1.02/1.09 | 0.70/0.64/0.55/0.48/0.45/0.42 | 0.33 / 0.67 | 2.74 | 0.19 | 0.64 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | short | 39 | 39 |  | 1.73/0.70 | 1.18/1.12 | 0.72/0.54/0.44/0.38/0.28/0.26 | 0.36 / 0.64 | 2.02 | 0.15 | 0.54 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | ALL | 306 | 306 |  | 1.62/0.79 | 1.07/1.06 | 0.73/0.61/0.51/0.44/0.34/0.27 | 0.40 / 0.60 | 1.75 | 0.21 | 0.61 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | long | 147 | 147 |  | 1.72/0.71 | 1.01/1.06 | 0.75/0.61/0.50/0.41/0.32/0.25 | 0.41 / 0.59 | 1.88 | 0.19 | 0.61 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | short | 159 | 159 |  | 1.53/0.79 | 1.13/1.06 | 0.71/0.62/0.53/0.46/0.35/0.28 | 0.38 / 0.62 | 1.62 | 0.22 | 0.62 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | ALL | 262 | 262 |  | 2.11/0.69 | 1.44/1.24 | 0.66/0.57/0.48/0.42/0.35/0.30 | 0.33 / 0.67 | 2.23 | 0.21 | 0.57 / 0.34 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | long | 136 | 136 |  | 1.95/0.61 | 1.34/1.24 | 0.63/0.54/0.46/0.39/0.32/0.25 | 0.32 / 0.68 | 2.14 | 0.22 | 0.54 / 0.37 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | short | 126 | 126 |  | 2.28/0.78 | 1.55/1.24 | 0.69/0.60/0.51/0.45/0.38/0.35 | 0.34 / 0.66 | 2.33 | 0.20 | 0.60 / 0.31 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | ALL | 74 | 74 |  | 1.91/0.70 | 1.20/1.11 | 0.73/0.55/0.43/0.41/0.32/0.27 | 0.36 / 0.64 | 2.10 | 0.17 | 0.55 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | long | 39 | 39 |  | 2.42/0.74 | 1.10/1.10 | 0.74/0.62/0.49/0.46/0.41/0.36 | 0.38 / 0.62 | 2.39 | 0.19 | 0.62 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | short | 35 | 35 |  | 1.34/0.45 | 1.32/1.20 | 0.71/0.49/0.37/0.34/0.23/0.17 | 0.34 / 0.66 | 1.77 | 0.14 | 0.49 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | ALL | 74 | 74 |  | 2.21/1.16 | 1.17/1.11 | 0.84/0.74/0.64/0.53/0.41/0.35 | 0.46 / 0.54 | 2.19 | 0.20 | 0.74 / 0.16 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | long | 35 | 35 |  | 2.23/1.17 | 1.19/1.12 | 0.80/0.74/0.66/0.54/0.43/0.43 | 0.54 / 0.46 | 2.16 | 0.20 | 0.74 / 0.20 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | short | 39 | 39 |  | 2.19/1.06 | 1.14/1.08 | 0.87/0.74/0.62/0.51/0.38/0.28 | 0.38 / 0.62 | 2.23 | 0.20 | 0.74 / 0.13 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | ALL | 159 | 135 |  | 1.75/0.87 | 1.26/1.12 | 0.76/0.63/0.54/0.46/0.38/0.33 | 0.40 / 0.60 | 1.79 | 0.22 | 0.63 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | long | 74 | 63 |  | 1.43/0.80 | 1.25/1.12 | 0.76/0.61/0.53/0.42/0.31/0.24 | 0.31 / 0.69 | 1.61 | 0.22 | 0.61 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | short | 85 | 72 |  | 2.02/0.96 | 1.27/1.13 | 0.76/0.65/0.55/0.49/0.44/0.41 | 0.47 / 0.53 | 1.95 | 0.23 | 0.65 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | ALL | 406 | 280 |  | 1.86/0.91 | 1.23/1.17 | 0.74/0.63/0.54/0.48/0.36/0.32 | 0.44 / 0.56 | 1.94 | 0.21 | 0.63 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | long | 212 | 144 |  | 1.74/0.91 | 1.14/1.15 | 0.70/0.60/0.54/0.48/0.35/0.28 | 0.43 / 0.57 | 1.86 | 0.23 | 0.60 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | short | 194 | 136 |  | 2.00/0.92 | 1.31/1.18 | 0.77/0.65/0.55/0.47/0.38/0.37 | 0.45 / 0.55 | 2.03 | 0.20 | 0.65 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | ALL | 973 | 876 |  | 1.69/0.73 | 1.21/1.14 | 0.71/0.58/0.49/0.43/0.33/0.27 | 0.35 / 0.65 | 1.86 | 0.20 | 0.58 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | long | 491 | 435 |  | 1.60/0.67 | 1.14/1.13 | 0.68/0.56/0.47/0.39/0.31/0.25 | 0.33 / 0.67 | 1.81 | 0.19 | 0.56 / 0.32 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | short | 482 | 441 |  | 1.79/0.85 | 1.27/1.15 | 0.75/0.60/0.52/0.46/0.35/0.30 | 0.37 / 0.63 | 1.90 | 0.21 | 0.60 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | ALL | 309 | 295 |  | 1.68/0.93 | 1.14/1.10 | 0.77/0.63/0.55/0.49/0.37/0.30 | 0.45 / 0.55 | 1.73 | 0.24 | 0.63 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | long | 161 | 151 |  | 1.43/0.83 | 1.12/1.12 | 0.75/0.59/0.52/0.45/0.33/0.24 | 0.42 / 0.58 | 1.60 | 0.22 | 0.59 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | short | 148 | 144 |  | 1.96/1.15 | 1.16/1.08 | 0.80/0.68/0.58/0.53/0.42/0.36 | 0.49 / 0.51 | 1.86 | 0.27 | 0.68 / 0.20 | USEFUL ENTRIES + BAD CAPTURE |

Exit-policy shadow comparison on the SAME entries (mean R, spread-adjusted gross; in brackets: entries the policy applies to; NA = no structural level):

| family:variant | dir | n | P1 FIXED_1_5R | P2 STRUCT_TP1 | P3 STRUCT_TP1_TP2 | P4 TP1_TP2_RUNNER | P5 STRUCT_TRAIL | P6 BREAKEVEN_LOCK | P7 MOMENTUM_EXIT | P8 TIME_ALPHA | P9 EOD_FORCED_FLAT | paired-structural n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 77 | +0.01(77) | -0.05(66) | -0.11(27) | -0.07(66) | -0.07(77) | -0.01(77) | -0.00(77) | +0.01(77) | -0.07(77) | 27 |
| EOD:continue | long | 40 | -0.11(40) | -0.09(32) | -0.13(12) | -0.04(32) | -0.14(40) | -0.13(40) | +0.04(40) | -0.11(40) | -0.17(40) | 12 |
| EOD:continue | short | 37 | +0.13(37) | -0.01(34) | -0.11(15) | -0.09(34) | +0.02(37) | +0.12(37) | -0.05(37) | +0.13(37) | +0.05(37) | 15 |
| EOD:reverse | ALL | 77 | +0.05(77) | -0.03(77) | -0.01(77) | -0.07(77) | -0.01(77) | -0.02(77) | -0.01(77) | +0.05(77) | +0.07(77) | 77 |
| EOD:reverse | long | 37 | +0.08(37) | -0.07(37) | -0.03(37) | -0.06(37) | +0.04(37) | -0.04(37) | -0.02(37) | +0.10(37) | +0.06(37) | 37 |
| EOD:reverse | short | 40 | +0.04(40) | +0.01(40) | +0.00(40) | -0.07(40) | -0.06(40) | +0.01(40) | -0.00(40) | +0.02(40) | +0.08(40) | 40 |
| GAP:fade | ALL | 78 | -0.17(78) | -0.10(71) | -0.16(69) | -0.10(71) | -0.18(78) | -0.19(78) | -0.22(78) | -0.16(78) | -0.36(78) | 69 |
| GAP:fade | long | 40 | -0.19(40) | -0.21(37) | -0.25(37) | -0.07(37) | -0.10(40) | -0.21(40) | -0.18(40) | -0.18(40) | -0.27(40) | 37 |
| GAP:fade | short | 38 | -0.14(38) | +0.02(34) | -0.05(32) | -0.13(34) | -0.28(38) | -0.18(38) | -0.27(38) | -0.14(38) | -0.46(38) | 32 |
| GAP:go | ALL | 72 | -0.10(72) | -0.13(61) | -0.15(31) | -0.18(61) | -0.14(72) | -0.12(72) | -0.12(72) | -0.10(72) | +0.23(72) | 31 |
| GAP:go | long | 33 | +0.14(33) | -0.09(26) | -0.21(15) | -0.16(26) | +0.10(33) | +0.04(33) | +0.01(33) | +0.14(33) | +1.00(33) | 15 |
| GAP:go | short | 39 | -0.29(39) | -0.17(35) | -0.08(16) | -0.20(35) | -0.35(39) | -0.26(39) | -0.24(39) | -0.29(39) | -0.42(39) | 16 |
| ORB:breakout | ALL | 306 | -0.13(306) | -0.10(229) | -0.15(166) | -0.10(229) | -0.18(306) | -0.12(306) | -0.12(306) | -0.13(306) | -0.02(306) | 166 |
| ORB:breakout | long | 147 | -0.16(147) | -0.13(107) | -0.17(76) | -0.09(107) | -0.18(147) | -0.13(147) | -0.10(147) | -0.16(147) | +0.13(147) | 76 |
| ORB:breakout | short | 159 | -0.09(159) | -0.08(122) | -0.14(90) | -0.10(122) | -0.17(159) | -0.10(159) | -0.14(159) | -0.10(159) | -0.16(159) | 90 |
| ORB:fade | ALL | 262 | -0.12(262) | -0.14(259) | -0.17(233) | -0.20(259) | -0.13(262) | -0.16(262) | -0.13(262) | -0.12(262) | -0.18(262) | 233 |
| ORB:fade | long | 136 | -0.19(136) | -0.22(135) | -0.22(121) | -0.20(135) | -0.23(136) | -0.22(136) | -0.21(136) | -0.20(136) | +0.01(136) | 121 |
| ORB:fade | short | 126 | -0.05(126) | -0.06(124) | -0.11(112) | -0.19(124) | -0.02(126) | -0.11(126) | -0.04(126) | -0.04(126) | -0.39(126) | 112 |
| OVERNIGHT:continue | ALL | 74 | -0.19(74) | -0.15(69) | -0.09(42) | -0.12(69) | -0.34(74) | -0.18(74) | -0.17(74) | -0.19(74) | -0.18(74) | 42 |
| OVERNIGHT:continue | long | 39 | +0.03(39) | -0.04(36) | -0.08(24) | -0.06(36) | -0.21(39) | -0.07(39) | -0.15(39) | +0.03(39) | +0.29(39) | 24 |
| OVERNIGHT:continue | short | 35 | -0.43(35) | -0.28(33) | -0.11(18) | -0.19(33) | -0.48(35) | -0.31(35) | -0.20(35) | -0.43(35) | -0.70(35) | 18 |
| OVERNIGHT:reverse | ALL | 74 | +0.01(74) | -0.06(71) | -0.06(70) | -0.09(71) | +0.02(74) | -0.03(74) | -0.12(74) | +0.01(74) | -0.08(74) | 70 |
| OVERNIGHT:reverse | long | 35 | +0.07(35) | +0.03(33) | -0.04(33) | +0.06(33) | +0.17(35) | +0.07(35) | +0.05(35) | +0.07(35) | -0.04(35) | 33 |
| OVERNIGHT:reverse | short | 39 | -0.04(39) | -0.13(38) | -0.09(37) | -0.22(38) | -0.11(39) | -0.12(39) | -0.27(39) | -0.04(39) | -0.12(39) | 37 |
| ROUND:break | ALL | 159 | -0.04(159) | -0.08(145) | -0.08(100) | -0.13(145) | -0.17(159) | -0.04(159) | -0.06(159) | -0.04(159) | -0.18(159) | 100 |
| ROUND:break | long | 74 | -0.17(74) | -0.08(68) | -0.08(51) | -0.17(68) | -0.35(74) | -0.19(74) | -0.11(74) | -0.17(74) | -0.32(74) | 51 |
| ROUND:break | short | 85 | +0.07(85) | -0.08(77) | -0.08(49) | -0.10(77) | -0.02(85) | +0.08(85) | -0.01(85) | +0.07(85) | -0.06(85) | 49 |
| ROUND:reject | ALL | 406 | -0.08(406) | -0.11(380) | -0.13(324) | -0.04(380) | +0.03(406) | -0.05(406) | +0.01(406) | -0.08(406) | -0.14(406) | 324 |
| ROUND:reject | long | 212 | -0.12(212) | -0.16(197) | -0.18(171) | -0.05(197) | +0.04(212) | -0.06(212) | -0.01(212) | -0.12(212) | -0.10(212) | 171 |
| ROUND:reject | short | 194 | -0.04(194) | -0.07(183) | -0.07(153) | -0.03(183) | +0.02(194) | -0.05(194) | +0.02(194) | -0.04(194) | -0.19(194) | 153 |
| VOLREV:expand | ALL | 973 | -0.16(973) | -0.15(608) | -0.13(386) | -0.15(608) | -0.22(973) | -0.18(973) | -0.16(973) | -0.16(973) | -0.24(973) | 386 |
| VOLREV:expand | long | 491 | -0.21(491) | -0.21(285) | -0.26(189) | -0.21(285) | -0.31(491) | -0.22(491) | -0.18(491) | -0.22(491) | -0.23(491) | 189 |
| VOLREV:expand | short | 482 | -0.11(482) | -0.10(323) | +0.00(197) | -0.09(323) | -0.13(482) | -0.13(482) | -0.13(482) | -0.11(482) | -0.26(482) | 197 |
| VOLREV:fade | ALL | 309 | -0.04(309) | -0.02(309) | -0.03(309) | -0.00(309) | -0.08(309) | -0.03(309) | -0.06(309) | -0.04(309) | +0.01(309) | 309 |
| VOLREV:fade | long | 161 | -0.17(161) | -0.13(161) | -0.15(161) | -0.00(161) | -0.19(161) | -0.12(161) | -0.11(161) | -0.16(161) | -0.20(161) | 161 |
| VOLREV:fade | short | 148 | +0.09(148) | +0.10(148) | +0.10(148) | -0.00(148) | +0.03(148) | +0.07(148) | +0.00(148) | +0.09(148) | +0.23(148) | 148 |

Timing / structure / cost state (medians; times dated by bar open): 

| family:variant | dir | n | t_MFE s | t_MAE s | t to 0.5R s | t to 1R s | bars above entry | max structure reached R | TP1 / TP2 reach | holding s | spread/1R | P1 exits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 77 | 1800 | 1800 | 900 | 1800 | 0.47 | 0.25 | 0.64 / 0.44 | 5700 | 0.052 | STOP 0.52, TP1 0.35, FORCED_FLAT 0.13 |
| EOD:reverse | ALL | 77 | 2250 | 1800 | 900 | 1800 | 0.55 | 0.18 | 0.51 / 0.39 | 4800 | 0.052 | STOP 0.52, TP1 0.34, FORCED_FLAT 0.14 |
| GAP:fade | ALL | 78 | 1350 | 2400 | 300 | 1200 | 0.49 | 0.28 | 0.59 / 0.36 | 2850 | 0.082 | STOP 0.65, TP1 0.33, STOP_GAP 0.01 |
| GAP:go | ALL | 72 | 1500 | 900 | 600 | 1200 | 0.52 | 0.28 | 0.59 / 0.45 | 2550 | 0.080 | STOP 0.64, TP1 0.36 |
| ORB:breakout | ALL | 306 | 3750 | 3000 | 1200 | 3000 | 0.48 | 0.21 | 0.67 / 0.49 | 10350 | 0.051 | STOP 0.63, TP1 0.34, FORCED_FLAT 0.03 |
| ORB:fade | ALL | 262 | 1500 | 1200 | 300 | 600 | 0.58 | 0.17 | 0.47 / 0.33 | 2100 | 0.110 | STOP 0.64, TP1 0.35, FORCED_FLAT 0.00 |
| OVERNIGHT:continue | ALL | 74 | 900 | 1200 | 600 | 1650 | 0.46 | 0.33 | 0.55 / 0.43 | 2700 | 0.076 | STOP 0.68, TP1 0.32 |
| OVERNIGHT:reverse | ALL | 74 | 1500 | 2100 | 300 | 1200 | 0.58 | 0.55 | 0.56 / 0.41 | 3450 | 0.077 | STOP 0.59, TP1 0.41 |
| ROUND:break | ALL | 159 | 1500 | 1800 | 450 | 1200 | 0.53 | 0.37 | 0.65 / 0.48 | 3900 | 0.073 | STOP 0.58, TP1 0.38, FORCED_FLAT 0.03 |
| ROUND:reject | ALL | 406 | 1500 | 1500 | 300 | 1200 | 0.53 | 0.35 | 0.57 / 0.40 | 3150 | 0.077 | STOP 0.63, TP1 0.36, FORCED_FLAT 0.00 |
| VOLREV:expand | ALL | 973 | 1200 | 1500 | 300 | 1200 | 0.47 | 0.18 | 0.55 / 0.38 | 2400 | 0.089 | STOP 0.65, TP1 0.33, FORCED_FLAT 0.01 |
| VOLREV:fade | ALL | 309 | 1500 | 1500 | 300 | 900 | 0.56 | 0.16 | 0.50 / 0.35 | 3300 | 0.059 | STOP 0.60, TP1 0.37, FORCED_FLAT 0.02 |

Profit capture per policy (mean floored capture ratio = share of the MFE kept; same entries):

| family:variant | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9 |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | 0.27 | 0.27 | 0.15 | 0.16 | 0.14 | 0.23 | 0.18 | 0.27 | 0.20 |
| EOD:reverse | 0.31 | 0.26 | 0.24 | 0.15 | 0.19 | 0.24 | 0.18 | 0.31 | 0.29 |
| GAP:fade | 0.19 | 0.23 | 0.13 | 0.16 | 0.11 | 0.15 | 0.14 | 0.19 | 0.09 |
| GAP:go | 0.17 | 0.26 | 0.13 | 0.17 | 0.13 | 0.15 | 0.14 | 0.17 | 0.17 |
| ORB:breakout | 0.21 | 0.24 | 0.16 | 0.16 | 0.12 | 0.18 | 0.12 | 0.20 | 0.18 |
| ORB:fade | 0.21 | 0.21 | 0.14 | 0.14 | 0.12 | 0.17 | 0.17 | 0.21 | 0.12 |
| OVERNIGHT:continue | 0.17 | 0.26 | 0.18 | 0.17 | 0.08 | 0.16 | 0.13 | 0.17 | 0.12 |
| OVERNIGHT:reverse | 0.20 | 0.20 | 0.14 | 0.14 | 0.13 | 0.15 | 0.14 | 0.20 | 0.11 |
| ROUND:break | 0.22 | 0.24 | 0.18 | 0.15 | 0.12 | 0.19 | 0.17 | 0.22 | 0.15 |
| ROUND:reject | 0.21 | 0.22 | 0.16 | 0.16 | 0.14 | 0.19 | 0.18 | 0.21 | 0.14 |
| VOLREV:expand | 0.20 | 0.24 | 0.16 | 0.17 | 0.11 | 0.16 | 0.16 | 0.20 | 0.12 |
| VOLREV:fade | 0.24 | 0.25 | 0.20 | 0.16 | 0.13 | 0.20 | 0.14 | 0.24 | 0.19 |

Session / regime cells with n >= 30:

| family:variant | cell | n | clusters | MFE med | MAE med | useful / failure | capture | baseline R | verdict |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | session=MIDDAY | 77 | 77 | 0.80 | 1.09 | 0.58 / 0.25 | 0.27 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:continue | regime=MID_VOL | 32 | 32 | 0.35 | 1.14 | 0.44 / 0.41 | 0.24 | -0.18 | BOTH |
| EOD:reverse | session=MIDDAY | 77 | 77 | 0.89 | 1.03 | 0.68 / 0.23 | 0.31 | 0.05 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | regime=MID_VOL | 32 | 32 | 1.41 | 0.94 | 0.72 / 0.22 | 0.37 | 0.35 | NO CLEAR DEFICIT |
| GAP:fade | session=OPEN_90M | 78 | 78 | 0.77 | 1.12 | 0.65 / 0.26 | 0.19 | -0.17 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | regime=MID_VOL | 38 | 38 | 1.08 | 1.13 | 0.84 / 0.11 | 0.18 | -0.08 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | session=OPEN_90M | 72 | 72 | 0.73 | 1.09 | 0.58 / 0.29 | 0.17 | -0.10 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | session=OPEN_90M | 299 | 299 | 0.78 | 1.06 | 0.61 / 0.27 | 0.20 | -0.15 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=HIGH_VOL | 57 | 57 | 1.00 | 1.03 | 0.67 / 0.21 | 0.24 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=LOW_VOL | 109 | 109 | 1.02 | 1.05 | 0.63 / 0.27 | 0.23 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=MID_VOL | 140 | 140 | 0.65 | 1.07 | 0.57 / 0.30 | 0.18 | -0.26 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | session=OPEN_90M | 250 | 250 | 0.73 | 1.25 | 0.58 / 0.34 | 0.22 | -0.09 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=HIGH_VOL | 46 | 46 | 0.71 | 1.16 | 0.57 / 0.35 | 0.22 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=LOW_VOL | 92 | 92 | 0.50 | 1.26 | 0.50 / 0.37 | 0.18 | -0.27 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=MID_VOL | 124 | 124 | 0.83 | 1.24 | 0.62 / 0.31 | 0.23 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | session=OPEN_90M | 74 | 74 | 0.70 | 1.11 | 0.55 / 0.27 | 0.17 | -0.19 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | session=OPEN_90M | 74 | 74 | 1.16 | 1.11 | 0.74 / 0.16 | 0.20 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=MIDDAY | 126 | 107 | 0.94 | 1.11 | 0.63 / 0.25 | 0.24 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=OPEN_90M | 33 | 32 | 0.68 | 1.13 | 0.61 / 0.21 | 0.14 | -0.24 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=HIGH_VOL | 60 | 54 | 1.08 | 1.11 | 0.68 / 0.25 | 0.24 | 0.08 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=LOW_VOL | 46 | 39 | 0.85 | 1.16 | 0.63 / 0.15 | 0.22 | 0.03 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=MID_VOL | 53 | 49 | 0.64 | 1.12 | 0.57 / 0.30 | 0.21 | -0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=MIDDAY | 318 | 216 | 1.05 | 1.15 | 0.67 / 0.23 | 0.23 | -0.01 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=OPEN_90M | 88 | 77 | 0.38 | 1.22 | 0.48 / 0.39 | 0.17 | -0.35 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=HIGH_VOL | 131 | 105 | 0.89 | 1.16 | 0.62 / 0.27 | 0.24 | -0.17 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=LOW_VOL | 137 | 96 | 0.97 | 1.18 | 0.61 / 0.26 | 0.20 | -0.03 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=MID_VOL | 138 | 115 | 0.91 | 1.16 | 0.65 / 0.26 | 0.21 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=MIDDAY | 712 | 655 | 0.73 | 1.14 | 0.58 / 0.28 | 0.21 | -0.16 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=OPEN_90M | 261 | 237 | 0.74 | 1.12 | 0.59 / 0.30 | 0.18 | -0.18 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=HIGH_VOL | 48 | 47 | 0.51 | 1.10 | 0.50 / 0.40 | 0.20 | -0.32 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=LOW_VOL | 486 | 453 | 0.67 | 1.15 | 0.56 / 0.29 | 0.19 | -0.21 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=MID_VOL | 439 | 415 | 0.84 | 1.13 | 0.61 / 0.27 | 0.21 | -0.09 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=MIDDAY | 244 | 234 | 0.98 | 1.09 | 0.64 / 0.22 | 0.26 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=OPEN_90M | 65 | 63 | 0.83 | 1.12 | 0.63 / 0.25 | 0.18 | -0.12 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | regime=HIGH_VOL | 309 | 295 | 0.93 | 1.10 | 0.63 / 0.23 | 0.24 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
