# Lane X - GER40

`eeq-1.0` hindsight diagnostics, no edge claim.

## GER40

Data: 2025-02-10..2026-08-31 (95011 M5 bars; evaluated from 2025-03-12); 14 frozen family specs (EOD:continue, EOD:reverse, GAP:fade, GAP:go, LEADLAG:, ORB:breakout, ORB:fade, OVERNIGHT:continue, OVERNIGHT:reverse, ROUND:break, ROUND:reject, VOLREV:expand, VOLREV:fade); analysed entries n=9644.

Exclusions / counters: `{"analysed": 9644, "candidates": 9645, "no_post_entry_bars": 1}`

Entry quality (potential; R against the initial risk; MFE/MAE to the stop or the flat deadline):

| family:variant | dir | n | clusters | flag | MFE mean/med | MAE mean/med | P(MFE>=.25/.5/.75/1/1.5/2) | MFE-first / MAE-first | giveback | capture (floored) | useful / failure share | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 95 | 95 |  | 1.27/0.85 | 0.85/0.95 | 0.73/0.64/0.53/0.45/0.36/0.23 | 0.39 / 0.61 | 1.18 | 0.33 | 0.64 / 0.27 | NO CLEAR DEFICIT |
| EOD:continue | long | 54 | 54 |  | 1.03/0.76 | 0.93/1.00 | 0.69/0.61/0.50/0.41/0.31/0.19 | 0.33 / 0.67 | 1.05 | 0.32 | 0.61 / 0.31 | NO CLEAR DEFICIT |
| EOD:continue | short | 41 | 41 |  | 1.59/1.07 | 0.75/0.76 | 0.78/0.68/0.56/0.51/0.41/0.29 | 0.46 / 0.54 | 1.35 | 0.33 | 0.68 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | ALL | 95 | 95 |  | 1.10/0.76 | 0.88/1.02 | 0.80/0.68/0.52/0.42/0.25/0.14 | 0.56 / 0.44 | 1.24 | 0.25 | 0.68 / 0.20 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | long | 41 | 41 |  | 0.90/0.70 | 0.87/1.06 | 0.76/0.61/0.41/0.34/0.17/0.07 | 0.51 / 0.49 | 1.17 | 0.23 | 0.61 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | short | 54 | 54 |  | 1.24/0.92 | 0.89/1.01 | 0.83/0.74/0.59/0.48/0.31/0.19 | 0.59 / 0.41 | 1.30 | 0.26 | 0.74 / 0.17 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | ALL | 92 | 92 |  | 2.49/0.94 | 1.12/1.13 | 0.83/0.68/0.58/0.49/0.43/0.36 | 0.45 / 0.55 | 2.41 | 0.21 | 0.68 / 0.17 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | long | 42 | 42 |  | 2.37/0.94 | 1.14/1.15 | 0.81/0.62/0.57/0.48/0.40/0.29 | 0.45 / 0.55 | 2.36 | 0.22 | 0.62 / 0.19 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | short | 50 | 50 |  | 2.60/1.11 | 1.10/1.11 | 0.84/0.74/0.58/0.50/0.46/0.42 | 0.44 / 0.56 | 2.45 | 0.21 | 0.74 / 0.16 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | ALL | 87 | 87 |  | 2.35/1.22 | 1.12/1.11 | 0.80/0.68/0.59/0.54/0.46/0.39 | 0.48 / 0.52 | 2.20 | 0.25 | 0.68 / 0.20 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | long | 55 | 55 |  | 2.31/1.42 | 1.13/1.11 | 0.78/0.71/0.62/0.58/0.47/0.36 | 0.55 / 0.45 | 2.13 | 0.29 | 0.71 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | short | 32 | 32 |  | 2.43/0.85 | 1.11/1.12 | 0.84/0.62/0.53/0.47/0.44/0.44 | 0.38 / 0.62 | 2.33 | 0.19 | 0.62 / 0.16 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | ALL | 453 | 294 |  | 1.43/0.65 | 1.07/1.09 | 0.68/0.58/0.47/0.39/0.30/0.25 | 0.42 / 0.58 | 1.63 | 0.22 | 0.58 / 0.32 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | long | 212 | 140 |  | 1.46/0.66 | 1.02/1.08 | 0.69/0.59/0.49/0.42/0.31/0.26 | 0.42 / 0.57 | 1.61 | 0.22 | 0.59 / 0.31 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | short | 241 | 154 |  | 1.41/0.60 | 1.12/1.10 | 0.66/0.56/0.45/0.37/0.29/0.23 | 0.41 / 0.59 | 1.65 | 0.21 | 0.56 / 0.34 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | ALL | 370 | 370 |  | 1.33/0.92 | 0.85/1.02 | 0.78/0.63/0.55/0.47/0.31/0.23 | 0.49 / 0.51 | 1.36 | 0.25 | 0.63 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | long | 183 | 183 |  | 1.26/0.84 | 0.88/1.03 | 0.79/0.64/0.55/0.42/0.28/0.20 | 0.49 / 0.51 | 1.37 | 0.24 | 0.64 / 0.21 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | short | 187 | 187 |  | 1.39/1.04 | 0.82/1.01 | 0.77/0.62/0.55/0.52/0.35/0.26 | 0.50 / 0.50 | 1.35 | 0.27 | 0.62 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | ALL | 311 | 311 |  | 2.40/0.90 | 1.25/1.19 | 0.72/0.63/0.56/0.49/0.41/0.31 | 0.49 / 0.51 | 2.37 | 0.25 | 0.63 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | long | 166 | 166 |  | 2.09/0.97 | 1.25/1.21 | 0.72/0.63/0.55/0.50/0.43/0.33 | 0.48 / 0.52 | 2.02 | 0.26 | 0.63 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | short | 145 | 145 |  | 2.75/0.90 | 1.25/1.18 | 0.71/0.63/0.56/0.48/0.39/0.28 | 0.49 / 0.51 | 2.77 | 0.23 | 0.63 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | ALL | 93 | 93 |  | 2.27/1.42 | 1.20/1.13 | 0.83/0.69/0.59/0.53/0.49/0.34 | 0.47 / 0.53 | 2.04 | 0.29 | 0.69 / 0.17 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | long | 55 | 55 |  | 2.24/1.63 | 1.17/1.17 | 0.82/0.78/0.65/0.60/0.56/0.35 | 0.53 / 0.47 | 1.83 | 0.37 | 0.78 / 0.18 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | short | 38 | 38 |  | 2.32/0.75 | 1.24/1.10 | 0.84/0.55/0.50/0.42/0.39/0.34 | 0.39 / 0.61 | 2.34 | 0.18 | 0.55 / 0.16 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | ALL | 93 | 93 |  | 2.09/0.83 | 1.15/1.14 | 0.77/0.65/0.53/0.46/0.40/0.33 | 0.42 / 0.58 | 2.10 | 0.24 | 0.65 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | long | 38 | 38 |  | 1.93/1.32 | 1.16/1.18 | 0.87/0.74/0.63/0.55/0.45/0.34 | 0.53 / 0.47 | 1.81 | 0.26 | 0.74 / 0.13 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | short | 55 | 55 |  | 2.20/0.65 | 1.14/1.09 | 0.71/0.58/0.45/0.40/0.36/0.33 | 0.35 / 0.65 | 2.29 | 0.21 | 0.58 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | ALL | 1733 | 1307 |  | 1.79/0.85 | 1.07/1.09 | 0.75/0.62/0.53/0.47/0.35/0.28 | 0.44 / 0.56 | 1.85 | 0.23 | 0.62 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | long | 874 | 672 |  | 1.70/0.81 | 1.05/1.09 | 0.75/0.62/0.52/0.46/0.34/0.26 | 0.46 / 0.54 | 1.77 | 0.24 | 0.62 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | short | 859 | 635 |  | 1.89/0.91 | 1.09/1.09 | 0.76/0.63/0.55/0.47/0.36/0.29 | 0.42 / 0.58 | 1.93 | 0.23 | 0.63 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | ALL | 4023 | 2246 |  | 1.73/0.89 | 1.11/1.10 | 0.77/0.64/0.54/0.47/0.36/0.29 | 0.45 / 0.55 | 1.78 | 0.23 | 0.64 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | long | 2033 | 1116 |  | 1.62/0.85 | 1.11/1.11 | 0.76/0.63/0.53/0.45/0.35/0.27 | 0.46 / 0.54 | 1.69 | 0.24 | 0.63 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | short | 1990 | 1130 |  | 1.85/0.93 | 1.11/1.10 | 0.77/0.64/0.56/0.48/0.36/0.30 | 0.43 / 0.57 | 1.88 | 0.22 | 0.64 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | ALL | 1688 | 1389 |  | 1.93/0.92 | 1.11/1.12 | 0.75/0.65/0.55/0.48/0.37/0.31 | 0.45 / 0.55 | 1.97 | 0.22 | 0.65 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | long | 905 | 731 |  | 1.89/0.92 | 1.09/1.12 | 0.76/0.65/0.56/0.48/0.37/0.30 | 0.46 / 0.54 | 1.90 | 0.24 | 0.65 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | short | 783 | 658 |  | 1.99/0.91 | 1.13/1.13 | 0.75/0.65/0.54/0.48/0.37/0.31 | 0.44 / 0.56 | 2.04 | 0.21 | 0.65 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | ALL | 511 | 443 |  | 1.72/0.95 | 1.06/1.08 | 0.78/0.67/0.56/0.49/0.36/0.28 | 0.49 / 0.51 | 1.76 | 0.24 | 0.67 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | long | 270 | 236 |  | 1.79/1.03 | 1.00/1.07 | 0.79/0.70/0.59/0.51/0.40/0.31 | 0.53 / 0.47 | 1.72 | 0.26 | 0.70 / 0.21 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | short | 241 | 207 |  | 1.65/0.83 | 1.13/1.09 | 0.76/0.63/0.53/0.46/0.32/0.25 | 0.44 / 0.56 | 1.79 | 0.22 | 0.63 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |

