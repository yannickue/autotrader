# Observer Gate C: preregistration of the single-feature enrichment family (version `observer-gate-c-prereg-2`)

Status: `OBSERVATION_ONLY_NOT_ALPHA_VALIDATED`. Offline research only. Nothing here changes a live entry, exit, stop, size, risk or execution decision.
Frozen by the git commit that introduces this file, BEFORE any Gate C result (enrichment) was computed or looked at.

## History: prereg-1 is superseded, not edited

`observer-gate-c-prereg-1` (commit `be7708e`, never run on real data) received an independent audit with verdict NO-GO for a first real run. Prereg-2 is a NEW
preregistration with a NEW registry namespace (`observer_gate_c_prereg2`, files `observer_gate_c2_*`, hypothesis names `gatec2|...`); prereg-1 and its registry
name stay untouched as history. No Gate C result of any version existed when prereg-2 was written. Changes against prereg-1 (audit finding in brackets):

* controls-3 provenance and balance are a preflight precondition [B2, M4]; censoring balance is a precondition [M4];
* independent blocks are counted per cell AND per arm [H1]; controls are blocked by the day of their event [H3]; week blocks as a sensitivity [H3];
* three mandatory negative controls beside the random feature: A/A test (NC-A), shift placebo (NC-B), controls-3 vs controls-2 bridge (NC-C) [H2];
* the fit stage runs exactly once per preregistration version (lock outside `--out`, pinned fit report hash, full core set, no overwrite) [M1, M2];
* Holm over ALL hypotheses of the stage and scope, one family [M3]; NaN bootstrap draws abort the stage [M5]; a fixed fallback for a failed balance gate.

## What was and was not looked at when this was written

* Written from code only: `src/market_observer/*` (feature definitions), `src/coverage_analysis/observer_lab/*` (labels, controls, splits, stats,
  enrichment) and `docs/OBSERVER.md` / `docs/OBSERVER_LAB.md`. The backfill Parquet files were opened for their SCHEMA (column names and dtypes) only;
  no feature value, no label value, no feature-vs-label distribution and no enrichment number of the backfill was computed or viewed. Prereg-2 additionally
  did not read any real controls-3 file or manifest: it does not exist yet; the manifest schema below is DEFINED here and the producer (lane `observer/lane-d3`)
  must deliver it.
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
  of the market, controls follow their event through `control_of`. Controls must be of method `observer-controls-3` (see Controls and balance below).
  The difference-to-base design is chosen so that the generic "events differ from controls" gap cannot masquerade as feature information.
* **Statistics version `observer-stats-2`** (new in prereg-2; the lab default `observer-stats-1` is unchanged for other lanes and tests):
  * paired bootstrap that resamples whole independent blocks of events and controls together (95 % percentile interval, seeded per market and stage);
  * **block assignment of a control = the Berlin trading day of its EVENT** (`control_of`), not its own decision day: a control is a paired draw for its
    event, and a block must contain both halves of every pair (stats-1 blocked a control by its own timestamp, which can fall on another day);
  * **minimum blocks per cell and per arm**: the number of blocks with at least one non-NaN outcome IN THE CELL is counted separately for the event arm and
    for the control arm of the cell; each must be >= 30 (stats-1 counted the union of both arms of the cell AND of the base arm);
  * **sensitivity with week blocks** (ISO weeks of the event day, B capped at 4000): reported next to every hypothesis (`week CI excludes 0`). It is
    descriptive: it takes no part in the correction and is not a survivor criterion. A survivor whose week-block interval contains 0 is reported as such;
  * **NaN bootstrap draws are counted** in every estimate. More than 0 aborts the stage (exit code 4, nothing is written to the report): a NaN draw means an
    empty arm in a resample, i.e. a cell too thin for this design; it is never silently dropped.
* **Cells**: terciles (`Q1/3`, `Q3/3`) fitted ONCE on TRAIN event rows and frozen (`CellDef`, stored in the report), fixed categories for the
  derived categorical features. Validation and OOS use the stored definitions unchanged. If a predeclared cell does not exist under the TRAIN
  definition (ties collapse the terciles, or the feature turns out categorical) the hypothesis is `CELL_UNDEFINED`: it counts in `m`, it is not tested and
  never replaced by another cell.
