# Lane X - XAUUSD

`eeq-1.0` hindsight diagnostics, no edge claim.

## XAUUSD

Data: 2025-05-05..2026-08-31 (94194 M5 bars; evaluated from 2025-06-04); 12 frozen family specs (EOD:continue, EOD:reverse, GAP:fade, GAP:go, ORB:breakout, ORB:fade, OVERNIGHT:continue, OVERNIGHT:reverse, ROUND:break, ROUND:reject, VOLREV:expand, VOLREV:fade); analysed entries n=7180.

Exclusions / counters: `{"analysed": 7180, "candidates": 7181, "entry_gap_stop": 1}`

Entry quality (potential; R against the initial risk; MFE/MAE to the stop or the flat deadline):

| family:variant | dir | n | clusters | flag | MFE mean/med | MAE mean/med | P(MFE>=.25/.5/.75/1/1.5/2) | MFE-first / MAE-first | giveback | capture (floored) | useful / failure share | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 78 | 78 |  | 1.54/1.01 | 1.08/1.11 | 0.82/0.68/0.58/0.51/0.36/0.28 | 0.38 / 0.62 | 1.60 | 0.23 | 0.68 / 0.18 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:continue | long | 47 | 47 |  | 1.53/1.10 | 1.09/1.14 | 0.83/0.68/0.57/0.53/0.36/0.28 | 0.34 / 0.66 | 1.59 | 0.24 | 0.68 / 0.17 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:continue | short | 31 | 31 |  | 1.54/0.94 | 1.07/1.09 | 0.81/0.68/0.58/0.48/0.35/0.29 | 0.45 / 0.55 | 1.61 | 0.21 | 0.68 / 0.19 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | ALL | 78 | 78 |  | 1.37/0.91 | 1.04/1.09 | 0.82/0.76/0.56/0.45/0.35/0.27 | 0.54 / 0.46 | 1.45 | 0.24 | 0.76 / 0.18 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | long | 31 | 31 |  | 1.28/0.81 | 1.01/1.06 | 0.84/0.77/0.55/0.45/0.35/0.23 | 0.48 / 0.52 | 1.29 | 0.28 | 0.77 / 0.16 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | short | 47 | 47 |  | 1.44/0.91 | 1.05/1.11 | 0.81/0.74/0.57/0.45/0.34/0.30 | 0.57 / 0.43 | 1.55 | 0.22 | 0.74 / 0.19 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | ALL | 77 | 77 |  | 3.12/1.14 | 1.13/1.08 | 0.74/0.64/0.58/0.53/0.43/0.35 | 0.49 / 0.51 | 3.05 | 0.19 | 0.64 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | long | 36 | 36 |  | 2.34/1.22 | 1.05/1.05 | 0.69/0.61/0.56/0.56/0.36/0.31 | 0.47 / 0.53 | 2.43 | 0.18 | 0.61 / 0.31 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | short | 41 | 41 |  | 3.81/1.14 | 1.19/1.09 | 0.78/0.66/0.61/0.51/0.49/0.39 | 0.51 / 0.49 | 3.59 | 0.20 | 0.66 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | ALL | 80 | 80 |  | 2.30/0.99 | 1.22/1.16 | 0.72/0.64/0.55/0.49/0.39/0.31 | 0.44 / 0.56 | 2.33 | 0.22 | 0.64 / 0.28 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | long | 51 | 51 |  | 1.51/0.81 | 1.32/1.20 | 0.75/0.63/0.51/0.43/0.33/0.22 | 0.45 / 0.55 | 1.67 | 0.22 | 0.63 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | short | 29 | 29 | n too small (<30): no conclusion | 3.69/1.40 | 1.05/1.10 | 0.69/0.66/0.62/0.59/0.48/0.48 | 0.41 / 0.59 | 3.48 | 0.22 | 0.66 / 0.31 | INCONCLUSIVE-n |
| ORB:breakout | ALL | 319 | 319 |  | 1.97/0.82 | 1.09/1.10 | 0.75/0.61/0.52/0.43/0.35/0.29 | 0.43 / 0.57 | 2.09 | 0.19 | 0.61 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | long | 169 | 169 |  | 1.61/0.72 | 1.10/1.12 | 0.71/0.58/0.50/0.42/0.33/0.26 | 0.41 / 0.59 | 1.77 | 0.21 | 0.58 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | short | 150 | 150 |  | 2.38/0.88 | 1.09/1.09 | 0.79/0.65/0.54/0.45/0.37/0.32 | 0.45 / 0.55 | 2.45 | 0.17 | 0.65 / 0.21 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | ALL | 282 | 282 |  | 2.90/0.79 | 1.41/1.21 | 0.70/0.61/0.52/0.45/0.38/0.31 | 0.39 / 0.61 | 2.95 | 0.20 | 0.61 / 0.30 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | long | 135 | 135 |  | 2.32/0.73 | 1.39/1.17 | 0.67/0.57/0.49/0.42/0.36/0.30 | 0.37 / 0.63 | 2.42 | 0.21 | 0.57 / 0.33 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | short | 147 | 147 |  | 3.43/0.94 | 1.42/1.23 | 0.73/0.65/0.56/0.48/0.39/0.33 | 0.40 / 0.60 | 3.44 | 0.19 | 0.65 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | ALL | 79 | 79 |  | 2.26/0.69 | 1.21/1.14 | 0.71/0.58/0.48/0.42/0.35/0.32 | 0.41 / 0.59 | 2.38 | 0.16 | 0.58 / 0.29 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | long | 49 | 49 |  | 1.77/0.69 | 1.28/1.17 | 0.69/0.59/0.49/0.41/0.35/0.29 | 0.43 / 0.57 | 1.90 | 0.19 | 0.59 / 0.31 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | short | 30 | 30 |  | 3.08/0.63 | 1.09/1.11 | 0.73/0.57/0.47/0.43/0.37/0.37 | 0.37 / 0.63 | 3.16 | 0.12 | 0.57 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | ALL | 79 | 79 |  | 2.77/0.99 | 1.18/1.09 | 0.77/0.66/0.58/0.49/0.42/0.33 | 0.54 / 0.46 | 2.73 | 0.20 | 0.66 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | long | 30 | 30 |  | 2.76/1.31 | 1.02/1.05 | 0.77/0.70/0.60/0.57/0.47/0.37 | 0.60 / 0.40 | 2.59 | 0.23 | 0.70 / 0.23 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | short | 49 | 49 |  | 2.78/0.97 | 1.27/1.13 | 0.78/0.63/0.57/0.45/0.39/0.31 | 0.51 / 0.49 | 2.81 | 0.19 | 0.63 / 0.22 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | ALL | 1620 | 1058 |  | 1.96/0.86 | 1.16/1.14 | 0.75/0.62/0.53/0.46/0.35/0.29 | 0.44 / 0.56 | 2.07 | 0.20 | 0.62 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | long | 824 | 522 |  | 1.75/0.83 | 1.16/1.14 | 0.74/0.62/0.53/0.46/0.34/0.27 | 0.43 / 0.57 | 1.88 | 0.21 | 0.62 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | short | 796 | 536 |  | 2.18/0.87 | 1.16/1.14 | 0.76/0.63/0.54/0.47/0.36/0.31 | 0.45 / 0.55 | 2.27 | 0.19 | 0.63 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | ALL | 3223 | 1502 |  | 2.03/0.88 | 1.21/1.15 | 0.75/0.63/0.54/0.47/0.37/0.31 | 0.45 / 0.55 | 2.09 | 0.21 | 0.63 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | long | 1649 | 741 |  | 1.88/0.84 | 1.23/1.17 | 0.74/0.62/0.53/0.46/0.36/0.29 | 0.44 / 0.56 | 1.96 | 0.22 | 0.62 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | short | 1574 | 761 |  | 2.20/0.93 | 1.19/1.14 | 0.76/0.64/0.55/0.48/0.39/0.33 | 0.45 / 0.55 | 2.22 | 0.21 | 0.64 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | ALL | 919 | 764 |  | 2.13/0.87 | 1.22/1.16 | 0.74/0.62/0.54/0.46/0.36/0.30 | 0.41 / 0.59 | 2.22 | 0.20 | 0.62 / 0.26 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | long | 498 | 399 |  | 2.27/0.80 | 1.20/1.16 | 0.73/0.61/0.51/0.44/0.37/0.31 | 0.43 / 0.57 | 2.34 | 0.20 | 0.61 / 0.27 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | short | 421 | 365 |  | 1.97/0.92 | 1.25/1.16 | 0.75/0.64/0.58/0.48/0.35/0.28 | 0.40 / 0.60 | 2.08 | 0.20 | 0.64 / 0.25 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | ALL | 346 | 284 |  | 1.82/0.84 | 1.19/1.16 | 0.79/0.64/0.55/0.46/0.37/0.32 | 0.47 / 0.53 | 1.89 | 0.21 | 0.64 / 0.21 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | long | 182 | 152 |  | 1.96/0.83 | 1.24/1.20 | 0.81/0.64/0.55/0.47/0.37/0.33 | 0.48 / 0.52 | 2.03 | 0.19 | 0.64 / 0.19 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | short | 164 | 132 |  | 1.67/0.86 | 1.12/1.11 | 0.76/0.63/0.55/0.45/0.37/0.30 | 0.46 / 0.54 | 1.74 | 0.23 | 0.63 / 0.24 | USEFUL ENTRIES + BAD CAPTURE |

