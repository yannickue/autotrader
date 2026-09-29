# AD1 survivors report (Train + Validation only)

OOS TOUCHED: NO

**ROBUST POSITIVE TRAIN+VALIDATION EDGE FOUND: NO**

Verdict rule: YES only if at least one overlap-cluster representative passes ALL stages (C, D, E) AND its pooled day-clustered t exceeds the selection null bound sqrt(2 ln N_total_trials); INCONCLUSIVE if some representative passes all stages but none exceeds the null bound; otherwise NO.

## Search accounting (from pool meta.ledger)

| total trials | unique specs | param | structural | duplicate rejects | invalid rejects | cache hits |
|---|---|---|---|---|---|---|
| 10111 | 9927 | 3200 | 6911 | 184 | 0 | 184 |

Selection null bound: E[max t] ~ sqrt(2 ln N) = 4.54 for N=30310 (rough extreme-value bound, NOT a proof); Bonferroni N_eff = 29798 unique canonical specs.

Cumulative N: this campaign 10111 trials / 9927 unique specs + prior campaigns 20199 trials / 19871 unique specs (`--prior-trials`, `--prior-unique-specs`) = 30310 / 29798.

Stage-E neighbour evaluations added 0 param trials (pipeline ledger delta, not part of the search N).

## Survivors per stage

| stage | count |
|---|---|
| pool (distinct) | 800 |
| Stage A pass (Train screen) | 800 |
| Train-positive (COMBINED_ADVERSE) | 788 |
| Validation-positive (COMBINED_ADVERSE) | 109 |
| Train- AND Validation-positive | 108 |
| Stage C (validation gate) | 0 |
| Stage D (cost stress, pooled) | 0 |
| Stage E (stability) | 0 |
| overlap clusters (Jaccard >= 0.6) | 0 |

No finalists: no candidate passed all stages.

---
OOS TOUCHED: NO
**ROBUST POSITIVE TRAIN+VALIDATION EDGE FOUND: NO**