Exit-policy shadow comparison on the SAME entries (mean R, spread-adjusted gross; in brackets: entries the policy applies to; NA = no structural level):

| family:variant | dir | n | P1 FIXED_1_5R | P2 STRUCT_TP1 | P3 STRUCT_TP1_TP2 | P4 TP1_TP2_RUNNER | P5 STRUCT_TRAIL | P6 BREAKEVEN_LOCK | P7 MOMENTUM_EXIT | P8 TIME_ALPHA | P9 EOD_FORCED_FLAT | paired-structural n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 95 | +0.09(95) | -0.09(90) | -0.07(38) | -0.08(90) | +0.06(95) | +0.12(95) | -0.03(95) | +0.07(95) | +0.12(95) | 38 |
| EOD:continue | long | 54 | -0.02(54) | -0.17(50) | -0.08(22) | -0.17(50) | -0.04(54) | -0.01(54) | -0.11(54) | -0.04(54) | -0.06(54) | 22 |
| EOD:continue | short | 41 | +0.24(41) | +0.02(40) | -0.05(16) | +0.04(40) | +0.20(41) | +0.29(41) | +0.07(41) | +0.22(41) | +0.36(41) | 16 |
| EOD:reverse | ALL | 95 | -0.15(95) | -0.13(95) | -0.12(95) | -0.01(95) | -0.15(95) | -0.15(95) | -0.12(95) | -0.12(95) | -0.23(95) | 95 |
| EOD:reverse | long | 41 | -0.26(41) | -0.26(41) | -0.21(41) | -0.16(41) | -0.24(41) | -0.24(41) | -0.25(41) | -0.22(41) | -0.28(41) | 41 |
| EOD:reverse | short | 54 | -0.06(54) | -0.04(54) | -0.05(54) | +0.10(54) | -0.09(54) | -0.07(54) | -0.02(54) | -0.03(54) | -0.18(54) | 54 |
| GAP:fade | ALL | 92 | +0.09(92) | +0.06(92) | +0.04(86) | +0.03(92) | +0.16(92) | -0.00(92) | -0.01(92) | +0.09(92) | +0.27(92) | 86 |
| GAP:fade | long | 42 | +0.01(42) | +0.04(42) | +0.02(40) | +0.02(42) | +0.09(42) | -0.02(42) | -0.00(42) | +0.01(42) | +0.29(42) | 40 |
| GAP:fade | short | 50 | +0.15(50) | +0.07(50) | +0.06(46) | +0.03(50) | +0.22(50) | +0.01(50) | -0.01(50) | +0.15(50) | +0.25(50) | 46 |
| GAP:go | ALL | 87 | +0.15(87) | -0.06(75) | +0.15(15) | -0.05(75) | +0.11(87) | +0.06(87) | +0.01(87) | +0.15(87) | +0.17(87) | 15 |
| GAP:go | long | 55 | +0.18(55) | -0.05(45) | -0.00(10) | -0.01(45) | +0.10(55) | +0.19(55) | +0.04(55) | +0.18(55) | +0.15(55) | 10 |
| GAP:go | short | 32 | +0.09(32) | -0.07(30) | +0.45(5) | -0.12(30) | +0.13(32) | -0.15(32) | -0.03(32) | +0.09(32) | +0.22(32) | 5 |
| LEADLAG | ALL | 453 | -0.20(453) | -0.14(450) | -0.17(361) | -0.11(450) | -0.11(453) | -0.23(453) | -0.21(453) | -0.20(453) | -0.20(453) | 361 |
| LEADLAG | long | 212 | -0.15(212) | -0.08(210) | -0.14(169) | -0.03(210) | +0.01(212) | -0.22(212) | -0.18(212) | -0.15(212) | -0.06(212) | 169 |
| LEADLAG | short | 241 | -0.24(241) | -0.20(240) | -0.20(192) | -0.17(240) | -0.21(241) | -0.23(241) | -0.24(241) | -0.24(241) | -0.33(241) | 192 |
| ORB:breakout | ALL | 370 | -0.03(370) | -0.03(289) | -0.01(159) | -0.02(289) | -0.00(370) | -0.03(370) | -0.02(370) | -0.03(370) | +0.02(370) | 159 |
| ORB:breakout | long | 183 | -0.10(183) | +0.00(129) | +0.04(66) | -0.00(129) | -0.04(183) | -0.10(183) | -0.04(183) | -0.12(183) | +0.02(183) | 66 |
| ORB:breakout | short | 187 | +0.04(187) | -0.05(160) | -0.05(93) | -0.03(160) | +0.03(187) | +0.03(187) | +0.01(187) | +0.06(187) | +0.03(187) | 93 |
| ORB:fade | ALL | 311 | +0.03(311) | -0.07(311) | -0.06(296) | -0.02(311) | +0.04(311) | +0.02(311) | +0.05(311) | +0.02(311) | +0.19(311) | 296 |
| ORB:fade | long | 166 | +0.07(166) | -0.08(166) | -0.07(158) | -0.04(166) | -0.01(166) | +0.04(166) | +0.09(166) | +0.06(166) | +0.08(166) | 158 |
| ORB:fade | short | 145 | -0.02(145) | -0.06(145) | -0.05(138) | +0.01(145) | +0.10(145) | +0.00(145) | +0.01(145) | -0.04(145) | +0.32(145) | 138 |
| OVERNIGHT:continue | ALL | 93 | +0.24(93) | -0.03(87) | -0.02(51) | +0.01(87) | +0.01(93) | +0.17(93) | +0.14(93) | +0.24(93) | +0.03(93) | 51 |
| OVERNIGHT:continue | long | 55 | +0.41(55) | +0.01(50) | +0.05(28) | +0.06(50) | -0.03(55) | +0.37(55) | +0.28(55) | +0.41(55) | -0.02(55) | 28 |
| OVERNIGHT:continue | short | 38 | -0.01(38) | -0.09(37) | -0.09(23) | -0.06(37) | +0.07(38) | -0.10(38) | -0.05(38) | -0.01(38) | +0.11(38) | 23 |
| OVERNIGHT:reverse | ALL | 93 | -0.01(93) | -0.06(93) | -0.06(92) | -0.02(93) | -0.09(93) | -0.07(93) | -0.05(93) | -0.01(93) | -0.02(93) | 92 |
| OVERNIGHT:reverse | long | 38 | +0.12(38) | +0.06(38) | +0.11(38) | +0.14(38) | -0.04(38) | +0.11(38) | +0.21(38) | +0.12(38) | -0.35(38) | 38 |
| OVERNIGHT:reverse | short | 55 | -0.09(55) | -0.14(55) | -0.18(54) | -0.13(55) | -0.13(55) | -0.19(55) | -0.23(55) | -0.09(55) | +0.22(55) | 54 |
| ROUND:break | ALL | 1733 | -0.05(1733) | -0.04(1669) | -0.06(1185) | -0.04(1669) | -0.03(1733) | -0.05(1733) | -0.05(1733) | -0.05(1733) | +0.04(1733) | 1185 |
| ROUND:break | long | 874 | -0.06(874) | -0.04(828) | -0.07(589) | -0.06(828) | -0.05(874) | -0.06(874) | -0.06(874) | -0.06(874) | +0.10(874) | 589 |
| ROUND:break | short | 859 | -0.04(859) | -0.03(841) | -0.04(596) | -0.02(841) | -0.02(859) | -0.04(859) | -0.04(859) | -0.04(859) | -0.02(859) | 596 |
| ROUND:reject | ALL | 4023 | -0.05(4023) | -0.04(3911) | -0.04(3307) | -0.03(3911) | -0.07(4023) | -0.05(4023) | -0.04(4023) | -0.05(4023) | -0.06(4023) | 3307 |
| ROUND:reject | long | 2033 | -0.06(2033) | -0.06(1958) | -0.06(1658) | -0.04(1958) | -0.08(2033) | -0.06(2033) | -0.04(2033) | -0.06(2033) | -0.05(2033) | 1658 |
| ROUND:reject | short | 1990 | -0.03(1990) | -0.02(1953) | -0.01(1649) | -0.03(1953) | -0.06(1990) | -0.03(1990) | -0.04(1990) | -0.03(1990) | -0.06(1990) | 1649 |
| VOLREV:expand | ALL | 1688 | -0.03(1688) | -0.07(1319) | -0.05(791) | -0.04(1319) | +0.01(1688) | -0.02(1688) | -0.05(1688) | -0.03(1688) | +0.09(1688) | 791 |
| VOLREV:expand | long | 905 | -0.02(905) | -0.07(658) | -0.00(381) | -0.04(658) | +0.06(905) | -0.01(905) | -0.04(905) | -0.02(905) | +0.18(905) | 381 |
| VOLREV:expand | short | 783 | -0.06(783) | -0.08(661) | -0.09(410) | -0.05(661) | -0.04(783) | -0.03(783) | -0.06(783) | -0.05(783) | -0.01(783) | 410 |
| VOLREV:fade | ALL | 511 | -0.03(511) | -0.05(511) | -0.06(511) | -0.00(511) | -0.06(511) | -0.01(511) | +0.03(511) | -0.03(511) | +0.01(511) | 511 |
| VOLREV:fade | long | 270 | +0.07(270) | +0.04(270) | +0.06(270) | +0.03(270) | +0.07(270) | +0.06(270) | +0.09(270) | +0.06(270) | +0.18(270) | 270 |
| VOLREV:fade | short | 241 | -0.14(241) | -0.15(241) | -0.18(241) | -0.04(241) | -0.21(241) | -0.08(241) | -0.03(241) | -0.14(241) | -0.19(241) | 241 |

