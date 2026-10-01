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
  Controls exclude every bar within `exclusion_bars` (= label horizon) of ANY event AND of every bar in `exclude_idx` (the backfill passes
  ALL opportunities of the market, all families, so another family's real opportunity can never become a control), are drawn without
  reuse, seeded (same seed => identical controls). **Partition rule (`observer-controls-2`)**: a control is drawn WITHIN its event's
  partition (TRAIN->TRAIN, VALIDATION->VALIDATION, OOS->OOS, FORWARD->FORWARD) from per-bar labels (`bar_partitions` =
  `splits.assign_partitions` with the label horizon); PURGED / EMBARGO / UNASSIGNED bars are never controls and events in them are
  reported unmatched. Percentile ranks: `rank_mode="partition"` (default; rank among the bars of the bar's own partition),
  `"causal"` (expanding past-only rank, `min_history` bars; REQUIRED for FORWARD events, the matcher refuses anything else) or `"sample"`
  (legacy whole-sample rank, descriptive only). `partition=None` is the explicit legacy opt-out. Output: `MatchingReport` (match rate overall
  and `by_partition`, unmatched events, SMD, `n_events_in_excluded_partition`, `n_exclusion_bars_blocked`). A naive random bar
  group is NOT a control. `placebo_controls` takes a feature-lane callable for "real level vs matched artificial level" and
  "ratio vs non-ratio" placebos, with the same validation (in range, outside event / `exclude_idx` neighbourhoods, same partition when `partition`
  is given) and report.
* **Effect size** = `P(y|event) - P(y|control)`. Two contrasts per feature cell, never mixed up (`EnrichmentResult.contrast`):
  `event_vs_same_cell_controls` (PRIMARY: events in the cell vs controls whose OWN feature value is in the same cell - what the feature adds)
  and `lift_within_cell` (events in the cell vs the controls matched to exactly those events, controls' feature ignored: the event lift inside
  the cell, non-zero in every cell whenever events differ from controls at all; compare it with `base_delta`).
* **Cells are frozen on TRAIN**: quantile edges / categories (`CellDef`) are fitted from TRAIN event rows only (`purpose='fit'`), returned in
  `EnrichmentReport.cell_defs`, and must be passed unchanged to validation / OOS / forward calls (no refit outside TRAIN).
* **Uncertainty** (`stats`): day-block bootstrap (whole local days of events and controls resampled together; seeded; 95 % percentile
  interval). The bootstrap p-value cannot be smaller than `2/(B+1)`; with m ~ 1000-3000 hypotheses and B = 1000 Holm could therefore NEVER
  reject (a null would be an artefact). Rules now: `B` is chosen automatically `>= 20 m / alpha` (`stats.choose_B`, floor 2000, cap 20 000 with a
  warning); a family with `m * 2/(B+1) >= alpha` is flagged `power_limited` (status `POWER_LIMITED`, never a quiet `NOT_SIGNIFICANT`;
  `resolution_limited` when the cap is what prevents it); the block-level standard error gives a normal-approximation `p_norm` (no floor) that is
  reported next to the bootstrap p and can be selected with `p_method="normal"`. Wilson interval for single rates (reused from `coverage_analysis.control`).
* **Multiple testing**: every hypothesis is registered in a `HypothesisRegistry` BEFORE evaluation, inside a FAMILY. The registry is PERSISTENT
  (JSON file, `HypothesisRegistry(name, path)`): re-registering a hypothesis from an earlier run is a `HypothesisReuseError` (no best-of-N
  reruns), `n_hypotheses` is the number ever tested and is stored in every result (`n_hypotheses_ever`). Holm (default) / BH run WITHIN a family
  (`adjust_scope="family"`, `m` = family size, unevaluated members count) and, for reference, over everything ever registered
  (`adjusted_p_registry`). Family definition (fixed BEFORE looking at the data): one family per **purpose x label x feature group**; the
  confirmatory use is `incremental_ablation(..., contrasts=[PredeclaredContrast(group, feature, cell), ...])` - a short predeclared list per
  group (group-level ablation delta `delta(base AND cell) - delta(base)`), NOT the 83 features x 4 cells table. The exhaustive table stays
  available as exploration; its families are large and report `power_limited` honestly. `declare_family` stores the declaration time and the
  number of results already recorded then (`family_info`): a family declared after results exist is visibly post hoc.
* **Calibration** (`calibration`): Brier, log loss, equal-count reliability table, expected calibration error, logistic recalibration
  intercept/slope (ridge towards 0/1, finite under separation), AUC as a supplementary metric only, Brier improvement vs a baseline;
  all with day-block bootstrap intervals; any `(p_hat, y)` arrays. No model is trained here.
* **Splits** (`splits`): boundaries are the repo's own freeze constants (core thresholds fitted to 2026-06-30, dev end 2026-08-31,
  forward holdout from 2026-09-01). TRAIN | VALIDATION (last 25 % of the trading days up to the core fit end, a day-count fraction) |
  OOS (2026-07-01 .. 2026-08-31, frozen, touched once) | FORWARD (observe only, `FORWARD_NEVER_USED_FOR_FITTING`). Purge: an event whose
  label horizon reaches into another partition is dropped; embargo: the first `horizon` seconds after a boundary are dropped. Rolling and
  expanding walk-forward windows (`walk_forward_windows`, `window_masks`) are an additional robustness check, same purge, embargo gap in
  trading days. Grouped by Berlin trading date, never shuffled; `guard_dev_only` refuses any bar after the dev end.
  **The guards are called, not only tested**: every enrichment entry point REQUIRES a `partition` column and a `purpose`
  (`fit` -> TRAIN only, `validate` -> VALIDATION only, `oos_test` -> OOS only, `forward_monitor` -> FORWARD only) and applies
  `assert_partition_use` to the event AND control rows and `guard_dev_only` to their decision timestamps (not for `forward_monitor`, which
  instead refuses rows before the forward start). The forward period is refused for every other purpose. PURGED / EMBARGO rows are dropped
  and counted; a control in another partition than its event is an error.

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
3. On TRAIN (`purpose='fit'`): the PREDECLARED family (short contrast list per group x label) survives its family correction with a
   non-`power_limited` family AND the primary same-cell contrast (not only `lift_within_cell`) differs from the overall event-vs-control gap
   (`base_delta`). Exploratory wide tables only generate hypotheses.
4. The same cells (`cell_defs` frozen on TRAIN), same contrasts, are evaluated once on VALIDATION (`purpose='validate'`), then once on OOS
   (`oos_test`); rolling/expanding windows must not contradict them. A new run of an already registered hypothesis is refused.
5. Only then may a forecast model be fitted (TRAIN only), and it is judged by calibration + Brier improvement vs the control/base-rate
   baseline on VALIDATION/OOS, never by AUC alone. The forward period is observed only.

## Negative control of the causality tests

`tests/unit/market_observer/_leak_harness.py` perturbs every value AFTER the decision bar of EVERY `ObserverBars` array (ts_ns, o, h, l, c,
tick_volume, spread, atr, segment_id, local_minute, local_day; scale / NaN / extreme junk; ts stays ascending), plus the prefix check, and
reports WHICH field leaked (the older scramble kept atr / ts / segment_id / local_day from the original bars and could not see leaks
through them). `test_negative_control_leaky.py` proves the harness has teeth: deliberately leaky groups (c[i+1], full-sample mean/std and
percentile, a future value of each field, a timestamp column stamped after the decision = `CausalityError`) are all DETECTED.
`test_negative_control_real_groups.py` applies the same harness to levels, swings, balance, acceptance (long/short), participation and the
orchestrator as a regression gate.

## How to read results

`delta` = event rate minus matched-control rate in percentage points of the outcome; `n_blocks` = independent days; `adjusted_p` is the
registry-wide Holm/BH value. `base_delta` (single) is the overall gap for the label; for `incremental` rows `delta` is
`delta(base AND cell) - delta(base)`. `warmup_ok` (optional column) excludes events inside a feature warm-up; the report counts excluded
events, excluded controls and orphaned controls. Table columns are asserted disjoint (`schema.assert_disjoint`) and every
`f_*_ts_ns` must be <= `decision_ts_ns` (`CausalityError`).

## Honest limits

* Bar-resolution labels (M5): intrabar order is unknown; stop-first is conservative for the favourable label, not a fill model.
* Costs, slippage, latency and the real stop are excluded; first-passage rates are not P&L.
* Percentile ranks used for matching are partition-internal (or causal past-only) descriptive covariates; they never use another partition's
  bars, but within a partition they are ranks over the partition's own sample, not live-computable features (use `rank_mode="causal"` for that).
* 07-01..08-31 is "frozen OOS" only relative to the observer and to the core thresholds; other repo lanes (alpha V2 folds) have used those
  dev days for validation. Treat OOS as weaker than a never-seen period; the forward period is the only untouched data.
* Day blocks assume independence between days; regime clustering across weeks is not modelled (use rolling windows as a check).
* No edge is claimed anywhere in this package.

## Deferred hypotheses (not implemented; shadow/OOS only, never live)

* Volatility-normalised confirmed swings (minimum displacement in ATR; inspired by the ZigZag indicator of QuantConnect LEAN) as a future
  SHADOW challenger against the fixed 2-left/2-right fractal pivot of the swings group. To be tested only as shadow / out of sample.
* Real exchange volume or a volume profile, only if a real volume source ever exists (MT5 tick volume is a tick count, not volume).

No external code is imported; external repositories are references for definitions and test patterns only.
