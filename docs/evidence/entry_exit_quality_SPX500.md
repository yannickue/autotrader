# Lane X - SPX500

`eeq-1.0` hindsight diagnostics, no edge claim.

## SPX500

Data: 2025-05-02..2026-08-31 (94212 M5 bars; evaluated from 2025-06-01); 14 frozen family specs (EOD:continue, EOD:reverse, GAP:fade, GAP:go, LEADLAG:, ORB:breakout, ORB:fade, OVERNIGHT:continue, OVERNIGHT:reverse, ROUND:break, ROUND:reject, VOLREV:expand, VOLREV:fade); analysed entries n=2990.

Exclusions / counters: `{"analysed": 2990, "candidates": 2991, "no_post_entry_bars": 1}`

Entry quality (potential; R against the initial risk; MFE/MAE to the stop or the flat deadline):

| family:variant | dir | n | clusters | flag | MFE mean/med | MAE mean/med | P(MFE>=.25/.5/.75/1/1.5/2) | MFE-first / MAE-first | giveback | capture (floored) | useful / failure share | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 75 | 75 |  | 1.16/1.03 | 0.80/0.72 | 0.79/0.71/0.61/0.51/0.31/0.17 | 0.59 / 0.41 | 1.00 | 0.39 | 0.71 / 0.21 | NO CLEAR DEFICIT |
| EOD:continue | long | 55 | 55 |  | 1.09/1.03 | 0.82/0.72 | 0.78/0.69/0.60/0.51/0.29/0.13 | 0.58 / 0.42 | 1.00 | 0.37 | 0.69 / 0.22 | NO CLEAR DEFICIT |
| EOD:continue | short | 20 | 20 | n too small (<30): no conclusion | 1.35/0.94 | 0.74/0.76 | 0.80/0.75/0.65/0.50/0.35/0.30 | 0.60 / 0.40 | 1.03 | 0.45 | 0.75 / 0.20 | INCONCLUSIVE-n |
| EOD:reverse | ALL | 75 | 75 |  | 0.96/0.50 | 0.93/1.05 | 0.61/0.51/0.41/0.32/0.25/0.15 | 0.31 / 0.69 | 1.11 | 0.34 | 0.51 / 0.39 | NO CLEAR DEFICIT |
| EOD:reverse | long | 20 | 20 | n too small (<30): no conclusion | 0.81/0.59 | 0.85/0.93 | 0.65/0.60/0.45/0.25/0.15/0.10 | 0.30 / 0.70 | 0.96 | 0.35 | 0.60 / 0.35 | INCONCLUSIVE-n |
| EOD:reverse | short | 55 | 55 |  | 1.02/0.41 | 0.96/1.06 | 0.60/0.47/0.40/0.35/0.29/0.16 | 0.31 / 0.69 | 1.17 | 0.34 | 0.47 / 0.40 | BAD ENTRIES |
| GAP:fade | ALL | 74 | 74 |  | 2.47/1.23 | 1.20/1.15 | 0.68/0.62/0.57/0.54/0.42/0.36 | 0.36 / 0.64 | 2.42 | 0.24 | 0.62 / 0.32 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | long | 29 | 29 | n too small (<30): no conclusion | 1.87/0.52 | 1.10/1.07 | 0.59/0.52/0.45/0.45/0.38/0.34 | 0.41 / 0.59 | 1.92 | 0.25 | 0.52 / 0.41 | INCONCLUSIVE-n |
| GAP:fade | short | 45 | 45 |  | 2.85/1.30 | 1.26/1.19 | 0.73/0.69/0.64/0.60/0.44/0.38 | 0.33 / 0.67 | 2.74 | 0.24 | 0.69 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | ALL | 90 | 90 |  | 1.82/0.63 | 1.20/1.19 | 0.69/0.61/0.48/0.43/0.36/0.28 | 0.39 / 0.61 | 1.93 | 0.23 | 0.61 / 0.31 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | long | 58 | 58 |  | 2.05/1.05 | 1.14/1.18 | 0.79/0.71/0.59/0.52/0.41/0.31 | 0.47 / 0.53 | 2.01 | 0.25 | 0.71 / 0.21 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | short | 32 | 32 |  | 1.41/0.26 | 1.30/1.20 | 0.50/0.44/0.28/0.28/0.25/0.22 | 0.25 / 0.75 | 1.79 | 0.19 | 0.44 / 0.50 | BOTH |
| LEADLAG | ALL | 184 | 172 |  | 1.98/0.99 | 1.15/1.18 | 0.70/0.64/0.57/0.49/0.38/0.29 | 0.43 / 0.57 | 2.03 | 0.26 | 0.64 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | long | 80 | 77 |  | 1.79/0.93 | 1.12/1.21 | 0.71/0.64/0.56/0.47/0.35/0.29 | 0.47 / 0.53 | 1.92 | 0.24 | 0.64 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | short | 104 | 95 |  | 2.12/1.08 | 1.17/1.15 | 0.69/0.64/0.57/0.51/0.40/0.29 | 0.39 / 0.61 | 2.11 | 0.28 | 0.64 / 0.31 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | ALL | 322 | 322 |  | 1.12/0.77 | 0.81/0.96 | 0.76/0.63/0.50/0.41/0.27/0.17 | 0.47 / 0.53 | 1.10 | 0.32 | 0.63 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | long | 175 | 175 |  | 0.98/0.75 | 0.80/0.91 | 0.78/0.66/0.50/0.38/0.21/0.11 | 0.48 / 0.51 | 0.98 | 0.34 | 0.66 / 0.22 | NO CLEAR DEFICIT |
| ORB:breakout | short | 147 | 147 |  | 1.29/0.81 | 0.83/1.00 | 0.75/0.59/0.51/0.44/0.35/0.24 | 0.46 / 0.54 | 1.25 | 0.28 | 0.59 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | ALL | 265 | 265 |  | 1.89/0.94 | 1.18/1.12 | 0.72/0.65/0.55/0.47/0.39/0.31 | 0.44 / 0.56 | 1.89 | 0.26 | 0.65 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | long | 135 | 135 |  | 1.64/0.97 | 1.06/1.08 | 0.73/0.67/0.58/0.48/0.40/0.29 | 0.50 / 0.50 | 1.60 | 0.30 | 0.67 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | short | 130 | 130 |  | 2.16/0.85 | 1.31/1.19 | 0.72/0.62/0.52/0.46/0.38/0.33 | 0.37 / 0.63 | 2.19 | 0.23 | 0.62 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | ALL | 79 | 79 |  | 1.79/0.75 | 1.19/1.15 | 0.72/0.62/0.51/0.49/0.35/0.30 | 0.42 / 0.58 | 1.90 | 0.23 | 0.62 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | long | 54 | 54 |  | 1.87/1.13 | 1.17/1.14 | 0.80/0.69/0.56/0.54/0.37/0.31 | 0.50 / 0.50 | 1.94 | 0.22 | 0.69 / 0.20 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | short | 25 | 25 | n too small (<30): no conclusion | 1.61/0.39 | 1.24/1.17 | 0.56/0.48/0.40/0.40/0.32/0.28 | 0.24 / 0.76 | 1.81 | 0.24 | 0.48 / 0.44 | INCONCLUSIVE-n |
| OVERNIGHT:reverse | ALL | 79 | 79 |  | 2.42/1.23 | 1.15/1.17 | 0.70/0.65/0.56/0.52/0.43/0.39 | 0.37 / 0.63 | 2.34 | 0.23 | 0.65 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | long | 25 | 25 | n too small (<30): no conclusion | 2.91/2.67 | 0.94/1.00 | 0.80/0.76/0.64/0.64/0.60/0.56 | 0.56 / 0.44 | 2.41 | 0.29 | 0.76 / 0.20 | INCONCLUSIVE-n |
| OVERNIGHT:reverse | short | 54 | 54 |  | 2.19/0.86 | 1.24/1.21 | 0.65/0.59/0.52/0.46/0.35/0.31 | 0.28 / 0.72 | 2.31 | 0.20 | 0.59 / 0.35 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | ALL | 218 | 200 |  | 1.58/0.90 | 0.98/1.08 | 0.75/0.62/0.55/0.47/0.41/0.32 | 0.43 / 0.57 | 1.49 | 0.30 | 0.62 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | long | 104 | 95 |  | 1.33/0.82 | 0.96/1.08 | 0.77/0.61/0.52/0.42/0.40/0.28 | 0.38 / 0.62 | 1.25 | 0.34 | 0.61 / 0.23 | NO CLEAR DEFICIT |
| ROUND:break | short | 114 | 105 |  | 1.81/1.11 | 0.99/1.08 | 0.74/0.64/0.58/0.51/0.42/0.35 | 0.47 / 0.53 | 1.71 | 0.27 | 0.64 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | ALL | 490 | 347 |  | 1.35/0.81 | 1.07/1.09 | 0.74/0.62/0.51/0.46/0.34/0.26 | 0.42 / 0.58 | 1.41 | 0.28 | 0.62 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | long | 243 | 175 |  | 1.26/0.80 | 1.06/1.06 | 0.74/0.62/0.51/0.45/0.34/0.26 | 0.44 / 0.56 | 1.31 | 0.27 | 0.62 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | short | 247 | 172 |  | 1.44/0.89 | 1.08/1.09 | 0.74/0.62/0.52/0.47/0.35/0.25 | 0.41 / 0.59 | 1.51 | 0.28 | 0.62 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | ALL | 798 | 704 |  | 1.60/0.81 | 1.06/1.09 | 0.72/0.62/0.52/0.46/0.36/0.28 | 0.41 / 0.59 | 1.61 | 0.28 | 0.62 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | long | 459 | 392 |  | 1.41/0.81 | 1.03/1.08 | 0.73/0.60/0.52/0.46/0.35/0.26 | 0.42 / 0.58 | 1.41 | 0.29 | 0.60 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | short | 339 | 312 |  | 1.85/0.82 | 1.10/1.11 | 0.71/0.63/0.53/0.46/0.38/0.29 | 0.38 / 0.62 | 1.87 | 0.26 | 0.63 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | ALL | 241 | 222 |  | 1.55/0.83 | 1.03/1.08 | 0.78/0.64/0.53/0.48/0.35/0.28 | 0.45 / 0.55 | 1.58 | 0.26 | 0.64 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | long | 140 | 125 |  | 1.31/0.81 | 1.04/1.09 | 0.77/0.64/0.53/0.47/0.32/0.23 | 0.46 / 0.54 | 1.39 | 0.28 | 0.64 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | short | 101 | 97 |  | 1.89/0.98 | 1.01/1.07 | 0.78/0.63/0.53/0.50/0.39/0.36 | 0.43 / 0.57 | 1.86 | 0.22 | 0.63 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |

