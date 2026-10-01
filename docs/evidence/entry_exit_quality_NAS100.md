# Lane X - NAS100

`eeq-1.0` hindsight diagnostics, no edge claim.

## NAS100

Data: 2025-05-02..2026-08-31 (94213 M5 bars; evaluated from 2025-06-01); 14 frozen family specs (EOD:continue, EOD:reverse, GAP:fade, GAP:go, LEADLAG:, ORB:breakout, ORB:fade, OVERNIGHT:continue, OVERNIGHT:reverse, ROUND:break, ROUND:reject, VOLREV:expand, VOLREV:fade); analysed entries n=5390.

Exclusions / counters: `{"analysed": 5390, "candidates": 5393, "no_post_entry_bars": 3}`

Entry quality (potential; R against the initial risk; MFE/MAE to the stop or the flat deadline):

| family:variant | dir | n | clusters | flag | MFE mean/med | MAE mean/med | P(MFE>=.25/.5/.75/1/1.5/2) | MFE-first / MAE-first | giveback | capture (floored) | useful / failure share | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 76 | 76 |  | 1.10/0.93 | 0.83/0.87 | 0.75/0.66/0.57/0.45/0.29/0.17 | 0.55 / 0.45 | 0.99 | 0.38 | 0.66 / 0.25 | NO CLEAR DEFICIT |
| EOD:continue | long | 56 | 56 |  | 1.08/0.93 | 0.84/0.82 | 0.73/0.62/0.55/0.45/0.29/0.18 | 0.54 / 0.46 | 0.96 | 0.38 | 0.62 / 0.27 | NO CLEAR DEFICIT |
| EOD:continue | short | 20 | 20 | n too small (<30): no conclusion | 1.17/0.91 | 0.79/0.91 | 0.80/0.75/0.60/0.45/0.30/0.15 | 0.60 / 0.40 | 1.08 | 0.37 | 0.75 / 0.20 | INCONCLUSIVE-n |
| EOD:reverse | ALL | 76 | 76 |  | 1.12/0.70 | 0.81/1.00 | 0.79/0.62/0.47/0.42/0.29/0.17 | 0.38 / 0.62 | 1.11 | 0.33 | 0.62 / 0.21 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | long | 20 | 20 | n too small (<30): no conclusion | 0.94/0.77 | 0.80/0.92 | 0.85/0.65/0.50/0.40/0.20/0.10 | 0.35 / 0.65 | 0.92 | 0.40 | 0.65 / 0.15 | INCONCLUSIVE-n |
| EOD:reverse | short | 56 | 56 |  | 1.19/0.68 | 0.81/1.00 | 0.77/0.61/0.46/0.43/0.32/0.20 | 0.39 / 0.61 | 1.18 | 0.31 | 0.61 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | ALL | 73 | 73 |  | 2.03/1.31 | 1.11/1.13 | 0.74/0.68/0.59/0.52/0.44/0.37 | 0.42 / 0.58 | 1.94 | 0.29 | 0.68 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | long | 30 | 30 |  | 1.52/0.78 | 1.13/1.21 | 0.67/0.57/0.50/0.47/0.43/0.33 | 0.43 / 0.57 | 1.44 | 0.35 | 0.57 / 0.33 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | short | 43 | 43 |  | 2.39/1.43 | 1.10/1.12 | 0.79/0.77/0.65/0.56/0.44/0.40 | 0.42 / 0.58 | 2.29 | 0.25 | 0.77 / 0.21 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | ALL | 96 | 96 |  | 1.80/0.92 | 1.25/1.27 | 0.77/0.65/0.55/0.48/0.35/0.30 | 0.45 / 0.55 | 1.92 | 0.22 | 0.65 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | long | 59 | 59 |  | 1.76/0.98 | 1.28/1.29 | 0.81/0.69/0.59/0.49/0.37/0.34 | 0.49 / 0.51 | 1.83 | 0.23 | 0.69 / 0.19 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | short | 37 | 37 |  | 1.88/0.68 | 1.19/1.26 | 0.70/0.57/0.49/0.46/0.32/0.24 | 0.38 / 0.62 | 2.06 | 0.21 | 0.57 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | ALL | 186 | 165 |  | 2.00/1.27 | 1.09/1.08 | 0.76/0.70/0.60/0.55/0.44/0.34 | 0.45 / 0.55 | 1.86 | 0.29 | 0.70 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | long | 97 | 86 |  | 2.14/1.40 | 1.05/1.06 | 0.81/0.77/0.66/0.60/0.48/0.37 | 0.51 / 0.49 | 1.85 | 0.33 | 0.77 / 0.19 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | short | 89 | 79 |  | 1.85/1.03 | 1.13/1.09 | 0.71/0.62/0.54/0.51/0.39/0.30 | 0.39 / 0.61 | 1.87 | 0.24 | 0.62 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | ALL | 322 | 322 |  | 0.93/0.69 | 0.74/0.79 | 0.78/0.60/0.48/0.38/0.20/0.10 | 0.45 / 0.52 | 0.91 | 0.33 | 0.60 / 0.19 | NO CLEAR DEFICIT |
| ORB:breakout | long | 167 | 167 |  | 0.87/0.71 | 0.72/0.68 | 0.82/0.64/0.49/0.36/0.14/0.07 | 0.49 / 0.47 | 0.86 | 0.35 | 0.64 / 0.17 | NO CLEAR DEFICIT |
| ORB:breakout | short | 155 | 155 |  | 0.99/0.67 | 0.77/0.88 | 0.74/0.55/0.47/0.39/0.28/0.14 | 0.42 / 0.57 | 0.96 | 0.31 | 0.55 / 0.21 | NO CLEAR DEFICIT |
| ORB:fade | ALL | 260 | 260 |  | 1.82/0.84 | 1.12/1.11 | 0.74/0.62/0.53/0.47/0.39/0.28 | 0.46 / 0.54 | 1.80 | 0.29 | 0.62 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | long | 138 | 138 |  | 1.51/0.84 | 1.06/1.11 | 0.72/0.62/0.56/0.46/0.36/0.26 | 0.50 / 0.50 | 1.52 | 0.31 | 0.62 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | short | 122 | 122 |  | 2.17/0.80 | 1.18/1.12 | 0.76/0.61/0.51/0.47/0.42/0.31 | 0.41 / 0.59 | 2.11 | 0.27 | 0.61 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | ALL | 83 | 83 |  | 1.77/0.82 | 1.22/1.20 | 0.77/0.63/0.51/0.48/0.37/0.31 | 0.35 / 0.65 | 1.83 | 0.23 | 0.63 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | long | 58 | 58 |  | 1.73/1.03 | 1.25/1.16 | 0.81/0.67/0.53/0.50/0.41/0.33 | 0.34 / 0.66 | 1.69 | 0.27 | 0.67 / 0.19 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | short | 25 | 25 | n too small (<30): no conclusion | 1.86/0.53 | 1.17/1.26 | 0.68/0.52/0.44/0.44/0.28/0.28 | 0.36 / 0.64 | 2.16 | 0.14 | 0.52 / 0.32 | INCONCLUSIVE-n |
| OVERNIGHT:reverse | ALL | 83 | 83 |  | 2.19/0.94 | 1.06/1.12 | 0.71/0.65/0.54/0.49/0.39/0.34 | 0.34 / 0.66 | 2.21 | 0.22 | 0.65 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | long | 25 | 25 | n too small (<30): no conclusion | 2.70/1.58 | 0.89/1.08 | 0.72/0.64/0.60/0.56/0.52/0.44 | 0.48 / 0.52 | 2.40 | 0.28 | 0.64 / 0.28 | INCONCLUSIVE-n |
| OVERNIGHT:reverse | short | 58 | 58 |  | 1.97/0.87 | 1.14/1.13 | 0.71/0.66/0.52/0.47/0.33/0.29 | 0.28 / 0.72 | 2.12 | 0.20 | 0.66 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | ALL | 950 | 694 |  | 1.46/0.96 | 1.00/1.05 | 0.80/0.67/0.57/0.49/0.34/0.25 | 0.49 / 0.51 | 1.49 | 0.26 | 0.67 / 0.20 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | long | 491 | 354 |  | 1.30/0.90 | 0.98/1.04 | 0.80/0.65/0.54/0.45/0.32/0.23 | 0.47 / 0.53 | 1.34 | 0.27 | 0.65 / 0.20 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | short | 459 | 340 |  | 1.64/1.10 | 1.01/1.06 | 0.80/0.70/0.60/0.53/0.37/0.27 | 0.51 / 0.49 | 1.65 | 0.26 | 0.70 / 0.20 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | ALL | 2151 | 1130 |  | 1.43/0.86 | 1.01/1.06 | 0.75/0.63/0.53/0.46/0.35/0.25 | 0.46 / 0.54 | 1.45 | 0.28 | 0.63 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | long | 1141 | 572 |  | 1.31/0.85 | 1.02/1.06 | 0.75/0.62/0.53/0.45/0.34/0.24 | 0.45 / 0.55 | 1.36 | 0.28 | 0.62 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | short | 1010 | 558 |  | 1.56/0.88 | 1.00/1.06 | 0.75/0.64/0.54/0.47/0.36/0.26 | 0.48 / 0.52 | 1.56 | 0.27 | 0.64 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | ALL | 769 | 618 |  | 1.65/0.78 | 1.10/1.09 | 0.71/0.60/0.51/0.45/0.35/0.28 | 0.41 / 0.59 | 1.68 | 0.27 | 0.60 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | long | 455 | 346 |  | 1.38/0.74 | 1.08/1.09 | 0.70/0.59/0.49/0.42/0.31/0.24 | 0.42 / 0.58 | 1.45 | 0.27 | 0.59 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | short | 314 | 272 |  | 2.05/0.98 | 1.11/1.11 | 0.73/0.62/0.54/0.50/0.40/0.32 | 0.40 / 0.60 | 2.02 | 0.26 | 0.62 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | ALL | 265 | 227 |  | 1.47/0.89 | 1.00/1.07 | 0.78/0.66/0.57/0.46/0.36/0.31 | 0.47 / 0.53 | 1.49 | 0.25 | 0.66 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | long | 146 | 124 |  | 1.45/1.04 | 0.98/1.07 | 0.82/0.72/0.61/0.51/0.38/0.29 | 0.53 / 0.47 | 1.38 | 0.29 | 0.72 / 0.18 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | short | 119 | 103 |  | 1.50/0.82 | 1.02/1.07 | 0.72/0.58/0.52/0.40/0.33/0.32 | 0.39 / 0.61 | 1.61 | 0.20 | 0.58 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |

