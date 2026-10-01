# Lane X - entry quality vs exit quality, and same-entry exit-policy shadow comparison

`eeq-1.0` / labels `eeq-labels-1` / policies `eeq-policies-1` / study `eeq-study-1`.

**HINDSIGHT DIAGNOSTICS on development frames. No edge / expectancy claim. Nothing is fitted, searched or promoted; the exit-policy numbers are hypotheses - forward evidence decides promotion.** Dev window ends 2026-08-31 (Berlin date); no holdout bar was opened. BTCUSD / BRENT have no frozen Train/holdout split at all (all their history is development data, STRUCT constants are discovery placeholders).

## Method (binding principle)

Negative final R != bad entry. Entry quality (MFE / MAE / timing / structure reached) and exit / profit-capture quality (capture ratio, giveback, policy R) are classified and reported SEPARATELY; no single final-R number is used as an entry verdict.

- Labels (predeclared, not tuned): POTENTIAL_USEFUL_ENTRY = MFE >= 0.5R; ENTRY_FAILURE = MFE < 0.25R and MAE >= 0.75R; everything else AMBIGUOUS. Capture (judged for useful entries only): GOOD if final_R/MFE >= 0.5, else POOR_PROFIT_CAPTURE (= EXIT_GIVEBACK). Combined: GOOD_ENTRY_GOOD_CAPTURE | POTENTIAL_USEFUL_ENTRY_EXIT_GIVEBACK | ENTRY_FAILURE | AMBIGUOUS.
- MFE_CAPTURE_RATIO = final_R / MFE, defined only for MFE >= 0.1R (else undefined: no favourable excursion), SIGNED (a +0.6R MFE that ends -1R is -1.67); the floored version clip(max(final,0)/MFE,0,1) is what the tables average. MFE_GIVEBACK = max(0, MFE - final_R).
- 'final R' of the entry verdict = the production baseline (fixed 1.5R, spread-adjusted gross, stop-first, forced flat at the Berlin deadline). MFE / MAE are the POTENTIAL path: they run to the initial stop or the flat deadline and are NOT cut by the 1.5R target.
- Verdict categories (shares per cell; INCONCLUSIVE-n if n < 30 or fewer than 20 independent event clusters): BAD ENTRIES = failure share >= 0.4; USEFUL ENTRIES + BAD CAPTURE = useful share >= 0.4 and >= 0.5 of the useful ones poorly captured; BOTH = both deficits (failure >= 0.4, useful >= 0.2, poor capture >= 0.5); else NO CLEAR DEFICIT.
- Dependence: event clusters = same market + same direction signals chain-linked within 3600 s; STRUCT entries are keyed by their break (structure_event_id), so the four STRUCT variants of one break are ONE observation. Market clusters (existing risk-policy config: GER40/NAS100/SPX500 = INDEX, ...) chain same-direction signals within 900 s across markets. Raw n is always shown next to clusters; means in JSON also exist cluster-equalised.
- **NULL REFERENCE (read every share against it):** for a driftless path P(touch +x R before the -1R stop) = 1/(1+x): 0.25R 0.80, 0.5R 0.67, 0.75R 0.57, 1R 0.50, 1.5R 0.40, 2R 0.33. With zero edge ~two thirds of entries are therefore 'useful' (MFE >= 0.5R) and ~25 % 'failures' by construction, and a 1.5R cap captures little of the MFE; the verdict categories classify cells against the PREDECLARED thresholds, they are NOT a test against this null and do not show that any entry is better than random. Compare the P(MFE>=x) columns with this line.
- Timing is dated by the bar OPEN (existing convention); resolution 5 min. Entry fill = next bar open (long at ask); no latency / slippage / commission; signal age is 0 by construction offline (real trades record it).
- Exit policies (same entry, same stop, same bars, stop-first, ratchets apply from the next bar; engine = existing `exits.ExitEngine` + E2 structure): `P1_FIXED_1_5R`; `P2_STRUCT_TP1`; `P3_STRUCT_TP1_TP2`; `P4_TP1_TP2_RUNNER`; `P5_STRUCT_TRAIL`; `P6_BREAKEVEN_LOCK`; `P7_MOMENTUM_EXIT`; `P8_TIME_ALPHA`; `P9_EOD_FORCED_FLAT`. Parameters (versioned, not searched): `{"atr_buffer_mult": 0.25, "be_trigger_r": "1.0", "fixed_r": "1.5", "momentum_bars": 3, "momentum_threshold_atr": "-1.0", "runner_fractions": ["0.5", "0.25"], "sizing": "normalised position 1.0; R = spread-adjusted gross against initial risk |fill-stop|", "swing_n": 2, "tick_model": "T0 open, T1 adverse extreme, T2 favourable extreme, T3 close; stop-first; ratchets apply from the next bar", "time_stop_bars": 24, "time_stop_min_mfe_r": "0.5", "tp12_fractions": ["0.5", "0.5"], "version": "eeq-policies-1"}`.
- An entry without the required structural level is NOT_APPLICABLE for that policy (no level is invented); the `paired-structural n` column counts the entries on which P2, P3 and P4 all apply.

