# Test Timing and Segments

Measured 2026-09-30 at base `d117016` on the shared 8 GB Windows machine (8 logical CPUs) while other
lanes and the live DEMO runner were active, so absolute times are noisy (+-20 %). Runs were serial,
one pytest process per segment, `--durations=30`. 4615 tests are collected at this base.

## 1. Baseline per top-level segment (serial, before tiering)

| Segment | Tests | Seconds | Slowest tests (s) |
|---|---:|---:|---|
| `tests/test_*.py` (root research/alpha/discovery/v2) | 1142 | 441-515 | test_ad1_benchmark small_real_benchmark 30; test_discovery_stages runner_cli 24; alpha_fast_sim_target_guard golden 23; ar2_fast_runner 22; alpha_fast_screen 22; alpha_fast_kernels_c/a 20 each |
| `tests/unit/demo` | 1500 | 167-182 | learning_shadow mlflow_lineage 20; opp_causality 11/6/4; opp_spec refit 10; opp_catchup 10+6+5; live_stack (whole file 27) |
| `tests/temporal` | 139 | 143-192 | real_events_causality (whole file 106): full_frame 39, rebuilt-prefix 10-21 each |
| `tests/events` | 69 | 52-59 | event_prefix_equality 4x ~9; event_store cache roundtrip 5 |
| `tests/parity` | 23 | 22 | execution_parity report 12-20 |
| `tests/unit/nautilus_mt5` | 219 | 21 | c7_demo_slice 1-2 each; c65_executor 3.4 total |
| `tests/unit/adapters` | 143 | 4 | - |
| `tests/unit/{nautilus_kernel,persistence,pipeline}` | 35/39/46 | 4/4/3 | store sqlite setup 2 |
| `tests/unit/risk` | 509 | 1.2 | - |
| `tests/contracts` | 185 | 0.6 | - |
| `tests/unit/execution` | 115 | 0.4 | - |
| all other unit dirs, replay, chaos, property, integration | 400 | < 2 each | - |

Serial total ~1045 s (17 min). Sum of all per-test call+setup+teardown >= 50 ms was 816 s.

## 2. Bottleneck analysis

* The suite is compute bound, not wait bound. Real-data causality/truncation/perturbation tests
  (temporal, events, opportunity, alpha_fast, v2_*), repeated module-scope frame/kernel building
  (setup of 2-21 s in discovery/alpha files), golden-variant kernel runs and GP/DEAP/MLflow training
  account for ~85 % of the time.
* Fixed waits are small and deliberate: `time.sleep` totals ~2 s (live_stack heartbeat/grace 0.3-0.6 s,
  c65_executor 2-300 ms lane threads, runner_resilience 0.05 s); `test_bounded.py` spawns a 60 s sleeper
  but kills it on a short timeout. They test real thread / OS-timeout behaviour of fail-closed code
  and were left real (no fake-clock rewrite that provably preserves the assertions).
* Subprocess/SQLite users (16 files, marked `serial`): test_bounded, test_live_stack, test_learning_shadow,
  test_opp_spec, test_runner_accounting/lane_i/resilience, test_heartbeat_resilience, test_demo_store_crash,
  test_c65_executor, test_reconciliation_source, test_store, test_backtest_cli, test_policy_purity_parity,
  test_mt5_scripts, test_alpha_fast_sim_target_guard. Each costs < 3 s; they are not the bottleneck.
* `tests/temporal`, `tests/events` and the real-slice tests read `data/` (gitignored, 260 MB). In a
  worktree without it they ERROR (FileNotFoundError on `data/ar1_ger40/download_manifest.json`).
  Junction the main checkout's `data` into the worktree (`mklink /J data <main>\data`).
* `tests/integration` was not runnable on its own (an `execution` package shadowed `src/execution` on
  `sys.path`); fixed by adding `tests/integration/__init__.py`.

## 3. Tiers (markers applied path-based in `tests/conftest.py`)

Tier markers partition the suite (`fast` + `integration` + `slow` == all 4615, nothing uncovered,
nothing deselected, skipped or xfailed). Overlays: `safety`, `chaos`, `replay`, `broker`, `serial`.