Timing / structure / cost state (medians; times dated by bar open): 

| family:variant | dir | n | t_MFE s | t_MAE s | t to 0.5R s | t to 1R s | bars above entry | max structure reached R | TP1 / TP2 reach | holding s | spread/1R | P1 exits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 95 | 3150 | 1800 | 900 | 2400 | 0.50 | 0.17 | 0.68 / 0.55 | 17700 | 0.020 | STOP 0.47, TP1 0.36, FORCED_FLAT 0.17 |
| EOD:reverse | ALL | 95 | 2100 | 2400 | 600 | 1800 | 0.51 | 0.00 | 0.46 / 0.32 | 12300 | 0.020 | STOP 0.54, TP1 0.25, FORCED_FLAT 0.20 |
| GAP:fade | ALL | 92 | 1200 | 1200 | 0 | 900 | 0.55 | 0.62 | 0.75 / 0.48 | 2400 | 0.032 | STOP 0.57, TP1 0.43 |
| GAP:go | ALL | 87 | 1200 | 900 | 0 | 600 | 0.63 | 0.25 | 0.67 / 0.60 | 3000 | 0.033 | STOP 0.54, TP1 0.46 |
| LEADLAG | ALL | 453 | 900 | 600 | 150 | 600 | 0.49 | 0.23 | 0.60 / 0.36 | 1800 | 0.035 | STOP 0.66, TP1 0.30, FORCED_FLAT 0.04 |
| ORB:breakout | ALL | 370 | 6300 | 5100 | 1800 | 5100 | 0.54 | 0.12 | 0.85 / 0.66 | 22350 | 0.013 | STOP 0.53, TP1 0.31, FORCED_FLAT 0.15 |
| ORB:fade | ALL | 311 | 900 | 900 | 0 | 300 | 0.59 | 0.19 | 0.52 / 0.39 | 2100 | 0.036 | STOP 0.59, TP1 0.41, DATA_END 0.00 |
| OVERNIGHT:continue | ALL | 93 | 1050 | 1500 | 0 | 600 | 0.62 | 0.38 | 0.70 / 0.47 | 3000 | 0.033 | STOP 0.51, TP1 0.49 |
| OVERNIGHT:reverse | ALL | 93 | 900 | 900 | 300 | 900 | 0.50 | 0.20 | 0.56 / 0.37 | 1800 | 0.033 | STOP 0.60, TP1 0.40 |
| ROUND:break | ALL | 1733 | 1500 | 1500 | 300 | 1200 | 0.51 | 0.33 | 0.69 / 0.43 | 3900 | 0.030 | STOP 0.60, TP1 0.35, FORCED_FLAT 0.05 |
| ROUND:reject | ALL | 4023 | 1500 | 1800 | 300 | 1200 | 0.53 | 0.35 | 0.63 / 0.41 | 3900 | 0.031 | STOP 0.60, TP1 0.36, FORCED_FLAT 0.04 |
| VOLREV:expand | ALL | 1688 | 1500 | 1500 | 300 | 1200 | 0.54 | 0.17 | 0.69 / 0.44 | 3150 | 0.037 | STOP 0.59, TP1 0.37, FORCED_FLAT 0.03 |
| VOLREV:fade | ALL | 511 | 1500 | 1500 | 300 | 900 | 0.54 | 0.27 | 0.53 / 0.36 | 3900 | 0.026 | STOP 0.59, TP1 0.36, FORCED_FLAT 0.04 |

