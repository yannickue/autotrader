# Observer Lab (research core)

Package: `src/coverage_analysis/observer_lab/` (labels, controls, stats, calibration, splits, enrichment).
Status of everything here: `OBSERVATION_ONLY_NOT_ALPHA_VALIDATED`. Offline only; nothing is imported by live code and nothing here may
change an entry, exit, stop, size, risk or execution decision. The package lives under `coverage_analysis` because only that package
(plus `alpha` and `demo.opportunity`) may import `alpha` under the isolation tests.

Question the lab answers: does the combination of objective market-structure features change the distribution of the NEXT price move
versus a MATCHED control, reproducibly and out of sample? It does not produce scores, signals or forecasts.

## Definitions and conventions

* **Event**: decision bar `i` (last CLOSED bar), direction, entry price (hypothetical fill), initial risk distance `R` in price units.
* **Labels** (`labels.label_event`, output `PostEventLabels`, columns `y_*` of `market_observer.schema`): strictly retrospective, only
  bars `j > i`. R multiples of the event's own `R`.
  * `y_fav<a>_before_adv<b>` for (0.25,0.25), (0.50,0.50), (0.75,0.50), (1.00,0.50): 1 favourable first, 0 adverse first, None = neither
    inside the horizon (censored; excluded from rates, never counted as 0).
  * **Tie rule: stop-first.** If one bar reaches both sides, the adverse side wins (same convention as `demo.labeling` and
    `demo.entry_exit_quality`; equality is pinned by a cross-check test on random paths, long/short, with/without spread).
  * Excursions run under a hypothetical 1R stop: the stop bar updates MAE but not MFE; a bar opening through the stop is a gap.
    `time_to_*` = seconds from the decision time to the OPEN of the extreme bar (first bar after the decision = 0).
  * `y_post_stop_favorable_excursion_r`: best favourable excursion on bars after the 1R stop bar (None if the stop was never hit).
  * `y_structural_target_reached` / `y_reclaim_or_followthrough`: caller-supplied target price / level, otherwise None.
  * **Horizon** = earlier of `max_bars` (default 48 = 4 h of M5), market-local session end (`flat_local_minute`, default the session's
    cash close), a `segment_id` change (data break) or a `local_day` change. **Costs are excluded** (bid bars, fill at entry); optional
    `spread_adjusted=True` uses the repo's exit-side convention (short exits at bid + spread).
* **Controls** (`controls.match_controls`): same market, matched on session bucket, local time of day (+-30 min), ATR percentile band
  (+-0.10 of the percentile rank of ATR/close), spread band (+-0.15 of the rank of spread/ATR), direction inherited from the event.
  Controls exclude every bar within `exclusion_bars` (= label horizon) of ANY event, are drawn without reuse, seeded
  (same seed => identical controls). Output: `MatchingReport` (match rate, unmatched events, standardised mean differences). A naive random bar
  group is NOT a control. `placebo_controls` takes a feature-lane callable for "real level vs matched artificial level" and
  "ratio vs non-ratio" placebos, with the same validation (in range, outside event neighbourhoods) and report.
* **Effect size** = `P(y|event) - P(y|matched control)`; controls of a cell are exactly the controls matched to the events of that cell.
* **Uncertainty** (`stats`): day-block bootstrap (whole local days of events and controls resampled together; seeded; default B = 2000,
  95 % percentile interval) and a bootstrap p-value with resolution `2/(B+1)`: choose B so that `2/(B+1) < alpha / m`, otherwise Holm
  can never reject. Wilson interval for single rates (reused from `coverage_analysis.control`).
* **Multiple testing**: every (feature, cell, label) is registered in a `HypothesisRegistry` BEFORE evaluation; Holm (default) or BH is
  applied with the FULL registered count `m` (unevaluated hypotheses count). The registry refuses results for unregistered hypotheses and
  refuses re-registering one. Report `registry.n_hypotheses` next to every claim.
* **Calibration** (`calibration`): Brier, log loss, equal-count reliability table, expected calibration error, logistic recalibration
  intercept/slope (ridge towards 0/1, finite under separation), AUC as a supplementary metric only, Brier improvement vs a baseline;
  all with day-block bootstrap intervals; any `(p_hat, y)` arrays. No model is trained here.
