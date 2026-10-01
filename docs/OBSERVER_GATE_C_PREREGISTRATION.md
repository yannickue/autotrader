# Observer Gate C: preregistration of the single-feature enrichment family

Status: `OBSERVATION_ONLY_NOT_ALPHA_VALIDATED`. Offline research only. Nothing here changes a live entry, exit, stop, size, risk or execution decision.
Frozen by the git commit that introduces this file, BEFORE any Gate C result (enrichment) was computed or looked at.

## What was and was not looked at when this was written

* Written from code only: `src/market_observer/*` (feature definitions), `src/coverage_analysis/observer_lab/*` (labels, controls, splits, stats,
  enrichment) and `docs/OBSERVER.md` / `docs/OBSERVER_LAB.md`. The backfill Parquet files were opened for their SCHEMA (column names and dtypes) only;
  no feature value, no label value, no feature-vs-label distribution and no enrichment number of the backfill was computed or viewed.
* The feature, cell and sign choices below are therefore a priori (market-structure reasoning), not data-driven. The expected signs are
  recorded so that a significant result in the OPPOSITE direction can never be reported as confirmation (it is reported as `CONTRARY`, hypothesis
  generation only, and is not a survivor).
* Fibonacci is NOT part of this family. It stays a separate experimental placebo lane (Lane E, `fib` group) with its own placebo controls.
* The machine-readable block at the end of this file is the single source of truth for `scripts/observer_gate_c.py`: the script parses it, records
  the file hash and the block hash in its report, and refuses to run if the block is malformed. Changing the block after the first run is a new
  preregistration (new version, new registry).

## Question

Does a single decision-time feature cell (frozen tercile / fixed category of ONE market-structure feature) change the probability of the first-passage
outcome `y_fav050_before_adv050` (favourable +0.5 R before adverse -0.5 R, stop-first on ties, censored outcomes excluded) relative to the same
market's matched controls, beyond the overall event-vs-control gap of that market, reproducibly TRAIN -> VALIDATION -> OOS?
This is enrichment, not a forecast and not an edge claim: costs, slippage and the real stop are excluded (see `docs/OBSERVER_LAB.md`, Honest limits).

## Design (fixed)

* **Label**: `y_fav050_before_adv050` for every hypothesis (one label, no label shopping). Other `y_*` columns are not used by Gate C.
* **Scope**: the five core markets (GER40, NAS100, SPX500, XAUUSD, EURUSD) are the confirmatory scope: they have a frozen split. BTCUSD and BRENT have
  no frozen split and a short history; if requested with `--markets` they run as scope `explore` in SEPARATE families and can never confirm
  anything. Each market is tested on its own (no pooling across markets); a pooled statement needs a new preregistration.
* **Test**: `observer_lab.enrichment.incremental_ablation` with a predeclared contrast list: `delta(base AND cell) - delta(base)`, base = all events
  of the market, controls follow their event through `control_of` (matched controls of `observer-controls-2`: same market, same partition,
  matched on session bucket, time of day, ATR percentile band, spread band, direction; every generator opportunity excluded). Paired
  day-block bootstrap (whole Berlin trading days of events and controls resampled together, 95 % percentile interval, seeded per market and stage).
  The difference-to-base design is chosen so that the generic "events differ from controls" gap cannot masquerade as feature information.
* **Cells**: terciles (`Q1/3`, `Q3/3`) fitted ONCE on TRAIN event rows and frozen (`CellDef`, stored in the report), fixed categories for the
  derived categorical features. Validation and OOS use the stored definitions unchanged. If a predeclared cell does not exist under the TRAIN
  definition (ties collapse the terciles, or the feature turns out categorical) the hypothesis is `CELL_UNDEFINED`: it counts in `m`, it is not tested and
  never replaced by another cell.
* **Derived features** (computed by the script from decision-time columns and the event direction, nothing else; names prefixed `x_`):
  `f_swings__x_ema_trend_aligned` = `with` if the EMA trend (`f_swings__ema_trend`) points in the event direction, `against` if opposite, `flat` if flat;
  `f_swings__x_m15_sequence_aligned` = `with` / `against` for an UP/DOWN sequence in / against the event direction, `mixed` for `MIXED_TRANSITION`,
  `range` for `RANGE_OR_UNDEFINED`. Missing source -> missing. Day block = Berlin trading date of the decision timestamp.
* **Minimum evidence** (stricter than the lab default, fixed here): cell events >= 100, cell controls >= 100, independent day blocks >= 30. Below any of
  them the result is `INSUFFICIENT_EVIDENCE` (numbers shown, no p-value in the correction, still counted in `m`). The table carries no event-cluster id,
  so the cluster criterion of the lab does not apply; same-bar events of several families are dependent, which the day-block bootstrap absorbs.
