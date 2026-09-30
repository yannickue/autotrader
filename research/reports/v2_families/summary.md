# V2 family probe (Train side of fold 0 only)

cumulative trials (incl. v1 cumulative): 31360, unique 30848 (this run: 1050 spec evaluations; 12600 shifted-null evaluations are diagnostics, not trials)

| market | family | specs | tr/day med | best fitness spec: n, E[R] adv, t | best E[R] adv (n>=min) | null pct fit / exp | p_best fit | min q_BH |
|---|---|---|---|---|---|---|---|---|
| GER40 | ORB | 150 | 0.80625 | 158, 0.23493, 1.981 | 0.23493 | 1.0 / 0.75 | 0.0769 | 0.99994 |
| GER40 | GAP | 150 | 0.25625 | 80, 0.09051, 0.544 | 0.18849 | 0.3333 / 0.3333 | 0.6923 | 0.99902 |
| GER40 | OVERNIGHT | 150 | 0.375 | 59, 0.12046, 0.921 | 0.19173 | 0.3333 / 0.4167 | 0.6923 | 0.99883 |
| GER40 | VOLREV | 150 | 0.78438 | 100, 0.02685, 0.32 | 0.09997 | 0.25 / 0.5 | 0.7692 | 1.0 |
| GER40 | ROUND | 150 | 4.25312 | 358, -0.0051, -0.071 | -0.0051 | 0.5833 / 0.5 | 0.4615 | 1.0 |
| GER40 | LEADLAG | 150 | 0.49062 | 56, 0.25178, 1.373 | 0.25178 | 0.75 / 0.75 | 0.3077 | 1.0 |
| GER40 | EOD | 150 | 0.4875 | 78, 0.12339, 1.056 | 0.12339 | 0.3333 / 0.0833 | 0.6923 | 0.99841 |
