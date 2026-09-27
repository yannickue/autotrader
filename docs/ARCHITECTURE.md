# Architecture Contract

## Scope and principles

V1 is a single-venue, paper-first, event-driven system for 20–100 liquid markets with long and
short support. It supports intraday/scalping and wider-horizon momentum research without imposing a
trade-count target. Capital-growth examples are stretch outcomes, never optimization objectives.

NautilusTrader is the primary event, trading, and execution framework. Python owns strategies and
policy; Rust-backed Nautilus components are preferred on latency-sensitive paths. VectorBT and
Optuna are research-only. Parquet is the historical-data baseline. PostgreSQL or Redis require a
measured durability, coordination, or latency need before introduction.

The production hot path is deterministic:

```text
venue data -> adapter -> MarketSnapshot -> features -> strategy -> Signal
    -> risk engine -> RiskDecision -> ExecutionRequest -> execution adapter
    -> order/fill events -> portfolio + recorder + monitoring
```

An LLM may assist offline research or operator workflows, but it is never a dependency of signal,
risk, execution, reconciliation, or kill-switch decisions. TradingView is an optional monitoring,
visualization, and authenticated alert input; it is never the sole execution path.

## Component boundaries

- `data`, `features`, `universe`: normalize venue events, assess quality/staleness, select markets.
- `signals`, `strategies`: emit opinions only. They have no broker credentials or order API access.
- `risk`: owns risk budgets, sizing, leverage, exposure, drawdown, correlation, daily loss controls,
  and the kill switch. No strategy can bypass it.
- `execution`, `adapters`: own order construction, order type, maker/taker choice, reduce-only,
  stops, profit-taking, trailing, partial fills, cancel/replace, duplicate protection, slippage and
  spread guards, reconnects, and venue reconciliation.
- `portfolio`: derives positions and PnL from idempotent execution events.
- `monitoring`: records health, decisions, state transitions, metrics, and operator alerts.
- `research`: backtests and optimization only; it is not importable by production execution code.

Boundary messages are immutable dataclasses using `Decimal` for price, quantity, notional, risk,
and leverage. UTC timestamps and stable identifiers are mandatory at adapter boundaries.

## Safety state machine

The runtime states are `STARTING`, `RECONCILING`, `READY`, `DEGRADED`, and `HALTED`. New exposure
is allowed only in `READY`. Reduce-only actions may be allowed in `DEGRADED` or `HALTED` when the
account state is known and the action demonstrably lowers exposure.

These conditions fail closed for new exposure: stale/invalid data, unknown account state,
unavailable risk engine, reconciliation failure, excessive spread, excessive expected slippage,
daily-loss breach, drawdown breach, or operator kill switch. Restart and reconnect always enter
`RECONCILING`; the venue remains the authority for live orders, fills, and positions.

## Version decision

Checked against the official PyPI release metadata on 2026-09-27:

- selected: `nautilus-trader==1.231.0`, stable, uploaded 2026-08-02;
- deferred: `2.0.0rc5`, release candidate, uploaded 2026-09-15;
- supported Python range from the selected package: `>=3.12,<3.15`.

Production bootstrap favors the latest non-yanked stable release. A 2.0 migration requires replay,
adapter, state-recovery, and performance regression evidence before adoption.

## V1 sequence

1. Contracts and deterministic boundary types.
2. One venue's recorded WebSocket data and Parquet normalization.
3. Deterministic replay, recorder, and portfolio reconstruction.
4. Risk engine and execution simulator with invariant/property tests.
5. Paper trading with reconciliation and chaos tests.
6. Three research families—momentum, breakout, pullback—promoted only after bias and cost gates.
7. Shadow mode, then an explicitly approved live-readiness review.

Not in V1: dashboards, deep learning, reinforcement learning, options, or multiple brokers.

## Open decisions

1. Venue and account model (spot, linear perpetual, or inverse perpetual).
2. Canonical instrument identifier and contract-size normalization.
3. Market-data staleness thresholds by feed and strategy horizon.
4. Portfolio exposure, correlation, daily-loss, and drawdown defaults.
5. Durable event-log technology after throughput and recovery measurements.
6. Execution latency/error budgets and clock-synchronization requirements.
7. Authentication and replay protection for optional TradingView alerts.
8. Criteria and target date for evaluating NautilusTrader 2.0.

