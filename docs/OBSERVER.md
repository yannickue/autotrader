# Market Structure Observer: orchestrator, bars adapter, live shadow hook (Lane O), live-path hardening (Lane H)

Status: `OBSERVATION_ONLY_NOT_ALPHA_VALIDATED`. Shadow only, DEFAULT OFF everywhere. The observer never changes an opportunity, direction, decision, reason
code, intent, entry, stop, target, size, exit profile, arbitration result, risk or execution. Feature groups: see `docs/OBSERVER_LAB.md` and the group
modules in `src/market_observer/`.

## Architecture and data flow

```
closed M5 frame (engine, read only) --+
                                      v
   demo/opportunity/observer_hook.py  ObserverShadow  (flag, budget, failure isolation, bounded queue)
        |  on_bar = O(1) stash (in the scan) | drain_cycle = ALL work, post-scan, ONE budget (Lane H)
        |  BarBuffer.sync (append-only per market, chunked)  market_observer/bars_adapter.py
        |  MarketStructureObserver.advance/observe        market_observer/observer.py
        |     levels -> reference level -> acceptance, swings, balance, participation   (feature groups, unchanged)
        v
   ObserverRecord (DecisionFeatures only, labels=None) -> demo/observer_store.py ObserverStore -> <artifacts_dir>/observer.sqlite (own file, Lane H)
                                                       -> demo.export.export_observer_records -> flat Parquet
```

* `market_observer/observer.py`: `observe_event(bars, i, event, config)` is the pure REFERENCE (replays the level registry from bar 0). The incremental
  `MarketStructureObserver` (level registry advanced once per bar; the other groups are pure, prefix-invariant functions evaluated at the event bar) equals it
  exactly (tested). `event_id` = sha256 of (market, decision_ts_ns, family, variant, direction, OBSERVER_VERSION). `OBSERVER_CONFIG` holds every group config;
  `with_round_steps(minor, major)` makes the per-market config (steps from `alpha.families.data.round_steps(asset_class, tick_size)`, never guessed).
* `market_observer/bars_adapter.py`: ONE path for live and historical use. `bars_from_frame` / `build_observer_bars` (batch) and `BarBuffer` (incremental, used
  by the hook; bit-identical to the batch result). No `alpha` import: ATR, segment ids and the local clock are computed from plain arrays.
  * ATR(14) = simple mean of the true range (same definition as `alpha.families.data.atr14`, verified numerically equal on real bars), evaluated per window so
    batch and incremental are bit-identical. `segment_id` +1 at every contiguity break (same fact as `FamilyData.contig_next`; `run_start` additionally splits
    at a local-day change, which is not a data break). `local_minute` / `local_day` from the calendar tz (DST by the tz database); `local_day` is an ABSOLUTE
    day ordinal (alpha's `local_clock` numbers the days of the loaded window instead, which would differ between a live window and a batch).
  * `SessionSpec`: tz always; cash open/close only where an open is defensible. `crypto_cfd` (BTCUSD) gets `None` (its calendar open is a provisional bootstrap
    schedule, not a session open); core markets and BRENT keep the calendar the engine itself uses.
* `demo/opportunity/observer_hook.py`: `ObserverShadow` (see below).

## Flags and wiring (all additive)

| Where | What |
|---|---|
| `RunnerConfig.market_observer_enabled` (default False), `market_observer_budget_s` (0.4) | runner flag |
| `build_live_runner(market_observer=None)`, `scripts/demo_trader.py --market-observer / --no-market-observer` | CLI / factory; default OFF; `--flatten-only` forces OFF |
| `scripts/autostart/supervisor.py` | NOT changed (production flags untouched) |
| heartbeat `market_observer` | ONLY when the flag is on: `{enabled, version, records_written, records_built, errors, last_cycle_ms, persist_ms_total, enqueue_ms_max, warmup_false_count, budget_s, budget_exhausted, skipped_*, deferred, dropped_pending, resets, pending, bars_seen, last_error}`. With the flag OFF the key does not exist (heartbeat dict, `DemoStore` schema, every table, the artifacts dir: exactly as before Lane O; tested against a baseline built from git `0d2ec08`) |

Insertion points: the engine keeps a reference to its closed-bar frame only when `retain_frame` is set (`src/demo/opportunity/engine.py`; the engine never
reads observer output). The runner calls `on_bar` ONCE per scanned closed bar in `DemoRunner._scan_market` AFTER every `_process_pair` of that bar has persisted
snapshot + decision (`_observer_after_bar`, O(1), before `set_bar_pointer`). ALL other observer work (`ObserverShadow.drain_cycle`, the SINGLE batched write
into `observer.sqlite`) runs in the post-scan `market_observer` section of `run_cycle` (`_observer_warm`), i.e. after every market's live scan and order
handling and after the shadow-universe section. Reporting in `status()` (flag on only).

## ObserverShadow (live hook)

Pattern copied from Lane U2 (`OutOfWindowShadow` / `ShadowUniverseScanner`): flag-gated, failure-isolated, bounded wall budget, additive persistence.

**Timing contract (Lane H; the observer must not delay the live decisions or orders of ANY market).** Two phases:

1. `on_bar(market, mspec, frame, pairs)`, called inside the live scan: O(1). It stashes a REFERENCE to the engine's frame (the engine builds a fresh frame per
   call and never mutates it afterwards) and one small plain-data tuple per opportunity (bar time, direction, family, variant, ids) into a bounded per-market
   queue (64). No array conversion, no buffer sync, no registry step, no features, no I/O. Measured: ~0.2 ms per call even with a 6000-bar frame.
