# Research-pipeline speed (LANE SPEED)

Development infrastructure only. It changes WHEN and WHERE independent work runs and whether an identical earlier result
is reused; it never changes a computed number, a research result, a preregistration or any live path
(`src/demo/**`, `src/exits/**`, `src/execution/**`, `src/risk/**` are untouched).

## What was built

| # | Item | Where |
|---|---|---|
| 1 | Market / segment parallelism, max 3 workers, memory guard, results independent of the worker count | `src/research_speed/{parallel,scheduler}.py`, `scripts/observer_backfill.py --segmented --jobs N`, `scripts/observer_gate_b_report.py --jobs N`, `scripts/observer_gate_c.py` (`MAX_JOBS` 2 -> 3) |
| 2 | Artifact cache: `ARTIFACT_ID = sha256(DATA_HASH, FEATURE_CODE_HASH, CONTROL_CODE_HASH, CONFIG_HASH, LABEL_VERSION, PREREG_VERSION)`; identical id = CACHE HIT, a miss names the changed component | `src/research_speed/{artifact,importgraph,segments}.py`, `src/coverage_analysis/observer_lab/backfill_segments.py`; the Gate C cache key now also carries the code content hash |
| 3 | Checkpoint / resume by market x stage segments, atomic commit markers, no half artifacts | same + staged writes in `backfill.py` (events / controls-2) and `backfill_controls3.py` (set A / set B swapped in at the end) |
| 4 | Impact-aware tests, tiers T0-T3 | `scripts/impact_tests.py`, `scripts/run_tests.py`, `docs/TEST_GATES.md` |
| 5 | Green-result cache (opt-in, default OFF) | `scripts/test_result_cache.py`, `run_tests.py changed --result-cache` |
| 6 | Runtime instrumentation: wall clock per market x stage in the segment manifests, `<out>/_status.json` with heartbeat | `src/research_speed/progress.py` |

### Parallelism design

* Unit of work = SEGMENT: `<MARKET>/events`, `<MARKET>/controls3_a`, `<MARKET>/controls3_b`. `events` gates both control sets of its market;
  everything else is independent (processes, no shared state, inputs by value, outputs in disjoint files).
* The two expensive observer passes of a market (set A and set B, each about one full pass over the bars) were serial inside one process; they are now separate
  segments. Set B re-derives the (cheap, ~7 s, deterministic) selection of A because it must stay disjoint from it.
* A single observer pass itself is NOT parallelisable without changing results (the incremental observer is a sequential state machine whose event registry is part of the
  features); this lane deliberately does not touch it.
* Scheduler: ready segments start in list order, at most `clamp_jobs(...)` at a time; `jobs == 1` runs in-process with the same code. A failing segment only skips its own dependants.
* Worker cap `MAX_WORKERS = 3`, further reduced by free memory: `(available_MB - reserve) // per_worker_MB` with `reserve = 1500 MB`, `per_worker = 300 MB` (measured peak working set 195-240 MB per
  market). FAIL CLOSED: known free memory below `reserve + per_worker` => exit 4, nothing started (never rounded up to 1 worker); unknown free memory => 1 worker; `reserve >= 0`, `per_worker >= 50`. Env `RESEARCH_SPEED_RESERVE_MB` / `RESEARCH_SPEED_PER_WORKER_MB` (and `--reserve-mb`) exist for controlled benchmarks; the hard cap of 3 is not overridable.
* Spawned workers: `scripts/coverage_analysis.py` shadows the `src/coverage_analysis` package when `scripts/` is first on `sys.path` (it is, in a spawned child). The pool initializer
  `_init_worker` puts `src/` back in front before the first task is unpickled (found by a real 2-worker run; covered by the initializer test).

### Cache / invalidation (what is in each component)

| Component | Content |
|---|---|
| DATA_HASH | `backfill.frame_fingerprint(frame)` (content of the bar frame) |
| FEATURE_CODE_HASH | sha256 over the static import closure (137 repo files) of `observer_lab/backfill.py` + `scripts/entry_exit_quality.py`, excluding the control-only modules; file content with CRLF normalised, never mtime; lazily imported modules are followed too (AST walk) |
| CONTROL_CODE_HASH | same for `backfill_controls3.py` + `controls_sametime.py` (`-` for the events stage) |
| CONFIG_HASH | stage, market, limit, seed, same-time spec, A/B design, observer config hash, label max bars, hash of all `configs/**` files, and for controls the ARTIFACT_ID of the events segment they read |
| LABEL_VERSION | label convention + observer + schema + pipeline versions |
| PREREG_VERSION | `-` for the backfill; the Gate C cache key keeps the prereg block hash |

