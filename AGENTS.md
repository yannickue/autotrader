# Agent Ownership

Agents must modify only their owned paths unless the Integration/Lead Agent explicitly authorizes
cross-boundary work. A boundary change requires review from every affected owner.

| Agent | Owned paths | Responsibilities |
|---|---|---|
| Data Agent | `src/data/`, `src/features/`, `src/universe/`, `tests/unit/data/`, `tests/integration/data/` | Normalization, quality, staleness, instrument universe, deterministic features |
| Strategy Agent | `src/signals/`, `src/strategies/`, `tests/unit/signals/`, `tests/unit/strategies/` | Signal production only; no risk sizing or venue calls |
| Risk Agent | `src/risk/`, `tests/unit/risk/`, `tests/property/risk/` | Risk budget, sizing, exposure, leverage, loss and kill-switch decisions |
| Execution Agent | `src/execution/`, `src/adapters/`, `tests/unit/execution/`, `tests/integration/execution/`, `tests/chaos/execution/` | Order lifecycle, idempotency, fills, reconciliation, venue adapters |
| Research Agent | `research/`, `scripts/backtest.py`, `scripts/replay.py`, `tests/unit/research/`, `tests/replay/` | Bias-safe research, cost models, replay, optimization |
| Integration/Lead Agent | All remaining paths, including `docs/`, `configs/`, root tooling, `src/portfolio/`, `src/monitoring/`, `scripts/paper.py`, `scripts/live.py`, and shared tests | Contracts, release gates, integration, ownership arbitration |

## Coordination rules

- Do not make broad speculative changes or rewrite unrelated modules.
- Shared contract changes begin with a document update and an integration test owned by Lead.
- Domain agents add tests only in their assigned test paths.
- Cross-owner refactors require a written scope, affected-owner review, and Lead integration.
- No agent may commit secrets, enable live trading, relax a safety gate, or bypass risk.
- Research code must never be imported by the production execution path.
- Completion claims require the commands in `docs/TEST_GATES.md` to have actually run.