## Independence: raw entries vs event clusters vs cross-market clusters

| market cluster | raw entries | event clusters | cross-market clusters |
|---|---|---|---|
| CRYPTO | 1795 | 1041 | 1032 |
| ENERGY | 2936 | 1651 | 1670 |
| FX | 2867 | 1562 | 2201 |
| INDEX | 18024 | 5734 | 8140 |
| METAL | 7180 | 1782 | 4635 |

## Headline: verdict per market x family:variant (all directions)

| market | family:variant | n (clusters) | useful / failure share | capture (floored) | baseline R | TP1+TP2 R | runner R | verdict |
|---|---|---|---|---|---|---|---|---|
| GER40 | EOD:continue | 95 (95) | 0.64 / 0.27 | 0.33 | 0.09 | -0.07(38) | -0.08(90) | NO CLEAR DEFICIT |
| GER40 | EOD:reverse | 95 (95) | 0.68 / 0.20 | 0.25 | -0.15 | -0.12(95) | -0.01(95) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | GAP:fade | 92 (92) | 0.68 / 0.17 | 0.21 | 0.09 | +0.04(86) | +0.03(92) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | GAP:go | 87 (87) | 0.68 / 0.20 | 0.25 | 0.15 | +0.15(15) | -0.05(75) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | LEADLAG | 453 (294) | 0.58 / 0.32 | 0.22 | -0.20 | -0.17(361) | -0.11(450) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | ORB:breakout | 370 (370) | 0.63 / 0.22 | 0.25 | -0.03 | -0.01(159) | -0.02(289) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | ORB:fade | 311 (311) | 0.63 / 0.28 | 0.25 | 0.03 | -0.06(296) | -0.02(311) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | OVERNIGHT:continue | 93 (93) | 0.69 / 0.17 | 0.29 | 0.24 | -0.02(51) | +0.01(87) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | OVERNIGHT:reverse | 93 (93) | 0.65 / 0.23 | 0.24 | -0.01 | -0.06(92) | -0.02(93) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | ROUND:break | 1733 (1307) | 0.62 / 0.25 | 0.23 | -0.05 | -0.06(1185) | -0.04(1669) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | ROUND:reject | 4023 (2246) | 0.64 / 0.23 | 0.23 | -0.05 | -0.04(3307) | -0.03(3911) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | VOLREV:expand | 1688 (1389) | 0.65 / 0.25 | 0.22 | -0.03 | -0.05(791) | -0.04(1319) | USEFUL ENTRIES + BAD CAPTURE |
| GER40 | VOLREV:fade | 511 (443) | 0.67 / 0.22 | 0.24 | -0.03 | -0.06(511) | -0.00(511) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | EOD:continue | 76 (76) | 0.66 / 0.25 | 0.38 | 0.11 | +0.11(33) | -0.02(71) | NO CLEAR DEFICIT |
| NAS100 | EOD:reverse | 76 (76) | 0.62 / 0.21 | 0.33 | 0.01 | -0.01(76) | -0.01(76) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | GAP:fade | 73 (73) | 0.68 / 0.26 | 0.29 | 0.10 | +0.01(53) | -0.01(72) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | GAP:go | 96 (96) | 0.65 / 0.23 | 0.22 | -0.11 | -0.12(40) | +0.00(87) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | LEADLAG | 186 (165) | 0.70 / 0.24 | 0.29 | 0.15 | -0.03(136) | +0.04(184) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | ORB:breakout | 322 (322) | 0.60 / 0.19 | 0.33 | 0.02 | -0.00(118) | +0.01(294) | NO CLEAR DEFICIT |
| NAS100 | ORB:fade | 260 (260) | 0.62 / 0.26 | 0.29 | 0.02 | -0.02(244) | -0.09(260) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | OVERNIGHT:continue | 83 (83) | 0.63 / 0.23 | 0.23 | -0.07 | -0.04(57) | +0.04(79) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | OVERNIGHT:reverse | 83 (83) | 0.65 / 0.29 | 0.22 | -0.01 | +0.01(70) | +0.09(83) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | ROUND:break | 950 (694) | 0.67 / 0.20 | 0.26 | -0.03 | -0.01(598) | +0.01(929) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | ROUND:reject | 2151 (1130) | 0.63 / 0.25 | 0.28 | -0.02 | -0.06(1673) | -0.03(2113) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | VOLREV:expand | 769 (618) | 0.60 / 0.29 | 0.27 | -0.03 | -0.13(300) | -0.04(629) | USEFUL ENTRIES + BAD CAPTURE |
| NAS100 | VOLREV:fade | 265 (227) | 0.66 / 0.22 | 0.25 | -0.01 | -0.02(265) | -0.03(265) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | EOD:continue | 75 (75) | 0.71 / 0.21 | 0.39 | 0.16 | +0.08(24) | +0.09(56) | NO CLEAR DEFICIT |
| SPX500 | EOD:reverse | 75 (75) | 0.51 / 0.39 | 0.34 | -0.15 | -0.17(75) | -0.19(75) | NO CLEAR DEFICIT |
| SPX500 | GAP:fade | 74 (74) | 0.62 / 0.32 | 0.24 | 0.05 | -0.01(51) | -0.06(70) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | GAP:go | 90 (90) | 0.61 / 0.31 | 0.23 | -0.11 | -0.14(26) | +0.03(65) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | LEADLAG | 184 (172) | 0.64 / 0.30 | 0.26 | -0.05 | -0.02(125) | -0.02(171) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | ORB:breakout | 322 (322) | 0.63 / 0.22 | 0.32 | 0.02 | -0.06(131) | -0.00(236) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | ORB:fade | 265 (265) | 0.65 / 0.28 | 0.26 | 0.00 | +0.03(246) | +0.02(263) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | OVERNIGHT:continue | 79 (79) | 0.62 / 0.28 | 0.23 | -0.11 | -0.15(48) | -0.01(69) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | OVERNIGHT:reverse | 79 (79) | 0.65 / 0.30 | 0.23 | 0.08 | +0.15(65) | +0.08(78) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | ROUND:break | 218 (200) | 0.62 / 0.25 | 0.30 | 0.09 | -0.12(109) | -0.01(194) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | ROUND:reject | 490 (347) | 0.62 / 0.26 | 0.28 | -0.06 | -0.08(362) | -0.09(459) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | VOLREV:expand | 798 (704) | 0.62 / 0.28 | 0.28 | -0.01 | -0.15(251) | -0.08(461) | USEFUL ENTRIES + BAD CAPTURE |
| SPX500 | VOLREV:fade | 241 (222) | 0.64 / 0.22 | 0.26 | -0.03 | -0.06(240) | -0.04(241) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | EOD:continue | 78 (78) | 0.68 / 0.18 | 0.23 | -0.06 | +0.07(22) | -0.01(66) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | EOD:reverse | 78 (78) | 0.76 / 0.18 | 0.24 | -0.07 | -0.01(78) | +0.05(78) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | GAP:fade | 77 (77) | 0.64 / 0.26 | 0.19 | 0.07 | -0.05(75) | -0.01(76) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | GAP:go | 80 (80) | 0.64 / 0.28 | 0.22 | -0.03 | -0.10(47) | -0.07(70) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | ORB:breakout | 319 (319) | 0.61 / 0.25 | 0.19 | -0.12 | -0.08(220) | -0.06(280) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | ORB:fade | 282 (282) | 0.61 / 0.30 | 0.20 | -0.05 | +0.03(270) | +0.03(282) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | OVERNIGHT:continue | 79 (79) | 0.58 / 0.29 | 0.16 | -0.11 | -0.16(56) | -0.09(76) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | OVERNIGHT:reverse | 79 (79) | 0.66 / 0.23 | 0.20 | 0.04 | -0.11(78) | -0.02(79) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | ROUND:break | 1620 (1058) | 0.62 / 0.25 | 0.20 | -0.11 | -0.07(1203) | -0.03(1550) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | ROUND:reject | 3223 (1502) | 0.63 / 0.25 | 0.21 | -0.05 | -0.06(2771) | -0.05(3151) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | VOLREV:expand | 919 (764) | 0.62 / 0.26 | 0.20 | -0.09 | -0.10(518) | -0.03(726) | USEFUL ENTRIES + BAD CAPTURE |
| XAUUSD | VOLREV:fade | 346 (284) | 0.64 / 0.21 | 0.21 | -0.07 | -0.05(346) | -0.03(346) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | EOD:continue | 77 (77) | 0.58 / 0.25 | 0.27 | 0.01 | -0.11(27) | -0.07(66) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | EOD:reverse | 77 (77) | 0.68 / 0.23 | 0.31 | 0.05 | -0.01(77) | -0.07(77) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | GAP:fade | 78 (78) | 0.65 / 0.26 | 0.19 | -0.17 | -0.16(69) | -0.10(71) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | GAP:go | 72 (72) | 0.58 / 0.29 | 0.17 | -0.10 | -0.15(31) | -0.18(61) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | ORB:breakout | 306 (306) | 0.61 / 0.27 | 0.21 | -0.13 | -0.15(166) | -0.10(229) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | ORB:fade | 262 (262) | 0.57 / 0.34 | 0.21 | -0.12 | -0.17(233) | -0.20(259) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | OVERNIGHT:continue | 74 (74) | 0.55 / 0.27 | 0.17 | -0.19 | -0.09(42) | -0.12(69) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | OVERNIGHT:reverse | 74 (74) | 0.74 / 0.16 | 0.20 | 0.01 | -0.06(70) | -0.09(71) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | ROUND:break | 159 (135) | 0.63 / 0.24 | 0.22 | -0.04 | -0.08(100) | -0.13(145) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | ROUND:reject | 406 (280) | 0.63 / 0.26 | 0.21 | -0.08 | -0.13(324) | -0.04(380) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | VOLREV:expand | 973 (876) | 0.58 / 0.29 | 0.20 | -0.16 | -0.13(386) | -0.15(608) | USEFUL ENTRIES + BAD CAPTURE |
| EURUSD | VOLREV:fade | 309 (295) | 0.63 / 0.23 | 0.24 | -0.04 | -0.03(309) | -0.00(309) | USEFUL ENTRIES + BAD CAPTURE |
| BRENT | STRUCT:breakout | 961 (961) | 0.53 / 0.28 | 0.27 | -0.12 | -0.10(401) | -0.12(564) | USEFUL ENTRIES + BAD CAPTURE |
| BRENT | STRUCT:confirmed | 757 (757) | 0.53 / 0.26 | 0.28 | -0.05 | -0.06(284) | -0.07(420) | USEFUL ENTRIES + BAD CAPTURE |
| BRENT | STRUCT:fade | 592 (592) | 0.52 / 0.38 | 0.22 | -0.24 | -0.22(533) | -0.17(566) | USEFUL ENTRIES + BAD CAPTURE |
| BRENT | STRUCT:retest | 626 (626) | 0.53 / 0.27 | 0.27 | -0.07 | -0.09(261) | -0.07(395) | USEFUL ENTRIES + BAD CAPTURE |
| BTCUSD | STRUCT:breakout | 580 (580) | 0.49 / 0.31 | 0.24 | -0.18 | -0.21(260) | -0.12(357) | USEFUL ENTRIES + BAD CAPTURE |
| BTCUSD | STRUCT:confirmed | 450 (450) | 0.51 / 0.29 | 0.25 | -0.12 | -0.22(196) | -0.10(291) | USEFUL ENTRIES + BAD CAPTURE |
| BTCUSD | STRUCT:fade | 392 (392) | 0.52 / 0.37 | 0.26 | -0.11 | -0.15(358) | -0.14(380) | USEFUL ENTRIES + BAD CAPTURE |
| BTCUSD | STRUCT:retest | 373 (373) | 0.51 / 0.31 | 0.24 | -0.11 | -0.22(172) | -0.10(259) | USEFUL ENTRIES + BAD CAPTURE |

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

