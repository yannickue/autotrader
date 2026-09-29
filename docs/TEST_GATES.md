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

## Commands and promotion

```shell
uv sync --frozen --python 3.12
uv run ruff check .
uv run python -m compileall -q src scripts tests
uv run pytest
```

Paper mode requires all implemented gates to pass. Shadow and live promotion additionally require
recorded replay parity, reconciliation and restart tests, chaos tests, venue sandbox evidence,
operational alerts, credential isolation, rollback/runbooks, and explicit human approval. A passing
unit suite alone never enables live trading.