2. `drain_cycle()`, once per cycle in the post-scan section: `frame_arrays`, `BarBuffer.sync` (chunked: `max_append` = 500 bars per budget check, so a
   6000-bar cold start or a buffer reset never runs as one block), the level-registry catch-up (~1.2 ms/bar), feature building (~34 ms per event) and
   (`charge`) the runner's persistence: all under ONE per-cycle budget (`market_observer_budget_s`, default 0.4 s; 0.02 s of it is reserved for the persist;
   an event is not started when less than the moving-average event cost is left). Work that does not fit stays queued and continues next cycle; queue
   overflow drops the oldest event and counts it (`skipped_budget`); the next cycle's start is never blocked beyond the budget.

* Read-only: frame, snapshot, decision are never mutated (tests: frame hash, snapshot/decision JSON, full engine state fingerprint).
* Observes every opportunity of the bar: accepted, engine-rejected (incl. CATCHUP_MISSED, CONCURRENT_SIGNAL, spread/overshoot rejects) and counterfactual
  ones. Direction = the snapshot's, event price = the decision bar's close. NOT observed: out-of-window shadow pairs and shadow-universe markets.
* Events are keyed by the TIMESTAMP of their decision bar, not a buffer index: they survive a buffer reset and are observed at THEIR OWN bar (the record
  equals the reference exactly). An event the registry has already passed, or whose bar is gone, is `skipped_stale`.
* Failure isolation (Lane H, finding 11): a bad snapshot is skipped and counted (`errors`) WITHOUT dropping the market state; an error while building one
  record drops that event only; an error in buffer / registry (state possibly inconsistent) drops the market state, counts every queued event as
  `dropped_pending` and rebuilds from the next frame (a cold start). Nothing is ever raised into the engine or runner.
* Zero code path when off: the runner creates no hook, no store, no file; the engine never retains the frame; the heartbeat has no observer key.

## Warm-up constants and what the live frame provides

| Group | Documented requirement (constant) |
|---|---|
| levels | `MIN_HISTORY_BARS` = 603 closed bars |
| swings | 480 bars (`LOOKBACK_BARS`) |
| acceptance | 98 bars (`max_break_lookback_bars` + 2) |
| balance | 48 bars |
| participation | 21 previous trading days (`MIN_HISTORY_PREV_DAYS`) |

`warmup_ok` = all of the above met at the decision bar and a finite ATR > 0; stored as `m_warmup_ok` (meta) and as the `warmup_ok` column. A record below the
requirement is still written but flagged; consumers must filter on the flag.

The engine's live frame is `DEFAULT_WINDOW_BARS` = 6000 closed M5 bars (the engine asks `LiveBarSource.m5_frame(market, 6000)`; the source's own
`bar_lookback` of 600 is only a minimum). 6000 bars cover levels, swings, acceptance and balance everywhere. Participation needs 21 previous trading days:
measured on real GER40 bars, 6000 bars contain 25 previous days (warmup_ok True); a 24 h market with a short weekend break (BTCUSD, ~280 bars/day) sits
at the edge (~21 days) and may produce `warmup_ok = false` for participation: the heartbeat counter `warmup_false_count` shows it live. A larger
observer-only window (an extra read-only `m5_frame(market, n)` request per market, which would also change the `LiveBarSource` cache request size) is NOT
implemented; it needs a lead decision. After a restart the observer replays from the first frame it sees (state is not persisted): the first minutes after a
start carry delayed records (queue) and, below the requirements, `warmup_ok = false`.