Exit-policy shadow comparison on the SAME entries (mean R, spread-adjusted gross; in brackets: entries the policy applies to; NA = no structural level):

| family:variant | dir | n | P1 FIXED_1_5R | P2 STRUCT_TP1 | P3 STRUCT_TP1_TP2 | P4 TP1_TP2_RUNNER | P5 STRUCT_TRAIL | P6 BREAKEVEN_LOCK | P7 MOMENTUM_EXIT | P8 TIME_ALPHA | P9 EOD_FORCED_FLAT | paired-structural n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 76 | +0.11(76) | -0.03(71) | +0.11(33) | -0.02(71) | -0.05(76) | +0.04(76) | +0.02(76) | +0.11(76) | +0.11(76) | 33 |
| EOD:continue | long | 56 | +0.12(56) | -0.07(52) | +0.17(23) | -0.02(52) | -0.05(56) | +0.03(56) | -0.04(56) | +0.12(56) | +0.11(56) | 23 |
| EOD:continue | short | 20 | +0.10(20) | +0.08(19) | -0.02(10) | -0.01(19) | -0.05(20) | +0.07(20) | +0.21(20) | +0.10(20) | +0.12(20) | 10 |
| EOD:reverse | ALL | 76 | +0.01(76) | +0.00(76) | -0.01(76) | -0.01(76) | -0.06(76) | -0.04(76) | -0.01(76) | +0.01(76) | +0.03(76) | 76 |
| EOD:reverse | long | 20 | +0.02(20) | -0.04(20) | -0.01(20) | -0.02(20) | -0.07(20) | -0.05(20) | -0.06(20) | +0.02(20) | +0.02(20) | 20 |
| EOD:reverse | short | 56 | +0.01(56) | +0.02(56) | -0.01(56) | -0.01(56) | -0.06(56) | -0.03(56) | +0.01(56) | +0.01(56) | +0.04(56) | 56 |
| GAP:fade | ALL | 73 | +0.10(73) | -0.00(72) | +0.01(53) | -0.01(72) | -0.01(73) | +0.12(73) | +0.07(73) | +0.10(73) | -0.07(73) | 53 |
| GAP:fade | long | 30 | +0.08(30) | -0.07(30) | -0.19(24) | -0.05(30) | -0.10(30) | +0.02(30) | +0.08(30) | +0.08(30) | -0.19(30) | 24 |
| GAP:fade | short | 43 | +0.10(43) | +0.04(42) | +0.18(29) | +0.02(42) | +0.05(43) | +0.19(43) | +0.06(43) | +0.10(43) | +0.01(43) | 29 |
| GAP:go | ALL | 96 | -0.11(96) | -0.09(87) | -0.12(40) | +0.00(87) | -0.10(96) | -0.05(96) | -0.09(96) | -0.11(96) | +0.15(96) | 40 |
| GAP:go | long | 59 | -0.07(59) | +0.02(52) | -0.10(21) | +0.11(52) | -0.13(59) | +0.00(59) | -0.04(59) | -0.07(59) | +0.12(59) | 21 |
| GAP:go | short | 37 | -0.19(37) | -0.26(35) | -0.14(19) | -0.16(35) | -0.06(37) | -0.13(37) | -0.16(37) | -0.19(37) | +0.19(37) | 19 |
| LEADLAG | ALL | 186 | +0.15(186) | -0.00(184) | -0.03(136) | +0.04(184) | +0.30(186) | +0.12(186) | +0.09(186) | +0.15(186) | +0.32(186) | 136 |
| LEADLAG | long | 97 | +0.29(97) | +0.05(95) | +0.03(65) | +0.13(95) | +0.45(97) | +0.22(97) | +0.23(97) | +0.29(97) | +0.65(97) | 65 |
| LEADLAG | short | 89 | -0.02(89) | -0.06(89) | -0.09(71) | -0.05(89) | +0.12(89) | +0.01(89) | -0.06(89) | -0.01(89) | -0.04(89) | 71 |
| ORB:breakout | ALL | 322 | +0.02(322) | -0.00(294) | -0.00(118) | +0.01(294) | -0.02(322) | +0.03(322) | +0.01(322) | +0.03(322) | -0.01(322) | 118 |
| ORB:breakout | long | 167 | +0.01(167) | +0.01(150) | -0.01(59) | +0.01(150) | +0.02(167) | +0.05(167) | +0.07(167) | +0.04(167) | +0.02(167) | 59 |
| ORB:breakout | short | 155 | +0.03(155) | -0.02(144) | +0.01(59) | +0.01(144) | -0.07(155) | +0.00(155) | -0.06(155) | +0.03(155) | -0.05(155) | 59 |
| ORB:fade | ALL | 260 | +0.02(260) | -0.08(260) | -0.02(244) | -0.09(260) | +0.04(260) | -0.02(260) | +0.00(260) | +0.02(260) | +0.07(260) | 244 |
| ORB:fade | long | 138 | -0.01(138) | -0.15(138) | -0.06(127) | -0.07(138) | -0.05(138) | -0.01(138) | -0.02(138) | -0.02(138) | -0.07(138) | 127 |
| ORB:fade | short | 122 | +0.06(122) | +0.00(122) | +0.03(117) | -0.11(122) | +0.15(122) | -0.04(122) | +0.03(122) | +0.06(122) | +0.22(122) | 117 |
| OVERNIGHT:continue | ALL | 83 | -0.07(83) | -0.03(79) | -0.04(57) | +0.04(79) | -0.01(83) | -0.08(83) | -0.08(83) | -0.07(83) | +0.07(83) | 57 |
| OVERNIGHT:continue | long | 58 | +0.03(58) | -0.03(54) | -0.13(37) | +0.08(54) | -0.11(58) | -0.01(58) | -0.03(58) | +0.03(58) | +0.05(58) | 37 |
| OVERNIGHT:continue | short | 25 | -0.30(25) | -0.04(25) | +0.11(20) | -0.07(25) | +0.23(25) | -0.26(25) | -0.21(25) | -0.30(25) | +0.12(25) | 20 |
| OVERNIGHT:reverse | ALL | 83 | -0.01(83) | +0.01(83) | +0.01(70) | +0.09(83) | +0.22(83) | +0.06(83) | +0.07(83) | -0.01(83) | +0.26(83) | 70 |
| OVERNIGHT:reverse | long | 25 | +0.30(25) | +0.32(25) | +0.20(22) | +0.22(25) | +1.01(25) | +0.28(25) | +0.30(25) | +0.30(25) | +0.86(25) | 22 |
| OVERNIGHT:reverse | short | 58 | -0.15(58) | -0.12(58) | -0.08(48) | +0.04(58) | -0.12(58) | -0.04(58) | -0.03(58) | -0.15(58) | +0.01(58) | 48 |
| ROUND:break | ALL | 950 | -0.03(950) | -0.00(929) | -0.01(598) | +0.01(929) | -0.03(950) | -0.02(950) | +0.01(950) | -0.02(950) | -0.02(950) | 598 |
| ROUND:break | long | 491 | -0.05(491) | -0.00(473) | -0.08(310) | +0.00(473) | -0.06(491) | -0.06(491) | -0.02(491) | -0.04(491) | -0.01(491) | 310 |
| ROUND:break | short | 459 | -0.01(459) | -0.01(456) | +0.06(288) | +0.02(456) | -0.01(459) | +0.03(459) | +0.05(459) | -0.01(459) | -0.03(459) | 288 |
| ROUND:reject | ALL | 2151 | -0.02(2151) | -0.05(2113) | -0.06(1673) | -0.03(2113) | -0.05(2151) | -0.03(2151) | -0.03(2151) | -0.02(2151) | -0.05(2151) | 1673 |
| ROUND:reject | long | 1141 | -0.04(1141) | -0.09(1113) | -0.09(879) | -0.05(1113) | -0.06(1141) | -0.06(1141) | -0.04(1141) | -0.04(1141) | -0.06(1141) | 879 |
| ROUND:reject | short | 1010 | +0.00(1010) | -0.02(1000) | -0.02(794) | -0.01(1000) | -0.04(1010) | -0.01(1010) | -0.02(1010) | +0.00(1010) | -0.04(1010) | 794 |
| VOLREV:expand | ALL | 769 | -0.03(769) | -0.11(629) | -0.13(300) | -0.04(629) | +0.06(769) | -0.04(769) | -0.03(769) | -0.03(769) | +0.11(769) | 300 |
| VOLREV:expand | long | 455 | -0.07(455) | -0.12(344) | -0.17(151) | -0.05(344) | -0.02(455) | -0.10(455) | -0.07(455) | -0.07(455) | +0.04(455) | 151 |
| VOLREV:expand | short | 314 | +0.03(314) | -0.11(285) | -0.08(149) | -0.03(285) | +0.18(314) | +0.05(314) | +0.03(314) | +0.03(314) | +0.19(314) | 149 |
| VOLREV:fade | ALL | 265 | -0.01(265) | -0.01(265) | -0.02(265) | -0.03(265) | +0.03(265) | -0.03(265) | -0.06(265) | -0.01(265) | -0.12(265) | 265 |
| VOLREV:fade | long | 146 | +0.07(146) | +0.03(146) | +0.02(146) | +0.03(146) | +0.03(146) | +0.03(146) | +0.01(146) | +0.06(146) | -0.08(146) | 146 |
| VOLREV:fade | short | 119 | -0.11(119) | -0.06(119) | -0.07(119) | -0.10(119) | +0.04(119) | -0.10(119) | -0.15(119) | -0.10(119) | -0.16(119) | 119 |