Exit-policy shadow comparison on the SAME entries (mean R, spread-adjusted gross; in brackets: entries the policy applies to; NA = no structural level):

| family:variant | dir | n | P1 FIXED_1_5R | P2 STRUCT_TP1 | P3 STRUCT_TP1_TP2 | P4 TP1_TP2_RUNNER | P5 STRUCT_TRAIL | P6 BREAKEVEN_LOCK | P7 MOMENTUM_EXIT | P8 TIME_ALPHA | P9 EOD_FORCED_FLAT | paired-structural n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 75 | +0.16(75) | +0.07(56) | +0.08(24) | +0.09(56) | +0.17(75) | +0.12(75) | +0.16(75) | +0.16(75) | +0.16(75) | 24 |
| EOD:continue | long | 55 | +0.10(55) | +0.01(39) | +0.30(13) | +0.11(39) | +0.22(55) | +0.08(55) | +0.07(55) | +0.10(55) | +0.07(55) | 13 |
| EOD:continue | short | 20 | +0.32(20) | +0.21(17) | -0.18(11) | +0.05(17) | +0.05(20) | +0.24(20) | +0.41(20) | +0.32(20) | +0.39(20) | 11 |
| EOD:reverse | ALL | 75 | -0.15(75) | -0.22(75) | -0.17(75) | -0.19(75) | -0.15(75) | -0.18(75) | -0.10(75) | -0.15(75) | -0.17(75) | 75 |
| EOD:reverse | long | 20 | -0.15(20) | -0.13(20) | -0.09(20) | -0.18(20) | -0.15(20) | -0.15(20) | -0.18(20) | -0.15(20) | -0.12(20) | 20 |
| EOD:reverse | short | 55 | -0.15(55) | -0.26(55) | -0.21(55) | -0.19(55) | -0.14(55) | -0.20(55) | -0.07(55) | -0.15(55) | -0.19(55) | 55 |
| GAP:fade | ALL | 74 | +0.05(74) | -0.13(70) | -0.01(51) | -0.06(70) | +0.24(74) | +0.10(74) | +0.09(74) | +0.05(74) | +0.31(74) | 51 |
| GAP:fade | long | 29 | -0.05(29) | -0.20(28) | -0.09(21) | -0.11(28) | +0.22(29) | -0.03(29) | +0.02(29) | -0.05(29) | +0.17(29) | 21 |
| GAP:fade | short | 45 | +0.11(45) | -0.08(42) | +0.05(30) | -0.03(42) | +0.25(45) | +0.18(45) | +0.13(45) | +0.11(45) | +0.40(45) | 30 |
| GAP:go | ALL | 90 | -0.11(90) | -0.19(65) | -0.14(26) | +0.03(65) | +0.05(90) | -0.08(90) | -0.06(90) | -0.11(90) | +0.13(90) | 26 |
| GAP:go | long | 58 | +0.03(58) | -0.05(42) | +0.01(14) | +0.24(42) | +0.26(58) | +0.09(58) | +0.10(58) | +0.03(58) | +0.30(58) | 14 |
| GAP:go | short | 32 | -0.38(32) | -0.44(23) | -0.31(12) | -0.37(23) | -0.32(32) | -0.39(32) | -0.35(32) | -0.38(32) | -0.19(32) | 12 |
| LEADLAG | ALL | 184 | -0.05(184) | -0.05(171) | -0.02(125) | -0.02(171) | +0.11(184) | -0.04(184) | +0.00(184) | -0.05(184) | +0.08(184) | 125 |
| LEADLAG | long | 80 | -0.12(80) | +0.01(69) | -0.03(41) | +0.01(69) | +0.07(80) | -0.07(80) | +0.02(80) | -0.12(80) | +0.22(80) | 41 |
| LEADLAG | short | 104 | +0.01(104) | -0.09(102) | -0.02(84) | -0.04(102) | +0.15(104) | -0.02(104) | -0.01(104) | +0.01(104) | -0.03(104) | 84 |
| ORB:breakout | ALL | 322 | +0.02(322) | -0.04(236) | -0.06(131) | -0.00(236) | +0.03(322) | +0.02(322) | +0.03(322) | +0.02(322) | +0.03(322) | 131 |
| ORB:breakout | long | 175 | +0.00(175) | -0.01(119) | -0.01(62) | +0.01(119) | +0.04(175) | -0.02(175) | +0.05(175) | -0.00(175) | +0.01(175) | 62 |
| ORB:breakout | short | 147 | +0.04(147) | -0.06(117) | -0.10(69) | -0.02(117) | +0.03(147) | +0.07(147) | -0.00(147) | +0.04(147) | +0.06(147) | 69 |
| ORB:fade | ALL | 265 | +0.00(265) | -0.02(263) | +0.03(246) | +0.02(263) | +0.02(265) | +0.01(265) | +0.01(265) | -0.00(265) | +0.02(265) | 246 |
| ORB:fade | long | 135 | +0.04(135) | -0.04(133) | +0.05(120) | +0.05(133) | -0.01(135) | +0.02(135) | +0.06(135) | +0.04(135) | -0.02(135) | 120 |
| ORB:fade | short | 130 | -0.03(130) | -0.00(130) | +0.01(126) | -0.01(130) | +0.05(130) | -0.00(130) | -0.03(130) | -0.04(130) | +0.06(130) | 126 |
| OVERNIGHT:continue | ALL | 79 | -0.11(79) | -0.16(69) | -0.15(48) | -0.01(69) | -0.03(79) | -0.06(79) | -0.01(79) | -0.11(79) | +0.13(79) | 48 |
| OVERNIGHT:continue | long | 54 | -0.07(54) | -0.13(47) | -0.21(31) | +0.13(47) | +0.13(54) | +0.02(54) | +0.06(54) | -0.07(54) | +0.12(54) | 31 |
| OVERNIGHT:continue | short | 25 | -0.20(25) | -0.22(22) | -0.03(17) | -0.30(22) | -0.39(25) | -0.23(25) | -0.15(25) | -0.20(25) | +0.16(25) | 17 |
| OVERNIGHT:reverse | ALL | 79 | +0.08(79) | +0.05(78) | +0.15(65) | +0.08(78) | +0.44(79) | +0.15(79) | +0.07(79) | +0.08(79) | +0.33(79) | 65 |
| OVERNIGHT:reverse | long | 25 | +0.50(25) | +0.28(25) | +0.42(22) | +0.40(25) | +1.25(25) | +0.54(25) | +0.50(25) | +0.50(25) | +0.92(25) | 22 |
| OVERNIGHT:reverse | short | 54 | -0.12(54) | -0.05(53) | +0.01(43) | -0.07(53) | +0.07(54) | -0.03(54) | -0.13(54) | -0.12(54) | +0.05(54) | 43 |
| ROUND:break | ALL | 218 | +0.09(218) | -0.06(194) | -0.12(109) | -0.01(194) | +0.03(218) | +0.04(218) | +0.09(218) | +0.09(218) | +0.06(218) | 109 |
| ROUND:break | long | 104 | +0.08(104) | -0.19(86) | -0.27(49) | -0.13(86) | -0.05(104) | +0.00(104) | +0.06(104) | +0.08(104) | -0.00(104) | 49 |
| ROUND:break | short | 114 | +0.10(114) | +0.04(108) | -0.00(60) | +0.08(108) | +0.10(114) | +0.08(114) | +0.12(114) | +0.10(114) | +0.13(114) | 60 |
| ROUND:reject | ALL | 490 | -0.06(490) | -0.12(459) | -0.08(362) | -0.09(459) | -0.13(490) | -0.03(490) | -0.04(490) | -0.06(490) | -0.22(490) | 362 |
| ROUND:reject | long | 243 | -0.05(243) | -0.10(221) | -0.10(174) | -0.07(221) | -0.11(243) | -0.03(243) | +0.02(243) | -0.05(243) | -0.19(243) | 174 |
| ROUND:reject | short | 247 | -0.07(247) | -0.14(238) | -0.07(188) | -0.11(238) | -0.14(247) | -0.04(247) | -0.09(247) | -0.07(247) | -0.25(247) | 188 |
| VOLREV:expand | ALL | 798 | -0.01(798) | -0.12(461) | -0.15(251) | -0.08(461) | +0.02(798) | -0.04(798) | -0.01(798) | -0.01(798) | +0.05(798) | 251 |
| VOLREV:expand | long | 459 | +0.00(459) | -0.09(220) | -0.20(111) | -0.05(220) | +0.07(459) | -0.02(459) | +0.01(459) | +0.00(459) | +0.07(459) | 111 |
| VOLREV:expand | short | 339 | -0.02(339) | -0.15(241) | -0.10(140) | -0.11(241) | -0.04(339) | -0.06(339) | -0.03(339) | -0.02(339) | +0.03(339) | 140 |
| VOLREV:fade | ALL | 241 | -0.03(241) | -0.03(241) | -0.06(240) | -0.04(241) | +0.07(241) | -0.04(241) | -0.04(241) | -0.04(241) | -0.01(241) | 240 |
| VOLREV:fade | long | 140 | -0.08(140) | -0.10(140) | -0.13(140) | -0.07(140) | -0.09(140) | -0.07(140) | +0.02(140) | -0.08(140) | -0.19(140) | 140 |
| VOLREV:fade | short | 101 | +0.04(101) | +0.05(101) | +0.04(100) | +0.01(101) | +0.30(101) | -0.00(101) | -0.11(101) | +0.02(101) | +0.25(101) | 100 |

