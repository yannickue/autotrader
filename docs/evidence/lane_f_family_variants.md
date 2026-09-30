# Lane F - STRUCT family variant measurement (lane-f-variants-v1)

**PHASE2_DISCOVERY / NOT_ALPHA_VALIDATED.** Hindsight diagnostics on development frames (no bar after 2026-08-31 Berlin date). Nothing is fitted, tuned or promoted; there is no frozen Train/holdout split for these markets. No expectancy/edge is claimed.

Constants (DISCOVERY PLACEHOLDERS, not fitted): `{"version": "struct-discovery-placeholders-v1", "comp_max_sqrt": 0.8, "min_width_sqrt": 0.25, "expansion_tr_atr": 1.0, "stop_buffer_atr": 0.25, "fail_bars": 6, "retest_bars": 12, "retest_tol_atr": 0.25, "cooldown": 12, "status": "DISCOVERY_PLACEHOLDER_NOT_FITTED"}`

Hypothetical walk: entry next bar open (long at ask=open+spread, short at bid), structural stop, 1.5R target, stop-first pessimistic, horizon 48 bars; candidates without a fully contiguous 12-bar look-ahead are excluded. Offline window = full day (session windows are operating policy, not family logic).

## BTCUSD  (75015 M5 bars, 2025-09-24 10:45:00+00:00 .. 2026-08-31 21:55:00+00:00)

| variant | n (L/S) | flag | TP-before-SL | SL share | horizon | mean R (1.5R tgt) | med MFE R | med MAE R | t_MFE/t_MAE bars | med stop ATR | spread/price | spread/stop | >20% of 1R | movement_to_cost | meanR 1st/2nd half (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| breakout | 792 (380/412) | ok | 0.18 | 0.47 | 0.36 | -0.181 | 0.62 | 0.97 | 17/25 | 4.22 | 0.00060 | 0.109 | 0.16 | 5.8 | -0.169/-0.193 (401/391) |
| confirmed | 613 (303/310) | ok | 0.18 | 0.43 | 0.39 | -0.130 | 0.58 | 0.87 | 17/24 | 4.44 | 0.00062 | 0.100 | 0.13 | 5.9 | -0.123/-0.137 (309/304) |
| retest | 491 (246/245) | ok | 0.20 | 0.45 | 0.35 | -0.135 | 0.61 | 0.93 | 18/24 | 4.17 | 0.00062 | 0.106 | 0.14 | 5.9 | -0.133/-0.138 (251/240) |
| fade | 518 (288/230) | ok | 0.29 | 0.63 | 0.09 | -0.182 | 1.53 | 1.81 | 25/19 | 2.02 | 0.00061 | 0.217 | 0.56 | 6.7 | -0.184/-0.181 (270/248) |

Per-break diagnostics (n_range=24, variant-independent): n=1036 (ok), false-break rate (close back inside within 6 bars) = 0.6071428571428571, reversal to the opposite edge within 4 h = 0.4671814671814672, follow-through >= 1 range width within 4 h = 0.5019305019305019.

Base-rate control (coverage_analysis.control, STRONG = 3 ATR favourable within 24 bars before 1 ATR adverse): breakout triggers n=732 strong-share=0.21721311475409835, random-bar base rate=0.21882288061899519; matched-hour sample=0.22117486338797815; verdict: indistinguishable from random bars (base rate inside the trigger 95% CI).

## BRENT  (82487 M5 bars, 2025-06-01 22:00:00+00:00 .. 2026-08-31 20:55:00+00:00)

| variant | n (L/S) | flag | TP-before-SL | SL share | horizon | mean R (1.5R tgt) | med MFE R | med MAE R | t_MFE/t_MAE bars | med stop ATR | spread/price | spread/stop | >20% of 1R | movement_to_cost | meanR 1st/2nd half (n) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| breakout | 1014 (504/510) | ok | 0.18 | 0.42 | 0.40 | -0.098 | 0.70 | 0.85 | 22/18 | 4.10 | 0.00061 | 0.088 | 0.10 | 8.0 | -0.190/0.014 (557/457) |
| confirmed | 803 (404/399) | ok | 0.16 | 0.37 | 0.47 | -0.047 | 0.66 | 0.76 | 23/18 | 4.39 | 0.00062 | 0.083 | 0.10 | 8.2 | -0.145/0.076 (447/356) |
| retest | 664 (331/333) | ok | 0.18 | 0.40 | 0.42 | -0.064 | 0.72 | 0.79 | 24/19 | 4.10 | 0.00062 | 0.088 | 0.10 | 8.1 | -0.185/0.084 (365/299) |
| fade | 620 (327/293) | ok | 0.27 | 0.66 | 0.07 | -0.260 | 1.40 | 1.87 | 21/21 | 1.93 | 0.00062 | 0.192 | 0.47 | 7.2 | -0.340/-0.163 (340/280) |

Per-break diagnostics (n_range=24, variant-independent): n=1250 (ok), false-break rate (close back inside within 6 bars) = 0.5624, reversal to the opposite edge within 4 h = 0.4672, follow-through >= 1 range width within 4 h = 0.5512.

Base-rate control (coverage_analysis.control, STRONG = 3 ATR favourable within 24 bars before 1 ATR adverse): breakout triggers n=1002 strong-share=0.2215568862275449, random-bar base rate=0.2059064275028875; matched-hour sample=0.21477045908183634; verdict: indistinguishable from random bars (base rate inside the trigger 95% CI).

## Caveats

- Neighbouring bars and breaks cluster strongly; counts are not independent, intervals would be optimistic.
- BTCUSD M5 history starts 2025-09-24 and lacks Oct-2025 and Mar-2026 (DST fold months rejected as AmbiguousServerTime for 24/7 data); Brent M5 from 2025-06.
- Brent calendar is structural-proposal/probe-derived, BTC bars are recorded BID with recorded bar spread; the live tick spread is wider (see docs/V2_MARKETS.md).
- Variant choice for real trading was fixed a priori (confirmed breakout) BEFORE this measurement and is not changed by it.