Exit-policy shadow comparison on the SAME entries (mean R, spread-adjusted gross; in brackets: entries the policy applies to; NA = no structural level):

| family:variant | dir | n | P1 FIXED_1_5R | P2 STRUCT_TP1 | P3 STRUCT_TP1_TP2 | P4 TP1_TP2_RUNNER | P5 STRUCT_TRAIL | P6 BREAKEVEN_LOCK | P7 MOMENTUM_EXIT | P8 TIME_ALPHA | P9 EOD_FORCED_FLAT | paired-structural n |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 78 | -0.06(78) | -0.02(66) | +0.07(22) | -0.01(66) | -0.06(78) | +0.05(78) | -0.21(78) | -0.06(78) | +0.00(78) | 22 |
| EOD:continue | long | 47 | -0.06(47) | +0.01(39) | +0.27(12) | -0.05(39) | -0.08(47) | +0.05(47) | -0.20(47) | -0.06(47) | +0.10(47) | 12 |
| EOD:continue | short | 31 | -0.08(31) | -0.06(27) | -0.16(10) | +0.04(27) | -0.03(31) | +0.04(31) | -0.21(31) | -0.08(31) | -0.14(31) | 10 |
| EOD:reverse | ALL | 78 | -0.07(78) | +0.03(78) | -0.01(78) | +0.05(78) | -0.13(78) | -0.01(78) | +0.03(78) | -0.07(78) | -0.12(78) | 78 |
| EOD:reverse | long | 31 | -0.01(31) | +0.05(31) | +0.10(31) | +0.18(31) | -0.18(31) | +0.05(31) | +0.22(31) | -0.01(31) | -0.19(31) | 31 |
| EOD:reverse | short | 47 | -0.11(47) | +0.01(47) | -0.08(47) | -0.03(47) | -0.10(47) | -0.06(47) | -0.09(47) | -0.11(47) | -0.07(47) | 47 |
| GAP:fade | ALL | 77 | +0.07(77) | -0.08(76) | -0.05(75) | -0.01(76) | +0.15(77) | +0.12(77) | +0.13(77) | +0.07(77) | +0.66(77) | 75 |
| GAP:fade | long | 36 | -0.10(36) | -0.16(36) | -0.09(36) | -0.09(36) | -0.13(36) | +0.02(36) | -0.01(36) | -0.10(36) | +0.43(36) | 36 |
| GAP:fade | short | 41 | +0.22(41) | -0.00(40) | -0.00(39) | +0.07(40) | +0.40(41) | +0.21(41) | +0.26(41) | +0.22(41) | +0.86(41) | 39 |
| GAP:go | ALL | 80 | -0.03(80) | -0.06(70) | -0.10(47) | -0.07(70) | +0.09(80) | -0.07(80) | -0.13(80) | -0.03(80) | +0.21(80) | 47 |
| GAP:go | long | 51 | -0.17(51) | -0.02(44) | -0.06(30) | -0.05(44) | -0.13(51) | -0.18(51) | -0.20(51) | -0.17(51) | -0.53(51) | 30 |
| GAP:go | short | 29 | +0.21(29) | -0.14(26) | -0.18(17) | -0.09(26) | +0.47(29) | +0.11(29) | -0.00(29) | +0.21(29) | +1.51(29) | 17 |
| ORB:breakout | ALL | 319 | -0.12(319) | -0.09(280) | -0.08(220) | -0.06(280) | -0.11(319) | -0.16(319) | -0.11(319) | -0.10(319) | +0.12(319) | 220 |
| ORB:breakout | long | 169 | -0.17(169) | -0.10(139) | -0.09(111) | -0.08(139) | -0.13(169) | -0.18(169) | -0.13(169) | -0.13(169) | -0.05(169) | 111 |
| ORB:breakout | short | 150 | -0.07(150) | -0.07(141) | -0.06(109) | -0.04(141) | -0.07(150) | -0.14(150) | -0.09(150) | -0.06(150) | +0.32(150) | 109 |
| ORB:fade | ALL | 282 | -0.05(282) | -0.02(282) | +0.03(270) | +0.03(282) | +0.01(282) | -0.06(282) | -0.07(282) | -0.06(282) | +0.32(282) | 270 |
| ORB:fade | long | 135 | -0.10(135) | -0.11(135) | -0.04(127) | -0.03(135) | -0.09(135) | -0.13(135) | -0.14(135) | -0.10(135) | +0.13(135) | 127 |
| ORB:fade | short | 147 | -0.01(147) | +0.07(147) | +0.10(143) | +0.08(147) | +0.11(147) | -0.01(147) | -0.01(147) | -0.01(147) | +0.50(147) | 143 |
| OVERNIGHT:continue | ALL | 79 | -0.11(79) | -0.14(76) | -0.16(56) | -0.09(76) | -0.04(79) | -0.16(79) | -0.08(79) | -0.11(79) | +0.44(79) | 56 |
| OVERNIGHT:continue | long | 49 | -0.13(49) | -0.09(47) | -0.16(33) | -0.09(47) | -0.21(49) | -0.19(49) | -0.10(49) | -0.13(49) | -0.02(49) | 33 |
| OVERNIGHT:continue | short | 30 | -0.08(30) | -0.22(29) | -0.16(23) | -0.07(29) | +0.23(30) | -0.11(30) | -0.05(30) | -0.08(30) | +1.20(30) | 23 |
| OVERNIGHT:reverse | ALL | 79 | +0.04(79) | -0.15(79) | -0.11(78) | -0.02(79) | -0.02(79) | +0.05(79) | +0.07(79) | +0.04(79) | +0.36(79) | 78 |
| OVERNIGHT:reverse | long | 30 | +0.17(30) | -0.10(30) | -0.03(30) | +0.09(30) | +0.09(30) | +0.22(30) | +0.20(30) | +0.17(30) | +0.56(30) | 30 |
| OVERNIGHT:reverse | short | 49 | -0.03(49) | -0.19(49) | -0.17(48) | -0.08(49) | -0.08(49) | -0.06(49) | -0.01(49) | -0.03(49) | +0.23(49) | 48 |
| ROUND:break | ALL | 1620 | -0.11(1620) | -0.05(1550) | -0.07(1203) | -0.03(1550) | -0.07(1620) | -0.08(1620) | -0.08(1620) | -0.11(1620) | -0.03(1620) | 1203 |
| ROUND:break | long | 824 | -0.13(824) | -0.07(767) | -0.09(585) | -0.04(767) | -0.07(824) | -0.10(824) | -0.09(824) | -0.13(824) | -0.09(824) | 585 |
| ROUND:break | short | 796 | -0.09(796) | -0.03(783) | -0.06(618) | -0.02(783) | -0.08(796) | -0.06(796) | -0.07(796) | -0.09(796) | +0.04(796) | 618 |
| ROUND:reject | ALL | 3223 | -0.05(3223) | -0.05(3151) | -0.06(2771) | -0.05(3151) | -0.09(3223) | -0.04(3223) | -0.06(3223) | -0.05(3223) | -0.04(3223) | 2771 |
| ROUND:reject | long | 1649 | -0.08(1649) | -0.07(1604) | -0.09(1388) | -0.05(1604) | -0.08(1649) | -0.07(1649) | -0.07(1649) | -0.08(1649) | -0.06(1649) | 1388 |
| ROUND:reject | short | 1574 | -0.02(1574) | -0.03(1547) | -0.04(1383) | -0.04(1547) | -0.11(1574) | -0.01(1574) | -0.05(1574) | -0.02(1574) | -0.01(1574) | 1383 |
| VOLREV:expand | ALL | 919 | -0.09(919) | -0.05(726) | -0.10(518) | -0.03(726) | -0.04(919) | -0.07(919) | -0.08(919) | -0.09(919) | +0.02(919) | 518 |
| VOLREV:expand | long | 498 | -0.07(498) | -0.06(356) | -0.08(251) | -0.03(356) | +0.11(498) | -0.08(498) | -0.06(498) | -0.06(498) | +0.20(498) | 251 |
| VOLREV:expand | short | 421 | -0.11(421) | -0.04(370) | -0.12(267) | -0.02(370) | -0.22(421) | -0.07(421) | -0.11(421) | -0.11(421) | -0.20(421) | 267 |
| VOLREV:fade | ALL | 346 | -0.07(346) | -0.03(346) | -0.05(346) | -0.03(346) | -0.06(346) | -0.08(346) | -0.10(346) | -0.07(346) | +0.00(346) | 346 |
| VOLREV:fade | long | 182 | -0.07(182) | -0.00(182) | -0.01(182) | +0.03(182) | +0.07(182) | -0.10(182) | -0.04(182) | -0.07(182) | +0.16(182) | 182 |
| VOLREV:fade | short | 164 | -0.07(164) | -0.05(164) | -0.09(164) | -0.09(164) | -0.22(164) | -0.07(164) | -0.16(164) | -0.07(164) | -0.18(164) | 164 |