A segment is a CACHE HIT only if the manifest exists, is COMPLETE, carries the same ARTIFACT_ID and every output file still has the recorded size AND sha256. Otherwise the reason is
`NO_MANIFEST | NOT_COMPLETE | FINGERPRINT_CHANGED:<components> | FILE_MISSING | FILE_CHANGED`, and the step is run with `force=True` so its own coarser idempotence can never resurrect stale files.
Negative tests (`tests/unit/observer_lab/test_ol_segments.py`, `tests/unit/research_speed/`) cover a changed data value, feature code, control code, config (limit, seed, observer config, upstream events
artifact), version, a tampered output file, a missing file, an incomplete / corrupt manifest.

`--adopt-existing` (opt-in) commits segment manifests for COMPLETE legacy outputs without recomputing them. The code identity of those files cannot be verified (they were produced before fingerprints existed);
the manifest records `adopted_unverified_code: true`. The existing real backfill under `%LOCALAPPDATA%\Temp\observer_backfill` was only READ for this lane and is not adopted.

### Checkpoint / resume

* An aborted step leaves no final file: events and controls-2 assemble in `_stage` and `os.replace` into place, controls-3 builds `controls3[_b]._tmp` and swaps it in last; manifests are written
  tmp + `os.replace` (the commit marker is always last). Tests: aborted events step; aborted set B (finished segments untouched, the resumed set B is byte-identical to the uninterrupted one).
* Resume = run the same command again: committed segments with a matching fingerprint are skipped, the rest is computed.
* Granularity: market x stage. The "partition" axis (TRAIN / VALIDATION / FROZEN_OOS) is a tag assigned inside one sequential pass, not a separable computation, so it is not a segment boundary for the backfill.
  Gate C already caches per market x stage (fit / validate / oos); its key now also contains the code content hash.
* Longest single segment today: events of GER40 462 s, controls-3 set A of BTCUSD 541 s, so every backfill segment is far below 30 min and checkpointed at that granularity.

### Instrumentation

`<out>/_status.json` (atomic rewrite, heartbeat refreshed every 30 s by a daemon thread): per segment `state` (pending / running / done / cached / failed / skipped_dep_failed), start / finish, wall seconds,
`elapsed_s` of running ones, `long_running` (running > 30 min), `segments_finished / total`. A stale `heartbeat_utc` means the run is dead. Segment manifests keep `wall_s`, `load_s`, `step_s`,
`peak_memory_mb`, `miss_reason`.

## Measurements

Method: real data, scratch output `%TEMP%\speed_bench` (never `%LOCALAPPDATA%\Temp\observer_backfill`), `BTCUSD --limit 300` (events step + controls-3 A + B), main-checkout `.venv` Python 3.12,
8 logical cores, 7.8 GB RAM with only 350-650 MB free during the benchmark (other builders + desktop apps), so timings carry +-30 % noise
(the same legacy command took 211 s and 126 s on two runs). Wall clock via `date +%s` around the CLI.

| Run | Wall |
|---|---|
| legacy serial, run 1 (`observer_backfill.py`) | 211 s (events 77 + A 61 + B 63 + load) |
| legacy serial, run 2 (fresh, same load as the segmented runs) | 126 s |
| `--segmented --jobs 1` | 164 s |
| `--segmented --jobs 2`, but the memory guard allowed only 1 worker (free 556 MB < 1500 + 300) | 148 s |
| `--segmented`, guard parameters overridden to 200 MB / worker, still 1 worker (346 MB free) | 176 s |
| `--segmented --jobs 2` with 2 REAL worker processes (`RESEARCH_SPEED_RESERVE_MB=0`, `RESEARCH_SPEED_PER_WORKER_MB=250`, 589 MB free): events 55 s, then set A 62 s in parallel with set B 60 s | **124 s** (sum of the segment walls 178 s = 1.4x its own serial equivalent; ideal 55 + 62 = 117 s) |
| `--segmented` re-run with identical fingerprints (CACHE HIT of all 3 segments) | 12 s (9.6 s inside the run) |

Per-segment fixed overhead of the segmented mode (separate process, data load 2-3 s, code-hash closure 2.8 s feature + 2.0 s control): about 5-8 s per segment, i.e. 2-4 % of a 200-540 s segment.
The segmented-serial runs were not faster than legacy serial; the benefit comes from running segments concurrently and from CACHE HITs.