## R2 store copy (real trades / labelled counterfactuals)

7 rows. classification from stored MFE/MAE/R (fill assumptions differ: real = actual fills, counterfactual = intended entry, no costs); path-level fields exist only for trades recorded after the Lane X hook

| cell | n | clusters | MFE med | MAE med | useful / failure | capture | verdict |
|---|---|---|---|---|---|---|---|
| COUNTERFACTUAL|NAS100|ROUND|short | 1 | 1 | 1.50 | 0.33 | 1.00 / 0.00 | 1.00 | INCONCLUSIVE-n |
| COUNTERFACTUAL|SPX500|ROUND|short | 2 | 1 | 0.49 | 1.13 | 0.50 / 0.00 | 0.00 | INCONCLUSIVE-n |
| COUNTERFACTUAL|SPX500|VOLREV|short | 1 | 1 | 0.50 | 1.23 | 1.00 / 0.00 | 0.00 | INCONCLUSIVE-n |
| REAL_TRADE|NAS100|VOLREV|short | 1 | 1 | 0.61 | 1.00 | 1.00 / 0.00 | 0.00 | INCONCLUSIVE-n |
| REAL_TRADE|SPX500|ROUND|long | 1 | 1 | 0.18 | 1.00 | 0.00 / 1.00 | 0.00 | INCONCLUSIVE-n |
| REAL_TRADE|SPX500|ROUND|short | 1 | 1 | 0.00 | 1.03 | 0.00 / 1.00 | - | INCONCLUSIVE-n |

