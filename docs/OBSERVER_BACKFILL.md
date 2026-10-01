# Observer historical backfill (Lane D2, Gate B)

Status: `OBSERVATION_ONLY_NOT_ALPHA_VALIDATED`. Offline / research only. Nothing here touches live code, the demo trader, MT5, schedulers or `artifacts/`;
no edge claim. Code: `src/coverage_analysis/observer_lab/backfill.py` (+ `backfill_audit.py`), CLIs `scripts/observer_backfill.py`,
`scripts/observer_gate_b_report.py`. Generated evidence: `docs/evidence/observer_backfill_report.{md,json}`.

## Pipeline (per market, one market at a time)

```
dev frame (existing loaders, bars <= 2026-08-31 Berlin, guard asserted twice)         scripts/entry_exit_quality.build_market_inputs
   |-- MarketInputs.specs (frozen production specs v1.2: core v1 families, BTCUSD/BRENT STRUCT variants)
   v
STEP 1  run_events_step                                                              manifest.json
   generate_candidates(...) per spec  -> EventRow (family, variant, direction, decision bar, entry=close, stop, risk, target, structure_event_id)
   ONE sequential pass of the incremental MarketStructureObserver (bars_adapter = the live adapter) -> ObserverRecord at every event bar
   label_event (first passage 0.25/0.25, 0.50/0.50, 0.75/0.50, 1.00/0.50, MFE/MAE, bar-resolution stop-first, horizon = min(48 bars, session end, segment break, local-day change))
   partition tag (observer_lab.splits) -> table / events / features / labels .parquet, opportunity_bars.parquet
STEP 2  run_controls_step  (independently re-runnable; READS step 1, never rewrites it)    controls_manifest.json
   select_controls -> observer_lab.controls.match_controls (market, session bucket, time of day +-30 min, ATR percentile +-0.10, spread band +-0.15,
   direction inherited, seeded, +-48 bars exclusion around EVERY generator candidate bar incl. the ones filtered out of the event table)
   same observer pass at the control bars, same labels (a control inherits the risk distance R, price units, of its event)
   -> controls / controls_events / controls_features / controls_labels .parquet
```

* No second replay engine, no second feature implementation: events = existing family generators (`alpha.families.registry.generate_candidates`, as in
  Lane X / the live engine); features = `market_observer` only; labels/controls = `observer_lab`.
* A candidate is dropped and COUNTED (manifest `events.exclusions`) when it lies before the evaluation start (30-day family warm-up), has no following bar,
  has no finite stop, or its stop is not on the loss side of the decision close; duplicate (bar, family, variant, direction) rows are collapsed (first spec wins).
  The live operating policy (entry windows, flatten, runway) is NOT applied: events are generator opportunities.
* Event price = decision-bar close (what the live hook observes); `risk = |close - stop|`. A target is stored when finite (production specs leave the target to the
  broker TP: usually NaN) and is NOT used by the labels (`y_structural_target_reached` stays None for events and controls alike).
* BTCUSD / BRENT bars come from the Phase-2 history (`--phase2-root`, default search as in `entry_exit_quality`); the Phase-2 loader opens only months <= 2026-08.
* Idempotent / resumable per step: a complete step with an identical fingerprint is skipped (`status_this_call = SKIPPED_COMPLETE`); an incomplete or changed step
  is rebuilt from scratch (the observer is sequential, so the unit of resumption is the step of a market). Memory: bar arrays + chunked rows (2000 rows per
  Parquet part, merged by streaming); no per-event copy of the history.

## File layout (`<out>/<MARKET>/`, default `%LOCALAPPDATA%\Temp\observer_backfill`, never committed)