Timing / structure / cost state (medians; times dated by bar open): 

| family:variant | dir | n | t_MFE s | t_MAE s | t to 0.5R s | t to 1R s | bars above entry | max structure reached R | TP1 / TP2 reach | holding s | spread/1R | P1 exits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 76 | 3600 | 1950 | 600 | 2850 | 0.56 | 0.25 | 0.70 / 0.61 | 6600 | 0.019 | STOP 0.45, TP1 0.29, FORCED_FLAT 0.26 |
| EOD:reverse | ALL | 76 | 3000 | 1800 | 900 | 2550 | 0.48 | 0.04 | 0.50 / 0.21 | 6600 | 0.019 | STOP 0.49, TP1 0.29, FORCED_FLAT 0.22 |
| GAP:fade | ALL | 73 | 1200 | 600 | 0 | 300 | 0.68 | 0.42 | 0.68 / 0.49 | 1200 | 0.013 | STOP 0.56, TP1 0.44 |
| GAP:go | ALL | 96 | 600 | 600 | 0 | 0 | 0.59 | 0.24 | 0.68 / 0.53 | 900 | 0.015 | STOP 0.65, TP1 0.35 |
| LEADLAG | ALL | 186 | 2100 | 600 | 0 | 600 | 0.64 | 0.47 | 0.70 / 0.48 | 3600 | 0.015 | STOP 0.53, TP1 0.44, FORCED_FLAT 0.03 |
| ORB:breakout | ALL | 322 | 4200 | 3000 | 1500 | 4200 | 0.53 | 0.11 | 0.90 / 0.69 | 19200 | 0.005 | STOP 0.39, FORCED_FLAT 0.38, TP1 0.20 |
| ORB:fade | ALL | 260 | 1200 | 600 | 0 | 300 | 0.59 | 0.32 | 0.58 / 0.45 | 2250 | 0.012 | STOP 0.58, TP1 0.39, FORCED_FLAT 0.03 |
| OVERNIGHT:continue | ALL | 83 | 600 | 300 | 0 | 300 | 0.58 | 0.26 | 0.66 / 0.46 | 900 | 0.014 | STOP 0.63, TP1 0.37 |
| OVERNIGHT:reverse | ALL | 83 | 900 | 300 | 0 | 300 | 0.62 | 0.41 | 0.60 / 0.43 | 900 | 0.014 | STOP 0.60, TP1 0.39, DATA_END 0.01 |
| ROUND:break | ALL | 950 | 1500 | 1500 | 300 | 1200 | 0.55 | 0.33 | 0.75 / 0.50 | 4200 | 0.012 | STOP 0.56, TP1 0.34, FORCED_FLAT 0.09 |
| ROUND:reject | ALL | 2151 | 1500 | 1200 | 300 | 1200 | 0.54 | 0.29 | 0.65 / 0.39 | 3900 | 0.013 | STOP 0.56, TP1 0.35, FORCED_FLAT 0.09 |
| VOLREV:expand | ALL | 769 | 1500 | 900 | 300 | 900 | 0.55 | 0.12 | 0.72 / 0.44 | 3000 | 0.017 | STOP 0.58, TP1 0.35, FORCED_FLAT 0.07 |
| VOLREV:fade | ALL | 265 | 1200 | 1200 | 300 | 1200 | 0.56 | 0.00 | 0.49 / 0.33 | 4200 | 0.009 | STOP 0.55, TP1 0.36, FORCED_FLAT 0.09 |

