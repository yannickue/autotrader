# Test Gates

## Test layers

- **Unit:** pure validation, sizing, transitions, normalization, and edge cases.
- **Integration:** framework/adapter boundaries, persistence, clocks, configuration, and schemas.
- **Replay:** deterministic outcomes from recorded event streams and restart checkpoints.
- **Property/invariant:** generated event sequences that pressure global safety properties.
- **Chaos/failure:** disconnects, timeouts, duplicates, reordering, partial fills, crashes, and
  reconciliation differences.

## Mandatory invariants

1. Exposure never exceeds the configured maximum.
2. Selected leverage never exceeds any applicable maximum or the 30x system ceiling.
3. Reduce-only never increases exposure.
4. A fill id is counted at most once.
5. Duplicate order events cannot create duplicate positions.
6. Stale signals or stale/invalid market data cannot open new positions.
7. Restart/reconnect cannot silently lose open positions or orders.
8. Risk-engine or data-feed failure prevents new exposure.
9. Replaying identical data/config/code produces identical decisions and order intents.

## Research gates

Backtests include fees, spread, slippage, funding where relevant, latency, and partial-fill models.
They use time-ordered walk-forward/out-of-sample evaluation, point-in-time universes, and explicit
controls for lookahead, survivorship, leakage, overfitting, and parameter instability. Optuna's
search space and seed are recorded; selection uses robustness and risk-adjusted evidence rather
than the highest in-sample return or a fixed daily trade count.

## Test policy and segments (persistent, user-mandated)

Markers are applied path-based by `tests/conftest.py`; timings are in `docs/TEST_TIMING.md`.
Tiers partition the suite: **FAST** (`-m fast`, target < 2 min), **INTEGRATION** (`-m integration`,
< 5 min), **SLOW/CHAOS/REPLAY** (`-m slow`); **SAFETY** (`-m safety`, < 8 min) is an overlay covering
risk, execution, reconciliation, persistence, reduce-only, idempotency, stale-signal, exposure and
leverage invariants (plus contracts, property, chaos and parity suites). **FULL** = all tiers. Every
test stays in FULL; no test is deselected, skipped or xfailed by the tiering.

| Situation | Required |
|---|---|
| Lane completion | targeted tests + relevant contracts/invariants + `ruff check` on the touched area + `compileall` of the relevant paths. No full repo. |
| Normal merge | targeted + FAST + relevant INTEGRATION (+ relevant SAFETY when risk, execution, reconciliation or persistence changed). Use `run_tests.py changed`. |
| Major release / phase / safety gate | FULL, preferably segmented (`run_tests.py full`). |

If FULL is expected to take more than 15 minutes, do not start it and report
`FULL DEFERRED — SLOW SUITE PERFORMANCE STILL ABOVE BUDGET`. Never run an unqualified whole-repo
`pytest` on the shared 8 GB machine while the live runner/MT5 is active. `serial`-marked tests
(sqlite/subprocess/MT5/heartbeat) are never spread across xdist workers concurrently.

## Commands and promotion

```shell
uv sync --frozen --python 3.12
uv run ruff check .
uv run python -m compileall -q src scripts tests
uv run python scripts/run_tests.py fast          # or integration | safety | slow | full | changed
```

`uv run pytest` over the whole repo is reserved for explicit release/phase/safety gates and remains
valid (every test is still collected); the segmented form is preferred.

Paper mode requires all implemented gates to pass. Shadow and live promotion additionally require
recorded replay parity, reconciliation and restart tests, chaos tests, venue sandbox evidence,
operational alerts, credential isolation, rollback/runbooks, and explicit human approval. A passing
unit suite alone never enables live trading.

## Named tiers T0-T3 and impact selection (additive; the segments above are unchanged)

| Tier | What | Target | Command |
|---|---|---|---|
| **T0** | `ruff check` + `compileall` of the changed python files + the touched test files themselves | < 60 s, local inner loop | `run_tests.py t0` |
| **T1** impact | the tests selected from the changed paths (`scripts/impact_tests.py`, conservative) | 2-5 min | `run_tests.py t1` (= `changed`) |
| **T2** | INTEGRATION + SAFETY segments (merge gate) | ~15 min | `run_tests.py t2` |
| **T3** release | FULL (fast + integration + slow) + SAFETY overlay; run the broker canary manually when execution / risk / exits changed | 20-45 min | `run_tests.py t3` |

Impact mapping (every pre-existing `MATRIX` rule stays; the rules below are added on top, all matching rules count):

| Changed path | Selected |
|---|---|
| `src/market_observer/`, `src/coverage_analysis/`, `src/research_speed/`, `src/demo/observer_store.py`, `scripts/observer_*` | observer tests, observer-lab tests (leakage / negative controls / controls / backfill / gate C), live-vs-batch parity, observer store, research tests, research_speed tests |
| `src/execution/` | execution + reconciliation/persistence + risk + contracts + chaos + demo execution + the SAFETY overlay (+ canary note) |
| `src/risk/` | risk + portfolio + property + contracts + execution + the SAFETY overlay |
| `src/exits/` | exit engine + protective orders (execution) + execution integration + integration tier |
| `docs/`, `reports/`, `*.md`, `*.txt` only | no pytest; `git diff --check` |
| test helper / `conftest.py` below `tests/<a>/` | that directory; `tests/conftest.py`, `pyproject.toml`, `uv.lock`, top-level test helpers -> segmented FULL |
| anything else (unknown path) | WIDER, never narrower: `-m "fast or integration or safety"` |