| File | Content |
|---|---|
| `table.parquet` | events: exactly `ObserverRecord.to_row()` + `warmup_ok` + `run_id` + `partition` (identity, `v_<group>`, `f_<group>__<name>`, `m_<meta>`, `horizon_end_ts_ns`, `y_*`) |
| `events.parquet` | identity + generator fields (`decision_idx`, `strategy_id`, `structure_event_id`, `entry`, `stop`, `risk`, `risk_atr`, `target`) + `partition` + `match_*` (descriptive full-sample percentile ranks, NOT features) |
| `features.parquet` | identity + `v_*` + `f_*` + `m_*` + `warmup_ok` + `run_id`; **no `y_*`, no `horizon_end_ts_ns`, no `partition`** |
| `labels.parquet` | `event_id`, `is_control`, `control_of`, `horizon_end_ts_ns`, `y_*` |
| `controls.parquet`, `controls_events / _features / _labels.parquet` | the same four files for the matched controls (`is_control = True`, `control_of = <event_id>`) |
| `opportunity_bars.parquet` | decision index of EVERY generator candidate (controls stay +-48 bars away from all of them) |
| `manifest.json`, `controls_manifest.json` | completion marker + provenance (see below) |
| `backfill.log` | run log |

Column naming is the schema of `market_observer.schema` (`f_<group>__<name>` decision features, `y_<name>` labels, `v_<group>` group versions,
`m_<name>` meta incl. `m_warmup_ok`, `m_hash_<group>` definition hashes, `m_config_hash`). `event_id` = sha256(market, decision_ts_ns, family, variant,
direction, OBSERVER_VERSION)[:32]; the controls use the same function at their own bar (inherited family / variant / direction).

`load_event_table(path, with_labels=False, controls=True)` returns events (+ controls) with features, joined on `event_id`. Labels are physically
separate: a labels file is opened only with `with_labels=True`; the feature/event files are asserted free of `y_*` / `horizon_end_ts_ns` (a corrupt directory raises).
`partition` and `horizon_end_ts_ns` use the label horizon (future bars of the event): they are split tags / labels, never model inputs.

## Manifests

`manifest.json` (events step): status `COMPLETE`, fingerprint / `run_id`, code identity (git SHA, dirty flag, source hash), `observer_version`, `schema_version`,
group versions and definition hashes, `observer_config_hash`, label convention version, dev-end guard result, data coverage (first / last bar, bars, gaps,
missing months, frame fingerprint, evaluation start), partition plan (`has_frozen_split`, note), event counts per family|variant|direction, exclusion counters,
warm-up counts, row counts, runtime, peak memory. `controls_manifest.json`: events fingerprint it was built from, seed, match spec,
`control_method_version`, `matching_revision` (partition-aware, `observer-controls-2`) and `matching_is_pre_revision` (false), match report (match rate, unmatched, SMD, session
share difference), match rate by family, controls in a different partition than their event, warm-up counts, rows, runtime, memory.

## Partition column

`TRAIN | VALIDATION | FROZEN_OOS | PURGED | EMBARGO` from `observer_lab.splits` (core fit end 2026-06-30, validation = last 25 % of the trading days up to it,
OOS = 2026-07-01..2026-08-31 written as `FROZEN_OOS`, FORWARD never occurs). PURGED: the label horizon reaches into another partition; EMBARGO: inside
the first 4 h after a boundary. BTCUSD / BRENT have NO frozen split: the same generic dev dates are applied for comparability, `has_frozen_split = false`
and the manifest note says the TRAIN / VALIDATION tags are not a frozen train/validation of any fitted parameter.

## Matching (controls)

Controls come from the partition-aware `observer_lab.controls.match_controls` (`CONTROL_METHOD_VERSION = observer-controls-2`): drawn INSIDE the event's
partition (TRAIN / VALIDATION / FROZEN_OOS), percentile ranks (ATR, spread) computed within the partition, PURGED / EMBARGO / UNASSIGNED bars are never
controls (events there are reported unmatched), `exclude_idx` = the decision bars of EVERY generator candidate of the market (all families, also those
filtered out of the event table) with the +-48 bar neighbourhood closed. Frames with fewer than 4 trading days up to the core fit end fall back to
`partition=None` (synthetic tests only). `select_controls` is the single selection function and `CONTROL_MATCHING_REVISION` is part of the controls
fingerprint, so a matching change re-runs only the controls (`--step controls`); events, features and labels of events are not recomputed.
The first (pre-revision, whole-sample ranks, no partition restriction) controls of every market are kept next to the backfill under `_prerevision/` and
compared in the Gate B report. BTCUSD / BRENT have no frozen split: their partition tags are generic dev-date tags.