## Persistence (Lane H: its own SQLite file)

`<artifacts_dir>/observer.sqlite` via `src/demo/observer_store.py` (`ObserverStore`), opened LAZILY on the first record and only when the flag is on. The live
`demo.sqlite` (synchronous=FULL, order/risk/intent state) is NOT touched: `DemoStore` has no observer table, trigger or method (its `store.py` is identical to
git `0d2ec08`; a test builds a baseline store from that commit's source and compares `sqlite_master` row by row). The observer file: own connection, WAL,
`synchronous=NORMAL` (a crash can lose the newest observer rows, never live state; a replay re-observes and the insert-once key makes it idempotent), ONE
transaction per runner cycle (`record_many`), immutability triggers (UPDATE/DELETE aborted).

| Table | Content |
|---|---|
| `definitions` | one row per observer definition (`definition_id` = sha256 prefix of observer/schema version, status, group versions `mso-<group>-1`, the per-group definition hashes + `config_hash`, the sorted feature column names): stored ONCE |
| `observer_records` | `record_key` (PK: opportunity id, else event id), identity columns (`event_id`, `opportunity_id`, `market`, `family`, `variant`, `direction`, `is_control`, `control_of`, `decision_ts_ns`), `definition_id`, `warmup_ok`, `values_json` (feature VALUES as a JSON array in the definition's column order), `meta_json` (per-event meta only: warm-up detail, event price, reference level id, ids), `created_utc` |
| `meta` | `schema_version` = `observer-store-1` |

`list_rows()` joins the definitions back: the flat rows carry EXACTLY the columns of `ObserverRecord.to_row()` (`event_id ... v_<group>, f_<group>__<name>,
m_<name>`), the names the offline backfill produces; `demo.export.export_observer_records(observer_store, out_dir)` writes `observer_records/data.parquet`
(not part of `export_all`). No `y_` column in the live path. The opportunity snapshot never contains observer data.

Idempotent replay: the identity comparison of a re-insert ignores the audit-only meta fields (`market_observer.observer.AUDIT_META_KEYS`: `bars_available`,
`prev_days_available`, which depend on process start). The same opportunity re-observed after a crash between the observer write and `set_bar_pointer` is a
no-op (no error, no duplicate row); any other difference raises `ImmutableRecordError` (counted, contained).

Size (measured on 200 real GER40 records, 6000-bar frames): ~1.9 KB per row on disk including both indexes (payload 1.1 KB; the previous in-`demo.sqlite` row
had ~4.8 KB of JSON payload alone), one 200-row batch commits in ~44 ms, a typical per-cycle batch of 1-5 rows in a few ms. Expectation at the observed
~50 opportunities per day per run: ~0.1 MB per day.

## Parity proof (Gate A)

1. Engine level (`tests/unit/demo/test_observer_parity.py`): the REAL `OpportunityEngine` (production spec v1.2 = the seven production markets incl. the BTCUSD
   / BRENT STRUCT variants) driven bar by bar over the same inputs twice, observer OFF vs ON (`on_bar` + `drain_cycle` after every decision). Compared byte for
   byte per bar: every snapshot JSON, decision JSON (accepted/rejected + reason codes), intent JSON (ids, entry, stop, target, risk fraction, ...), the accepted
   candidates, arbitration results (inside the snapshot), plus the engine's full state fingerprint before and after every hook call, and `last_frame is None`
   when off. Data: real DEV bars of the five core markets; BTCUSD/BRENT frames are price-rescaled copies of real XAUUSD bars. Plus the CATCH-UP path.
2. Runner level, content (`tests/unit/demo/runner/test_runner_observer.py`): real engine + `DemoRunner` + `FakeStack` over replayed real bars; EVERY table of
   `demo.sqlite` (no table differs, there is no observer table), the stack's submits and the heartbeat (minus its `market_observer` section, which only exists
   when on) are identical ON vs OFF; one observer row per persisted opportunity in `observer.sqlite`; records of a cycle go out in one batched call; a
   crashing hook and a failing observer write change nothing.
3. Runner level, TIMING (`tests/unit/demo/runner/test_runner_observer_timing.py`, Lane H): four markets in the same cycles, the real 0.4 s budget, the
   engine's 6000-bar production window (cold start in cycle 0) and the REAL wall clock. Per cycle the scan-start offset of every market is measured with the
   observer OFF and ON (minimum over 3 interleaved repeats each); the ON-OFF difference must be <= epsilon = max(60 ms, 2 x run-to-run noise of the OFF case,
   noise = best-vs-second-best OFF run per cell), and epsilon itself must stay < 0.75 x budget. The test also asserts that all observer work (everything but the
   O(1) `on_bar`) starts after the LAST market's scan ended, that every `on_bar` is < 10 ms, and that the observer's in-cycle time is <= budget + 150 ms. The
   pre-fix implementation (all work inside `on_bar`) fails it: `on_bar` took 0.40 s and delayed every later market.
4. Live == batch features (`test_observer_hook.py`): a hook fed the engine-style sliding 6000-bar frame produces exactly the feature columns of the batch
   computation; a chunked cold start (buffer filled 500 bars per budget check) is bit-identical to the unbounded batch conversion and yields the reference record.
5. Static: no execution / risk / exits / nautilus_mt5 / adapter module (nor `store.py` / `export.py`) imports `market_observer`, the hook or `observer_store`;
   the only importer of `observer_store` in `src` is the runner (lazily, flag on); `SwingReplay` is imported by no `src` module.

## Test catalogue to file

Baseline parity, prefix invariance, long/short symmetry, confirmation timing at the boundary, no future usage, touch counting, role transitions, cluster
determinism, session/timezone through the adapter (BTCUSD no session; GER40 Berlin and NAS100 New York DST weeks 2026-03/10/11 with synthetic local arrays and
the real calendar), gap reset at segment boundaries, serialisation + schema version, restart/replay determinism, feature vs label separation, no effect on
risk/execution (static), live vs historical parity, recursive/warm-up invariance (120/240/500/700 vs 6000/9000 bars), HTF closed-bar alignment: see the
docstring lists at the top of `tests/unit/market_observer/test_observer.py`, `test_observer_adapter.py`, `tests/unit/demo/opportunity/test_observer_hook.py`,
`tests/unit/demo/test_observer_store.py`, `tests/unit/demo/test_observer_parity.py`, `tests/unit/demo/runner/test_runner_observer.py`. Matched-control
reproducibility belongs to the lab (Lane D1).

## Isolation rules (tested)

`market_observer` imports no `alpha` / execution / risk / exits module (it reuses `demo.structure` through the swing group, as before). `observer_hook` imports
only the allowlisted `alpha.families` (`round_steps`) from alpha, nothing from execution/risk/exits; no execution / risk / exits / nautilus / adapter module
imports `market_observer`; the engine and decision modules never reference observer output. The `demo.opportunity` alpha allowlist is NOT extended.

## Measured cost (real GER40 bars, 6000-bar frame)

Cold start ~5.7 s of registry work per market (spread over cycles by the budget; with four markets the observer uses the whole budget for ~1-2 minutes of
cycles after a start / reset and delays each following cycle's start by at most the budget); steady state per bar ~1.2 ms median without an event, ~33 ms per
observed event (all five groups at the decision bar); `frame_arrays` of a 6000-row frame ~1 ms; the in-scan `on_bar` ~0.2 ms; a 500-bar buffer chunk a few
tens of ms. Default cycle budget 0.4 s (all of it: arrays, sync, registry, features, persist).

## What is NOT built / honest limits

* No labels, no matched controls, no models, no scores: the live path writes decision features only. Nothing here is an edge claim.
* Out-of-window shadow pairs and the shadow universe are not observed; no per-market round steps for markets without a `ROUND_TICKS` scale (`None`).
* A larger observer-only history window and persisted observer state across restarts are not implemented (replay from the engine frame instead).
* During a cold start the observer occupies up to the full per-cycle budget of every cycle (after the live scans), i.e. the next cycle's start (position
  management / feed refresh) can be later by at most `market_observer_budget_s` for the first minutes after a start; lower the budget to trade a longer
  warm-up for less per-cycle occupancy. Live decisions and orders of the same cycle are never delayed (tested).
* `SwingReplay` (swings.py) is a TEST-ONLY helper (it computes over the whole segment, also after bar i); the live path uses `swing_features`.
* Fib experiments (`fib` group) are not part of this orchestrator.
* BTCUSD/BRENT parity runs use rescaled XAUUSD bars (no real history in the repo). The real sizer (`Mt5DemoStack`) is not exercised by the parity tests (the engine
  emits intents with `risk_fraction`; the fake stack does no sizing); the runner/engine boundary is what the observer could possibly touch and is proven identical.
* The new real-data tests are heavy (parity ~130 s, runner parity ~90 s, timing parity ~25 s) and are unmarked, i.e. they fall into the `fast` tier by path: `tests/conftest.py`
  (not owned by this lane) may want to list them as `slow`.