* **Minimum effect**: |delta| >= 0.02 (2 percentage points of the first-passage rate relative to the base gap) for a survivor.
* **Multiple testing**: Holm within the family **stage x scope x label x feature group**, `m` = the number of (hypothesis x market) pairs of that family
  (not-evaluated members count). Own persistent registry `observer_gate_c_registry.json` (name `observer_gate_c`, separate from every other lab or
  Gate B registry); all hypotheses of a stage are registered BEFORE any is evaluated; a hypothesis ever registered cannot be run again with other
  results (`HypothesisReuseError`, no best-of-N). Bootstrap B is chosen so that the smallest attainable p cannot limit Holm
  (`B >= 20 m / alpha`, floor 2000, cap 20000); a family that still cannot reach significance is reported `POWER_LIMITED`, never silently null.
* **Stages**: `fit` (TRAIN only; the whole family below) -> `validate` (VALIDATION only; ONLY the fit survivors, one family per group, `m` = number of
  survivors, declared after the fit result by this rule) -> `oos` (OOS only, ONCE; only the validation survivors; needs `--confirm-oos-once`).
  The forward period (>= 2026-09-01) is never read: the script refuses any file whose timestamps reach it and has no forward stage.
* **Negative control** (in every stage, own family `ctrl`): a deterministic pseudo-random uniform feature (hash of `event_id`, carries no information by
  construction), cells `Q3/3` and `Q1/3`. If any negative-control cell is `SIGNIFICANT_ADJUSTED`, the whole stage verdict is
  `INVALID_NEGATIVE_CONTROL_FAILED` (a false positive rate of ~5 % per family is expected under the null; a failure means: do not trust the
  stage, investigate, do not re-run with different settings). Placebo controls of the lab (`placebo_controls`) are a separate check of the matching
  and are not part of this family.

## Hypotheses (all tested on the label `y_fav050_before_adv050`; sign = expected sign of `delta` = enrichment of the cell above the base gap)

| id | group | feature | cell | sign | a priori reasoning |
|---|---|---|---|---|---|
| H01 | acceptance | `f_acceptance__followthrough_dir_atr` | `Q3/3` | + | large favourable follow-through beyond the broken level so far -> acceptance, continuation |
| H02 | acceptance | `f_acceptance__time_held_beyond_level_bars` | `Q3/3` | + | price held beyond the level for a long time -> accepted, not a probe |
| H03 | acceptance | `f_acceptance__reclaim_occurred` | `=True` | - | a close back at/inside the edge after the break -> failed acceptance |
| H04 | acceptance | `f_acceptance__max_reentry_depth_atr` | `Q3/3` | - | deep wick back through the edge -> weak acceptance |
| H05 | levels | `f_levels__nearest_level_distance_atr` | `Q3/3` | + | nearest level far AHEAD in trade direction -> room before resistance |
| H06 | levels | `f_levels__zone_width_atr` | `Q3/3` | - | wide, imprecise zone -> noisier reference, less decisive |
| H07 | swings | `f_swings__x_ema_trend_aligned` | `=with` | + | event in the direction of the EMA(8/21) trend |
| H08 | swings | `f_swings__x_m15_sequence_aligned` | `=with` | + | event in the direction of the M15 swing sequence |
| H09 | swings | `f_swings__x_m15_sequence_aligned` | `=against` | - | event against the M15 swing sequence |
| H10 | balance | `f_balance__directional_efficiency_w24` | `Q3/3` | + | trending (efficient) recent window continues |
| H11 | balance | `f_balance__range_width_atr_w48` | `Q1/3` | + | compressed 48-bar window -> expansion in the event direction |
| H12 | participation | `f_participation__activity_vs_same_tod` | `Q3/3` | + | tick activity well above the same-time-of-day median -> participation behind the move |

12 hypotheses x 5 core markets = 60 tests per stage in the fit family, split into families of m = 20 (acceptance), 10 (levels), 15 (swings), 10 (balance),
5 (participation) and m = 10 (ctrl). H11 is a compression hypothesis whose sign is the weakest a priori claim; its sign is fixed as written and is not revisited.

## Decision and stop rules

* **Survivor of a stage**: status `SIGNIFICANT_ADJUSTED` (Holm-adjusted p < 0.05 within its family AND the block-bootstrap interval excludes 0), family not
  `POWER_LIMITED`, sign equal to the expected sign, |delta| >= 0.02, minimum evidence met, negative control not failed.
* **fit verdict**: `ENRICHMENT_CANDIDATES_TO_VALIDATE` (>= 1 survivor), `NO_ENRICHMENT` (nothing significant in the expected direction; includes
  significant contrary results, which are only listed), `INCONCLUSIVE_INSUFFICIENT_EVIDENCE` (every test insufficient / power limited / cell undefined),
  `INVALID_NEGATIVE_CONTROL_FAILED`.
