# Market Structure Observer: orchestrator, bars adapter, live shadow hook (Lane O)

Status: `OBSERVATION_ONLY_NOT_ALPHA_VALIDATED`. Shadow only, DEFAULT OFF everywhere. The observer never changes an opportunity, direction, decision, reason
code, intent, entry, stop, target, size, exit profile, arbitration result, risk or execution. Feature groups: see `docs/OBSERVER_LAB.md` and the group
modules in `src/market_observer/`.

## Architecture and data flow

```
closed M5 frame (engine, read only) --+
                                      v
   demo/opportunity/observer_hook.py  ObserverShadow  (flag, budget, failure isolation, bounded queue)
        |  BarBuffer.sync (append-only per market)        market_observer/bars_adapter.py
        |  MarketStructureObserver.advance/observe        market_observer/observer.py
        |     levels -> reference level -> acceptance, swings, balance, participation   (feature groups, unchanged)
        v
   ObserverRecord (DecisionFeatures only, labels=None) -> DemoStore.record_observer -> table observer_records (additive)
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
| `scripts/autostart/supervisor.py` | NOT changed by this lane (production flags untouched) |
| heartbeat `market_observer` | `{enabled, version, records_written, records_built, errors, last_cycle_ms, warmup_false_count, budget_s, budget_exhausted, skipped_*, deferred, resets, pending, bars_seen, last_error}`; `{"enabled": false}` when off |

Insertion points (worktree `observer/lane-o`): the engine keeps a reference to its closed-bar frame only when `retain_frame` is set
(`src/demo/opportunity/engine.py` lines 310-313 and 384/392-393; the engine never reads observer output). The runner calls the hook ONCE per scanned closed
bar in `DemoRunner._scan_market` AFTER every `_process_pair` of that bar has persisted snapshot + decision (`src/demo/runner.py` line 1131, before
`set_bar_pointer`), spends the leftover cycle budget on registry catch-up in `run_cycle` (line 993), and reports in `status()` (line 2038). Table:
`src/demo/store.py` (`observer_records`, `record_observer`, `list_observer_rows`).

## ObserverShadow (live hook)

Pattern copied from Lane U2 (`OutOfWindowShadow` / `ShadowUniverseScanner`): flag-gated, failure-isolated, bounded wall budget, additive persistence.

* Called only after the decisions of a bar are final; receives the engine's frame and the final `(snapshot, decision)` pairs; READS them only (tests: frame
  hash, snapshot/decision JSON and the full engine state fingerprint are identical before/after every call).
* Observes every opportunity of the bar: accepted, engine-rejected (incl. CATCHUP_MISSED, CONCURRENT_SIGNAL, spread/overshoot rejects) and counterfactual
  ones. Direction = the snapshot's, event price = the decision bar's close. NOT observed: out-of-window shadow pairs and shadow-universe markets (not a
  trivial reuse: the frame belongs to a different scan call), see "not built".
* Every exception is caught and counted (`errors`, `last_error`); the affected market's state is dropped and rebuilt from the next frame. Nothing is ever
  raised into the engine or runner; a failing observer write is counted and contained too.
* Budget: one wall budget per runner cycle shared by all markets (`begin_cycle`). The level registry costs ~1 ms per bar, so a cold start (6000 bars)
  is spread over cycles by `warm_step`; events of bars the registry has not reached are queued (bounded, 64 per market, overflow counted as
  `skipped_budget`) and observed at THEIR OWN bar once it is reached, so the record equals the reference exactly. With the budget exhausted nothing is forced.
* Zero code path when off: the runner creates no hook, the engine never retains the frame (`retain_frame` False), the heartbeat shows `{"enabled": false}`.

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

## Persistence

Table `observer_records` (additive: `CREATE TABLE IF NOT EXISTS`, verified on a copy of the live `demo_100k/demo.sqlite`: table created empty, all 16 old
tables unchanged row for row, `PRAGMA integrity_check` ok, store schema version still `demo-store-1`). Immutable (UPDATE/DELETE aborted by trigger),
insert-once; identical re-insert is a no-op, different content for the same key raises `ImmutableRecordError` (contained by the runner).

| Column | Meaning |
|---|---|
| `record_key` (PK) | opportunity id, else event id |
| `event_id`, `opportunity_id`, `market`, `family`, `variant`, `direction`, `is_control`, `control_of`, `decision_ts_ns` | identity |
| `observer_version`, `schema_version`, `status` | `market-structure-observer-v1`, `mso-schema-1`, `OBSERVATION_ONLY_NOT_ALPHA_VALIDATED` |
| `warmup_ok` | 0/1 |
| `versions_json` | group versions (`mso-<group>-1`) |
| `features_json` | flat `f_<group>__<name>` columns (decision features only) |
| `meta_json` | warm-up detail, definition hashes per group (`hash_<group>`), config hash, `opportunity_id`, `structure_event_id`, event price, reference level id |
| `created_utc` | store clock |

NEVER inside the opportunity snapshot payload (tests: the `snapshots` rows are byte-identical with and without observer rows). No labels in the live path (no
`y_` column; labels are retrospective, computed by the lab). `demo.export.export_observer_records(store, out_dir)` writes `observer_records/data.parquet`
with exactly the columns of `ObserverRecord.to_row()` (`event_id ... v_<group>, f_<group>__<name>, m_<name>`), the same names the offline backfill produces; it is
not part of `export_all`.

## Parity proof (Gate A)

1. Engine level (`tests/unit/demo/test_observer_parity.py`): the REAL `OpportunityEngine` (production spec v1.2 = the seven production markets incl. the BTCUSD
   / BRENT STRUCT variants) driven bar by bar over the same inputs twice, observer OFF vs ON (hook after every decision). Compared byte for byte per bar:
   every snapshot JSON, decision JSON (accepted/rejected + reason codes), intent JSON (ids, entry, stop, target, risk fraction, ...), the accepted candidates,
   arbitration results (inside the snapshot), plus the engine's full state fingerprint (seen ids, intents, last bar, health, duplicate counters) before and after
   every hook call, and `last_frame is None` when off. Data: real DEV bars of the five core markets; BTCUSD/BRENT have no history in the repo, so their frames are
   price-rescaled copies of real XAUUSD bars (synthetic level, real timestamps/ranges/tick volumes). Plus the CATCH-UP path (accepted opportunities become
   rejected CATCHUP_MISSED non-trades).
2. Runner level (`tests/unit/demo/runner/test_runner_observer.py`): real engine + `DemoRunner` + `FakeStack` over replayed real bars; all store tables except
   `observer_records` (snapshots, decisions, intents, intent_events, risk, tca, pointers, seen, ...), the stack's submits and the heartbeat (minus its
   `market_observer` section) are identical ON vs OFF; one observer row per persisted opportunity; a crashing hook and a failing observer write change nothing.
3. Live == batch features (`test_observer_hook.py`): a hook fed the engine-style sliding 6000-bar frame produces exactly the feature columns of the batch
   computation over all bars (history >= every MIN_HISTORY, `warmup_ok` True both sides).

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

Cold start ~5.7 s (spread over cycles by the budget); steady state per bar ~1.2 ms median without an event, ~33 ms per observed event (all five groups at
the decision bar); `frame_arrays` of a 6000-row frame 0.9 ms. Default cycle budget 0.4 s.

## What is NOT built / honest limits

* No labels, no matched controls, no models, no scores: the live path writes decision features only. Nothing here is an edge claim.
* Out-of-window shadow pairs and the shadow universe are not observed; no per-market round steps for markets without a `ROUND_TICKS` scale (`None`).
* A larger observer-only history window and persisted observer state across restarts are not implemented (replay from the engine frame instead).
* Fib experiments (`fib` group) are not part of this orchestrator.
* BTCUSD/BRENT parity runs use rescaled XAUUSD bars (no real history in the repo). The real sizer (`Mt5DemoStack`) is not exercised by the parity tests (the engine
  emits intents with `risk_fraction`; the fake stack does no sizing); the runner/engine boundary is what the observer could possibly touch and is proven identical.
* The new real-data tests are heavy (parity ~130 s, runner parity ~60 s) and are unmarked, i.e. they fall into the `fast` tier by path: `tests/conftest.py`
  (not owned by this lane) may want to list them as `slow`.
