# Research Agent Report — Sprint 1

## Delivered

- Immutable, versioned Parquet partitions by dataset version, venue, instrument, event type, and
  date, with SHA-256 manifests, schema version, provenance, receive-time ordering, idempotent
  identical duplicates, and fail-closed conflicting duplicates.
- Deterministic replay with canonical dataset and configuration hashes plus compact JSON summaries.
- A small deterministic backtest event loop that orders by `available_at`, exposes only the history
  available at each decision, rejects data available before event time, and requires explicit `is`
  or `oos` labeling.
- Cost-aware metrics: trade count, win rate, average win/loss, expectancy, profit factor, per-trade
  Sharpe and Sortino, max drawdown, turnover, fees, spread, slippage, funding, and net PnL.
- Rolling and anchored walk-forward window construction with disjoint time-ordered IS/OOS slices.
- Dependency-light VectorBT input preparation and a seeded Optuna-style search-space contract that
  only permits an `oos_` objective. No optimization job is launched.
- Directly runnable `scripts/backtest.py` and `scripts/replay.py` entry points producing one compact,
  sorted JSON object.

## TDD Evidence

- Initial owned test collection: RED with five missing modules.
- Core implementation: GREEN, 12 tests passed.
- CLI tests: RED because both `main` functions were absent; GREEN, 2 tests passed.
- Conflicting duplicate test: RED because the second payload was silently ignored; GREEN after
  fail-closed validation.
- Direct-script regression test: RED because repository root/`src` were absent from `sys.path`;
  GREEN after scoped entry-point bootstrapping.

## Final Verification

All commands used the requested Python environment from `trader/.venv` and this worktree as cwd.

- Owned tests: `16 passed in 1.66s`.
- Full pytest suite: `23 passed in 1.62s`.
- Ruff over the full worktree: `All checks passed!`.
- Compileall over owned production and test paths: exit 0.
- Direct script `--help` smoke checks: exit codes `[0, 0]`.
- Import smoke check for all new modules: `imports: PASS`.

Pytest was run with `-p no:cacheprovider` because the managed worktree denies creation of
`.pytest_cache`; this does not affect collection or test execution.

## Dependency Need

`pyarrow` is available in the supplied virtual environment and is essential for real Parquet I/O,
but it is not a direct project dependency. Per lead instruction, `pyproject.toml` and `uv.lock` were
not edited. Integration must add an explicit pinned/direct `pyarrow` dependency and regenerate the
lockfile.

VectorBT and Optuna remain optional and were not imported or added as dependencies.

## Open Questions / Sprint 1 Decisions

1. The data contract specifies idempotent duplicate source keys but not conflicting payloads for the
   same key. Lead approved Sprint 1 behavior: logically identical rows deduplicate; conflicting rows
   raise `ValueError` and fail closed. The shared contract should codify this.
2. The contracts do not define the return sampling frequency or annualization factor. Sprint 1
   reports deterministic, non-annualized per-trade Sharpe and Sortino ratios.
3. The initial Parquet writer infers physical Arrow field types from each partition. A production
   schema registry and compatibility/migration rules remain to be defined before ingesting venue
   data.
4. Fees, spread, slippage, and funding are explicit trade inputs and included in net PnL. Latency
   and partial-fill simulation require execution-model contracts and are not invented here.
5. Point-in-time availability is guarded per event, but canonical universe-membership and delisting
   schemas remain undefined in the current data contract.

## Changed Files

- `RESEARCH_AGENT_REPORT.md`
- `research/README.md`
- `research/__init__.py`
- `research/backtest.py`
- `research/preparation.py`
- `research/replay.py`
- `research/storage.py`
- `research/walk_forward.py`
- `research/optuna/README.md`
- `research/vectorbt/README.md`
- `scripts/backtest.py`
- `scripts/replay.py`
- `src/monitoring/metrics.py`
- `tests/replay/test_parquet_replay.py`
- `tests/replay/test_replay_cli.py`
- `tests/unit/research/test_backtest.py`
- `tests/unit/research/test_backtest_cli.py`
- `tests/unit/research/test_metrics.py`
- `tests/unit/research/test_preparation.py`
- `tests/unit/research/test_walk_forward.py`

No commit or push was performed.