Timing / structure / cost state (medians; times dated by bar open): 

| family:variant | dir | n | t_MFE s | t_MAE s | t to 0.5R s | t to 1R s | bars above entry | max structure reached R | TP1 / TP2 reach | holding s | spread/1R | P1 exits |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | ALL | 78 | 2100 | 1800 | 600 | 1650 | 0.54 | 0.17 | 0.76 / 0.64 | 3600 | 0.025 | STOP 0.59, TP1 0.36, FORCED_FLAT 0.05 |
| EOD:reverse | ALL | 78 | 1650 | 1800 | 300 | 1800 | 0.57 | 0.27 | 0.53 / 0.35 | 3300 | 0.026 | STOP 0.59, TP1 0.35, FORCED_FLAT 0.05 |
| GAP:fade | ALL | 77 | 1800 | 1500 | 300 | 1200 | 0.58 | 0.65 | 0.62 / 0.44 | 5400 | 0.041 | STOP 0.57, TP1 0.43 |
| GAP:go | ALL | 80 | 1200 | 1500 | 300 | 1500 | 0.56 | 0.30 | 0.66 / 0.45 | 2850 | 0.043 | STOP 0.61, TP1 0.39 |
| ORB:breakout | ALL | 319 | 2550 | 4200 | 900 | 3150 | 0.49 | 0.27 | 0.71 / 0.51 | 8700 | 0.031 | STOP 0.65, TP1 0.35, FORCED_FLAT 0.01 |
| ORB:fade | ALL | 282 | 900 | 1200 | 0 | 300 | 0.56 | 0.37 | 0.54 / 0.39 | 1800 | 0.066 | STOP 0.62, TP1 0.38, FORCED_FLAT 0.00 |
| OVERNIGHT:continue | ALL | 79 | 1200 | 1800 | 600 | 1200 | 0.53 | 0.25 | 0.58 / 0.38 | 3300 | 0.042 | STOP 0.65, TP1 0.35 |
| OVERNIGHT:reverse | ALL | 79 | 1200 | 1800 | 300 | 1200 | 0.54 | 0.31 | 0.53 / 0.38 | 2700 | 0.041 | STOP 0.58, TP1 0.42 |
| ROUND:break | ALL | 1620 | 1200 | 1500 | 300 | 900 | 0.51 | 0.40 | 0.68 / 0.45 | 2700 | 0.038 | STOP 0.63, TP1 0.35, FORCED_FLAT 0.01 |
| ROUND:reject | ALL | 3223 | 1500 | 1500 | 300 | 1200 | 0.53 | 0.41 | 0.64 / 0.43 | 3000 | 0.041 | STOP 0.62, TP1 0.37, FORCED_FLAT 0.01 |
| VOLREV:expand | ALL | 919 | 900 | 1500 | 300 | 900 | 0.50 | 0.21 | 0.67 / 0.39 | 2400 | 0.050 | STOP 0.63, TP1 0.36, FORCED_FLAT 0.00 |
| VOLREV:fade | ALL | 346 | 1500 | 1200 | 300 | 1200 | 0.57 | 0.18 | 0.52 / 0.34 | 3000 | 0.029 | STOP 0.63, TP1 0.37, FORCED_FLAT 0.01 |