Profit capture per policy (mean floored capture ratio = share of the MFE kept; same entries):

| family:variant | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9 |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | 0.33 | 0.21 | 0.21 | 0.12 | 0.21 | 0.31 | 0.18 | 0.30 | 0.28 |
| EOD:reverse | 0.25 | 0.23 | 0.21 | 0.14 | 0.12 | 0.21 | 0.11 | 0.25 | 0.19 |
| GAP:fade | 0.21 | 0.23 | 0.14 | 0.15 | 0.14 | 0.17 | 0.17 | 0.21 | 0.15 |
| GAP:go | 0.25 | 0.19 | 0.12 | 0.13 | 0.14 | 0.21 | 0.17 | 0.25 | 0.16 |
| LEADLAG | 0.22 | 0.25 | 0.18 | 0.18 | 0.17 | 0.17 | 0.16 | 0.22 | 0.17 |
| ORB:breakout | 0.25 | 0.16 | 0.16 | 0.11 | 0.15 | 0.21 | 0.13 | 0.22 | 0.24 |
| ORB:fade | 0.25 | 0.19 | 0.15 | 0.16 | 0.14 | 0.22 | 0.21 | 0.24 | 0.14 |
| OVERNIGHT:continue | 0.29 | 0.21 | 0.11 | 0.14 | 0.12 | 0.26 | 0.22 | 0.29 | 0.13 |
| OVERNIGHT:reverse | 0.24 | 0.20 | 0.15 | 0.16 | 0.11 | 0.19 | 0.19 | 0.24 | 0.12 |
| ROUND:break | 0.23 | 0.24 | 0.16 | 0.16 | 0.14 | 0.20 | 0.18 | 0.23 | 0.18 |
| ROUND:reject | 0.23 | 0.23 | 0.18 | 0.15 | 0.14 | 0.20 | 0.17 | 0.23 | 0.17 |
| VOLREV:expand | 0.22 | 0.21 | 0.17 | 0.15 | 0.15 | 0.20 | 0.18 | 0.22 | 0.19 |
| VOLREV:fade | 0.24 | 0.24 | 0.19 | 0.15 | 0.15 | 0.21 | 0.17 | 0.24 | 0.19 |

