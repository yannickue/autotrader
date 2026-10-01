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

No test is removed, deselected or weakened; FULL stays available (`full` / `t3`).

### Green-result cache (opt-in, `run_tests.py changed --result-cache`; default OFF)

A previous GREEN run of an explicit file list may be reused only when the test files, the conftest chain, the static import
closure of repo modules, `pyproject.toml`, `uv.lock`, `configs/`, `tests/fixtures/`, the Python version and the pytest arguments
are byte-identical (`scripts/test_result_cache.py`). In doubt: miss. It never serves `-m` selections, SAFETY / chaos / replay /
parity / broker / serial suites, or the integration and slow tiers (so no safety segment is ever "proved" by the cache), and the
`fast|integration|safety|slow|full|t2|t3` commands never consult it. A hit is printed as `CACHE HIT` with the original green
time; a safety segment must always run for a release.