Profit capture per policy (mean floored capture ratio = share of the MFE kept; same entries):

| family:variant | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9 |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | 0.38 | 0.27 | 0.22 | 0.16 | 0.22 | 0.32 | 0.25 | 0.38 | 0.37 |
| EOD:reverse | 0.33 | 0.29 | 0.27 | 0.18 | 0.21 | 0.29 | 0.22 | 0.33 | 0.31 |
| GAP:fade | 0.29 | 0.22 | 0.21 | 0.17 | 0.16 | 0.25 | 0.24 | 0.29 | 0.17 |
| GAP:go | 0.22 | 0.22 | 0.20 | 0.19 | 0.14 | 0.19 | 0.20 | 0.22 | 0.20 |
| LEADLAG | 0.29 | 0.22 | 0.17 | 0.17 | 0.21 | 0.24 | 0.23 | 0.29 | 0.26 |
| ORB:breakout | 0.33 | 0.15 | 0.20 | 0.11 | 0.20 | 0.31 | 0.19 | 0.31 | 0.30 |
| ORB:fade | 0.29 | 0.21 | 0.21 | 0.16 | 0.17 | 0.23 | 0.21 | 0.28 | 0.22 |
| OVERNIGHT:continue | 0.23 | 0.24 | 0.21 | 0.19 | 0.16 | 0.18 | 0.17 | 0.23 | 0.19 |
| OVERNIGHT:reverse | 0.22 | 0.22 | 0.21 | 0.21 | 0.19 | 0.20 | 0.22 | 0.22 | 0.22 |
| ROUND:break | 0.26 | 0.22 | 0.19 | 0.15 | 0.17 | 0.22 | 0.21 | 0.26 | 0.22 |
| ROUND:reject | 0.28 | 0.24 | 0.19 | 0.16 | 0.17 | 0.23 | 0.21 | 0.27 | 0.22 |
| VOLREV:expand | 0.27 | 0.20 | 0.16 | 0.15 | 0.19 | 0.22 | 0.23 | 0.27 | 0.23 |
| VOLREV:fade | 0.25 | 0.26 | 0.21 | 0.16 | 0.20 | 0.21 | 0.16 | 0.25 | 0.19 |