## Forward system (live table vs backfill)

The live shadow hook writes the SAME `ObserverRecord` through the SAME adapter and observer version: `demo.export.export_observer_records` produces
`observer_records/data.parquet` with exactly the `ObserverRecord.to_row()` columns (`event_id ... v_<group>, f_<group>__<name>, m_<name>`), no `y_*`
(labels are retrospective). A backfill feature table and the live table are therefore column-compatible and version-compatible
(`observer_version`, `schema_version`, `v_<group>`, `m_hash_<group>`, `m_config_hash` must be equal before rows are mixed; a version bump of any group means
re-running the backfill). Live rows have `family`/`variant` from the engine, `opportunity_id` set and `m_event_price` = decision close, as here. The
live-style incremental `BarBuffer` path is proven equal to the batch features by the Gate B audit (below) and by Gate A.

## How to run

```
uv run python scripts/observer_backfill.py --market GER40 --limit 60 --out <tmp>                  # smoke
uv run python scripts/observer_backfill.py --out C:\Users\yanni\AppData\Local\Temp\observer_backfill # all seven markets, one at a time
uv run python scripts/observer_backfill.py --step controls --force                                 # only the controls (after a matching revision)
uv run python scripts/observer_gate_b_report.py --root C:\Users\yanni\AppData\Local\Temp\observer_backfill
uv run pytest tests/unit/observer_lab/test_ol_backfill.py tests/unit/observer_lab/test_ol_backfill_audit.py -q -p no:xdist
```

## Gate B checks (scripts/observer_gate_b_report.py)

1. Plausibility per market and group: n, missing share, quantiles, constant columns, impossible values (negative widths / counts / ages, ratios outside
   [0, 1], non-finite, timestamps later than the decision time, enum membership).
2. Leakage audit on real data (seeded sample, default 300 events + 30 controls per market): (a) a live-style incremental pass (engine-style sliding 6000-bar
   frames synced into a `BarBuffer`, the observer sees only physically truncated views, full history from bar 0) must equal the stored records EXACTLY
   (prefix invariance with full history; also the live-vs-batch parity proof); (b) records recomputed from a RAW frame window truncated exactly at the
   decision bar and rebuilt through the batch adapter; (c) the same with 500 future bars present in the frame; (d) shallow histories (120/240/500 bars)
   must be flagged `warmup_ok = false`. The harness has a negative control (a deliberately leaky toy feature is detected; unit tests).
3. Warm-up accounting per group (the documented MIN_HISTORY constants) and per family.
4. Label sanity: base rates of events and controls against the zero-drift reference `P = fav / (fav + adv)`, censoring share, internal consistency
   (first-passage vs MFE/MAE, monotone chain), horizon bounds.
5. Matching quality: match rate, unmatched events, standardised mean differences of the matching variables, rate by family.
6. Data coverage and the honest caveats (BTC/BRENT: no frozen split, short history, missing months; core thresholds partly fitted to 2026-06-30; the dev
   window is not a clean holdout for the core; the forward period is never read).

## Limits

* Events are generator opportunities, strongly clustered in time (many families fire on the same bars); controls must stay 48 bars away from every
  opportunity, so the match rate can be low in busy markets. Low rates are reported, not hidden; they are a property of the exclusion rule.
* Bar-resolution labels (M5), stop-first, cost-free; first-passage rates are not P&L.
* Percentile ranks used for matching are descriptive over the whole market sample (descriptive `match_*` columns of `events.parquet`; the matching itself uses partition-internal ranks), `match_*` columns must never be used as features.
* The audit compares exact equality (NaN == None); records whose windowed history is not warm are skipped and counted, not hidden.
* One market per run; the level registry costs ~1 ms per bar, an observed record ~30 ms, so a market takes minutes to tens of minutes.