* **Splits** (`splits`): boundaries are the repo's own freeze constants (core thresholds fitted to 2026-06-30, dev end 2026-08-31,
  forward holdout from 2026-09-01). TRAIN | VALIDATION (last 25 % of the trading days up to the core fit end, a day-count fraction) |
  OOS (2026-07-01 .. 2026-08-31, frozen, touched once) | FORWARD (observe only, `FORWARD_NEVER_USED_FOR_FITTING`). Purge: an event whose
  label horizon reaches into another partition is dropped; embargo: the first `horizon` seconds after a boundary are dropped. Rolling and
  expanding walk-forward windows (`walk_forward_windows`, `window_masks`) are an additional robustness check, same purge, embargo gap in
  trading days. Grouped by Berlin trading date, never shuffled; `guard_dev_only` refuses any bar after the dev end.

## INSUFFICIENT_EVIDENCE

Predeclared minimums (`stats.DEFAULT_MIN_EVIDENCE`): >= 30 events and >= 30 controls in the cell, >= 20 independent day blocks, and (when
`structure_event_id` clusters exist) >= 20 independent clusters. Below any of them the cell is `INSUFFICIENT_EVIDENCE`: the numbers are
shown, but no p-value enters the correction and no verdict is given. It means "cannot tell", NOT "no effect". It still counts in `m`.
Other statuses: `NOT_SIGNIFICANT` (adjusted p >= 0.05 or the interval contains 0), `SIGNIFICANT_ADJUSTED` (adjusted p < 0.05 and the
block-bootstrap interval excludes 0 - still not an edge claim).

## Gate logic (before any forecast model)

1. Labels reproduce the repo's conventions (cross-check tests green) and features are causal (prefix-invariance tests of the feature lanes).
2. Controls are balanced (SMD of the matching variables small, match rate reported) and placebo controls (artificial levels, non-ratio)
   do not show the same enrichment as the real feature.
3. On TRAIN: single-feature / base+group enrichment shows something that survives the registered multiplicity AND differs from the
   overall event-vs-control gap (`base_delta`; a cell that merely mirrors the gap carries no feature information).
4. The same cells, frozen, are evaluated once on VALIDATION, then once on OOS; rolling/expanding windows must not contradict them.
5. Only then may a forecast model be fitted (TRAIN only), and it is judged by calibration + Brier improvement vs the control/base-rate
   baseline on VALIDATION/OOS, never by AUC alone. The forward period is observed only.

## How to read results

`delta` = event rate minus matched-control rate in percentage points of the outcome; `n_blocks` = independent days; `adjusted_p` is the
registry-wide Holm/BH value. `base_delta` (single) is the overall gap for the label; for `incremental` rows `delta` is
`delta(base AND cell) - delta(base)`. `warmup_ok` (optional column) excludes events inside a feature warm-up; the report counts excluded
events, excluded controls and orphaned controls. Table columns are asserted disjoint (`schema.assert_disjoint`) and every
`f_*_ts_ns` must be <= `decision_ts_ns` (`CausalityError`).

## Honest limits

* Bar-resolution labels (M5): intrabar order is unknown; stop-first is conservative for the favourable label, not a fill model.
* Costs, slippage, latency and the real stop are excluded; first-passage rates are not P&L.
* Percentile ranks used for matching are descriptive over the supplied sample, not causal features.
* 07-01..08-31 is "frozen OOS" only relative to the observer and to the core thresholds; other repo lanes (alpha V2 folds) have used those
  dev days for validation. Treat OOS as weaker than a never-seen period; the forward period is the only untouched data.
* Day blocks assume independence between days; regime clustering across weeks is not modelled (use rolling windows as a check).
* No edge is claimed anywhere in this package.

## Deferred hypotheses (not implemented; shadow/OOS only, never live)

* Volatility-normalised confirmed swings (minimum displacement in ATR; inspired by the ZigZag indicator of QuantConnect LEAN) as a future
  SHADOW challenger against the fixed 2-left/2-right fractal pivot of the swings group. To be tested only as shadow / out of sample.
* Real exchange volume or a volume profile, only if a real volume source ever exists (MT5 tick volume is a tick count, not volume).

No external code is imported; external repositories are references for definitions and test patterns only.
