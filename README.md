# Autotrader Infrastructure

Production-oriented bootstrap for a deterministic, event-driven automated trading system.
The repository currently defines boundaries, safety contracts, and executable smoke tests; it
does **not** contain a trading strategy or a live venue integration.

## Safety posture

- Strategies emit signals and cannot place orders.
- Every exposure-increasing request must pass the risk engine.
- The execution path is deterministic and never waits for an LLM.
- Stale data, unknown account state, unavailable risk, or failed reconciliation fail closed.
- Leverage is bounded by both a configured limit and the 30x system ceiling.
- Paper trading is the first executable milestone. Live mode remains disabled until its gates pass.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the component model and the other files in
`docs/` for boundary contracts and test gates.

## Local setup

Python 3.12–3.14 is supported. Python 3.12 is the baseline.

```shell
uv sync --python 3.12
uv run pytest
uv run ruff check .
uv run python -m compileall -q src scripts tests
```

`nautilus-trader==1.231.0` is pinned in `pyproject.toml` and `uv.lock`. Version 2.0 remains a
release-candidate line and is not used by this production bootstrap.

## Current scope

The current phase provides:

- data, signal, risk-decision, and execution-request types;
- architecture and safety contracts;
- paper, shadow, and live configuration skeletons;
- import, validation, and compile smoke tests;
- isolated ownership rules for future coding agents.

The next milestone should select one venue and implement recorded market-data replay before any
strategy or live order adapter is added.