* **Derived features** (computed by the script from decision-time columns and the event direction, nothing else; names prefixed `x_`):
  `f_swings__x_ema_trend_aligned` = `with` if the EMA trend (`f_swings__ema_trend`) points in the event direction, `against` if opposite, `flat` if flat;
  `f_swings__x_m15_sequence_aligned` = `with` / `against` for an UP/DOWN sequence in / against the event direction, `mixed` for `MIXED_TRANSITION`,
  `range` for `RANGE_OR_UNDEFINED`. Missing source -> missing. Day block = Berlin trading date of the decision timestamp.
* **Minimum evidence** (stricter than the lab default, fixed here): cell events >= 100, cell controls >= 100, independent day blocks >= 30 **in each arm of the
  cell**. Below any of them the result is `INSUFFICIENT_EVIDENCE` (numbers shown, no p-value in the correction, still counted in `m`). The table carries no
  event-cluster id, so the cluster criterion of the lab does not apply; same-bar events of several families are dependent, which the block bootstrap absorbs.
* **Minimum effect**: |delta| >= 0.02 (2 percentage points of the first-passage rate relative to the base gap) for a survivor.
* **Multiple testing (one family per stage and scope)**: Holm over ALL hypotheses of the stage and scope together, `m` = the number of (hypothesis x market)
  pairs that are registered in that stage and scope (not-evaluated members count): 60 in `fit` (12 x 5 core markets), NOT 10 per feature group. `validate` and
  `oos` each form ONE family of the survivors of the previous stage (`m` = number of survivors, declared after the fit result by this rule) and must replicate
  there: a survivor of `validate` needs `SIGNIFICANT_ADJUSTED` in that family; `oos` likewise on its own. The negative controls have their own families (below)
  and never dilute or relax this one. Markets that are `descriptive_only` (fallback below) are NOT registered and do not count in `m`. Own persistent registry
  `observer_gate_c2_registry.json` (name `observer_gate_c_prereg2`, separate from every other lab, Gate B and prereg-1 registry); all confirmatory tests of a stage
  are registered BEFORE any is evaluated; a hypothesis ever registered cannot be run again with other results (`HypothesisReuseError`, no best-of-N).
  Bootstrap B is chosen so that the smallest attainable p cannot limit Holm (`B >= 20 m / alpha`, floor 2000, cap 24000); a family that still cannot reach
  significance is reported `POWER_LIMITED`, never silently null.
* **Stages**: `fit` (TRAIN only; the whole family) -> `validate` (VALIDATION only; ONLY the fit survivors) -> `oos` (OOS only, ONCE; only the validation
  survivors; needs `--confirm-oos-once`). The forward period (>= 2026-09-01) is never read: the script refuses any file whose timestamps reach it and has no
  forward stage.
* **`fit` runs exactly once per preregistration version** (no best-of-N):
  * `fit` requires the FULL preregistered core set in `--markets` (explore markets may be added); a partial fit is refused;
  * a lock file `<LOCK_DIR>/observer-gate-c-prereg-2.fit.lock.json` lies OUTSIDE `--out` at a fixed location (`%LOCALAPPDATA%\Temp\observer_gate_c_locks`). It
    records the `--out` of the one allowed fit, the data fingerprints and, after the fit, the SHA-256 of the fit stage result (the "fit report hash");
  * a second `fit` with another `--out`, with another data fingerprint, or after the lock exists with a different preregistration block is refused (exit code 3);
    the same `--out` may be re-run (resume / identical recompute) and must reproduce the registered numbers;
  * `validate` / `oos` refuse unless the fit stage in the report still has exactly the pinned hash; the report writer never overwrites an existing stage with
    different content. (Honest limit: this is a procedural guard, not a cryptographic one; whoever edits the script or the lock can bypass it. The commit of
    this document and the lock hash in the report are the audit trail.)

## Controls and balance (preflight; new in prereg-2)

