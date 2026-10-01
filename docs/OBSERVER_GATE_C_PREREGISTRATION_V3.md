# Observer Gate C: preregistration of the single-feature enrichment family (version `observer-gate-c-prereg-3`, DRAFT)

Status: `OBSERVATION_ONLY_NOT_ALPHA_VALIDATED`. Offline research only. Nothing here changes a live entry, exit, stop, size, risk or execution decision.
**DRAFT, NOT FROZEN, NOT RUN.** This file becomes a preregistration only when the owner freezes it by commit BEFORE any Gate C result of ANY version is computed or
looked at. No Gate C result (fit / validate / oos) of prereg-2 or prereg-3 exists at the time of writing. See "Freeze blocker" below: as drafted, the block rule makes
every test `INSUFFICIENT_EVIDENCE` on the current data volume, which is an owner decision, not something this draft decides silently.

## History: prereg-2 is referenced, not edited

`observer-gate-c-prereg-2` (`docs/OBSERVER_GATE_C_PREREGISTRATION.md`, SHA-256 `4413e35ea3fc7fe38131a3956875ec1402534f10ba55b711d199722300091110`) stays
byte-identical as history; it is never edited. Prereg-3 is a NEW preregistration with a NEW registry namespace (`gatec3`, registry
`observer_gate_c_prereg3`, files `observer_gate_c3_*`, hypothesis names `gatec3|...`), so nothing registered under prereg-2 can collide with it and a prereg-3 `fit`
takes its own fit lock (`observer-gate-c-prereg-3.fit.lock.json`; the lock is per preregistration version). A `fit` under prereg-3 therefore uses a NEW registry.

## What changes against prereg-2 (and why)

| # | Change | Reason |
|---|---|---|
| 1 | Statistics version `observer-stats-3`: contiguous blocks of >= 21 trading days instead of single days | controls lie +-10 trading days from their event and controls of neighbouring events share label windows; a single day is not an independent unit |
| 2 | The balance gate states the session-share tolerance (0.02) and the minimum number of events per partition (20, below: `INSUFFICIENT_N`) explicitly | the controls-3 builder gate already enforces both; prereg-2 does not contain them, which made a builder `FAIL` and the preregistered recomputation disagree (a structural preflight error). Prereg-3 declares them, so builder and preflight apply ONE gate |
| 3 | ONE censoring denominator: the matched pairs | the builder computes the censored share over matched pairs (each control joined to its event); the prereg-2 preflight recomputed it over ALL stage events vs ALL stage controls. With match rates of 0.73-0.84 these are different numbers |
| 4 | `controls_sha256` is mandatory; for artifacts whose manifest predates the field a read-only attestation pins it | provenance: a control file must be the one that was manifested (no controls-2 / controls-3 mixing) |
| 5 | New registry namespace / file names / fit lock | the statistics and the gate differ, so prereg-2 and prereg-3 results must never share a registry |

## What is NOT changed

Same hypothesis family (H01-H12 and N01 / N02, identical features, cells, signs), one label `y_fav050_before_adv050`, same scope (five core markets confirmatory, BTCUSD /
BRENT explore only), same test (`incremental_ablation`, difference to base), Holm over all hypotheses of the stage and scope (m = 60 in `fit`), alpha 0.05, minimum effect
0.02, minimum evidence numbers (events 100, controls 100, **blocks 30 per cell and per arm: unchanged, see the blocker below**), negative controls NC-A / NC-B / NC-C at
level 0.01, stop rules, the fit-once lock, the forward-period ban. The balance thresholds are **unchanged: match rate >= 0.90, |SMD| <= 0.10, |censoring diff| <= 5 pp**.
**No threshold is loosened.** `descriptive_only` means the controls are not balanced well enough to ask the confirmatory question; it never means "no enrichment"
(`INCONCLUSIVE_NOT_ASKED`).

## Block rule (`observer-stats-3`) and why contiguous blocks