* **STOP RULE**: if `fit` ends in anything but `ENRICHMENT_CANDIDATES_TO_VALIDATE`, the result is documented as "no enrichment" (or inconclusive /
  invalid), `validate` and `oos` are NOT run (the script refuses), and the line of work stops. No new features, cells, labels, markets or
  thresholds are tried afterwards inside this preregistration; a new idea needs a new preregistration and a new registry. The same applies after a
  `validate` result without survivors: `oos` is not run and the OOS partition stays untouched.
* **OOS** is touched exactly once (registry-enforced). Rolling / expanding walk-forward windows (`splits.walk_forward_windows`) are an additional
  robustness read for survivors only, not a second chance.
* A confirmed survivor means "this cell shows reproducible first-passage enrichment against matched controls on frozen cells", nothing more: no signal,
  no filter, no live use. A forecast model (`docs/OBSERVER_LAB.md`, gate step 5) needs its own decision.

## Machine-readable preregistration (parsed by `scripts/observer_gate_c.py`)

<!-- PREREG-JSON-BEGIN -->
```json
{
  "prereg_version": "observer-gate-c-prereg-1",
  "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
  "registry_name": "observer_gate_c",
  "label": "y_fav050_before_adv050",
  "markets": {
    "core": ["GER40", "NAS100", "SPX500", "XAUUSD", "EURUSD"],
    "explore": ["BTCUSD", "BRENT"]
  },
  "stages": {
    "fit": {"partition": "TRAIN", "purpose": "fit"},
    "validate": {"partition": "VALIDATION", "purpose": "validate"},
    "oos": {"partition": "OOS", "purpose": "oos_test"}
  },
  "test": {
    "kind": "incremental_ablation",
    "n_quantiles": 3,
    "p_method": "bootstrap",
    "alpha": 0.05,
    "adjust": "holm",
    "b_min": 2000,
    "b_max": 20000
  },
  "min_evidence": {"events": 100, "controls": 100, "blocks": 30},
  "min_effect_abs": 0.02,
  "hypotheses": [
    {"id": "H01", "group": "acceptance", "feature": "f_acceptance__followthrough_dir_atr", "cell": "Q3/3", "sign": 1},
    {"id": "H02", "group": "acceptance", "feature": "f_acceptance__time_held_beyond_level_bars", "cell": "Q3/3", "sign": 1},
    {"id": "H03", "group": "acceptance", "feature": "f_acceptance__reclaim_occurred", "cell": "=True", "sign": -1},
    {"id": "H04", "group": "acceptance", "feature": "f_acceptance__max_reentry_depth_atr", "cell": "Q3/3", "sign": -1},
    {"id": "H05", "group": "levels", "feature": "f_levels__nearest_level_distance_atr", "cell": "Q3/3", "sign": 1},
    {"id": "H06", "group": "levels", "feature": "f_levels__zone_width_atr", "cell": "Q3/3", "sign": -1},
    {"id": "H07", "group": "swings", "feature": "f_swings__x_ema_trend_aligned", "cell": "=with", "sign": 1},
    {"id": "H08", "group": "swings", "feature": "f_swings__x_m15_sequence_aligned", "cell": "=with", "sign": 1},
    {"id": "H09", "group": "swings", "feature": "f_swings__x_m15_sequence_aligned", "cell": "=against", "sign": -1},
    {"id": "H10", "group": "balance", "feature": "f_balance__directional_efficiency_w24", "cell": "Q3/3", "sign": 1},
    {"id": "H11", "group": "balance", "feature": "f_balance__range_width_atr_w48", "cell": "Q1/3", "sign": 1},
    {"id": "H12", "group": "participation", "feature": "f_participation__activity_vs_same_tod", "cell": "Q3/3", "sign": 1}
  ],
  "negative_controls": [
    {"id": "N01", "group": "ctrl", "feature": "f_ctrl__x_random_uniform", "cell": "Q3/3", "sign": 0},
    {"id": "N02", "group": "ctrl", "feature": "f_ctrl__x_random_uniform", "cell": "Q1/3", "sign": 0}
  ]
}
```
<!-- PREREG-JSON-END -->

## Honest limits

* One label, 12 contrasts, terciles: a deliberately narrow family. "No enrichment" here does not mean no structure exists, only that none of these
  predeclared cells shows reproducible first-passage enrichment against matched controls.
* Same-bar events of several setup families and neighbouring bars are dependent; day blocks are the independence unit; regime clustering across weeks is
  not modelled. Bar-resolution labels, no costs. The core thresholds are partly in-sample up to 2026-06-30 (TRAIN), 07-01..08-31 (OOS) is not a clean
  holdout for other lanes (`docs/OBSERVER_LAB.md`).
* The a priori reasoning column is a rationale, not evidence.