Profit capture per policy (mean floored capture ratio = share of the MFE kept; same entries):

| family:variant | P1 | P2 | P3 | P4 | P5 | P6 | P7 | P8 | P9 |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | 0.23 | 0.22 | 0.21 | 0.14 | 0.15 | 0.22 | 0.12 | 0.23 | 0.22 |
| EOD:reverse | 0.24 | 0.28 | 0.21 | 0.18 | 0.13 | 0.24 | 0.17 | 0.24 | 0.21 |
| GAP:fade | 0.19 | 0.18 | 0.12 | 0.15 | 0.14 | 0.17 | 0.16 | 0.19 | 0.17 |
| GAP:go | 0.22 | 0.22 | 0.19 | 0.16 | 0.12 | 0.17 | 0.14 | 0.22 | 0.14 |
| ORB:breakout | 0.19 | 0.20 | 0.16 | 0.14 | 0.11 | 0.15 | 0.11 | 0.18 | 0.17 |
| ORB:fade | 0.20 | 0.22 | 0.17 | 0.17 | 0.10 | 0.17 | 0.17 | 0.20 | 0.12 |
| OVERNIGHT:continue | 0.16 | 0.22 | 0.11 | 0.13 | 0.10 | 0.13 | 0.11 | 0.16 | 0.19 |
| OVERNIGHT:reverse | 0.20 | 0.15 | 0.11 | 0.13 | 0.11 | 0.18 | 0.15 | 0.20 | 0.13 |
| ROUND:break | 0.20 | 0.24 | 0.16 | 0.16 | 0.12 | 0.17 | 0.16 | 0.20 | 0.14 |
| ROUND:reject | 0.21 | 0.23 | 0.16 | 0.16 | 0.12 | 0.18 | 0.17 | 0.21 | 0.13 |
| VOLREV:expand | 0.20 | 0.23 | 0.13 | 0.16 | 0.12 | 0.17 | 0.17 | 0.20 | 0.13 |
| VOLREV:fade | 0.21 | 0.24 | 0.16 | 0.14 | 0.14 | 0.17 | 0.11 | 0.21 | 0.18 |