Production-reachable paths (added 2026-10-02, port of the semantics of `dc6346c` onto the union mapping in `scripts/impact_tests.py`,
`PRODUCTION_SAFETY_RULES`):

| Changed path | Selected (in addition to the pre-existing rule) |
|---|---|
| `src/alpha/{families,fast,common,signals}/`, `src/alpha/{session.py,context*,timeframe*,regime*,__init__.py}` (production-live alpha) | `tests/unit/demo` + the SAFETY overlay; `src/alpha/common/` also `tests/unit/markets` |
| `src/nautilus_mt5/`, `src/adapters/`, `src/persistence/`, `src/demo/` (whole), `src/data/`, `src/markets/`, `src/instruments/`, `src/margin/`, `src/market_observer/`, `src/research_speed/`, `scripts/autostart/`, `scripts/demo_trader.py` | the SAFETY overlay (`-m safety`) |
| `src/exits/`, `src/risk/`, `src/execution/` | SAFETY overlay (already) |
| research-only alpha (`src/alpha/discovery/` ...) | `tests/unit/alpha`, strategies, contracts: no demo tests, no safety overlay |

Invariants of the impact selection (enforced by `tests/unit/scripts/test_impact_live_alpha_and_safety.py` and `test_run_tests_tiers.py`):

* **Never narrower:** rules only ever ADD targets (union of the `MATRIX` rule and every matching `EXTRA_RULES` entry); an unknown
  non-doc path widens to `-m "fast or integration or safety"`; global config (`pyproject.toml`, `uv.lock`, `tests/conftest.py`)
  selects the segmented FULL suite; docs-only changes select no pytest.
* **Loud git failure:** `run_tests.changed_paths` raises `RuntimeError` when `git diff --name-only <base>...HEAD`, `git diff --name-only`
  or `git ls-files --others` fails (unknown base ref, not a repo). A broken git call can never turn into an empty plan / "nothing changed".
* **Drift guard:** every file in the production runtime closure (below) must select `-m safety`; every reachable `src/alpha/` file must
  also select `tests/unit/demo`. A module that becomes reachable without a rule fails the test, so the prefix lists cannot rot silently.
  A module being UNREACHABLE never removes a test: FULL still collects everything.

### Production runtime import manifest (`scripts/runtime_import_manifest.py`)

`uv run python scripts/runtime_import_manifest.py [--check] [--summary] [--out PATH]` writes
`artifacts/research/runtime_import_manifest.json` (never committed) with `entry_points`, `modules` (sorted; file, loc, package),
`packages` / `subpackages` (reachable vs unreachable LOC and files), `dynamic_import_sites`, `generated_commit`.

* Entry points (explicit list `ENTRY_POINTS`): `scripts/demo_trader.py`, `scripts/autostart/{supervisor,eod_recovery,deploy_gate,instance_lock}.py`;
  python files named by `scripts/autostart/*.ps1` / `*.task.xml` launchers are parsed and must be in that list (or in `EXCLUDED_AUTOSTART`, today
  `approve_deploy.py`, an operator tool). A new `scripts/autostart/*.py` that is neither fails `test_every_autostart_python_file_is_an_entry_point_or_explicitly_excluded`.
* Closure: re-uses `src/research_speed/importgraph.py` (AST, lazy/function-level imports, relative imports, `from x import y` submodules, parent
  packages). A constant `importlib.import_module("a.b")` / `__import__("a.b")` is resolved and followed; every other dynamic loader
  (`import_module(var)`, `spec_from_file_location`, `runpy`, `exec`/`eval` of import strings) is listed as an unresolved site.
* `--check` exits non-zero on an unresolved dynamic import (unless reviewed in `REVIEWED_DYNAMIC_SITES` with a reason), a missing entry
  point or an unlisted autostart script. Measured at 2026-10-02: 0 dynamic import sites in the production closure.

### Observer parity classification

`tests/unit/demo/test_observer_parity.py` (GATE A: the real `OpportunityEngine` replayed bar by bar over real DEV slices with the observer on/off,
12 tests, ~130 s) is a real-data parity suite and lives in the **slow** tier (`SLOW_FILES` in `tests/conftest.py`); it used to run in FAST and dominated the
< 2 min loop. `tests/unit/demo/opportunity/test_observer_hook.py` is already slow (directory rule); `tests/unit/demo/runner/test_runner_observer.py` stays
INTEGRATION (runner directory rule, fake stack, no measured evidence that it is heavy) and `tests/unit/demo/test_observer_store.py` stays FAST (store
unit tests). The impact rules for observer paths still select all of them (`OBSERVER_TESTS`). Tiers still partition the suite (FULL == fast + integration + slow).

No test is removed, deselected or weakened; FULL stays available (`full` / `t3`).

### Green-result cache (opt-in, `run_tests.py changed --result-cache`; default OFF)

A previous GREEN run of an explicit file list may be reused only when the test files, the conftest chain, the static import
closure of repo modules, `pyproject.toml`, `uv.lock`, `configs/`, `tests/fixtures/`, the Python version and the pytest arguments
are byte-identical (`scripts/test_result_cache.py`). In doubt: miss. It never serves `-m` selections, SAFETY / chaos / replay /
parity / broker / serial suites, or the integration and slow tiers (so no safety segment is ever "proved" by the cache), and the
`fast|integration|safety|slow|full|t2|t3` commands never consult it. A hit is printed as `CACHE HIT` with the original green
time; a safety segment must always run for a release.