Bit equality (sha256 of all 15 parquet files of the market piece: table / events / features / labels / opportunity bars, controls-3 A and B incl. diag): legacy serial run 1 == legacy serial run 2 ==
segmented jobs 1 == segmented jobs 2 (guard-limited to 1 worker) == segmented (override, 1 worker) == segmented with 2 real worker processes: 15 / 15 identical for every pair.
Synthetic data: `test_segmented_run_is_bit_identical_to_the_monolithic_run` (45-day synthetic world, monolithic run vs three segments, all parquet hashes equal),
`test_scheduler_results_do_not_depend_on_worker_count` (real worker processes, jobs 1 vs 3), existing `test_parallel_equals_serial_bit_identical` (Gate C).

### Wall-clock estimates for the real 7-market runs (model, not a measurement)

Inputs are the REAL per-step runtimes of the existing backfill (events 147-463 s, controls-3 set A 218-541 s, A+B 404-891 s per market, read from the manifests), list scheduling exactly as the scheduler
does it (`events` before its controls; B-only segment = B - A + 7 s selection):

| Run | serial | legacy `--jobs 2` | segmented 1 / 2 / 3 workers |
|---|---|---|---|
| Controls-3 (A+B, 7 markets) | 79.6 min (observed 73 min) | 42.7 min | 80.4 / 42.0 / **29.9 min** |
| full backfill (events + controls-3) | 114.0 min | 60.2 min | 114.9 / 59.2 / **41.0 min** |
| Gate B (7 markets x ~1800 s each, 3.5 h observed; markets share no state) | 3.5 h | - | - / 2.0 / **1.5 h** (`--jobs 3`) |
| Re-run with identical inputs | - | - | seconds (CACHE HIT, ~10 s per market incl. hashing) |
| Re-run after a change that only touches control code | - | - | events CACHE HIT, only the control segments run |

So, relative to the 73-80 min serial Controls-3: about 2.5-2.7x at 3 workers (about 1.4x over the previous legacy `--jobs 2`), and a re-run with identical inputs costs seconds. These figures assume enough free
memory: with the 1500 MB reserve the guard allows 3 workers only when about 2.4 GB are free; on the benchmark day the machine had < 700 MB free, which limits a default run to 1 worker (correctly, and visibly in the log:
`jobs={'requested': 2, 'effective': 1, ...}`). Not measured: a 3-worker multi-market run (no memory headroom on the shared machine) and a complete Gate B re-run.

### Process hardening (never starve the live trader)

**Research runs are NEVER to be started next to the live runner / MT5.** `research_speed.parallel.harden_process()` is the first call of `observer_backfill`, `observer_gate_b_report` and `observer_gate_c` and enforces it as far as a script can:

* Windows: BELOW_NORMAL priority class (children inherit) and a job object with KILL_ON_JOB_CLOSE (workers die with the parent, also on a crash / kill); `OMP/OPENBLAS/MKL/NUMEXPR_NUM_THREADS=1` (set in the parent's environment, so every spawned worker inherits it; the parent's own BLAS may already be initialised when `main()` runs).
* Fail closed: free RAM < reserve + one worker => exit 4 without starting anything; invalid reserve / per-worker => exit 2.
* A python process whose command line contains `demo_trader.py` or `supervisor.py` (process table only, no artifacts/ access) or an unreadable process table => `jobs` forced to 1 with a log line; only `RESEARCH_SPEED_ALLOW_WITH_LIVE=1` overrides (default OFF).
* All pools are `managed_pool`: on any exception (incl. Ctrl-C) queued work is cancelled and the workers are terminated; a dying worker (BrokenProcessPool) marks running and open segments FAILED (exit 3, no traceback).
* Gate B writes every finished market's `gate_b.json` atomically at once and re-raises a failure only after all markets were attempted. The segmented backfill (`<out>/_run.lock`) and Gate B (`<root>/_run.lock`) take an exclusive lock (pid inside; a dead owner's lock is taken over); a second run on the same directory exits 5. A recycled pid of a long-dead owner reads as alive (the second run is refused, never doubled).

## Limits (read before relying on it)