| Tier / overlay | Tests | Serial s | xdist s | Target |
|---|---:|---:|---|---|
| FAST (`-m fast`) | 3218 | 89 | 65 (-n2), 55 (-n4) | < 2 min: met |
| INTEGRATION (`-m integration`) | 792 (1 existing skip) | 112 | not used (serial) | < 5 min: met |
| SAFETY (`-m safety`, overlay across tiers) | 2819 | 126 | not used (serial) | < 8 min: met |
| SLOW/CHAOS/REPLAY (`-m slow`) | 605 | 634 | 393 (-n2), 281 (-n4) | - |
| FULL = fast + integration + slow | 4615 | ~835 (13.9 min) | ~570 (-n2 fast/slow), ~450 (-n4) | < 15 min: met, marginal |

FULL is a sum of segment runs measured on a loaded machine; expect 9-14 min. It stays below the 15 min
deferral threshold, but only just when serial; use xdist for `fast` and `slow` (the default of
`scripts/run_tests.py`).

Reclassification 2026-10-02: `tests/unit/demo/test_observer_parity.py` (12 tests, real OpportunityEngine over real DEV
slices, ~130 s measured by the lead) moved FAST -> SLOW (`SLOW_FILES`). Collected at that base (6318 tests with the
research-workbench additions): fast 4500 -> 4488, slow 759 -> 771, integration 1059 -> 1114 (+55 = the new impact/manifest guard tests in `tests/unit/scripts`, not a move); the FAST serial time drops by
~130 s. `test_runner_observer.py` (integration tier, same engine behind a fake stack) was not re-measured and stays put.

## 4. xdist decision

`pytest-xdist==3.8.0` added as a DEV dependency (uv.lock updated): measured gain is real on the
heavy segments (slow 634 -> 393 s at 2 workers, 281 s at 4; fast 89 -> 65 -> 55 s). It is opt-in:
nothing in `addopts`. `scripts/run_tests.py` uses `-n 2 --dist loadgroup` for fast and slow only;
integration and safety run serially (sqlite/subprocess/MT5 lock/heartbeat/broker tests). `serial`
tests carry `xdist_group("serial")`, so even with `-n N --dist loadgroup` they share one worker.
4 workers are safe on this machine only when the live runner and other lanes are idle (peak ~1.7 GB
free was observed at -n 4); default is 2.

## 5. Commands

```shell
uv run python scripts/run_tests.py fast|integration|safety|slow|full|changed [--workers N] [--dry-run] [-- pytest args]
scripts/test.ps1 fast            # PowerShell wrapper
scripts/test.sh fast             # bash wrapper
```

Changed-path matrix (`run_tests.py changed`, from `git diff` vs `--base main`, or explicit paths):

| Changed | Runs |
|---|---|
| `src/exits/**` | `tests/unit/exits` + reduce-only execution tests + reduce-only contracts + `tests/integration` |
| `src/risk/**` | `unit/risk`, `property`, `contracts`, `unit/execution` |
| `src/execution/**` | `unit/execution`, `unit/persistence`, `unit/risk`, `contracts`, `chaos` |
| `src/nautilus_mt5/**` | `unit/nautilus_mt5`, `unit/execution`, `unit/persistence`, `unit/risk`, `contracts` |
| `src/demo/**` | `unit/demo`, `unit/persistence`, `unit/risk`, `unit/execution`, `contracts` |
| `src/markets/**`, `src/instruments/**` | `unit/markets`, `unit/instruments`, `unit/risk`, `unit/demo/execution` |
| `src/strategies/**`, `src/alpha/**` | family unit tests (+ root `test_alpha_*`) + `contracts` (+ `parity` for strategies) |
| `research/**` | `unit/research`, `temporal` (no demo safety suite) |
| changed test files | those files |
| docs only | nothing |

## 6. Remaining bottlenecks

1. Slow tier ~6.5 min at 2 workers (real-data causality suites, alpha_fast golden kernels). Next lever:
   module-scoped shared frame fixtures for `test_v2_*`/`test_discovery_*` setups (2-21 s each) and
   caching golden-variant runs; needs per-test equivalence proof, not done here.
2. `tests/parity` (22 s) and `unit/demo/opportunity` (62 s) real-slice runs are inherently costly.
3. Machine contention: live runner + other lanes inflate every number above.