Timing / structure / cost state (medians; times dated by bar open): 

| family:variant | dir | n | t_MFE s | t_MAE s | t to 0.5R s | t to 1R s | bars above entry | max structure reached R | TP1 / TP2 reach | holding s | spread/1R | P1 exits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 75 | 3600 | 1800 | 600 | 2700 | 0.66 | 0.26 | 0.71 / 0.54 | 6600 | 0.075 | STOP 0.41, TP1 0.31, FORCED_FLAT 0.28 |
| EOD:reverse | ALL | 75 | 2400 | 1200 | 1200 | 2550 | 0.38 | 0.00 | 0.41 / 0.28 | 4500 | 0.081 | STOP 0.59, TP1 0.25, FORCED_FLAT 0.16 |
| GAP:fade | ALL | 74 | 1800 | 300 | 0 | 300 | 0.69 | 0.35 | 0.63 / 0.53 | 2550 | 0.049 | STOP 0.58, TP1 0.42 |
| GAP:go | ALL | 90 | 900 | 450 | 0 | 300 | 0.64 | 0.13 | 0.58 / 0.54 | 900 | 0.049 | STOP 0.64, TP1 0.36 |
| LEADLAG | ALL | 184 | 1500 | 600 | 0 | 300 | 0.62 | 0.36 | 0.61 / 0.50 | 2100 | 0.058 | STOP 0.62, TP1 0.38 |
| ORB:breakout | ALL | 322 | 3900 | 2700 | 1200 | 3150 | 0.53 | 0.11 | 0.82 / 0.60 | 17550 | 0.023 | STOP 0.45, TP1 0.27, FORCED_FLAT 0.25 |
| ORB:fade | ALL | 265 | 900 | 600 | 0 | 600 | 0.57 | 0.47 | 0.57 / 0.46 | 1800 | 0.056 | STOP 0.58, TP1 0.39, FORCED_FLAT 0.02 |
| OVERNIGHT:continue | ALL | 79 | 600 | 300 | 0 | 300 | 0.62 | 0.19 | 0.57 / 0.42 | 1200 | 0.044 | STOP 0.65, TP1 0.35 |
| OVERNIGHT:reverse | ALL | 79 | 1800 | 300 | 0 | 300 | 0.64 | 0.45 | 0.60 / 0.48 | 2100 | 0.046 | STOP 0.57, TP1 0.43 |
| ROUND:break | ALL | 218 | 1800 | 1200 | 600 | 1200 | 0.54 | 0.26 | 0.63 / 0.41 | 3600 | 0.041 | STOP 0.53, TP1 0.41, FORCED_FLAT 0.06 |
| ROUND:reject | ALL | 490 | 1500 | 1200 | 300 | 1200 | 0.51 | 0.24 | 0.58 / 0.41 | 3600 | 0.046 | STOP 0.59, TP1 0.34, FORCED_FLAT 0.07 |
| VOLREV:expand | ALL | 798 | 1500 | 900 | 300 | 900 | 0.54 | 0.11 | 0.62 / 0.39 | 3300 | 0.060 | STOP 0.57, TP1 0.36, FORCED_FLAT 0.07 |
| VOLREV:fade | ALL | 241 | 1800 | 1500 | 300 | 1200 | 0.52 | 0.00 | 0.46 / 0.30 | 4200 | 0.038 | STOP 0.58, TP1 0.35, FORCED_FLAT 0.07 |