Session / regime cells with n >= 30:

| family:variant | cell | n | clusters | MFE med | MAE med | useful / failure | capture | baseline R | verdict |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | session=MIDDAY | 76 | 76 | 0.93 | 0.87 | 0.66 / 0.25 | 0.38 | 0.11 | NO CLEAR DEFICIT |
| EOD:continue | regime=LOW_VOL | 33 | 33 | 0.92 | 1.01 | 0.64 / 0.27 | 0.41 | 0.08 | NO CLEAR DEFICIT |
| EOD:reverse | session=MIDDAY | 76 | 76 | 0.70 | 1.00 | 0.62 / 0.21 | 0.33 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | regime=LOW_VOL | 33 | 33 | 0.79 | 1.00 | 0.58 / 0.24 | 0.37 | 0.13 | NO CLEAR DEFICIT |
| GAP:fade | session=OPEN_90M | 73 | 73 | 1.31 | 1.13 | 0.68 / 0.26 | 0.29 | 0.10 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | regime=HIGH_VOL | 56 | 56 | 1.53 | 1.14 | 0.71 / 0.25 | 0.33 | 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | session=OPEN_90M | 96 | 96 | 0.92 | 1.27 | 0.65 / 0.23 | 0.22 | -0.11 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | regime=HIGH_VOL | 65 | 65 | 0.98 | 1.27 | 0.68 / 0.22 | 0.21 | -0.12 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | session=MIDDAY | 31 | 31 | 1.28 | 1.04 | 0.71 / 0.26 | 0.31 | 0.08 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | session=OPEN_90M | 152 | 136 | 1.30 | 1.10 | 0.69 / 0.24 | 0.29 | 0.16 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | regime=HIGH_VOL | 125 | 117 | 1.38 | 1.07 | 0.72 / 0.21 | 0.33 | 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | regime=MID_VOL | 45 | 41 | 0.93 | 1.14 | 0.62 / 0.31 | 0.23 | 0.00 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | session=OPEN_90M | 308 | 308 | 0.73 | 0.80 | 0.61 / 0.19 | 0.33 | 0.03 | NO CLEAR DEFICIT |
| ORB:breakout | regime=HIGH_VOL | 262 | 262 | 0.68 | 0.81 | 0.60 / 0.20 | 0.32 | -0.02 | NO CLEAR DEFICIT |
| ORB:breakout | regime=MID_VOL | 55 | 55 | 0.94 | 0.72 | 0.64 / 0.13 | 0.37 | 0.20 | NO CLEAR DEFICIT |
| ORB:fade | session=OPEN_90M | 251 | 251 | 0.83 | 1.11 | 0.62 / 0.27 | 0.29 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=HIGH_VOL | 212 | 212 | 0.92 | 1.10 | 0.63 / 0.25 | 0.30 | 0.08 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=MID_VOL | 40 | 40 | 0.62 | 1.11 | 0.55 / 0.33 | 0.19 | -0.24 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | session=OPEN_90M | 83 | 83 | 0.82 | 1.20 | 0.63 / 0.23 | 0.23 | -0.07 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | regime=HIGH_VOL | 57 | 57 | 0.72 | 1.21 | 0.61 / 0.26 | 0.22 | -0.08 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | session=OPEN_90M | 83 | 83 | 0.94 | 1.12 | 0.65 / 0.29 | 0.22 | -0.01 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | regime=HIGH_VOL | 57 | 57 | 0.94 | 1.11 | 0.67 / 0.32 | 0.27 | 0.14 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=CLOSE_90M | 62 | 62 | 0.70 | 1.04 | 0.61 / 0.18 | 0.22 | -0.19 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=MIDDAY | 515 | 424 | 0.92 | 1.04 | 0.67 / 0.20 | 0.26 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=OPEN_90M | 373 | 312 | 1.11 | 1.07 | 0.68 / 0.20 | 0.28 | 0.05 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=HIGH_VOL | 791 | 571 | 0.96 | 1.05 | 0.67 / 0.20 | 0.26 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=LOW_VOL | 42 | 41 | 0.86 | 1.06 | 0.67 / 0.29 | 0.32 | 0.04 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=MID_VOL | 117 | 112 | 1.08 | 1.05 | 0.69 / 0.17 | 0.23 | -0.07 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=CLOSE_90M | 161 | 161 | 0.69 | 1.01 | 0.60 / 0.27 | 0.31 | -0.00 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=MIDDAY | 1232 | 750 | 0.85 | 1.05 | 0.63 / 0.23 | 0.27 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=OPEN_90M | 758 | 511 | 0.90 | 1.11 | 0.64 / 0.27 | 0.27 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=HIGH_VOL | 1610 | 855 | 0.86 | 1.06 | 0.63 / 0.24 | 0.28 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=LOW_VOL | 142 | 111 | 0.77 | 1.07 | 0.61 / 0.31 | 0.30 | -0.00 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=MID_VOL | 399 | 335 | 0.86 | 1.08 | 0.66 / 0.25 | 0.26 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=CLOSE_90M | 51 | 51 | 0.74 | 1.02 | 0.63 / 0.25 | 0.28 | 0.00 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=MIDDAY | 416 | 355 | 0.85 | 1.05 | 0.63 / 0.24 | 0.27 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=OPEN_90M | 302 | 285 | 0.74 | 1.19 | 0.56 / 0.37 | 0.25 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=HIGH_VOL | 411 | 354 | 0.81 | 1.09 | 0.63 / 0.25 | 0.26 | -0.05 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=LOW_VOL | 119 | 109 | 0.94 | 1.09 | 0.58 / 0.36 | 0.32 | 0.07 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=MID_VOL | 239 | 221 | 0.74 | 1.12 | 0.56 / 0.33 | 0.25 | -0.05 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=MIDDAY | 101 | 89 | 1.24 | 1.04 | 0.69 / 0.18 | 0.30 | 0.09 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=OPEN_90M | 156 | 153 | 0.79 | 1.09 | 0.64 / 0.24 | 0.22 | -0.07 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | regime=HIGH_VOL | 265 | 227 | 0.89 | 1.07 | 0.66 / 0.22 | 0.25 | -0.01 | USEFUL ENTRIES + BAD CAPTURE |
