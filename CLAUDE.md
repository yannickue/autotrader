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
3. Run targeted tests first and the full suite before completion.
4. Record architecture decisions and unresolved questions in `docs/ARCHITECTURE.md`.
5. Do not implement speculative strategies, dashboards, deep learning, options, or extra venues.

## Commands

```shell
uv sync --python 3.12
uv run pytest
uv run ruff check .
uv run python -m compileall -q src scripts tests
```