Profit capture per policy (mean floored capture ratio = share of the MFE kept; same entries):

| family:variant | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9 |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | 0.39 | 0.31 | 0.26 | 0.20 | 0.29 | 0.34 | 0.29 | 0.39 | 0.37 |
| EOD:reverse | 0.34 | 0.28 | 0.30 | 0.14 | 0.22 | 0.32 | 0.24 | 0.34 | 0.27 |
| GAP:fade | 0.24 | 0.22 | 0.16 | 0.18 | 0.17 | 0.22 | 0.21 | 0.24 | 0.19 |
| GAP:go | 0.23 | 0.21 | 0.27 | 0.20 | 0.18 | 0.21 | 0.22 | 0.23 | 0.22 |
| LEADLAG | 0.26 | 0.26 | 0.23 | 0.21 | 0.17 | 0.22 | 0.23 | 0.26 | 0.19 |
| ORB:breakout | 0.32 | 0.19 | 0.21 | 0.14 | 0.22 | 0.29 | 0.19 | 0.30 | 0.30 |
| ORB:fade | 0.26 | 0.23 | 0.22 | 0.18 | 0.18 | 0.24 | 0.20 | 0.26 | 0.20 |
| OVERNIGHT:continue | 0.23 | 0.20 | 0.21 | 0.17 | 0.15 | 0.19 | 0.21 | 0.23 | 0.22 |
| OVERNIGHT:reverse | 0.23 | 0.23 | 0.20 | 0.19 | 0.23 | 0.23 | 0.19 | 0.23 | 0.22 |
| ROUND:break | 0.30 | 0.24 | 0.15 | 0.17 | 0.18 | 0.26 | 0.24 | 0.30 | 0.24 |
| ROUND:reject | 0.28 | 0.23 | 0.20 | 0.15 | 0.17 | 0.24 | 0.21 | 0.27 | 0.17 |
| VOLREV:expand | 0.28 | 0.23 | 0.16 | 0.18 | 0.20 | 0.24 | 0.24 | 0.28 | 0.24 |
| VOLREV:fade | 0.26 | 0.25 | 0.20 | 0.14 | 0.18 | 0.21 | 0.17 | 0.25 | 0.21 |