* Controls are `observer-controls-3` (produced by lane `observer/lane-d3`). Per market the backfill directory must contain, besides `table.parquet` and
  `controls.parquet`, a `controls_manifest.json` and, for core markets, `controls_b.parquet` (the second disjoint control set for NC-A, same columns and
  `control_of` semantics as `controls.parquet`). Explore markets may carry `controls_v2.parquet` (the controls-2 set, NC-C).
* **Manifest schema** (`controls_manifest.json`, fields beyond these are ignored):

  ```json
  {
    "control_method_version": "observer-controls-3",
    "controls_sha256": "<optional: sha256 of controls.parquet>",
    "balance_gate": {
      "TRAIN":      {"match_rate": 0.0, "smd_local_minute": 0.0, "smd_atr_pct": 0.0, "smd_spread_pct": 0.0, "censoring_diff_pp": 0.0, "passed": true, "status": "passed", "n_controls": 0},
      "VALIDATION": { "...": "same fields" },
      "FROZEN_OOS": { "...": "same fields" }
    }
  }
  ```

  `status` is `passed` or `descriptive_only`; `n_controls` is optional (cross-checked against the Parquet row counts per partition when present).
  `censoring_diff_pp` = share of censored (NaN) labels among controls minus among events, in percentage points (sign irrelevant for the gate).
* **Balance gate** (recomputed by the script from the preregistered thresholds in the block below; the manifest's own `passed` / `status` must agree, otherwise
  the manifest is inconsistent and the PREFLIGHT fails): `match_rate >= 0.90`, `|SMD|` of local minute, ATR percentile and spread <= 0.10 each, and
  **censoring balance** `|censoring_diff_pp| <= 5`. The censoring balance is additionally RECOMPUTED by the script from the label NaN counts of the stage
  partition (events vs controls; counts only) and must also stay within +-5 pp. These thresholds are the preregistrar's choices; the manifest producer
  must be aligned with them before the first run (a mismatch shows up as an inconsistent manifest, not as a silent pass).
* **PREFLIGHT** (always runs first; `--preflight` runs only this) reads ONLY counts and manifests: file content hashes, row counts, schema hashes,
  per-partition counts, the number of NaN labels per arm, the manifest. It checks: all files and required columns exist, the Parquet footers do not reach
  the forward period, `control_method_version == observer-controls-3`, the manifest schema, the manifest's `controls_sha256` (if given) and `n_controls`
  (if given) against the files. **A failing preflight registers NOTHING, creates no lock, and does not consume the stop rule** (exit code 5, the findings
  are written to `observer_gate_c2_preflight_<stage>.json`). A balance gate that is merely not passed is NOT a preflight failure; it triggers the fallback below.