## Data used and missing

- GER40: 2025-02-10..2026-08-31 (95011 M5 bars; evaluated from 2025-03-12); n_entries=9644
- NAS100: 2025-05-02..2026-08-31 (94213 M5 bars; evaluated from 2025-06-01); n_entries=5390
- SPX500: 2025-05-02..2026-08-31 (94212 M5 bars; evaluated from 2025-06-01); n_entries=2990
- XAUUSD: 2025-05-05..2026-08-31 (94194 M5 bars; evaluated from 2025-06-04); n_entries=7180
- EURUSD: 2025-05-26..2026-08-31 (94458 M5 bars; evaluated from 2025-06-25); n_entries=2867
- BRENT: 2025-06-01..2026-08-31 (82487 M5 bars; evaluated from 2025-07-01); n_entries=2936
- BTCUSD: 2025-09-24..2026-08-31 (75015 M5 bars; evaluated from 2025-10-24); n_entries=1795
- shadow markets: 100 shadow specs (configs/markets_shadow) - NO bar history exists in the repo / data junction / Lane F root for any of them; the analysis accepts any MarketInputs frame, so they can be added as soon as shadow bars are recorded (Lane U2 scanner). Family applicability: fit-free STRUCT only; other families need Train-fitted thresholds and valid session semantics -> NO_SETUP until then.

## Caveats

- Hindsight diagnostics on a development window; entries of overlapping signals are not independent (see clusters); no edge claim, no promotion.
- GER40/NAS100/SPX500 are strongly correlated (INDEX cluster): their entries share the same market moves; see the market-cluster table.
- Fills at the next bar open (long: ask), stop-first bar semantics, spread-adjusted gross R, no commission / slippage / swap; intrabar order of high/low is unknown (conservative: adverse first).
- Structural TP1/TP2/trail come from the E2 fractal geometry on closed M5/M15 bars; absent levels -> NOT_APPLICABLE, never invented.
- Development end 2026-08-31 (Berlin date) is enforced by the existing loaders; BTCUSD/BRENT have no frozen split, their STRUCT constants are discovery placeholders and BTC M5 history lacks Oct-2025 and Mar-2026 (DST fold months).
- Family generators run on Train-fitted frozen thresholds (fit_end 2026-06-30 for the five core markets): the evaluation window overlaps that fit window for part of the data (in-sample for thresholds) - a further reason the numbers are descriptive only.
- The exit-policy parameters are predeclared constants (POLICY_SET_VERSION); no threshold was searched or tuned on these results.