Session / regime cells with n >= 30:

| family:variant | cell | n | clusters | MFE med | MAE med | useful / failure | capture | baseline R | verdict |
|---|---|---|---|---|---|---|---|---|---|
| EOD:continue | session=MIDDAY | 78 | 78 | 1.01 | 1.11 | 0.68 / 0.18 | 0.23 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:continue | regime=HIGH_VOL | 53 | 53 | 1.03 | 1.09 | 0.70 / 0.17 | 0.25 | 0.04 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | session=MIDDAY | 78 | 78 | 0.91 | 1.09 | 0.76 / 0.18 | 0.24 | -0.07 | USEFUL ENTRIES + BAD CAPTURE |
| EOD:reverse | regime=HIGH_VOL | 53 | 53 | 0.82 | 1.09 | 0.74 / 0.19 | 0.27 | -0.07 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | session=OPEN_90M | 77 | 77 | 1.14 | 1.08 | 0.64 / 0.26 | 0.19 | 0.07 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:fade | regime=HIGH_VOL | 35 | 35 | 0.72 | 1.09 | 0.54 / 0.37 | 0.15 | -0.14 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | session=OPEN_90M | 80 | 80 | 0.99 | 1.16 | 0.64 / 0.28 | 0.22 | -0.03 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | regime=HIGH_VOL | 30 | 30 | 1.43 | 1.18 | 0.67 / 0.27 | 0.27 | 0.17 | USEFUL ENTRIES + BAD CAPTURE |
| GAP:go | regime=MID_VOL | 30 | 30 | 0.92 | 1.16 | 0.67 / 0.23 | 0.19 | 0.00 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | session=OPEN_90M | 306 | 306 | 0.82 | 1.10 | 0.62 / 0.25 | 0.20 | -0.08 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=HIGH_VOL | 101 | 101 | 0.97 | 1.08 | 0.65 / 0.20 | 0.20 | 0.01 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=LOW_VOL | 94 | 94 | 0.70 | 1.12 | 0.59 / 0.32 | 0.16 | -0.31 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:breakout | regime=MID_VOL | 124 | 124 | 0.72 | 1.09 | 0.60 / 0.24 | 0.21 | -0.09 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | session=OPEN_90M | 271 | 271 | 0.78 | 1.21 | 0.61 / 0.30 | 0.20 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=HIGH_VOL | 96 | 96 | 0.72 | 1.21 | 0.58 / 0.33 | 0.20 | -0.14 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=LOW_VOL | 82 | 82 | 0.70 | 1.22 | 0.55 / 0.34 | 0.20 | -0.09 | USEFUL ENTRIES + BAD CAPTURE |
| ORB:fade | regime=MID_VOL | 104 | 104 | 1.02 | 1.20 | 0.68 / 0.23 | 0.20 | 0.05 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | session=OPEN_90M | 79 | 79 | 0.69 | 1.14 | 0.58 / 0.29 | 0.16 | -0.11 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:continue | regime=HIGH_VOL | 38 | 38 | 0.73 | 1.13 | 0.66 / 0.29 | 0.16 | -0.08 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | session=OPEN_90M | 79 | 79 | 0.99 | 1.09 | 0.66 / 0.23 | 0.20 | 0.04 | USEFUL ENTRIES + BAD CAPTURE |
| OVERNIGHT:reverse | regime=HIGH_VOL | 38 | 38 | 1.14 | 1.08 | 0.58 / 0.34 | 0.20 | 0.12 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=MIDDAY | 1272 | 864 | 0.85 | 1.13 | 0.63 / 0.24 | 0.20 | -0.12 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | session=OPEN_90M | 348 | 289 | 0.87 | 1.14 | 0.61 / 0.28 | 0.19 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=HIGH_VOL | 858 | 547 | 1.04 | 1.11 | 0.66 / 0.23 | 0.22 | -0.03 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=LOW_VOL | 296 | 265 | 0.71 | 1.21 | 0.60 / 0.25 | 0.17 | -0.21 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:break | regime=MID_VOL | 466 | 397 | 0.70 | 1.15 | 0.57 / 0.28 | 0.18 | -0.18 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=MIDDAY | 2478 | 1208 | 0.87 | 1.16 | 0.62 / 0.25 | 0.22 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | session=OPEN_90M | 745 | 499 | 0.92 | 1.14 | 0.65 / 0.24 | 0.19 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=HIGH_VOL | 1431 | 699 | 0.97 | 1.13 | 0.66 / 0.23 | 0.23 | 0.00 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=LOW_VOL | 781 | 534 | 0.84 | 1.22 | 0.61 / 0.27 | 0.21 | -0.09 | USEFUL ENTRIES + BAD CAPTURE |
| ROUND:reject | regime=MID_VOL | 1011 | 684 | 0.77 | 1.15 | 0.60 / 0.27 | 0.19 | -0.10 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=MIDDAY | 691 | 576 | 0.89 | 1.16 | 0.63 / 0.26 | 0.21 | -0.06 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | session=OPEN_90M | 228 | 216 | 0.79 | 1.16 | 0.59 / 0.28 | 0.18 | -0.16 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=HIGH_VOL | 189 | 179 | 0.87 | 1.13 | 0.62 / 0.29 | 0.22 | -0.14 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=LOW_VOL | 358 | 321 | 0.85 | 1.19 | 0.61 / 0.27 | 0.20 | -0.11 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:expand | regime=MID_VOL | 372 | 342 | 0.87 | 1.15 | 0.64 / 0.23 | 0.19 | -0.04 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=MIDDAY | 287 | 240 | 0.84 | 1.16 | 0.63 / 0.21 | 0.21 | -0.10 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | session=OPEN_90M | 59 | 59 | 0.99 | 1.08 | 0.68 / 0.24 | 0.19 | 0.06 | USEFUL ENTRIES + BAD CAPTURE |
| VOLREV:fade | regime=HIGH_VOL | 346 | 284 | 0.84 | 1.16 | 0.64 / 0.21 | 0.21 | -0.07 | USEFUL ENTRIES + BAD CAPTURE |
