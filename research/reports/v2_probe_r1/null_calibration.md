# V2 probe null calibration (Train side of search fold 0)

Null types: A sign_flip, B block_shuffle, C zero_drift. Empirical p = (1 + #null>=real)/(1 + K), one-sided;
resolution is 1/(K+1): K = number of null runs.

## EURUSD
real: passers 193, best E 0.3166, best t 0.93, n(E>0) 29, n(t>2) 0, p95 E 0.1021, median E -0.2094; distinct behaviours (corr>0.8) 98 of 193
deflated Sharpe (informational, N_eff 98): best-t candidate {'sharpe_day': 0.074717, 'sr0': 0.285894, 'skew': 5.0635, 'kurt': 37.1565, 'dsr': 0.000624}; best-E candidate {'sharpe_day': 0.074717, 'sr0': 0.285894, 'skew': 5.0635, 'kurt': 37.1565, 'dsr': 0.000624}
- null A (3 runs):
  - n_passers: real 193 | null min 162.0 p50 166.0 p95 183.1 p99 184.62 max 185.0 | p=0.25
  - best_e: real 0.3166 | null min 0.4266 p50 0.43598 p95 1.54876 p99 1.64768 max 1.67241 | p=1.0
  - best_t: real 0.9333 | null min 0.94993 p50 1.14151 p95 1.63318 p99 1.67688 max 1.68781 | p=1.0
  - n_e_pos: real 29 | null min 14.0 p50 23.0 p95 56.3 p99 59.26 max 60.0 | p=0.5
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.0 p99 0.0 max 0.0 | p=1.0
  - p95_e: real 0.1021 | null min 0.03332 p50 0.12487 p95 0.55623 p99 0.59458 max 0.60416 | p=0.75
- null B (3 runs):
  - n_passers: real 193 | null min 164.0 p50 191.0 p95 195.5 p99 195.9 max 196.0 | p=0.5
  - best_e: real 0.3166 | null min 0.18055 p50 0.18094 p95 0.30099 p99 0.31166 max 0.31433 | p=0.25
  - best_t: real 0.9333 | null min 0.59942 p50 0.67869 p95 1.47592 p99 1.54679 max 1.56451 | p=0.5
  - n_e_pos: real 29 | null min 4.0 p50 9.0 p95 27.0 p99 28.6 max 29.0 | p=0.5
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.0 p99 0.0 max 0.0 | p=1.0
  - p95_e: real 0.1021 | null min -0.03568 p50 -0.02453 p95 0.08399 p99 0.09364 max 0.09605 | p=0.25
- null C (3 runs):
  - n_passers: real 193 | null min 161.0 p50 167.0 p95 185.9 p99 187.58 max 188.0 | p=0.25
  - best_e: real 0.3166 | null min 0.22591 p50 0.24927 p95 0.50371 p99 0.52633 max 0.53198 | p=0.5
  - best_t: real 0.9333 | null min 0.84205 p50 0.97418 p95 1.49231 p99 1.53837 max 1.54988 | p=0.75
  - n_e_pos: real 29 | null min 6.0 p50 10.0 p95 28.0 p99 29.6 max 30.0 | p=0.5
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.0 p99 0.0 max 0.0 | p=1.0
  - p95_e: real 0.1021 | null min -0.03555 p50 0.02193 p95 0.20007 p99 0.2159 max 0.21986 | p=0.5
- null ALL (9 runs):
  - n_passers: real 193 | null min 161.0 p50 167.0 p95 194.0 p99 195.6 max 196.0 | p=0.2
  - best_e: real 0.3166 | null min 0.18055 p50 0.31433 p95 1.21624 p99 1.58117 max 1.67241 | p=0.5
  - best_t: real 0.9333 | null min 0.59942 p50 0.97418 p95 1.63849 p99 1.67794 max 1.68781 | p=0.7
  - n_e_pos: real 29 | null min 4.0 p50 14.0 p95 48.0 p99 57.6 max 60.0 | p=0.4
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.0 p99 0.0 max 0.0 | p=1.0
  - p95_e: real 0.1021 | null min -0.03568 p50 0.03332 p95 0.45044 p99 0.57342 max 0.60416 | p=0.4

## GER40
real: passers 250, best E 0.3936, best t 2.25, n(E>0) 75, n(t>2) 1, p95 E 0.1709, median E -0.1029; distinct behaviours (corr>0.8) 185 of 250
deflated Sharpe (informational, N_eff 185): best-t candidate {'sharpe_day': 0.177985, 'sr0': 0.27186, 'skew': 0.5774, 'kurt': 7.1473, 'dsr': 0.111785}; best-E candidate {'sharpe_day': 0.177985, 'sr0': 0.27186, 'skew': 0.5774, 'kurt': 7.1473, 'dsr': 0.111785}
- null A (6 runs):
  - n_passers: real 250 | null min 245.0 p50 256.0 p95 286.0 p99 288.4 max 289.0 | p=0.7143
  - best_e: real 0.3936 | null min 0.3157 p50 0.39761 p95 0.56774 p99 0.57297 max 0.57428 | p=0.5714
  - best_t: real 2.249 | null min 1.42059 p50 2.06268 p95 2.62761 p99 2.7494 max 2.77985 | p=0.2857
  - n_e_pos: real 75 | null min 45.0 p50 64.0 p95 109.75 p99 115.55 max 117.0 | p=0.4286
  - n_t_gt2: real 1 | null min 0.0 p50 1.0 p95 2.75 p99 2.95 max 3.0 | p=0.7143
  - p95_e: real 0.1709 | null min 0.11378 p50 0.1563 p95 0.21232 p99 0.22004 max 0.22198 | p=0.4286
- null B (6 runs):
  - n_passers: real 250 | null min 230.0 p50 266.0 p95 292.5 p99 294.5 max 295.0 | p=0.7143
  - best_e: real 0.3936 | null min 0.23621 p50 0.41833 p95 0.47513 p99 0.47658 max 0.47694 | p=0.7143
  - best_t: real 2.249 | null min 1.357 p50 1.64368 p95 2.36831 p99 2.45513 max 2.47684 | p=0.2857
  - n_e_pos: real 75 | null min 24.0 p50 35.0 p95 77.0 p99 80.2 max 81.0 | p=0.2857
  - n_t_gt2: real 1 | null min 0.0 p50 0.0 p95 3.25 p99 3.85 max 4.0 | p=0.4286
  - p95_e: real 0.1709 | null min 0.02172 p50 0.11414 p95 0.21507 p99 0.24013 max 0.2464 | p=0.2857
- null C (6 runs):
  - n_passers: real 250 | null min 241.0 p50 258.0 p95 273.75 p99 275.55 max 276.0 | p=0.8571
  - best_e: real 0.3936 | null min 0.27906 p50 0.31126 p95 0.48083 p99 0.5226 max 0.53304 | p=0.2857
  - best_t: real 2.249 | null min 0.83551 p50 1.50303 p95 2.03574 p99 2.04465 max 2.04688 | p=0.1429
  - n_e_pos: real 75 | null min 28.0 p50 37.5 p95 91.25 p99 103.05 max 106.0 | p=0.2857
  - n_t_gt2: real 1 | null min 0.0 p50 0.0 p95 1.75 p99 1.95 max 2.0 | p=0.4286
  - p95_e: real 0.1709 | null min 0.03417 p50 0.07688 p95 0.21875 p99 0.24188 max 0.24766 | p=0.2857
- null ALL (18 runs):
  - n_passers: real 250 | null min 230.0 p50 258.0 p95 289.9 p99 293.98 max 295.0 | p=0.7368
  - best_e: real 0.3936 | null min 0.23621 p50 0.34323 p95 0.55205 p99 0.56983 max 0.57428 | p=0.4737
  - best_t: real 2.249 | null min 0.83551 p50 1.74413 p95 2.52229 p99 2.72834 max 2.77985 | p=0.1579
  - n_e_pos: real 75 | null min 24.0 p50 46.0 p95 107.65 p99 115.13 max 117.0 | p=0.2632
  - n_t_gt2: real 1 | null min 0.0 p50 0.0 p95 3.15 p99 3.83 max 4.0 | p=0.4737
  - p95_e: real 0.1709 | null min 0.02172 p50 0.12003 p95 0.24659 p99 0.24744 max 0.24766 | p=0.2632

## NAS100
real: passers 193, best E 0.4314, best t 1.47, n(E>0) 52, n(t>2) 0, p95 E 0.1219, median E -0.0681; distinct behaviours (corr>0.8) 126 of 193
deflated Sharpe (informational, N_eff 126): best-t candidate {'sharpe_day': 0.123799, 'sr0': 0.243053, 'skew': 0.9962, 'kurt': 5.5623, 'dsr': 0.068523}; best-E candidate {'sharpe_day': 0.08956, 'sr0': 0.243053, 'skew': 5.4342, 'kurt': 46.2103, 'dsr': 0.009941}
- null A (3 runs):
  - n_passers: real 193 | null min 171.0 p50 182.0 p95 186.5 p99 186.9 max 187.0 | p=0.25
  - best_e: real 0.4314 | null min 0.23205 p50 0.30179 p95 0.5263 p99 0.54626 max 0.55125 | p=0.5
  - best_t: real 1.471 | null min 1.35361 p50 1.87495 p95 1.95199 p99 1.95884 max 1.96055 | p=0.75
  - n_e_pos: real 52 | null min 26.0 p50 36.0 p95 53.1 p99 54.62 max 55.0 | p=0.5
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.0 p99 0.0 max 0.0 | p=1.0
  - p95_e: real 0.1219 | null min 0.05927 p50 0.1309 p95 0.13702 p99 0.13756 max 0.1377 | p=0.75
- null B (3 runs):
  - n_passers: real 193 | null min 145.0 p50 172.0 p95 194.5 p99 196.5 max 197.0 | p=0.5
  - best_e: real 0.4314 | null min 0.18086 p50 0.29041 p95 0.45667 p99 0.47145 max 0.47514 | p=0.5
  - best_t: real 1.471 | null min 1.54013 p50 1.71447 p95 2.49395 p99 2.56324 max 2.58056 | p=1.0
  - n_e_pos: real 52 | null min 48.0 p50 49.0 p95 93.1 p99 97.02 max 98.0 | p=0.5
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 1.8 p99 1.96 max 2.0 | p=1.0
  - p95_e: real 0.1219 | null min 0.08806 p50 0.16921 p95 0.19508 p99 0.19738 max 0.19795 | p=0.75
- null C (3 runs):
  - n_passers: real 193 | null min 171.0 p50 185.0 p95 187.7 p99 187.94 max 188.0 | p=0.25
  - best_e: real 0.4314 | null min 0.19075 p50 0.21699 p95 0.33736 p99 0.34806 max 0.35074 | p=0.25
  - best_t: real 1.471 | null min 1.22559 p50 1.47782 p95 1.86904 p99 1.90381 max 1.91251 | p=0.75
  - n_e_pos: real 52 | null min 10.0 p50 13.0 p95 66.1 p99 70.82 max 72.0 | p=0.5
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.0 p99 0.0 max 0.0 | p=1.0
  - p95_e: real 0.1219 | null min 0.00196 p50 0.02635 p95 0.17979 p99 0.19343 max 0.19684 | p=0.5
- null ALL (9 runs):
  - n_passers: real 193 | null min 145.0 p50 182.0 p95 193.4 p99 196.28 max 197.0 | p=0.2
  - best_e: real 0.4314 | null min 0.18086 p50 0.29041 p95 0.52081 p99 0.54516 max 0.55125 | p=0.3
  - best_t: real 1.471 | null min 1.22559 p50 1.71447 p95 2.33255 p99 2.53096 max 2.58056 | p=0.8
  - n_e_pos: real 52 | null min 10.0 p50 48.0 p95 87.6 p99 95.92 max 98.0 | p=0.4
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 1.2 p99 1.84 max 2.0 | p=1.0
  - p95_e: real 0.1219 | null min 0.00196 p50 0.1309 p95 0.19751 p99 0.19786 max 0.19795 | p=0.6

## SPX500
real: passers 186, best E 0.1722, best t 0.85, n(E>0) 22, n(t>2) 0, p95 E 0.0132, median E -0.1249; distinct behaviours (corr>0.8) 116 of 186
deflated Sharpe (informational, N_eff 116): best-t candidate {'sharpe_day': 0.073001, 'sr0': 0.282334, 'skew': -0.3873, 'kurt': 5.4525, 'dsr': 0.007615}; best-E candidate {'sharpe_day': 0.057614, 'sr0': 0.282334, 'skew': 2.4188, 'kurt': 21.6303, 'dsr': 0.002343}
- null A (3 runs):
  - n_passers: real 186 | null min 160.0 p50 161.0 p95 179.0 p99 180.6 max 181.0 | p=0.25
  - best_e: real 0.1722 | null min 0.06693 p50 0.13597 p95 0.27122 p99 0.28325 max 0.28625 | p=0.5
  - best_t: real 0.8539 | null min 0.63932 p50 0.75197 p95 1.01048 p99 1.03346 max 1.0392 | p=0.5
  - n_e_pos: real 22 | null min 3.0 p50 3.0 p95 15.6 p99 16.72 max 17.0 | p=0.25
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.0 p99 0.0 max 0.0 | p=1.0
  - p95_e: real 0.01316 | null min -0.0693 p50 -0.03648 p95 0.01932 p99 0.02428 max 0.02552 | p=0.5
- null B (3 runs):
  - n_passers: real 186 | null min 166.0 p50 171.0 p95 180.9 p99 181.78 max 182.0 | p=0.25
  - best_e: real 0.1722 | null min 0.41177 p50 0.44278 p95 0.47408 p99 0.47686 max 0.47756 | p=1.0
  - best_t: real 0.8539 | null min 1.52988 p50 2.08052 p95 2.72304 p99 2.78015 max 2.79443 | p=1.0
  - n_e_pos: real 22 | null min 21.0 p50 23.0 p95 26.6 p99 26.92 max 27.0 | p=0.75
  - n_t_gt2: real 0 | null min 0.0 p50 1.0 p95 1.0 p99 1.0 max 1.0 | p=1.0
  - p95_e: real 0.01316 | null min 0.0328 p50 0.05411 p95 0.12337 p99 0.12952 max 0.13106 | p=1.0
- null C (3 runs):
  - n_passers: real 186 | null min 128.0 p50 154.0 p95 168.4 p99 169.68 max 170.0 | p=0.25
  - best_e: real 0.1722 | null min 0.04335 p50 0.07045 p95 0.12761 p99 0.13269 max 0.13397 | p=0.25
  - best_t: real 0.8539 | null min 0.19944 p50 0.48433 p95 0.5518 p99 0.5578 max 0.5593 | p=0.25
  - n_e_pos: real 22 | null min 1.0 p50 2.0 p95 12.8 p99 13.76 max 14.0 | p=0.25
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.0 p99 0.0 max 0.0 | p=1.0
  - p95_e: real 0.01316 | null min -0.25511 p50 -0.17459 p95 -0.00302 p99 0.01223 max 0.01604 | p=0.5
- null ALL (9 runs):
  - n_passers: real 186 | null min 128.0 p50 166.0 p95 181.6 p99 181.92 max 182.0 | p=0.1
  - best_e: real 0.1722 | null min 0.04335 p50 0.13597 p95 0.46365 p99 0.47478 max 0.47756 | p=0.5
  - best_t: real 0.8539 | null min 0.19944 p50 0.75197 p95 2.50887 p99 2.73732 max 2.79443 | p=0.5
  - n_e_pos: real 22 | null min 1.0 p50 14.0 p95 25.4 p99 26.68 max 27.0 | p=0.3
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 1.0 p99 1.0 max 1.0 | p=1.0
  - p95_e: real 0.01316 | null min -0.25511 p50 0.01604 p95 0.10028 p99 0.12491 max 0.13106 | p=0.6

## XAUUSD
real: passers 197, best E 0.2996, best t 1.20, n(E>0) 10, n(t>2) 0, p95 E 0.0023, median E -0.2433; distinct behaviours (corr>0.8) 130 of 197
deflated Sharpe (informational, N_eff 130): best-t candidate {'sharpe_day': 0.101126, 'sr0': 0.369434, 'skew': 2.728, 'kurt': 13.6899, 'dsr': 0.000145}; best-E candidate {'sharpe_day': 0.101126, 'sr0': 0.369434, 'skew': 2.728, 'kurt': 13.6899, 'dsr': 0.000145}
- null A (6 runs):
  - n_passers: real 197 | null min 171.0 p50 203.0 p95 210.75 p99 210.95 max 211.0 | p=0.7143
  - best_e: real 0.2996 | null min 0.02626 p50 0.2742 p95 0.43297 p99 0.4675 max 0.47613 | p=0.4286
  - best_t: real 1.199 | null min 0.21396 p50 1.17573 p95 2.02012 p99 2.12112 max 2.14637 | p=0.4286
  - n_e_pos: real 10 | null min 1.0 p50 26.0 p95 49.5 p99 53.9 max 55.0 | p=0.7143
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.75 p99 0.95 max 1.0 | p=1.0
  - p95_e: real 0.0023 | null min -0.07252 p50 0.10991 p95 0.18368 p99 0.19329 max 0.19569 | p=0.7143
- null B (6 runs):
  - n_passers: real 197 | null min 195.0 p50 206.5 p95 213.75 p99 213.95 max 214.0 | p=0.8571
  - best_e: real 0.2996 | null min 0.11282 p50 0.32025 p95 0.46373 p99 0.4955 max 0.50344 | p=0.7143
  - best_t: real 1.199 | null min 0.48921 p50 1.606 p95 2.25697 p99 2.38332 max 2.41491 | p=0.7143
  - n_e_pos: real 10 | null min 4.0 p50 23.0 p95 55.0 p99 56.6 max 57.0 | p=0.8571
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 0.75 p99 0.95 max 1.0 | p=1.0
  - p95_e: real 0.0023 | null min -0.03123 p50 0.06536 p95 0.16636 p99 0.17474 max 0.17683 | p=0.8571
- null C (6 runs):
  - n_passers: real 197 | null min 169.0 p50 178.0 p95 199.0 p99 199.8 max 200.0 | p=0.2857
  - best_e: real 0.2996 | null min 0.09085 p50 0.23316 p95 0.4791 p99 0.4932 max 0.49672 | p=0.4286
  - best_t: real 1.199 | null min 0.4951 p50 1.01575 p95 2.24473 p99 2.31729 max 2.33542 | p=0.4286
  - n_e_pos: real 10 | null min 4.0 p50 12.0 p95 19.5 p99 19.9 max 20.0 | p=0.5714
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 2.25 p99 2.85 max 3.0 | p=1.0
  - p95_e: real 0.0023 | null min -0.07773 p50 0.01357 p95 0.05745 p99 0.05896 max 0.05933 | p=0.5714
- null ALL (18 runs):
  - n_passers: real 197 | null min 169.0 p50 201.0 p95 213.15 p99 213.83 max 214.0 | p=0.5789
  - best_e: real 0.2996 | null min 0.02626 p50 0.2742 p95 0.49773 p99 0.5023 max 0.50344 | p=0.4737
  - best_t: real 1.199 | null min 0.21396 p50 1.17573 p95 2.34735 p99 2.40139 max 2.41491 | p=0.4737
  - n_e_pos: real 10 | null min 1.0 p50 19.0 p95 55.3 p99 56.66 max 57.0 | p=0.6842
  - n_t_gt2: real 0 | null min 0.0 p50 0.0 p95 1.3 p99 2.66 max 3.0 | p=1.0
  - p95_e: real 0.0023 | null min -0.07773 p50 0.05556 p95 0.17966 p99 0.19249 max 0.19569 | p=0.6842