* **Fingerprint**: per market, the SHA-256 over {manifest hash, per file: content hash, row count, schema hash, per-partition counts}. Size / mtime play no
  role. Manifest hash, control method version, fingerprint and the balance numbers are written to the report; the fingerprint is part of the cache key and
  of the fit lock (`validate` / `oos` refuse if the data fingerprint of a market differs from the fit's).
* **Fallback, fixed in advance**: a CORE market that does not pass the balance gate in the partition of the stage is **`descriptive_only`**: its hypotheses
  are computed and listed, but they are not registered, do not count in `m`, can never be survivors, and the report states explicitly that this is NOT
  "no enrichment" (the controls were not good enough to ask the question). If no market passes, the stage is not run (`STOP`, nothing registered).

## Negative controls (mandatory, every stage; fail closed)

The stage verdict is `INVALID_NEGATIVE_CONTROL_FAILED` (stop rule applies: do not trust the stage, investigate, do not re-run with different settings) if ANY
of the following holds. A negative control that cannot be evaluated (every row `INSUFFICIENT_EVIDENCE` / `CELL_UNDEFINED`) or whose family is `POWER_LIMITED` (at its own level)
counts as failed, not as passed.

* **Significance level of the negative controls: 0.01** (Holm within each negative-control family; the NC-A base-delta interval uses 0.01 / n core markets).
  Three negative-control families at 0.05 would declare a perfectly valid stage invalid in roughly 15 % of runs by chance alone; at 0.01 the joint chance
  is about 4 %. The price is a lower sensitivity to modest defects (a gross defect such as a 20 pp offset between the control sets is still caught); this
  trade-off is chosen here and not revisited. B is large enough that no negative-control family is power-limited at 0.01 (otherwise: failed).
* **N01 / N02, random-feature check** (family `ctrl`, m = 2 per market): a deterministic pseudo-random uniform feature (hash of `event_id`), cells `Q3/3` and
  `Q1/3`. Honest description: this checks ONLY the calibration of the block bootstrap and of the correction (a feature carrying no information must not
  show enrichment). It does NOT test the control matching: events and controls both receive independent hash values, so it cannot detect a matching
  imbalance. It is kept for that narrow purpose.
* **NC-A, A/A test** (family `nc_a`, core markets, same 12 cells as the hypotheses): the second disjoint control set `controls_b` plays the pseudo-event
  against the first control set (`controls`); both are draws from the same matched distribution, so the "event vs control" gap and every cell contrast must be
  null. Pairing: by original event and rank inside the event, one `controls_b` row per `controls` row; the block of both rows is the day of the ORIGINAL
  event. Fail if (a) the base delta (pseudo-events vs controls) is not null: its 1 - alpha/n confidence interval (alpha = 0.01, n = number of core markets
  with an A/A test, Bonferroni) excludes 0, or (b) any NC-A cell is `SIGNIFICANT_ADJUSTED` in the `nc_a` family (Holm over its own family).
* **NC-B, shift placebo** (family `nc_b`, core markets, same 12 cells): every row keeps its features but receives the label of the row of the same arm (event / control)
  that lies closest to the SAME Berlin time of day on the NEXT trading day present in the data, within +-30 minutes (NaN where there is none; the coverage is
  reported; events are sparse in time, an exact-bar match would leave too few pairs, hence the preregistered tolerance; the direction is not matched, the label is
  already direction-relative). A decision-time
  feature cannot carry information about another day's outcome, so any cell enrichment must be null. Fail if any NC-B cell is `SIGNIFICANT_ADJUSTED` in the
  `nc_b` family. Honest limit: persistent day-to-day regimes can, in principle, create a genuine (non-causal) signal here; a failure therefore means
  "stop and investigate", which includes the possibility that the label has day-level structure that the day blocks do not absorb.
* **NC-C, bridge controls-3 vs controls-2** (explore markets BTCUSD / BRENT, only if `controls_v2.parquet` exists): reports balance and the base delta under
  both control sets for the SAME events plus their difference with a paired block CI, and the censoring difference of each set. Descriptive only: no feature
  cells, no verdict effect (it documents how much the controls-3 change moves the base gap).
* In `validate` / `oos` the negative controls run for the markets of the survivors and, for NC-A / NC-B, for the cells of the survivors.

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

12 hypotheses x 5 core markets = 60 confirmatory tests in the single `fit` family (m = 60), plus the negative-control families: `ctrl` m = 10, `nc_a` m = 60,
`nc_b` m = 60. The feature groups (acceptance, levels, ...) are only labels for reading the table; they are not correction families. H11 is a
compression hypothesis whose sign is the weakest a priori claim; its sign is fixed as written and is not revisited.

**Power note (stated before the first run, not a post hoc excuse).** The acceptance-group columns are, in GER40 and probably in other markets, empty
(missing) for 50 % or more of the events (acceptance is only defined after a level break). Cells are defined on non-missing TRAIN events, and the
minimum-evidence rule is per cell and per arm. H01-H04 can therefore be `INSUFFICIENT_EVIDENCE` or `CELL_UNDEFINED` in some markets. They stay in `m` regardless
(the family was declared before the data was looked at), which costs power for the other hypotheses; that is accepted and not revisited. A stage in which
every hypothesis is insufficient is `INCONCLUSIVE_INSUFFICIENT_EVIDENCE`, not "no enrichment".

## Decision and stop rules

* **Survivor of a stage**: status `SIGNIFICANT_ADJUSTED` (Holm-adjusted p < 0.05 within the stage family AND the block-bootstrap interval excludes 0), family
  not `POWER_LIMITED`, sign equal to the expected sign, |delta| >= 0.02, minimum evidence met (per cell and per arm), market not `descriptive_only`, scope
  core, negative controls not failed.
* **fit verdict**: `ENRICHMENT_CANDIDATES_TO_VALIDATE` (>= 1 survivor), `NO_ENRICHMENT` (nothing significant in the expected direction; includes
  significant contrary results, which are only listed), `INCONCLUSIVE_INSUFFICIENT_EVIDENCE` (every test insufficient / power limited / cell undefined),
  `INVALID_NEGATIVE_CONTROL_FAILED`.
* **STOP RULE**: if `fit` ends in anything but `ENRICHMENT_CANDIDATES_TO_VALIDATE`, the result is documented as "no enrichment" (or inconclusive /
  invalid), `validate` and `oos` are NOT run (the script refuses), and the line of work stops. No new features, cells, labels, markets or
  thresholds are tried afterwards inside this preregistration; a new idea needs a new preregistration and a new registry. The same applies after a
  `validate` result without survivors: `oos` is not run and the OOS partition stays untouched. What does NOT consume the stop rule: a failed PREFLIGHT, a
  refused invocation (wrong markets, missing flag) and an aborted run (NaN draws); these leave the fit unrun (an aborted run after registration stays
  registered and can only be repeated with the same `--out`, reproducing the same numbers).
* **OOS** is touched exactly once (registry-enforced). Rolling / expanding walk-forward windows (`splits.walk_forward_windows`) are an additional
  robustness read for survivors only, not a second chance.
* A confirmed survivor means "this cell shows reproducible first-passage enrichment against matched controls on frozen cells", nothing more: no signal,
  no filter, no live use. A forecast model (`docs/OBSERVER_LAB.md`, gate step 5) needs its own decision.

## Machine-readable preregistration (parsed by `scripts/observer_gate_c.py`)

<!-- PREREG-JSON-BEGIN -->
```json
{
  "prereg_version": "observer-gate-c-prereg-2",
  "supersedes": "observer-gate-c-prereg-1",
  "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
  "registry_name": "observer_gate_c_prereg2",
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
    "holm_scope": "stage_all",
    "b_min": 2000,
    "b_max": 24000
  },
  "stats": {
    "version": "observer-stats-2",
    "block_unit": "day",
    "control_block": "event_day",
    "sensitivity_block_unit": "week",
    "sensitivity_B_max": 4000,
    "nan_draws": "abort"
  },
  "controls": {
    "required_method_version": "observer-controls-3",
    "bridge_method_version": "observer-controls-2",
    "balance": {"min_match_rate": 0.90, "max_abs_smd": 0.10, "max_abs_censoring_diff_pp": 5.0},
    "fallback": "descriptive_only"
  },
  "placebos": {
    "alpha": 0.01,
    "nc_a": {"controls_file": "controls_b.parquet", "scope": "core", "base_alpha": "alpha / n_core_markets"},
    "nc_b": {"shift": "same arm, nearest row to the same Berlin time of day on the next trading day", "tolerance_minutes": 30, "scope": "core"},
    "nc_c": {"controls_file": "controls_v2.parquet", "scope": "explore", "descriptive": true}
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
* Same-bar events of several setup families and neighbouring bars are dependent; blocks (days, weeks as sensitivity) are the independence unit; regime clustering
  across longer horizons is not modelled. Bar-resolution labels, no costs. The core thresholds are partly in-sample up to 2026-06-30 (TRAIN), 07-01..08-31 (OOS)
  is not a clean holdout for other lanes (`docs/OBSERVER_LAB.md`).
* The balance thresholds, the A/A Bonferroni rule and the NC-B shift construction are choices made here; the NC-A / NC-B / NC-C inputs (`controls_b`,
  `controls_v2`, the manifest) do not exist yet and their producer must match the schema above.
* The a priori reasoning column is a rationale, not evidence.