* No speed-up inside one observer pass; the longest segment (9-15 min) bounds the parallel wall clock. With 7 markets three workers give about 2.5-2.7x, not 3x (tail effects).
* The memory guard can reduce the worker count to 1; real gains need free RAM. Peak working set per worker 195-240 MB measured.
* Legacy outputs have no segment manifests: the first `--segmented` run recomputes them (or use `--adopt-existing`, unverified code identity). The existing `observer_backfill` directory was not touched.
* The code hash is a static over-approximation: any change to a module in the import closure invalidates (also one that cannot affect results); `importlib`-style dynamic imports are not followed. The green-test-result cache therefore REFUSES (always runs) a test whose file, conftest or closure module uses `importlib.import_module` / `__import__` / importlib.util spec loading / runpy (`importgraph.dynamic_import_files`).
* Review fixes: DATA_HASH of a segment = frame bytes + normalized `eval_from` (`backfill_segments.data_identity`); a segment manifest without a non-empty, well-formed `files` set is NOT_COMPLETE (a miss, never an exception); Gate C per-market cache records are written atomically and a corrupt record is a cache miss; `src/exits/` changes also select the `-m safety` overlay.
* Gate B: parallel per market (`--jobs`); the leakage audit inside one market stays serial. The Gate B wall clock is modelled from the observed per-market times (894 / 1126 / 1301 s for the first three markets);
  only the orchestration is covered by tests (`test_gate_b_jobs.py`); no full Gate B was re-run.
* Gate C: per-market process pool as before (cap now 3); its cache key now includes the code content hash, so existing Gate C caches are recomputed once. p-values of registered hypotheses must
  reproduce exactly (existing check), so a recomputation can never silently replace a registered result.
* Result cache for tests: opt-in, fast-tier plain files only (never safety / integration / slow / -m); reads of state outside the repo (data roots, clock, network, env) cannot be validated, hence the restriction.
  It is a convenience for the inner loop; a release (T3) always runs everything.
* `run_tests.py changed` / `t1` is now conservative for paths that no rule knows (wider selection) and includes untracked new files; the previous behaviour for unmapped paths was to select nothing.

## Test timing (this lane)

Measured on the shared machine under load from other builders (`uv`-free: main-checkout `.venv`, `-p no:xdist`):

| Run | Result | Time |
|---|---|---|
| `tests/unit/research_speed` (23 tests: importgraph, artifact id, segment store, scheduler incl. real worker processes, status file, memory guard) | 23 passed | ~4 s |
| `tests/unit/scripts/test_run_tests_tiers.py` (17: existing tiers unchanged, impact mapping, result cache) | 17 passed | ~5 s |
| `tests/unit/observer_lab` + `research_speed` + tiers (final gate for the touched area, incl. `test_ol_segments`, Gate C / Gate B script tests, existing backfill / controls-3 / audit tests) | **214 passed** | **503 s (8:23)** |
| of which `test_ol_segments.py::test_segmented_run_is_bit_identical_to_the_monolithic_run` | passed | 124 s (381 s on the first, more loaded run) |
| earlier gate on the touched backfill code only (`test_ol_backfill.py` + `test_ol_controls3.py`, 32 tests) | 32 passed | 383 s |
| T1 for the whole change (`run_tests.py t1 --base observer/gate-c-run`: tests/unit/scripts, market_observer, observer_lab, research_speed, observer parity / store / hook / runner, tests/unit/research) | 750 passed, 1 skipped, **43 failed + 5 errors** | 833 s (13:48) |

The 43 failures + 5 errors of the T1 run are NOT caused by this lane: the identical set (43 failed, 5 errors, 108 passed, 1 skipped) fails on an unmodified checkout of `observer/gate-c-run` (bd3197a) in a scratch worktree
for the same test files (`MarketDataError: no manifest ...` = the real market data is not available in a worktree; `AttributeError: 'Namespace' object has no attribute 'market_observer'` in `scripts/demo_trader.py`;
scheduled-task dry-run tests of this machine). Affected files: `test_autostart`, `test_lane_r_eod_recovery`, `test_lane_v2_hard_exit`, `test_observer_adapter`, `test_observer::test_warmup_invariance_real_data`,
`test_observer_parity`, `test_observer_hook`, `test_runner_observer`.

Test-tooling change check: the segments `fast / integration / safety / slow / full` produce the same pytest commands as before (`test_existing_commands_produce_the_same_pytest_invocations`, golden timeouts / workers / marker maps).
No full suite was run (policy).

Cost of the new tests: `test_ol_segments.py` is the expensive one (two 45-day synthetic worlds: monolithic + segmented, 2.5-6 min depending on machine load); the other new files take seconds.