Session / regime cells with n >= 30:

| family:variant | cell | n | clusters | MFE med | MAE med | useful / failure | capture | baseline R | verdict |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | session=CLOSE_90M | 95 | 95 | 0.85 | 0.95 | 0.64 / 0.27 | 0.33 | 0.09 | NO CLEAR DEFICIT |
| EOD:continue | regime=HIGH_VOL | 49 | 49 | 1.04 | 0.78 | 0.67 / 0.24 | 0.34 | 0.15 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:continue | regime=MID_VOL | 45 | 45 | 0.83 | 0.99 | 0.62 / 0.29 | 0.31 | 0.06 | NO CLEAR DEFICIT |
| EOD:reverse | session=CLOSE_90M | 95 | 95 | 0.76 | 1.02 | 0.68 / 0.20 | 0.25 | -0.15 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | regime=HIGH_VOL | 49 | 49 | 0.67 | 1.06 | 0.59 / 0.27 | 0.27 | -0.16 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | regime=MID_VOL | 45 | 45 | 0.90 | 1.02 | 0.78 / 0.13 | 0.23 | -0.17 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | session=OPEN_90M | 92 | 92 | 0.94 | 1.13 | 0.68 / 0.17 | 0.21 | 0.09 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | regime=HIGH_VOL | 50 | 50 | 1.50 | 1.07 | 0.72 / 0.18 | 0.24 | 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | regime=MID_VOL | 41 | 41 | 0.80 | 1.15 | 0.63 / 0.17 | 0.19 | -0.09 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | session=OPEN_90M | 87 | 87 | 1.22 | 1.11 | 0.68 / 0.20 | 0.25 | 0.15 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | regime=HIGH_VOL | 42 | 42 | 0.85 | 1.10 | 0.67 / 0.21 | 0.26 | 0.07 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | regime=MID_VOL | 45 | 45 | 1.49 | 1.15 | 0.69 / 0.18 | 0.25 | 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | session=CLOSE_90M | 92 | 67 | 0.54 | 1.08 | 0.54 / 0.40 | 0.27 | -0.21 | BOTH |
| LEADLAG | session=MIDDAY | 304 | 202 | 0.72 | 1.10 | 0.59 / 0.32 | 0.19 | -0.20 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | session=OFF_CASH | 57 | 43 | 0.84 | 1.04 | 0.60 / 0.23 | 0.29 | -0.17 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | regime=HIGH_VOL | 106 | 72 | 0.53 | 1.09 | 0.54 / 0.32 | 0.22 | -0.34 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | regime=LOW_VOL | 35 | 24 | 0.50 | 1.06 | 0.51 / 0.34 | 0.21 | -0.28 | USEFUL ENTRIES + BAD CAPTURE |
| LEADLAG | regime=MID_VOL | 312 | 208 | 0.73 | 1.09 | 0.60 / 0.32 | 0.22 | -0.14 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | session=MIDDAY | 30 | 30 | 1.22 | 0.64 | 0.87 / 0.03 | 0.36 | 0.41 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | session=OPEN_90M | 340 | 340 | 0.87 | 1.03 | 0.61 / 0.23 | 0.24 | -0.07 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=HIGH_VOL | 205 | 205 | 0.84 | 1.02 | 0.60 / 0.24 | 0.25 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=MID_VOL | 163 | 163 | 1.07 | 1.01 | 0.67 / 0.18 | 0.25 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | session=OPEN_90M | 293 | 293 | 0.88 | 1.18 | 0.63 / 0.28 | 0.25 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=HIGH_VOL | 170 | 170 | 0.81 | 1.18 | 0.64 / 0.28 | 0.24 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=MID_VOL | 139 | 139 | 1.17 | 1.21 | 0.62 / 0.29 | 0.25 | 0.09 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | session=OPEN_90M | 93 | 93 | 1.42 | 1.13 | 0.69 / 0.17 | 0.29 | 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | regime=HIGH_VOL | 53 | 53 | 0.80 | 1.13 | 0.68 / 0.17 | 0.29 | 0.13 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | regime=MID_VOL | 40 | 40 | 1.71 | 1.15 | 0.70 / 0.17 | 0.29 | 0.38 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | session=OPEN_90M | 93 | 93 | 0.83 | 1.14 | 0.65 / 0.23 | 0.24 | -0.01 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | regime=HIGH_VOL | 53 | 53 | 1.07 | 1.12 | 0.72 / 0.19 | 0.24 | 0.08 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | regime=MID_VOL | 40 | 40 | 0.65 | 1.16 | 0.55 / 0.28 | 0.23 | -0.12 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=CLOSE_90M | 277 | 263 | 0.79 | 1.05 | 0.62 / 0.26 | 0.24 | -0.11 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=MIDDAY | 827 | 675 | 0.83 | 1.12 | 0.62 / 0.25 | 0.21 | -0.10 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=OFF_CASH | 301 | 260 | 0.90 | 1.02 | 0.63 / 0.23 | 0.29 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=OPEN_90M | 328 | 288 | 0.99 | 1.10 | 0.63 / 0.25 | 0.24 | 0.05 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=HIGH_VOL | 880 | 667 | 0.91 | 1.08 | 0.65 / 0.23 | 0.24 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=LOW_VOL | 62 | 59 | 0.81 | 1.10 | 0.60 / 0.32 | 0.25 | -0.10 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=MID_VOL | 791 | 681 | 0.77 | 1.10 | 0.59 / 0.26 | 0.22 | -0.08 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=CLOSE_90M | 610 | 469 | 0.88 | 1.06 | 0.66 / 0.23 | 0.27 | -0.02 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=MIDDAY | 1969 | 1227 | 0.91 | 1.12 | 0.64 / 0.23 | 0.21 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=OFF_CASH | 699 | 504 | 0.81 | 1.05 | 0.62 / 0.24 | 0.27 | -0.07 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=OPEN_90M | 745 | 524 | 0.91 | 1.12 | 0.64 / 0.23 | 0.21 | -0.00 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=HIGH_VOL | 1844 | 1073 | 0.90 | 1.09 | 0.65 / 0.22 | 0.24 | -0.03 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=LOW_VOL | 193 | 164 | 0.77 | 1.17 | 0.58 / 0.30 | 0.22 | -0.18 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=MID_VOL | 1986 | 1375 | 0.90 | 1.12 | 0.64 / 0.24 | 0.23 | -0.05 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=CLOSE_90M | 180 | 176 | 1.06 | 1.03 | 0.71 / 0.19 | 0.30 | 0.12 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=MIDDAY | 866 | 738 | 0.86 | 1.15 | 0.63 / 0.25 | 0.20 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=OFF_CASH | 297 | 277 | 0.85 | 1.05 | 0.64 / 0.26 | 0.27 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=OPEN_90M | 345 | 315 | 1.01 | 1.14 | 0.67 / 0.26 | 0.21 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=HIGH_VOL | 344 | 324 | 0.91 | 1.10 | 0.65 / 0.22 | 0.21 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=LOW_VOL | 177 | 168 | 0.90 | 1.10 | 0.63 / 0.31 | 0.28 | 0.04 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=MID_VOL | 1167 | 1000 | 0.93 | 1.13 | 0.65 / 0.24 | 0.22 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=CLOSE_90M | 89 | 87 | 0.68 | 1.04 | 0.60 / 0.25 | 0.24 | -0.13 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=MIDDAY | 193 | 170 | 1.16 | 1.10 | 0.71 / 0.22 | 0.26 | 0.05 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=OFF_CASH | 59 | 57 | 1.18 | 1.00 | 0.73 / 0.15 | 0.31 | 0.21 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=OPEN_90M | 170 | 166 | 0.89 | 1.10 | 0.63 / 0.24 | 0.19 | -0.16 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | regime=HIGH_VOL | 511 | 443 | 0.95 | 1.08 | 0.67 / 0.22 | 0.24 | -0.03 | USEFUL ENTRIES + BAD CAPTURE |