* The distinct trading days (Berlin dates present in the stage's event table) are sorted and cut into consecutive runs of 21; a trailing remainder shorter than 21 is merged
  into the previous run, so every block holds >= 21 trading days. The mapping depends on the days only (never on a cell, label or arm).
* A control takes the block of its EVENT (as in stats-2). Per cell, the number of blocks with at least one non-NaN outcome is counted separately for the event arm and the
  control arm and each must reach the minimum (below). Sensitivity (descriptive, outside the correction): the same test with blocks of 42 trading days.
* The paired bootstrap resamples whole blocks of events and controls together; NaN draws abort the stage (unchanged).
* **Why this and not a two-way cluster (event day x control day):** the defect to remove is the dependence BETWEEN an event and controls up to 10 trading days away and
  between controls of neighbouring events. A two-way cluster bootstrap that resamples event days and control days independently treats the two arms as independent samples and
  ignores exactly that cross-arm link; contiguous blocks of >= 2 x 10 + 1 days contain an event together with all of its controls and with the neighbours that share its
  windows, so only the block edges remain dependent. The price is fewer independent units (next section).

## Freeze blocker (counts only, from the backfill manifests; no feature or label value was looked at)

Distinct business days of the frozen split plan (upper bounds on trading days; blocks = days // 21):

| market | TRAIN days | max TRAIN blocks | VALIDATION days | max VALIDATION blocks |
|---|---|---|---|---|
| GER40 | ~271 | 12 | ~91 | 4 |
| NAS100 / SPX500 | ~227 | 10 | ~76 | 3 |
| XAUUSD | ~226 | 10 | ~76 | 3 |
| EURUSD | ~214 | 10 | ~72 | 3 |

With `min_evidence.blocks = 30` per cell and per arm (carried over unchanged) NO cell of any market can reach the minimum in `fit` or `validate`: every test would be
`INSUFFICIENT_EVIDENCE` and every stage `INCONCLUSIVE_INSUFFICIENT_EVIDENCE`. A bootstrap over 3-4 blocks would be degenerate in any case (a handful of distinct resamples;
Holm over m = 60 cannot be reached). Lowering the minimum would be a loosening of a threshold and is therefore NOT done here. The owner must decide BEFORE freezing, without
looking at results: (a) accept a lower, explicitly justified block minimum, (b) a design that pools markets (a new preregistration), (c) more history, or (d) accept that the
block-bootstrap question cannot be asked on this data volume. Until then this draft must not be frozen or run.

## Balance gate (preflight)

* Unit: shares and the censoring threshold are fractions in the builder manifest (0.05); this preregistration speaks percentage points (5 pp). The adapter converts
  `censoring_diff_pp = 100 x censored_share.diff` and refuses a manifest that declares another unit or whose diff lies outside [-1, 1].
* Per partition (TRAIN, VALIDATION, FROZEN_OOS) PASS requires: match rate >= 0.90, |SMD| of local minute / ATR percentile / spread <= 0.10, |censoring diff| <= 5 pp over the
  MATCHED PAIRS, max session-share difference <= 0.02, and >= 20 events (otherwise `INSUFFICIENT_N`, not a pass). The preflight recomputes the verdict from the manifest
  numbers and refuses a manifest whose own verdict disagrees. The censoring balance is additionally recomputed from the Parquet label availability of the stage partition
  (same matched-pair denominator) and must also stay within +-5 pp.
* A core market that does not PASS in the stage partition is `descriptive_only` (hypotheses listed, not registered, not in m, never survivors).

## Provenance and layout

* Layout (`scripts/observer_backfill.py --step controls3`): `<M>/controls3/{controls.parquet, controls_manifest.json}` (set A), `<M>/controls3_b/{controls.parquet,
  controls_manifest.json}` (set B); the flat `<M>/controls.parquet` of that layout is the controls-2 set and is used ONLY as the explore NC-C bridge input. If a `controls3`
  directory exists it is authoritative; controls-2 files are never read as controls-3 files.
* `controls_sha256` of EVERY used control file (A and B) is mandatory: from the manifest written by the builder, or, for artifacts built before the field existed, from the
  read-only attestation (`observer_gate_c.py --attest`, written to `--out`, never into the artifacts). Conflicting declarations, a missing declaration or a hash that is not the
  file's own fail the preflight. Honest limit: an attestation is a pin (trust on first attest, tamper-evident afterwards), not proof of who built the files.

## Order of decisions (fixed)

1. balance eligibility per core market (preflight) -> 2. confirmatory eligibility (at least one core market with a registered hypothesis; explore markets never count) ->
3. only then the registry and the fit lock. If nothing is eligible the stop rule "no confirmatory test left" fires before 3: nothing is registered, no lock is taken, exit code 3,
verdict `INCONCLUSIVE_NOT_ASKED` ("kein no-enrichment").

## Machine-readable preregistration (parsed by `scripts/observer_gate_c.py`)

<!-- PREREG-JSON-BEGIN -->
```json
{
  "prereg_version": "observer-gate-c-prereg-3",
  "supersedes": "observer-gate-c-prereg-2",
  "status": "OBSERVATION_ONLY_NOT_ALPHA_VALIDATED",
  "namespace": "gatec3",
  "registry_name": "observer_gate_c_prereg3",
  "files": {
    "registry": "observer_gate_c3_registry.json",
    "report": "observer_gate_c3_report",
    "preflight": "observer_gate_c3_preflight"
  },
  "label": "y_fav050_before_adv050",
  "markets": {
    "core": [
      "GER40",
      "NAS100",
      "SPX500",
      "XAUUSD",
      "EURUSD"
    ],
    "explore": [
      "BTCUSD",
      "BRENT"
    ]
  },
  "stages": {
    "fit": {
      "partition": "TRAIN",
      "purpose": "fit"
    },
    "validate": {
      "partition": "VALIDATION",
      "purpose": "validate"
    },
    "oos": {
      "partition": "OOS",
      "purpose": "oos_test"
    }
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
    "version": "observer-stats-3",
    "block_unit": "tdays",
    "block_len_days": 21,
    "control_block": "event_day",
    "sensitivity_block_len_days": 42,
    "sensitivity_B_max": 4000,
    "nan_draws": "abort"
  },
  "controls": {
    "required_method_version": "observer-controls-3",
    "bridge_method_version": "observer-controls-2",
    "balance": {
      "min_match_rate": 0.9,
      "max_abs_smd": 0.1,
      "max_abs_censoring_diff_pp": 5.0,
      "max_session_share_diff": 0.02,
      "min_events": 20,
      "censoring_denominator": "matched_pairs"
    },
    "fallback": "descriptive_only",
    "provenance": "controls_sha256 mandatory (builder manifest or read-only attestation)"
  },
  "placebos": {
    "alpha": 0.01,
    "nc_a": {
      "controls_file": "controls_b.parquet",
      "scope": "core",
      "base_alpha": "alpha / n_core_markets"
    },
    "nc_b": {
      "shift": "same arm, nearest row to the same Berlin time of day on the next trading day",
      "tolerance_minutes": 30,
      "scope": "core"
    },
    "nc_c": {
      "controls_file": "controls_v2.parquet",
      "scope": "explore",
      "descriptive": true
    }
  },
  "min_evidence": {
    "events": 100,
    "controls": 100,
    "blocks": 30
  },
  "min_effect_abs": 0.02,
  "hypotheses": [
    {
      "id": "H01",
      "group": "acceptance",
      "feature": "f_acceptance__followthrough_dir_atr",
      "cell": "Q3/3",
      "sign": 1
    },
    {
      "id": "H02",
      "group": "acceptance",
      "feature": "f_acceptance__time_held_beyond_level_bars",
      "cell": "Q3/3",
      "sign": 1
    },
    {
      "id": "H03",
      "group": "acceptance",
      "feature": "f_acceptance__reclaim_occurred",
      "cell": "=True",
      "sign": -1
    },
    {
      "id": "H04",
      "group": "acceptance",
      "feature": "f_acceptance__max_reentry_depth_atr",
      "cell": "Q3/3",
      "sign": -1
    },
    {
      "id": "H05",
      "group": "levels",
      "feature": "f_levels__nearest_level_distance_atr",
      "cell": "Q3/3",
      "sign": 1
    },
    {
      "id": "H06",
      "group": "levels",
      "feature": "f_levels__zone_width_atr",
      "cell": "Q3/3",
      "sign": -1
    },
    {
      "id": "H07",
      "group": "swings",
      "feature": "f_swings__x_ema_trend_aligned",
      "cell": "=with",
      "sign": 1
    },
    {
      "id": "H08",
      "group": "swings",
      "feature": "f_swings__x_m15_sequence_aligned",
      "cell": "=with",
      "sign": 1
    },
    {
      "id": "H09",
      "group": "swings",
      "feature": "f_swings__x_m15_sequence_aligned",
      "cell": "=against",
      "sign": -1
    },
    {
      "id": "H10",
      "group": "balance",
      "feature": "f_balance__directional_efficiency_w24",
      "cell": "Q3/3",
      "sign": 1
    },
    {
      "id": "H11",
      "group": "balance",
      "feature": "f_balance__range_width_atr_w48",
      "cell": "Q1/3",
      "sign": 1
    },
    {
      "id": "H12",
      "group": "participation",
      "feature": "f_participation__activity_vs_same_tod",
      "cell": "Q3/3",
      "sign": 1
    }
  ],
  "negative_controls": [
    {
      "id": "N01",
      "group": "ctrl",
      "feature": "f_ctrl__x_random_uniform",
      "cell": "Q3/3",
      "sign": 0
    },
    {
      "id": "N02",
      "group": "ctrl",
      "feature": "f_ctrl__x_random_uniform",
      "cell": "Q1/3",
      "sign": 0
    }
  ]
}
```
<!-- PREREG-JSON-END -->

## Honest limits

* As drafted the design is infeasible on the current history (blocker above). Even when feasible, few blocks mean wide intervals: a null is not proof of no structure.
* Block edges stay dependent (a control up to 10 days away can sit in the neighbouring block); the sensitivity with 42-day blocks is descriptive only.
* The trading-day count is taken from the event table (days with at least one event). Days without events only make a block longer in calendar time, never shorter in trading days.
* Bar-resolution labels, no costs, partly in-sample thresholds up to 2026-06-30 (see prereg-2 and `docs/OBSERVER_LAB.md`).