Session / regime cells with n >= 30:

| family:variant | cell | n | clusters | MFE med | MAE med | useful / failure | capture | baseline R | verdict |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | session=MIDDAY | 75 | 75 | 1.03 | 0.72 | 0.71 / 0.21 | 0.39 | 0.16 | NO CLEAR DEFICIT |
| EOD:continue | regime=LOW_VOL | 36 | 36 | 1.08 | 0.70 | 0.75 / 0.19 | 0.45 | 0.26 | NO CLEAR DEFICIT |
| EOD:reverse | session=MIDDAY | 75 | 75 | 0.50 | 1.05 | 0.51 / 0.39 | 0.34 | -0.15 | NO CLEAR DEFICIT |
| EOD:reverse | regime=LOW_VOL | 36 | 36 | 0.32 | 1.06 | 0.42 / 0.44 | 0.23 | -0.33 | BOTH |
| GAP:fade | session=OPEN_90M | 74 | 74 | 1.23 | 1.15 | 0.62 / 0.32 | 0.24 | 0.05 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | regime=MID_VOL | 34 | 34 | 1.26 | 1.09 | 0.65 / 0.32 | 0.21 | 0.03 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | session=OPEN_90M | 90 | 90 | 0.63 | 1.19 | 0.61 / 0.31 | 0.23 | -0.11 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | regime=HIGH_VOL | 42 | 42 | 0.55 | 1.19 | 0.60 / 0.33 | 0.31 | -0.17 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | regime=MID_VOL | 40 | 40 | 0.85 | 1.20 | 0.62 / 0.30 | 0.17 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | session=OPEN_90M | 156 | 145 | 1.03 | 1.19 | 0.64 / 0.31 | 0.26 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | regime=HIGH_VOL | 60 | 60 | 1.08 | 1.07 | 0.72 / 0.25 | 0.25 | 0.00 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | regime=LOW_VOL | 36 | 35 | 0.49 | 1.21 | 0.47 / 0.39 | 0.16 | -0.31 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | regime=MID_VOL | 88 | 85 | 1.07 | 1.23 | 0.66 / 0.30 | 0.31 | 0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | session=OPEN_90M | 313 | 313 | 0.78 | 0.96 | 0.63 / 0.23 | 0.32 | 0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=HIGH_VOL | 197 | 197 | 0.67 | 0.99 | 0.59 / 0.23 | 0.28 | -0.05 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=MID_VOL | 110 | 110 | 0.94 | 0.83 | 0.68 / 0.21 | 0.39 | 0.15 | NO CLEAR DEFICIT |
| ORB:fade | session=OPEN_90M | 251 | 251 | 0.96 | 1.12 | 0.65 / 0.27 | 0.26 | 0.00 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=HIGH_VOL | 152 | 152 | 1.12 | 1.09 | 0.66 / 0.26 | 0.28 | 0.10 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=MID_VOL | 98 | 98 | 0.76 | 1.15 | 0.63 / 0.29 | 0.25 | -0.13 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | session=OPEN_90M | 79 | 79 | 0.75 | 1.15 | 0.62 / 0.28 | 0.23 | -0.11 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | regime=HIGH_VOL | 34 | 34 | 0.62 | 1.17 | 0.56 / 0.32 | 0.32 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | regime=MID_VOL | 36 | 36 | 1.08 | 1.16 | 0.69 / 0.25 | 0.17 | -0.17 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | session=OPEN_90M | 79 | 79 | 1.23 | 1.17 | 0.65 / 0.30 | 0.23 | 0.08 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | regime=HIGH_VOL | 34 | 34 | 1.57 | 1.22 | 0.68 / 0.26 | 0.27 | 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | regime=MID_VOL | 36 | 36 | 0.99 | 1.07 | 0.61 / 0.36 | 0.18 | -0.10 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=MIDDAY | 122 | 114 | 0.86 | 1.06 | 0.64 / 0.25 | 0.28 | 0.08 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=OPEN_90M | 79 | 76 | 1.30 | 1.09 | 0.61 / 0.25 | 0.35 | 0.18 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=HIGH_VOL | 162 | 147 | 0.91 | 1.06 | 0.62 / 0.23 | 0.29 | 0.09 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=MID_VOL | 47 | 46 | 0.72 | 1.11 | 0.62 / 0.28 | 0.30 | 0.07 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=MIDDAY | 276 | 194 | 0.83 | 1.06 | 0.62 / 0.25 | 0.29 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=OPEN_90M | 185 | 154 | 0.90 | 1.12 | 0.62 / 0.24 | 0.27 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=HIGH_VOL | 355 | 259 | 0.78 | 1.08 | 0.61 / 0.25 | 0.29 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=MID_VOL | 108 | 89 | 0.90 | 1.12 | 0.66 / 0.25 | 0.23 | -0.08 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=CLOSE_90M | 64 | 64 | 0.67 | 1.08 | 0.58 / 0.36 | 0.25 | -0.20 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=MIDDAY | 430 | 397 | 0.82 | 1.05 | 0.63 / 0.26 | 0.29 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=OPEN_90M | 304 | 281 | 0.85 | 1.14 | 0.60 / 0.30 | 0.26 | -0.00 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=HIGH_VOL | 315 | 297 | 0.88 | 1.08 | 0.65 / 0.25 | 0.29 | 0.00 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=LOW_VOL | 162 | 152 | 0.65 | 1.13 | 0.56 / 0.36 | 0.30 | -0.08 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=MID_VOL | 321 | 299 | 0.81 | 1.08 | 0.61 / 0.27 | 0.26 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=MIDDAY | 108 | 100 | 0.75 | 1.07 | 0.64 / 0.19 | 0.27 | -0.09 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=OPEN_90M | 118 | 117 | 1.07 | 1.09 | 0.64 / 0.25 | 0.24 | 0.03 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | regime=HIGH_VOL | 241 | 222 | 0.83 | 1.08 | 0.64 / 0.22 | 0.26 | -0.03 | USEFUL ENTRIES + BAD CAPTURE |
