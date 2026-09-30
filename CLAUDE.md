# Repository Operating Rules

This repository contains safety-critical trading infrastructure.

## Non-negotiable boundaries

- Keep market data, strategy, risk, and execution separate.
- Strategies may emit `Signal` objects only; they may never call venue APIs.
- Only risk-approved `ExecutionRequest` objects may reach execution.
- The live hot path must be deterministic and must not depend on an LLM or remote reasoning service.
- Fail closed when data is stale, account state is unknown, risk is unavailable, or reconciliation
  fails.
- Never weaken idempotency, reduce-only, exposure, leverage, or stale-signal invariants.
- Keep research and production execution dependencies separated.
- Never put credentials in source or configuration files.

## Working method

1. Read the relevant document in `docs/` before changing a boundary.
2. Add a failing test for behavioral changes, then implement the smallest passing change.
3. Run targeted tests first, then the gate for the change class (see "Test policy" below). Do not run an unqualified whole-repo `pytest` for a lane or an ordinary merge.
4. Record architecture decisions and unresolved questions in `docs/ARCHITECTURE.md`.
5. Do not implement speculative strategies, dashboards, deep learning, options, or extra venues.

## Commands

```shell
uv sync --python 3.12
uv run python scripts/run_tests.py fast          # FAST tier, target < 2 min
uv run python scripts/run_tests.py integration   # INTEGRATION tier, target < 5 min
uv run python scripts/run_tests.py safety        # SAFETY overlay, target < 8 min
uv run python scripts/run_tests.py slow          # SLOW/CHAOS/REPLAY tier
uv run python scripts/run_tests.py changed       # changed-path matrix (git diff vs main)
uv run ruff check .
uv run python -m compileall -q src scripts tests
```

## Test policy (persistent, user-mandated; details in `docs/TEST_GATES.md`)

- **Lane completion:** targeted tests + the relevant contracts/invariants + `ruff check` on the touched
  area + `compileall` of the relevant paths. No full-repo run.
- **Normal merge:** targeted + FAST + the relevant INTEGRATION segment (+ the relevant SAFETY segment
  when risk, execution, reconciliation or persistence code changed).
- **FULL suite:** only for a major release, phase or safety gate, preferably via the segmented suite
  (`scripts/run_tests.py full`). If FULL is expected to take more than 15 minutes, do not start it and
  report `FULL DEFERRED — SLOW SUITE PERFORMANCE STILL ABOVE BUDGET`.
- Safety coverage is not reduced by this policy: every test stays in FULL; SAFETY is a marker overlay
  (risk, execution, reconciliation, persistence, reduce-only, idempotency, stale-signal, exposure,
  leverage invariants) that must run whenever those areas change.
- Never run a whole-repo pytest on the shared 8 GB machine while the live runner/MT5 is active; use
  per-segment runs with a hard `timeout` wrapper.
